import json
import os
import uuid
import datetime
from decimal import Decimal
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

try:
    from aws_xray_sdk.core import patch_all, xray_recorder
    patch_all()
    xray_recorder.configure(context_missing='LOG_ERROR')
    XRAY_ENABLED = True
except Exception:
    XRAY_ENABLED = False
    xray_recorder = None

s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')
sfn_client = boto3.client('stepfunctions')

TABLE_NAME = os.environ.get('TABLE_NAME', 'DocuFlowRecords')
DOCS_BUCKET_NAME = os.environ.get('DOCS_BUCKET_NAME', 'docuflow-docs-local')
STATE_MACHINE_ARN = os.environ.get('STATE_MACHINE_ARN', '')
DOCUFLOW_API_KEY = os.environ.get('DOCUFLOW_API_KEY', '')

table = dynamodb.Table(TABLE_NAME)

try:
    from utils import DecimalEncoder, decimal_serialize, dumps_api_response, parse_dynamo_payload
except ImportError:
    try:
        from src.utils import DecimalEncoder, decimal_serialize, dumps_api_response, parse_dynamo_payload
    except ImportError:
        pass

MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024 # 10 MB
ALLOWED_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.txt'}

def build_response(status_code: int, body: dict):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Api-Key"
        },
        "body": dumps_api_response(body)
    }

def is_authorized(event: dict) -> bool:
    """
    Strict fail-closed authorization check.
    Accepts:
    1. API Gateway JWT authorizer claims (Cognito User Pools / OAuth PKCE).
    2. API Gateway Lambda authorizer context.
    3. Authorization header with valid Bearer token matching DOCUFLOW_API_KEY.
    4. x-api-key header matching DOCUFLOW_API_KEY.

    FAIL-CLOSED: If no authorizer claims exist and DOCUFLOW_API_KEY is not configured
    or token does not match, all requests are strictly rejected with 401 Unauthorized.
    """
    # 1. API Gateway JWT / Cognito claims
    jwt_claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims")
    if jwt_claims:
        return True

    # 2. Lambda authorizer context
    auth_ctx = event.get("requestContext", {}).get("authorizer", {}).get("lambda")
    if auth_ctx:
        return True

    # 3. Environment token validation - Fail closed if DOCUFLOW_API_KEY is unset or empty
    if not DOCUFLOW_API_KEY:
        return False

    headers = event.get("headers") or {}
    auth_header = None
    x_api_key = None
    for k, v in headers.items():
        k_lower = k.lower()
        if k_lower == "authorization":
            auth_header = v
        elif k_lower == "x-api-key":
            x_api_key = v

    if auth_header:
        token = auth_header.replace("Bearer ", "").strip()
        if token and token == DOCUFLOW_API_KEY:
            return True

    if x_api_key and x_api_key.strip() == DOCUFLOW_API_KEY:
        return True

    return False

def handle_get_upload_url(event):
    """
    Step 1: Client requests a secure presigned URL to upload directly to S3.
    Note: Does NOT trigger Step Functions prematurely.
    Workflow starts only after document bytes are actually uploaded to S3.
    """
    if not is_authorized(event):
        return build_response(401, {
            "error": "Unauthorized: A valid Authorization header or API key is required to request upload URLs."
        })
    body = {}
    if event.get("body"):
        try:
            body = json.loads(event["body"])
        except Exception:
            pass

    filename = body.get("filename", f"doc_{uuid.uuid4().hex[:8]}.pdf")
    content_type = body.get("content_type", "application/pdf")
    document_id = str(uuid.uuid4())
    s3_key = f"uploads/{document_id}/{filename}"
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Upfront format validation
    _, ext = os.path.splitext(filename.lower())
    if ext and ext not in ALLOWED_EXTENSIONS:
        return build_response(400, {
            "error": f"Unsupported file extension '{ext}'. Allowed formats: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        })

    # Upfront upload size limit validation - Mandatory field to enforce 10MB upload boundary
    raw_size = body.get("file_size") if body.get("file_size") is not None else body.get("content_length")
    if raw_size is None:
        return build_response(400, {
            "error": "Missing mandatory field 'file_size'. Declared file size in bytes is required to enforce upload boundaries."
        })

    try:
        declared_size = int(raw_size)
    except (ValueError, TypeError):
        return build_response(400, {"error": "Invalid 'file_size': must be an integer representing byte count."})

    if declared_size <= 0:
        return build_response(400, {"error": f"Invalid 'file_size' ({declared_size} bytes): must be greater than 0 bytes."})

    if declared_size > MAX_UPLOAD_SIZE_BYTES:
        return build_response(400, {
            "error": f"Declared file size ({declared_size} bytes) exceeds maximum allowable limit of {MAX_UPLOAD_SIZE_BYTES} bytes (10MB). Presigned URL denied."
        })

    # Always sign ContentLength into the S3 presigned URL params so S3 strictly enforces byte count at upload
    put_params = {
        'Bucket': DOCS_BUCKET_NAME,
        'Key': s3_key,
        'ContentType': content_type,
        'ContentLength': declared_size
    }

    try:
        presigned_url = s3_client.generate_presigned_url(
            'put_object',
            Params=put_params,
            ExpiresIn=900
        )
    except Exception as e:
        print(f"S3 presigned URL generation error: {e}")
        return build_response(500, {
            "error": f"Failed to generate secure S3 presigned URL: {str(e)}",
            "document_id": document_id
        })

    item = {
        "PK": f"DOC#{document_id}",
        "SK": "METADATA",
        "GSI1PK": "DOCS",
        "GSI1SK": now_iso,
        "document_id": document_id,
        "filename": filename,
        "s3_bucket": DOCS_BUCKET_NAME,
        "s3_key": s3_key,
        "content_type": content_type,
        "max_size_bytes": MAX_UPLOAD_SIZE_BYTES,
        "status": "UPLOAD_PENDING",
        "created_at": now_iso,
        "updated_at": now_iso
    }
    if declared_size is not None:
        item["declared_size_bytes"] = declared_size

    try:
        table.put_item(Item=decimal_serialize(item))
    except Exception as e:
        print(f"DynamoDB put_item error: {e}")
        return build_response(500, {
            "error": f"Failed to initialize document record in database: {str(e)}",
            "document_id": document_id
        })

    return build_response(201, {
        "document_id": document_id,
        "upload_url": presigned_url,
        "s3_bucket": DOCS_BUCKET_NAME,
        "s3_key": s3_key,
        "max_size_bytes": MAX_UPLOAD_SIZE_BYTES,
        "status": "UPLOAD_PENDING",
        "message": "Presigned URL generated. Upload document to S3, then trigger processing."
    })

def handle_process_document(event, document_id):
    """
    Step 2: Client or EventBridge confirms document is uploaded to S3.
    Now Step Functions is invoked with the verified S3 object.
    """
    if not is_authorized(event):
        return build_response(401, {
            "error": "Unauthorized: A valid Authorization header or API key is required to trigger document processing."
        })

    # 1. Fetch document metadata - Fail closed on DB read failure or missing record (no guessing)
    try:
        res = table.get_item(Key={"PK": f"DOC#{document_id}", "SK": "METADATA"})
        doc = res.get("Item")
    except Exception as e:
        print(f"DynamoDB get error for document {document_id}: {e}")
        return build_response(500, {
            "error": f"Database read failure: Unable to retrieve document record from DynamoDB: {str(e)}",
            "document_id": document_id
        })

    if not doc:
        return build_response(404, {
            "error": f"Document '{document_id}' not found in database. An upload URL must be requested before processing.",
            "document_id": document_id
        })

    s3_key = doc.get("s3_key")
    s3_bucket = doc.get("s3_bucket", DOCS_BUCKET_NAME)
    filename = doc.get("filename", "document.pdf")
    if not s3_key:
        return build_response(400, {
            "error": f"Document record '{document_id}' is missing required 's3_key' metadata.",
            "document_id": document_id
        })

    # 2. FAIL CLOSED: Verify object exists on S3 and enforce size limit before triggering Step Functions
    try:
        head_res = s3_client.head_object(Bucket=s3_bucket, Key=s3_key)
        reported_size = head_res.get('ContentLength', 0)
        object_exists = True
    except Exception as e:
        return build_response(400, {
            "error": f"Document file not found in S3 bucket '{s3_bucket}'. Please upload bytes before triggering processing.",
            "s3_key": s3_key
        })

    if reported_size <= 0:
        return build_response(400, {
            "error": f"Uploaded document file in '{s3_bucket}' is empty (0 bytes).",
            "s3_key": s3_key
        })

    if reported_size > MAX_UPLOAD_SIZE_BYTES:
        return build_response(400, {
            "error": f"Uploaded document size ({reported_size} bytes) exceeds maximum limit of {MAX_UPLOAD_SIZE_BYTES} bytes (10MB).",
            "s3_key": s3_key
        })

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Update status to PROCESSING
    try:
        table.update_item(
            Key={"PK": f"DOC#{document_id}", "SK": "METADATA"},
            UpdateExpression="SET #st = :st, updated_at = :now",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":st": "PROCESSING", ":now": now_iso}
        )
    except Exception as e:
        print(f"DynamoDB update error: {e}")
        return build_response(500, {
            "error": f"Failed to update document status to PROCESSING: {str(e)}",
            "document_id": document_id
        })

    # 3. Start Step Functions Execution - Fail closed if execution cannot start
    if not STATE_MACHINE_ARN:
        # Revert status back to UPLOAD_PENDING / FAILED so document is not left stuck in PROCESSING
        try:
            table.update_item(
                Key={"PK": f"DOC#{document_id}", "SK": "METADATA"},
                UpdateExpression="SET #st = :st, updated_at = :now, error_message = :err",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":st": "FAILED",
                    ":now": now_iso,
                    ":err": "STATE_MACHINE_ARN not configured"
                }
            )
        except Exception:
            pass
        return build_response(500, {
            "error": "STATE_MACHINE_ARN environment variable is not configured on API handler.",
            "document_id": document_id
        })

    try:
        sfn_input = {
            "document_id": str(document_id),
            "s3_bucket": str(s3_bucket),
            "s3_key": str(s3_key),
            "filename": str(filename)
        }
        sfn_res = sfn_client.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=f"exec-{document_id[:8]}-{int(datetime.datetime.now().timestamp())}",
            input=json.dumps(sfn_input, default=str)
        )
        execution_arn = sfn_res.get("executionArn")
    except Exception as e:
        print(f"Step Functions start_execution error: {e}")
        # Mark record as FAILED with error detail so it is not stuck in PROCESSING
        try:
            table.update_item(
                Key={"PK": f"DOC#{document_id}", "SK": "METADATA"},
                UpdateExpression="SET #st = :st, updated_at = :now, error_message = :err",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":st": "FAILED",
                    ":now": now_iso,
                    ":err": f"Step Functions start_execution failed: {str(e)}"
                }
            )
        except Exception as ddb_err:
            print(f"Failed to mark document as FAILED after SFN start error: {ddb_err}")

        return build_response(500, {
            "error": f"Failed to start Step Functions workflow execution: {str(e)}",
            "document_id": document_id
        })

    return build_response(200, {
        "document_id": document_id,
        "status": "PROCESSING",
        "s3_verified": object_exists,
        "execution_arn": execution_arn,
        "message": "Step Functions state machine triggered."
    })

def sanitize_document_record(item: dict) -> dict:
    """
    Strips internal execution and security-sensitive tokens (such as task_token)
    before returning document details to public API callers.
    """
    if not isinstance(item, dict):
        return item
    sanitized = dict(item)
    sanitized.pop("task_token", None)
    return sanitized

def handle_list_documents(event):
    if not is_authorized(event):
        return build_response(401, {
            "error": "Unauthorized: A valid Authorization token or API key is required to list documents."
        })
    items = []
    try:
        response = table.query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq("DOCS"),
            ScanIndexForward=False,
            Limit=50
        )
        items = response.get("Items", [])
    except Exception as query_err:
        print(f"DynamoDB query failed, attempting scan fallback: {query_err}")
        try:
            scan_res = table.scan(Limit=50)
            items = scan_res.get("Items", [])
        except Exception as scan_err:
            print(f"DynamoDB scan fallback also failed: {scan_err}")
            return build_response(500, {
                "error": f"Failed to retrieve documents from DynamoDB: {str(scan_err)}"
            })

    sanitized_items = [sanitize_document_record(i) for i in items]
    return build_response(200, {"count": len(sanitized_items), "documents": sanitized_items})

def handle_get_document(event, document_id):
    if not is_authorized(event):
        return build_response(401, {
            "error": "Unauthorized: A valid Authorization token or API key is required to view document details."
        })
    try:
        res = table.get_item(Key={"PK": f"DOC#{document_id}", "SK": "METADATA"})
        item = res.get("Item")
        if not item:
            return build_response(404, {"error": f"Document '{document_id}' not found."})
        return build_response(200, sanitize_document_record(item))
    except Exception as e:
        print(f"DynamoDB get_item error for document {document_id}: {e}")
        return build_response(500, {
            "error": f"Failed to retrieve document record from DynamoDB: {str(e)}",
            "document_id": document_id
        })

def handle_approve_document(event, document_id):
    """
    Step 3: Human review decision callback.
    Resumes suspended Step Functions state machine via send_task_success(TaskToken)!
    Fails closed if document is not awaiting approval or if Step Functions fails to resume.
    """
    # Verify authorization for approval callback
    if not is_authorized(event):
        return build_response(401, {
            "error": "Unauthorized: A valid Authorization header or API key is required to submit approval decisions."
        })

    raw_body = event.get("body")
    if not raw_body:
        return build_response(400, {
            "error": "Missing request body. 'action' ('APPROVE' or 'REJECT') is required."
        })

    try:
        body = json.loads(raw_body)
        if not isinstance(body, dict):
            return build_response(400, {"error": "Malformed request body: payload must be a JSON object."})
    except Exception as e:
        return build_response(400, {"error": f"Invalid JSON format in request body: {str(e)}"})

    raw_action = body.get("action")
    if not raw_action:
        return build_response(400, {
            "error": "Missing mandatory field 'action'. Must be explicitly specified as 'APPROVE' or 'REJECT'."
        })

    action = str(raw_action).strip().upper()
    if action not in ("APPROVE", "REJECT"):
        return build_response(400, {
            "error": f"Invalid action '{raw_action}'. Allowed approval decisions are strictly 'APPROVE' or 'REJECT'."
        })

    reviewer = body.get("reviewer") or "admin"
    notes = body.get("notes") or "Reviewed through DocuFlow dashboard"
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    new_status = "APPROVED" if action == "APPROVE" else "REJECTED"

    # 1. Fetch document record from DynamoDB to check existence and state - Fail closed on DB error
    try:
        res = table.get_item(Key={"PK": f"DOC#{document_id}", "SK": "METADATA"})
        item = res.get("Item")
    except Exception as e:
        print(f"Error reading document from DynamoDB: {e}")
        return build_response(500, {
            "error": f"Database read failure: Unable to retrieve document record for approval: {str(e)}",
            "document_id": document_id
        })

    if not item:
        return build_response(404, {"error": f"Document '{document_id}' not found"})

    # 2. Extract task token: check body first, then DynamoDB record
    task_token = body.get("task_token") or item.get("task_token")
    if not task_token:
        return build_response(400, {
            "error": f"Document '{document_id}' is not awaiting approval (no active Step Functions task token found).",
            "current_status": item.get("status")
        })

    # 3. Resume paused Step Functions execution via task token
    review_message = f"Manual decision {new_status} by {reviewer}. Notes: {notes}"
    callback_payload = {
        "document_id": str(document_id),
        "status": new_status,
        "decision": f"MANUAL_{new_status}",
        "reviewer": str(reviewer),
        "notes": str(notes),
        "message": review_message,
        "approved_at": now_iso
    }

    try:
        sfn_client.send_task_success(
            taskToken=task_token,
            output=json.dumps(callback_payload)
        )
        print(f"Successfully sent TaskSuccess for document {document_id}")
    except Exception as e:
        print(f"Error resuming Step Functions for document {document_id}: {e}")
        return build_response(500, {
            "error": f"Failed to resume Step Functions execution: {str(e)}",
            "document_id": document_id
        })

    # 4. Only after Step Functions resumes successfully, commit decision to DynamoDB
    try:
        table.update_item(
            Key={"PK": f"DOC#{document_id}", "SK": "METADATA"},
            UpdateExpression="SET #st = :st, updated_at = :now, reviewer = :rev, review_notes = :notes, sfn_resumed = :sfn REMOVE task_token",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":st": new_status,
                ":now": now_iso,
                ":rev": reviewer,
                ":notes": notes,
                ":sfn": True
            }
        )
    except Exception as e:
        print(f"DynamoDB local update note: {e}")

    return build_response(200, {
        "document_id": document_id,
        "status": new_status,
        "decision": f"MANUAL_{new_status}",
        "action": action,
        "message": review_message,
        "step_functions_resumed": True,
        "reviewer": reviewer,
        "updated_at": now_iso
    })

def lambda_handler(event, context):
    print("Received event:", json.dumps(event, default=str))
    route_key = event.get("routeKey") or event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path_parameters = event.get("pathParameters") or {}
    raw_path = event.get("rawPath", "")
    raw_parts = [p for p in raw_path.split("/") if p]

    if method == "OPTIONS":
        return build_response(200, {"message": "OK"})

    # Helper to resolve doc_id from pathParameters or real rawPath
    def get_doc_id(is_nested_action=False):
        if path_parameters.get("id"):
            return path_parameters["id"]
        if is_nested_action and len(raw_parts) >= 2:
            return raw_parts[-2]
        if raw_parts:
            return raw_parts[-1]
        return ""

    if "/api/upload-url" in route_key:
        return handle_get_upload_url(event)
    elif "/api/documents/" in route_key and route_key.endswith("/process"):
        return handle_process_document(event, get_doc_id(is_nested_action=True))
    elif "/api/documents/" in route_key and route_key.endswith("/approve"):
        return handle_approve_document(event, get_doc_id(is_nested_action=True))
    elif "/api/documents/" in route_key:
        return handle_get_document(event, get_doc_id(is_nested_action=False))
    elif "/api/documents" in route_key:
        return handle_list_documents(event)

    return build_response(404, {"error": "Route not found", "route_key": route_key})
