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

def build_response(status_code: int, body: dict):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization"
        },
        "body": dumps_api_response(body)
    }

def handle_get_upload_url(event):
    """
    Step 1: Client requests a secure presigned URL to upload directly to S3.
    Note: Does NOT trigger Step Functions prematurely.
    Workflow starts only after document bytes are actually uploaded to S3.
    """
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

    try:
        presigned_url = s3_client.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': DOCS_BUCKET_NAME,
                'Key': s3_key,
                'ContentType': content_type
            },
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
        "status": "UPLOAD_PENDING",
        "created_at": now_iso,
        "updated_at": now_iso
    }

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
        "status": "UPLOAD_PENDING",
        "message": "Presigned URL generated. Upload document to S3, then trigger processing."
    })

def handle_process_document(event, document_id):
    """
    Step 2: Client or EventBridge confirms document is uploaded to S3.
    Now Step Functions is invoked with the verified S3 object.
    """
    # 1. Fetch document metadata
    doc = None
    try:
        res = table.get_item(Key={"PK": f"DOC#{document_id}", "SK": "METADATA"})
        doc = res.get("Item")
    except Exception as e:
        print(f"DynamoDB get error: {e}")

    if not doc:
        # Create minimal record if triggered directly from S3 event
        s3_key = f"uploads/{document_id}/document.pdf"
        s3_bucket = DOCS_BUCKET_NAME
        filename = "document.pdf"
    else:
        s3_key = doc.get("s3_key")
        s3_bucket = doc.get("s3_bucket", DOCS_BUCKET_NAME)
        filename = doc.get("filename", "document.pdf")

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
    items = []
    try:
        response = table.query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq("DOCS"),
            ScanIndexForward=False,
            Limit=50
        )
        items = response.get("Items", [])
    except Exception as e:
        try:
            scan_res = table.scan(Limit=50)
            items = scan_res.get("Items", [])
        except Exception:
            items = []

    sanitized_items = [sanitize_document_record(i) for i in items]
    return build_response(200, {"count": len(sanitized_items), "documents": sanitized_items})

def handle_get_document(event, document_id):
    try:
        res = table.get_item(Key={"PK": f"DOC#{document_id}", "SK": "METADATA"})
        item = res.get("Item")
        if not item:
            return build_response(404, {"error": "Document not found"})
        return build_response(200, sanitize_document_record(item))
    except Exception as e:
        return build_response(500, {"error": str(e)})

def is_authorized(event: dict) -> bool:
    """
    Validates caller authorization for privileged actions (e.g. human approval decisions).
    If DOCUFLOW_API_KEY environment variable is configured, checks Authorization header
    ('Bearer <key>' or '<key>').
    If unconfigured (local evaluation / demo mode), requests pass through with a logged warning.
    """
    if not DOCUFLOW_API_KEY:
        return True

    headers = event.get("headers") or {}
    # Case-insensitive header lookup
    auth_header = None
    for k, v in headers.items():
        if k.lower() == "authorization":
            auth_header = v
            break

    if not auth_header:
        return False

    token = auth_header.replace("Bearer ", "").strip()
    return token == DOCUFLOW_API_KEY

def handle_approve_document(event, document_id):
    """
    Step 3: Human review decision callback.
    Resumes suspended Step Functions state machine via send_task_success(TaskToken)!
    Fails closed if document is not awaiting approval or if Step Functions fails to resume.
    """
    # Verify authorization for approval callback
    if not is_authorized(event):
        return build_response(401, {
            "error": "Unauthorized: A valid Authorization header is required to submit approval decisions."
        })
    body = {}
    if event.get("body"):
        try:
            body = json.loads(event["body"])
        except Exception:
            pass

    action = body.get("action", "APPROVE").upper() # APPROVE or REJECT
    reviewer = body.get("reviewer", "admin")
    notes = body.get("notes", "Reviewed through DocuFlow dashboard")
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    new_status = "APPROVED" if action == "APPROVE" else "REJECTED"

    # 1. Fetch document record from DynamoDB to check existence and state
    item = None
    try:
        res = table.get_item(Key={"PK": f"DOC#{document_id}", "SK": "METADATA"})
        item = res.get("Item")
    except Exception as e:
        print(f"Error reading document from DynamoDB: {e}")

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
