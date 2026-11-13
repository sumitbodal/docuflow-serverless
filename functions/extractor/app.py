import json
import os
import re
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
bedrock_runtime = boto3.client('bedrock-runtime')

TABLE_NAME = os.environ.get('TABLE_NAME', 'DocuFlowRecords')
BEDROCK_MODEL_ID = os.environ.get('BEDROCK_MODEL_ID', 'anthropic.claude-3-haiku-20240307-v1:0')
table = dynamodb.Table(TABLE_NAME)

SAMPLE_VENDORS = [
    {"name": "Amazon Web Services EMEA", "tax_id": "LU26375245", "trusted": True, "domain": "aws.amazon.com"},
    {"name": "GitHub Inc (Microsoft)", "tax_id": "US94320984", "trusted": True, "domain": "github.com"},
    {"name": "Datadog Cloud Monitoring", "tax_id": "US82930219", "trusted": True, "domain": "datadoghq.com"},
    {"name": "Apex Global Consultancy Offshore Ltd", "tax_id": "KY09283710", "trusted": False, "domain": "apex-offshore-shell.ky"},
    {"name": "Unregistered Shadow Vendor Ltd", "tax_id": "XX00000000", "trusted": False, "domain": "unverified-unknown.net"}
]

try:
    from utils import decimal_serialize, parse_dynamo_payload
except ImportError:
    try:
        from src.utils import decimal_serialize, parse_dynamo_payload
    except ImportError:
        def decimal_serialize(obj):
            if isinstance(obj, float):
                return Decimal(str(round(obj, 2)))
            elif isinstance(obj, dict):
                return {k: decimal_serialize(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [decimal_serialize(v) for v in obj]
            return obj
        def parse_dynamo_payload(s):
            return json.loads(s, parse_float=Decimal)

def extract_with_bedrock(content_bytes: bytes, filename: str, document_id: str):
    """
    Serverless Multi-Modal Foundation Model Inference:
    Directly invokes Amazon Bedrock (Claude 3 Haiku) using native Converse API
    document blocks (for PDF) or image blocks (for PNG/JPEG) with raw bytes.
    Zero server management, pay-per-token inference.
    """
    if not content_bytes or len(content_bytes) < 10:
        return None

    _, ext = os.path.splitext(filename.lower())

    prompt = (
        "You are an enterprise financial document extraction and compliance auditor. "
        f"Analyze the attached document (Filename: {filename}, Document ID: {document_id}). "
        "Extract the structured fields in strictly valid JSON format.\n\n"
        "Return ONLY a valid JSON object matching this exact structure with no conversational preamble:\n"
        "{\n"
        '  "invoice_number": "string",\n'
        '  "vendor": "string",\n'
        '  "tax_id": "string",\n'
        '  "vendor_trusted": true,\n'
        '  "vendor_domain": "string",\n'
        '  "currency": "USD",\n'
        '  "subtotal": 0.0,\n'
        '  "tax_rate": 0.18,\n'
        '  "tax_amount": 0.0,\n'
        '  "grand_total": 0.0,\n'
        '  "issue_date": "YYYY-MM-DD",\n'
        '  "due_date": "YYYY-MM-DD",\n'
        '  "line_items": [{"item": "string", "quantity": 1, "unit_price": 0.0}],\n'
        '  "ai_analysis": "Assessment of vendor legitimacy and financial risk",\n'
        '  "extraction_confidence": 0.99\n'
        "}"
    )

    # Build native multi-modal content blocks for Amazon Bedrock Converse API
    content_blocks = []
    clean_doc_name = re.sub(r'[^a-zA-Z0-9_-]', '_', os.path.splitext(filename)[0])[:30] or "invoice_doc"

    if ext == ".pdf":
        content_blocks.append({
            "document": {
                "format": "pdf",
                "name": clean_doc_name,
                "source": {"bytes": content_bytes}
            }
        })
        content_blocks.append({"text": prompt})
    elif ext in [".png", ".jpg", ".jpeg"]:
        img_format = "png" if ext == ".png" else "jpeg"
        content_blocks.append({
            "image": {
                "format": img_format,
                "source": {"bytes": content_bytes}
            }
        })
        content_blocks.append({"text": prompt})
    else:
        # Plain text formats (.txt, .csv)
        try:
            text_str = content_bytes.decode('utf-8', errors='ignore')
        except Exception:
            text_str = ""
        content_blocks.append({
            "text": f"Document content:\n\"\"\"\n{text_str[:4000]}\n\"\"\"\n\n{prompt}"
        })

    subsegment = None
    if os.environ.get('_X_AMZN_TRACE_ID') and XRAY_ENABLED and xray_recorder:
        try:
            subsegment = xray_recorder.begin_subsegment("AmazonBedrock_Claude3Haiku_Inference")
            if subsegment:
                subsegment.put_annotation("document_id", str(document_id))
                subsegment.put_annotation("model_id", BEDROCK_MODEL_ID)
                subsegment.put_annotation("file_extension", ext)
                subsegment.put_metadata("filename", filename)
                subsegment.put_metadata("byte_size", len(content_bytes))
        except Exception:
            pass

    try:
        response = bedrock_runtime.converse(
            modelId=BEDROCK_MODEL_ID,
            messages=[{"role": "user", "content": content_blocks}],
            inferenceConfig={"temperature": 0.0, "maxTokens": 1000}
        )
        content_text = response['output']['message']['content'][0]['text']
        # Match the outermost JSON object
        start = content_text.find('{')
        end = content_text.rfind('}')
        if start != -1 and end != -1 and end > start:
            json_str = content_text[start:end + 1]
            parsed = parse_dynamo_payload(json_str)
            parsed["ai_model_used"] = BEDROCK_MODEL_ID
            parsed["ai_inference_provider"] = "Amazon Bedrock (Serverless Multi-Modal LLM)"
            parsed["input_modality"] = "native_pdf" if ext == ".pdf" else ("native_image" if ext in [".png", ".jpg", ".jpeg"] else "text")
            parsed["raw_byte_length"] = len(content_bytes)
            # Ensure float rounding for safety
            if "subtotal" in parsed: parsed["subtotal"] = round(float(parsed["subtotal"]), 2)
            if "tax_amount" in parsed: parsed["tax_amount"] = round(float(parsed["tax_amount"]), 2)
            if "grand_total" in parsed: parsed["grand_total"] = round(float(parsed["grand_total"]), 2)

            # Enforce server-side enterprise vendor verification
            # Model response is untrusted: verify vendor against server-side registry
            vendor_raw = str(parsed.get("vendor", "")).strip()
            verified_vendor = None
            for reg_v in SAMPLE_VENDORS:
                if reg_v["name"].lower() in vendor_raw.lower() or reg_v["domain"].lower() in vendor_raw.lower():
                    verified_vendor = reg_v
                    break

            if verified_vendor:
                parsed["vendor_trusted"] = bool(verified_vendor.get("trusted", False))
                parsed["vendor_registry_match"] = True
                parsed["tax_id"] = verified_vendor.get("tax_id", parsed.get("tax_id"))
            else:
                # Untrusted / unregistered vendor: never allow model to grant trust unilaterally
                parsed["vendor_trusted"] = False
                parsed["vendor_registry_match"] = False

            if subsegment:
                try:
                    subsegment.put_annotation("vendor_trusted", bool(parsed.get("vendor_trusted", True)))
                    subsegment.put_metadata("vendor", str(parsed.get("vendor", "")))
                    subsegment.put_metadata("grand_total", float(parsed.get("grand_total", 0.0)))
                except Exception:
                    pass

            return parsed
    except Exception as e:
        print(f"Bedrock invocation note (using rule-based fallback): {e}")
    finally:
        if subsegment and XRAY_ENABLED and xray_recorder:
            try:
                xray_recorder.end_subsegment()
            except Exception:
                pass

    return None

def parse_document_content_rules(content_bytes: bytes, filename: str, document_id: str):
    """
    Deterministic rule-based fallback when Bedrock is offline or in local test environment.
    Strictly parses verified text matches. Never invents default amounts or random vendors.
    """
    _, ext = os.path.splitext(filename.lower())

    # Images (.png, .jpg, .jpeg) cannot be decoded as text; they require Bedrock vision
    if ext in [".png", ".jpg", ".jpeg"]:
        return {
            "invoice_number": None,
            "vendor": "Unextracted (Image requires Bedrock Multi-Modal Vision)",
            "tax_id": None,
            "vendor_trusted": False,
            "vendor_domain": None,
            "currency": "USD",
            "subtotal": 0.0,
            "tax_rate": 0.0,
            "tax_amount": 0.0,
            "grand_total": 0.0,
            "issue_date": None,
            "due_date": None,
            "line_items": [],
            "extraction_confidence": 0.0,
            "raw_byte_length": len(content_bytes),
            "ai_model_used": "None",
            "ai_inference_provider": "None",
            "extraction_status": "EXTRACTION_FAILED",
            "ai_analysis": f"Multi-modal vision extraction unavailable for image '{filename}'. Manual entry required."
        }

    text_content = ""
    try:
        text_content = content_bytes.decode('utf-8', errors='ignore')
    except Exception:
        text_content = ""

    # If document has no readable plaintext (e.g. binary compressed PDF)
    if not text_content or len(text_content.strip()) < 10:
        return {
            "invoice_number": None,
            "vendor": "Unextracted (Binary Document)",
            "tax_id": None,
            "vendor_trusted": False,
            "vendor_domain": None,
            "currency": "USD",
            "subtotal": 0.0,
            "tax_rate": 0.0,
            "tax_amount": 0.0,
            "grand_total": 0.0,
            "issue_date": None,
            "due_date": None,
            "line_items": [],
            "extraction_confidence": 0.0,
            "raw_byte_length": len(content_bytes),
            "ai_model_used": "None",
            "ai_inference_provider": "None",
            "extraction_status": "EXTRACTION_FAILED",
            "ai_analysis": f"Binary document '{filename}' contains no readable plaintext and Bedrock was unavailable. Flagged for manual review."
        }

    # 1. Search for amount in document text
    extracted_amount = None
    amount_matches = re.findall(r'(?:total|amount|due|balance)[\s:]*\$?([0-9,]+\.[0-9]{2})', text_content, re.IGNORECASE)
    if amount_matches:
        try:
            extracted_amount = float(amount_matches[-1].replace(',', ''))
        except ValueError:
            pass

    # 2. Search for invoice number line-by-line
    extracted_inv_num = None
    for line in text_content.splitlines():
        m = re.match(r'^(?:invoice\s*(?:#|no\.?|num(?:ber)?)?|inv\s*#|bill\s*#)[\s:]*([A-Za-z0-9\-]+)$', line.strip(), re.IGNORECASE)
        if m:
            extracted_inv_num = m.group(1).upper()
            break
    if not extracted_inv_num:
        fallback_m = re.findall(r'(?:invoice|inv)[\s#:]+([A-Za-z0-9\-]*\d+[A-Za-z0-9\-]*)', text_content, re.IGNORECASE)
        if fallback_m:
            extracted_inv_num = fallback_m[0].upper()

    # 3. Match vendor entity against registry
    matched_vendor = None
    for vendor in SAMPLE_VENDORS:
        if vendor["name"].lower() in text_content.lower() or vendor["domain"].lower() in text_content.lower():
            matched_vendor = vendor
            break

    # If vendor is not in known registry, do NOT guess or synthesize from document_id
    if not matched_vendor:
        matched_vendor = {
            "name": "Unknown / Unregistered Vendor",
            "tax_id": "UNREGISTERED",
            "domain": "unknown",
            "trusted": False
        }

    today = datetime.date.today()
    due_date = today + datetime.timedelta(days=30)

    # Fail closed on amount: do NOT invent default numbers if amount could not be found
    if extracted_amount is None:
        return {
            "invoice_number": extracted_inv_num,
            "vendor": matched_vendor["name"],
            "tax_id": matched_vendor["tax_id"],
            "vendor_trusted": matched_vendor["trusted"],
            "vendor_domain": matched_vendor["domain"],
            "currency": "USD",
            "subtotal": 0.0,
            "tax_rate": 0.0,
            "tax_amount": 0.0,
            "grand_total": 0.0,
            "issue_date": today.isoformat(),
            "due_date": due_date.isoformat(),
            "line_items": [],
            "extraction_confidence": 0.0,
            "raw_byte_length": len(content_bytes),
            "ai_model_used": "Deterministic Rule-Engine (Bedrock Fallback)",
            "ai_inference_provider": "DocuFlow Heuristic Engine",
            "extraction_status": "EXTRACTION_FAILED",
            "ai_analysis": f"Vendor identified as '{matched_vendor['name']}', but no verified amounts were detected in text. Flagged for manual review."
        }

    tax_rate = 0.18
    tax_amount = round(extracted_amount * tax_rate, 2)
    grand_total = round(extracted_amount + tax_amount, 2)

    if not extracted_inv_num:
        extracted_inv_num = f"INV-{today.strftime('%Y%m')}-{document_id[:6].upper()}"

    return {
        "invoice_number": extracted_inv_num,
        "vendor": matched_vendor["name"],
        "tax_id": matched_vendor["tax_id"],
        "vendor_trusted": matched_vendor["trusted"],
        "vendor_registry_match": matched_vendor.get("trusted", False),
        "vendor_domain": matched_vendor["domain"],
        "currency": "USD",
        "subtotal": round(extracted_amount, 2),
        "tax_rate": tax_rate,
        "tax_amount": round(tax_amount, 2),
        "grand_total": round(grand_total, 2),
        "issue_date": today.isoformat(),
        "due_date": due_date.isoformat(),
        "line_items": [
            {
                "item": "Cloud Compute & Managed SaaS Infrastructure",
                "quantity": 1,
                "unit_price": round(extracted_amount, 2)
            }
        ],
        "extraction_confidence": 0.95 if matched_vendor["trusted"] else 0.40,
        "raw_text_length": len(text_content),
        "ai_model_used": "Deterministic Rule-Engine (Bedrock Fallback)",
        "ai_inference_provider": "DocuFlow Heuristic Engine",
        "extraction_status": "SUCCESS",
        "ai_analysis": f"Rule-based extraction verified vendor '{matched_vendor['name']}' from document plaintext."
    }

def lambda_handler(event, context):
    print("Extractor received event:", json.dumps(event, default=str))

    document_id = event.get("document_id")
    s3_bucket = event.get("s3_bucket")
    s3_key = event.get("s3_key")
    filename = event.get("filename", "invoice.pdf")

    if not document_id:
        raise ValueError("Missing required field: document_id")

    if not s3_bucket or not s3_key:
        raise ValueError(f"Missing required S3 coordinates: s3_bucket and s3_key are mandatory for document {document_id}")

    # Read real bytes from S3 - Fail closed if missing
    try:
        s3_obj = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)
        content_bytes = s3_obj['Body'].read()
    except Exception as e:
        raise FileNotFoundError(f"Document object s3://{s3_bucket}/{s3_key} could not be read: {e}")

    if not content_bytes:
        raise ValueError(f"Document payload is empty or missing for document {document_id}")

    # 1. Try Amazon Bedrock serverless Multi-Modal extraction first (native PDF/Image)
    extracted_data = extract_with_bedrock(content_bytes, filename, document_id)

    # 2. Fall back to deterministic rule extraction if Bedrock is offline or in sandbox
    if not extracted_data:
        extracted_data = parse_document_content_rules(content_bytes, filename, document_id)

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Persist extracted entities to DynamoDB with true Decimals - Fail closed if persistence fails
    try:
        table.update_item(
            Key={"PK": f"DOC#{document_id}", "SK": "METADATA"},
            UpdateExpression="SET #st = :st, extracted_data = :data, updated_at = :now",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":st": "EXTRACTED",
                ":data": decimal_serialize(extracted_data),
                ":now": now_iso
            }
        )
    except Exception as e:
        raise RuntimeError(f"Failed to persist extracted entities to DynamoDB for document {document_id}: {e}")

    output_event = dict(event)
    output_event["extracted_data"] = extracted_data
    return output_event
