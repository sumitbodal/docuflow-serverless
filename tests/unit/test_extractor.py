import sys
import os
import json
import importlib.util
from io import BytesIO

os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"

import unittest
from unittest.mock import MagicMock, patch

MODULE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../functions/extractor/app.py'))
spec = importlib.util.spec_from_file_location("extractor_app", MODULE_PATH)
extractor_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extractor_app)

class TestExtractor(unittest.TestCase):

    def test_bedrock_foundation_model_extraction(self):
        """Verifies true serverless AI inference using Amazon Bedrock Converse API."""
        sample_invoice_text = (
            "TAX INVOICE\n"
            "Amazon Web Services EMEA SARL\n"
            "Invoice Number: INV-AWS-2026-9912\n"
            "Date: 2026-11-10\n"
            "Total Amount: $1,420.50 USD\n"
        ).encode('utf-8')

        bedrock_mock_response = {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps({
                                "invoice_number": "INV-AWS-2026-9912",
                                "vendor": "Amazon Web Services EMEA SARL",
                                "tax_id": "LU26375245",
                                "vendor_trusted": True,
                                "vendor_domain": "aws.amazon.com",
                                "currency": "USD",
                                "subtotal": 1203.81,
                                "tax_rate": 0.18,
                                "tax_amount": 216.69,
                                "grand_total": 1420.50,
                                "issue_date": "2026-11-10",
                                "due_date": "2026-12-10",
                                "line_items": [{"item": "Amazon EC2 and Lambda compute", "quantity": 1, "unit_price": 1203.81}],
                                "ai_analysis": "Verified enterprise infrastructure vendor entity with low risk anomaly profile.",
                                "extraction_confidence": 0.995
                            })
                        }
                    ]
                }
            }
        }

        event = {
            "document_id": "doc-bedrock-1111",
            "filename": "aws_monthly.pdf",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/doc-bedrock-1111/aws_monthly.pdf"
        }

        with patch.object(extractor_app, 'table'):
            with patch.object(extractor_app, 's3_client') as mock_s3:
                with patch.object(extractor_app, 'bedrock_runtime') as mock_bedrock:
                    mock_s3.get_object.return_value = {"Body": BytesIO(sample_invoice_text)}
                    mock_bedrock.converse.return_value = bedrock_mock_response

                    result = extractor_app.lambda_handler(event, None)
                    self.assertIn("extracted_data", result)
                    data = result["extracted_data"]
                    self.assertEqual(data["invoice_number"], "INV-AWS-2026-9912")
                    self.assertEqual(data["vendor"], "Amazon Web Services EMEA SARL")
                    self.assertEqual(data["grand_total"], 1420.50)
                    self.assertIn("Amazon Bedrock", data["ai_inference_provider"])
                    self.assertEqual(data["input_modality"], "native_pdf")
                    mock_bedrock.converse.assert_called_once()
                    # Verify raw bytes were passed in native document content block
                    call_args = mock_bedrock.converse.call_args[1]
                    blocks = call_args["messages"][0]["content"]
                    self.assertIn("document", blocks[0])
                    self.assertEqual(blocks[0]["document"]["format"], "pdf")
                    self.assertEqual(blocks[0]["document"]["source"]["bytes"], sample_invoice_text)

    def test_bedrock_native_image_extraction(self):
        """Verifies multi-modal image support passing raw bytes in Bedrock image block."""
        png_header_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRmock_invoice_image_bytes"
        event = {
            "document_id": "doc-img-9999",
            "filename": "receipt_scan.png",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/doc-img-9999/receipt_scan.png"
        }
        bedrock_mock_response = {
            "output": {
                "message": {
                    "content": [{"text": json.dumps({"invoice_number": "REC-999", "grand_total": 55.00, "vendor": "Datadog Cloud Monitoring"})}]
                }
            }
        }
        with patch.object(extractor_app, 'table'):
            with patch.object(extractor_app, 's3_client') as mock_s3:
                with patch.object(extractor_app, 'bedrock_runtime') as mock_bedrock:
                    mock_s3.get_object.return_value = {"Body": BytesIO(png_header_bytes)}
                    mock_bedrock.converse.return_value = bedrock_mock_response
                    res = extractor_app.lambda_handler(event, None)
                    data = res["extracted_data"]
                    self.assertEqual(data["input_modality"], "native_image")
                    call_args = mock_bedrock.converse.call_args[1]
                    blocks = call_args["messages"][0]["content"]
                    self.assertIn("image", blocks[0])
                    self.assertEqual(blocks[0]["image"]["format"], "png")

    def test_missing_s3_file_fails_closed(self):
        """Verifies fail-closed behavior: missing S3 object raises error instead of fake data."""
        event = {
            "document_id": "doc-missing-0000",
            "filename": "missing.pdf",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/doc-missing-0000/missing.pdf"
        }
        with patch.object(extractor_app, 's3_client') as mock_s3:
            mock_s3.get_object.side_effect = Exception("NoSuchKey: The specified key does not exist.")
            with self.assertRaises(FileNotFoundError):
                extractor_app.lambda_handler(event, None)

    def test_real_content_byte_extraction_fallback(self):
        """Verifies deterministic rule fallback if Bedrock model is unavailable."""
        sample_invoice_text = (
            "INVOICE\n"
            "Vendor: GitHub Inc (Microsoft)\n"
            "Invoice #: INV-98765\n"
            "Date: 2026-11-01\n"
            "Line Item: Enterprise Plan Licenses\n"
            "Total: $540.00\n"
        ).encode('utf-8')

        event = {
            "document_id": "doc-test-1111",
            "filename": "custom_invoice.pdf",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/doc-test-1111/custom_invoice.pdf"
        }
        with patch.object(extractor_app, 'table'):
            with patch.object(extractor_app, 's3_client') as mock_s3:
                with patch.object(extractor_app, 'bedrock_runtime') as mock_bedrock:
                    mock_s3.get_object.return_value = {"Body": BytesIO(sample_invoice_text)}
                    # Simulate Bedrock offline/disabled
                    mock_bedrock.converse.side_effect = Exception("ModelNotSubscribedException")

                    result = extractor_app.lambda_handler(event, None)
                    self.assertIn("extracted_data", result)
                    data = result["extracted_data"]
                    self.assertEqual(data["invoice_number"], "INV-98765")
                    self.assertEqual(data["vendor"], "GitHub Inc (Microsoft)")
                    self.assertEqual(data["subtotal"], 540.00)
                    self.assertTrue(data["vendor_trusted"])
                    self.assertIn("Fallback", data["ai_model_used"])

    def test_suspicious_offshore_vendor_extraction(self):
        sample_suspicious_text = (
            "BILLING STATEMENT\n"
            "Apex Global Consultancy Offshore Ltd\n"
            "Amount Due: $89,500.00\n"
        ).encode('utf-8')

        event = {
            "document_id": "doc-test-2222",
            "filename": "wire_invoice.pdf",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/doc-test-2222/wire_invoice.pdf"
        }
        with patch.object(extractor_app, 'table'):
            with patch.object(extractor_app, 's3_client') as mock_s3:
                with patch.object(extractor_app, 'bedrock_runtime') as mock_bedrock:
                    mock_s3.get_object.return_value = {"Body": BytesIO(sample_suspicious_text)}
                    mock_bedrock.converse.side_effect = Exception("BedrockDisabled")

                    result = extractor_app.lambda_handler(event, None)
                    data = result["extracted_data"]
                    self.assertFalse(data["vendor_trusted"])
                    self.assertEqual(data["subtotal"], 89500.00)

    def test_bedrock_unregistered_vendor_cannot_claim_trust(self):
        """Verifies server-side registry overrides model: unknown vendor claiming trust is marked untrusted."""
        event = {
            "document_id": "doc-shady-vendor",
            "filename": "fake_invoice.pdf",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/doc-shady-vendor/fake_invoice.pdf"
        }
        bedrock_mock = {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps({
                                "invoice_number": "INV-SHADY-001",
                                "vendor": "Shady Shell Co Offshore",
                                "vendor_trusted": True,  # Model claims trust, but server-side registry must reject it
                                "grand_total": 450.00
                            })
                        }
                    ]
                }
            }
        }
        with patch.object(extractor_app, 'table'):
            with patch.object(extractor_app, 's3_client') as mock_s3:
                with patch.object(extractor_app, 'bedrock_runtime') as mock_bedrock:
                    mock_s3.get_object.return_value = {"Body": BytesIO(b"%PDF-1.4 Fake Invoice Bytes")}
                    mock_bedrock.converse.return_value = bedrock_mock
                    res = extractor_app.lambda_handler(event, None)
                    data = res["extracted_data"]
                    # Server-side registry must override the model's claim
                    self.assertFalse(data["vendor_trusted"])
                    self.assertFalse(data["vendor_registry_match"])

    def test_binary_image_without_bedrock_fails_closed_without_inventing_data(self):
        """Verifies that when Bedrock is offline, image documents fail closed without inventing fake amounts."""
        binary_image_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDRbinary_mock_bytes"
        event = {
            "document_id": "doc-test-binary",
            "filename": "scan.png",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/doc-test-binary/scan.png"
        }
        with patch.object(extractor_app, 'table'):
            with patch.object(extractor_app, 's3_client') as mock_s3:
                with patch.object(extractor_app, 'bedrock_runtime') as mock_bedrock:
                    mock_s3.get_object.return_value = {"Body": BytesIO(binary_image_bytes)}
                    mock_bedrock.converse.side_effect = Exception("BedrockUnavailable")

                    result = extractor_app.lambda_handler(event, None)
                    data = result["extracted_data"]
                    self.assertEqual(data["extraction_status"], "EXTRACTION_FAILED")
                    self.assertEqual(data["grand_total"], 0.0)
                    self.assertFalse(data["vendor_trusted"])
                    self.assertEqual(data["extraction_confidence"], 0.0)

    def test_missing_s3_coordinates_raises_value_error(self):
        """Verifies that extractor requires s3_bucket and s3_key."""
        event = {
            "document_id": "doc-no-s3-coords",
            "filename": "invoice.pdf"
        }
        with self.assertRaises(ValueError) as ctx:
            extractor_app.lambda_handler(event, None)
        self.assertIn("Missing required S3 coordinates", str(ctx.exception))

    def test_substring_vendor_spoofing_rejected(self):
        """Verifies substring matching is rejected: 'AWS Phishing Scam LLC' must not match AWS."""
        sample_invoice_text = (
            "INVOICE\n"
            "Vendor: AWS Phishing Scam LLC\n"
            "Invoice Number: INV-FAKE-999\n"
            "Total Amount: $500.00\n"
        ).encode('utf-8')

        event = {
            "document_id": "doc-spoof-1",
            "filename": "fake_aws.pdf",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/doc-spoof-1/fake_aws.pdf"
        }
        with patch.object(extractor_app, 'table'):
            with patch.object(extractor_app, 's3_client') as mock_s3:
                mock_s3.get_object.return_value = {"Body": BytesIO(sample_invoice_text)}
                with patch.object(extractor_app, 'bedrock_runtime') as mock_bedrock:
                    mock_bedrock.converse.side_effect = Exception("BedrockUnavailable")
                    result = extractor_app.lambda_handler(event, None)
                    data = result["extracted_data"]
                    self.assertFalse(data["vendor_trusted"])
                    self.assertFalse(data["vendor_registry_match"])
                    self.assertNotEqual(data["tax_id"], "LU26375245")

    def test_tax_id_mismatch_detected_and_fails_closed(self):
        """Verifies independent identity verification: mismatched Tax ID fails closed as spoofing."""
        sample_invoice_text = (
            "INVOICE\n"
            "Vendor: Amazon Web Services EMEA\n"
            "Tax ID: KY99999999\n"
            "Invoice Number: INV-IMPOSTOR-1\n"
            "Total: $1,200.00\n"
        ).encode('utf-8')

        event = {
            "document_id": "doc-spoof-tax",
            "filename": "tax_mismatch.pdf",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/doc-spoof-tax/tax_mismatch.pdf"
        }
        with patch.object(extractor_app, 'table'):
            with patch.object(extractor_app, 's3_client') as mock_s3:
                mock_s3.get_object.return_value = {"Body": BytesIO(sample_invoice_text)}
                with patch.object(extractor_app, 'bedrock_runtime') as mock_bedrock:
                    mock_bedrock.converse.side_effect = Exception("BedrockUnavailable")
                    result = extractor_app.lambda_handler(event, None)
                    data = result["extracted_data"]
                    self.assertFalse(data["vendor_trusted"])
                    self.assertFalse(data["vendor_registry_match"])
                    self.assertTrue(data.get("tax_id_mismatch"))

    def test_verify_vendor_against_registry_strictness(self):
        """Direct test of verify_vendor_against_registry for exact matching and domain checks."""
        # Exact match
        ok_match = extractor_app.verify_vendor_against_registry("Amazon Web Services EMEA", "LU26375245", "aws.amazon.com")
        self.assertTrue(ok_match["matched"])
        self.assertTrue(ok_match["trusted"])
        self.assertTrue(ok_match["independent_verified"])

        # Substring attempt
        sub_match = extractor_app.verify_vendor_against_registry("AWS Security LLC")
        self.assertFalse(sub_match["matched"])
        self.assertFalse(sub_match["trusted"])

        # Domain spoof attempt (e.g. attacker domain claiming to be Datadog)
        domain_spoof = extractor_app.verify_vendor_against_registry("Datadog", "US82930219", "datadog.attacker.com")
        self.assertFalse(domain_spoof["matched"])
        self.assertTrue(domain_spoof.get("tax_id_mismatch"))

    def test_extractor_fails_closed_when_dynamo_errors(self):
        """Verifies extractor raises RuntimeError if DynamoDB update fails."""
        event = {
            "document_id": "doc-test-db-fail",
            "filename": "invoice.pdf",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/doc-test-db-fail/invoice.pdf"
        }
        with patch.object(extractor_app, 'table') as mock_table:
            with patch.object(extractor_app, 's3_client') as mock_s3:
                mock_s3.get_object.return_value = {"Body": BytesIO(b"INVOICE\nVendor: AWS\nTotal: $100.00")}
                mock_table.update_item.side_effect = Exception("DynamoDBUpdateError")
                with self.assertRaises(RuntimeError) as ctx:
                    extractor_app.lambda_handler(event, None)
                self.assertIn("Failed to persist extracted entities to DynamoDB", str(ctx.exception))

if __name__ == '__main__':
    unittest.main()
