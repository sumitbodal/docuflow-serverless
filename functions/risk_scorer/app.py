import json
import os
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

dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ.get('TABLE_NAME', 'DocuFlowRecords')
table = dynamodb.Table(TABLE_NAME)

try:
    from utils import decimal_serialize
except ImportError:
    try:
        from src.utils import decimal_serialize
    except ImportError:
        def decimal_serialize(obj):
            if isinstance(obj, float):
                return Decimal(str(round(obj, 2)))
            elif isinstance(obj, dict):
                return {k: decimal_serialize(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [decimal_serialize(v) for v in obj]
            return obj

def calculate_anomaly_risk(extracted_data: dict, security_scan: dict = None) -> dict:
    risk_score = 10
    risk_factors = []

    vendor = extracted_data.get("vendor", "")
    vendor_trusted = extracted_data.get("vendor_trusted", True)
    grand_total = float(extracted_data.get("grand_total", 0.0))

    if not vendor_trusted:
        risk_score += 45
        risk_factors.append(f"Untrusted or unregistered offshore entity: '{vendor}' (+45)")
    else:
        risk_score = max(5, risk_score - 10)
        risk_factors.append(f"Verified enterprise vendor: '{vendor}' (-10)")

    if grand_total > 20000.0:
        risk_score += 40
        risk_factors.append(f"High-value financial transaction exceeding $20,000 threshold (${grand_total:,.2f}) (+40)")
    elif grand_total > 5000.0:
        risk_score += 15
        risk_factors.append(f"Moderate value invoice (${grand_total:,.2f}) (+15)")

    if security_scan and not security_scan.get("compliance_cleared", True):
        risk_score += 30
        risk_factors.append("Security scan flagged compliance or integrity irregularity (+30)")

    risk_score = min(100, max(0, risk_score))

    if risk_score >= 70:
        risk_level = "HIGH"
        recommendation = "FLAG_FOR_MANUAL_REVIEW"
    elif risk_score >= 35:
        risk_level = "MEDIUM"
        recommendation = "AUTO_APPROVE_WITH_FLAG"
    else:
        risk_level = "LOW"
        recommendation = "AUTO_APPROVE"

    return {
        "score": risk_score,
        "level": risk_level,
        "recommendation": recommendation,
        "factors": risk_factors,
        "evaluated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }

def lambda_handler(event, context):
    """
    Risk Scoring Engine:
    Processes merged output from Step Functions parallel branches (extraction + security scan)
    or sequential input.
    """
    print("Risk Scorer received event:", json.dumps(event, default=str))

    document_id = None
    extracted_data = {}
    security_scan = {}

    # Step Functions Parallel state passes: {"parallel_results": [branch1, branch2]}
    # Direct call or dict input is also handled.
    parallel_array = None
    if isinstance(event, dict) and "parallel_results" in event:
        parallel_array = event["parallel_results"]
    elif isinstance(event, list):
        parallel_array = event  # legacy / direct-test call

    if parallel_array is not None:
        for item in parallel_array:
            if isinstance(item, dict):
                if not document_id: document_id = item.get("document_id")
                if "extracted_data" in item: extracted_data = item["extracted_data"]
                if "security_scan" in item: security_scan = item["security_scan"]
                if "validation" in item and not security_scan: security_scan = item["validation"]
    elif isinstance(event, dict):
        document_id = event.get("document_id")
        extracted_data = event.get("extracted_data", {})
        security_scan = event.get("security_scan", event.get("validation", {}))

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    risk_evaluation = calculate_anomaly_risk(extracted_data, security_scan)

    if os.environ.get('_X_AMZN_TRACE_ID') and XRAY_ENABLED and xray_recorder:
        try:
            curr_sub = xray_recorder.current_subsegment() or xray_recorder.current_segment()
            if curr_sub:
                curr_sub.put_annotation("document_id", str(document_id))
                curr_sub.put_annotation("risk_score", int(risk_evaluation["score"]))
                curr_sub.put_annotation("risk_level", str(risk_evaluation["level"]))
                curr_sub.put_annotation("recommendation", str(risk_evaluation["recommendation"]))
        except Exception:
            pass

    try:
        table.update_item(
            Key={"PK": f"DOC#{document_id}", "SK": "METADATA"},
            UpdateExpression="SET #st = :st, risk = :risk, updated_at = :now",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":st": "RISK_SCORED",
                ":risk": decimal_serialize(risk_evaluation),
                ":now": now_iso
            }
        )
    except Exception as e:
        print(f"DynamoDB update error in risk_scorer: {e}")

    # Build merged output: preserve top-level metadata and merge parallel branches
    output_event = dict(event) if isinstance(event, dict) else {}
    if parallel_array is not None:
        for item in parallel_array:
            if isinstance(item, dict):
                output_event.update(item)
    output_event["document_id"] = document_id
    output_event["extracted_data"] = extracted_data
    output_event["security_scan"] = security_scan
    output_event["risk"] = risk_evaluation
    return output_event

def review_handler(event, context):
    """
    Step Functions waitForTaskToken Handler:
    Invoked when risk >= 70.
    Stores the task token into DynamoDB and sets status to PENDING_APPROVAL.
    Step Functions pauses execution until send_task_success is called by the API.
    """
    print("Review Handler received waitForTaskToken event:", json.dumps(event, default=str))

    document_id = event.get("document_id")
    task_token = event.get("task_token")
    risk = event.get("risk", {})
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    try:
        table.update_item(
            Key={"PK": f"DOC#{document_id}", "SK": "METADATA"},
            UpdateExpression="SET #st = :st, task_token = :tok, updated_at = :now, review_requested_at = :now",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":st": "PENDING_APPROVAL",
                ":tok": task_token,
                ":now": now_iso
            }
        )
        print(f"Stored task token for document {document_id}; Step Functions paused.")
    except Exception as e:
        print(f"DynamoDB update error in review_handler: {e}")

    # Also notify administrators via SNS topic if configured
    sns_topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if sns_topic_arn:
        try:
            boto3.client('sns').publish(
                TopicArn=sns_topic_arn,
                Subject=f"[ACTION REQUIRED] Invoice Approval Required: {document_id}",
                Message=f"DocuFlow AI Anomaly Flag: Invoice {document_id} has Risk Score {risk.get('score', 'HIGH')}. Review in dashboard or call /api/documents/{document_id}/approve."
            )
        except Exception as e:
            print(f"SNS publish note in review_handler: {e}")

    return {
        "statusCode": 200,
        "message": "Task token saved. Awaiting human decision via API.",
        "document_id": document_id
    }

def notifier_handler(event, context):
    """
    Final Dispatch & Completion Notifier:
    Commits final status to DynamoDB and logs enterprise notification event.
    """
    print("Notifier received event:", json.dumps(event, default=str))

    document_id = event.get("document_id")
    if not document_id:
        print("Warning: notifier received event without document_id")
        return {"statusCode": 400, "error": "Missing document_id"}

    final_status = event.get("status", "APPROVED")
    decision = event.get("decision", "AUTO_APPROVED")
    # Support both pre-built 'message' (AutoApprove path) and reviewer callback fields
    message = (
        event.get("message")
        or f"Reviewed by {event.get('reviewer', 'system')}. Notes: {event.get('notes', 'N/A')}"
    )
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    try:
        table.update_item(
            Key={"PK": f"DOC#{document_id}", "SK": "METADATA"},
            UpdateExpression="SET #st = :st, decision = :dec, completion_message = :msg, completed_at = :now, updated_at = :now REMOVE task_token",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":st": final_status,
                ":dec": decision,
                ":msg": message,
                ":now": now_iso
            }
        )
    except Exception as e:
        print(f"DynamoDB update error in notifier: {e}")

    print(f"DISPATCHED EVENT: Document {document_id} finalized with {final_status} ({decision})")

    return {
        "statusCode": 200,
        "document_id": document_id,
        "final_status": final_status,
        "decision": decision,
        "dispatched_at": now_iso,
        "message": message
    }
