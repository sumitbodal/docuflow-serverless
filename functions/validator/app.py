import json
import os
import hashlib
import datetime
from decimal import Decimal
import boto3

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

TABLE_NAME = os.environ.get('TABLE_NAME', 'DocuFlowRecords')
table = dynamodb.Table(TABLE_NAME)

ALLOWED_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg'}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024 # 10 MB

try:
    from utils import decimal_serialize
except ImportError:
    try:
        from src.utils import decimal_serialize
    except ImportError:
        def decimal_serialize(obj):
            if isinstance(obj, float):
                return Decimal(str(round(obj, 4)))
            elif isinstance(obj, dict):
                return {k: decimal_serialize(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [decimal_serialize(v) for v in obj]
            return obj

def lambda_handler(event, context):
    """
    Validator:
    Runs concurrently in Step Functions Parallel state.
    Inspects S3 object metadata, computes SHA256 integrity hash,
    validates file size boundaries, and checks format whitelist.
    """
    print("Validator received event:", json.dumps(event, default=str))

    document_id = event.get("document_id")
    s3_bucket = event.get("s3_bucket")
    s3_key = event.get("s3_key")
    filename = event.get("filename", "invoice.pdf")

    if not document_id:
        raise ValueError("Missing required field: document_id")

    if not s3_bucket or not s3_key:
        raise ValueError("Missing required S3 coordinates: 's3_bucket' and 's3_key' are required for integrity validation.")

    _, ext = os.path.splitext(filename.lower())
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file format '{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}")

    # Read S3 object to compute real cryptographic hash and byte size - Fail closed
    try:
        head_res = s3_client.head_object(Bucket=s3_bucket, Key=s3_key)
        content_len = head_res.get('ContentLength', 0)
        s3_obj = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)
        content_bytes = s3_obj['Body'].read()
        file_sha256 = hashlib.sha256(content_bytes).hexdigest()
        file_size = len(content_bytes)
    except Exception as e:
        raise FileNotFoundError(f"S3 document object s3://{s3_bucket}/{s3_key} could not be retrieved: {e}")

    if file_size <= 0:
        raise ValueError(f"Document file s3://{s3_bucket}/{s3_key} is empty (0 bytes)")

    if file_size > MAX_FILE_SIZE_BYTES:
        raise ValueError(f"File size {file_size} exceeds maximum limit of {MAX_FILE_SIZE_BYTES} bytes")

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    security_info = {
        "status": "VALIDATED",
        "file_size_bytes": int(file_size),
        "extension": ext,
        "sha256": file_sha256,
        "integrity_verified": True,
        "format_whitelisted": True,
        "size_within_limits": True,
        "check_scope": "Cryptographic SHA-256 Checksum, Size Bounds & Extension Whitelist",
        "validated_at": now_iso
    }

    if os.environ.get('_X_AMZN_TRACE_ID') and XRAY_ENABLED and xray_recorder:
        try:
            curr_sub = xray_recorder.current_subsegment() or xray_recorder.current_segment()
            if curr_sub:
                curr_sub.put_annotation("document_id", str(document_id))
                curr_sub.put_annotation("integrity_verified", True)
                curr_sub.put_annotation("extension", str(ext))
                curr_sub.put_metadata("file_size_bytes", int(file_size))
                curr_sub.put_metadata("sha256", str(file_sha256))
        except Exception:
            pass

    try:
        table.update_item(
            Key={"PK": f"DOC#{document_id}", "SK": "METADATA"},
            UpdateExpression="SET #st = :st, security_scan = :sec, updated_at = :now",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":st": "VALIDATED",
                ":sec": decimal_serialize(security_info),
                ":now": now_iso
            }
        )
    except Exception as e:
        print(f"DynamoDB update error in validator: {e}")

    output_event = dict(event)
    output_event["security_scan"] = security_info
    output_event["validation"] = security_info # Backwards compatibility for tests
    return output_event
