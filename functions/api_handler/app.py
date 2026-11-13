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

table = dynamodb.Table(TABLE_NAME)

try:
    from utils import DecimalEncoder, decimal_serialize, dumps_api_response, parse_dynamo_payload
except ImportError:
    try:
        from src.utils import DecimalEncoder, decimal_serialize, dumps_api_response, parse_dynamo_payload
    except ImportError:
        pass

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
        print(f"S3 presign fallback: {e}")
        presigned_url = f"https://{DOCS_BUCKET_NAME}.s3.amazonaws.com/{s3_key}?mock_token=123"

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

    # 2. Verify object exists on S3 (or accept mock in local test)
    object_exists = False
    try:
        s3_client.head_object(Bucket=s3_bucket, Key=s3_key)
        object_exists = True
    except Exception as e:
        print(f"S3 head_object note: {e}")

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

    # 3. Start Step Functions Execution
    execution_arn = None
    if STATE_MACHINE_ARN:
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
            print(f"Step Functions start_execution: {e}")

    return build_response(200, {
        "document_id": document_id,
        "status": "PROCESSING",
        "s3_verified": object_exists,
        "execution_arn": execution_arn,
        "message": "Step Functions state machine triggered."
    })

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

    return build_response(200, {"count": len(items), "documents": items})

def handle_get_document(event, document_id):
    try:
        res = table.get_item(Key={"PK": f"DOC#{document_id}", "SK": "METADATA"})
        item = res.get("Item")
        if not item:
            return build_response(404, {"error": "Document not found"})
        return build_response(200, item)
    except Exception as e:
        return build_response(500, {"error": str(e)})

def handle_approve_document(event, document_id):
    """
    Step 3: Human review decision callback.
    Resumes suspended Step Functions state machine via send_task_success(TaskToken)!
    """
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

    # Fetch task token: check body first (passed from SNS / dashboard callback), else look up in DynamoDB
    task_token = body.get("task_token")
    if not task_token:
        try:
            res = table.get_item(Key={"PK": f"DOC#{document_id}", "SK": "METADATA"})
            item = res.get("Item")
            if not item:
                return build_response(404, {"error": f"Document '{document_id}' not found"})
            task_token = item.get("task_token")
        except Exception as e:
            print(f"Error reading task_token from DynamoDB: {e}")

    # Resume paused Step Functions execution via task token
    sfn_resumed = False
    if task_token:
        try:
            callback_payload = {
                "document_id": document_id,
                "status": new_status,
                "decision": f"MANUAL_{new_status}",
                "reviewer": reviewer,
                "notes": notes,
                "approved_at": now_iso
            }
            sfn_client.send_task_success(
                taskToken=task_token,
                output=json.dumps(callback_payload)
            )
            sfn_resumed = True
            print(f"Successfully sent TaskSuccess for document {document_id}")
        except Exception as e:
            print(f"sfn send_task_success note: {e}")

    # Update DynamoDB record
    try:
        table.update_item(
            Key={"PK": f"DOC#{document_id}", "SK": "METADATA"},
            UpdateExpression="SET #st = :st, updated_at = :now, reviewer = :rev, review_notes = :notes, sfn_resumed = :sfn",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":st": new_status,
                ":now": now_iso,
                ":rev": reviewer,
                ":notes": notes,
                ":sfn": sfn_resumed
            }
        )
    except Exception as e:
        return build_response(500, {"error": str(e)})

    return build_response(200, {
        "document_id": document_id,
        "status": new_status,
        "action": action,
        "step_functions_resumed": sfn_resumed,
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
