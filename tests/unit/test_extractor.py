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
                    self.assertEqual(data["ai_inference_provider"], "Amazon Bedrock (Serverless LLM)")
                    self.assertEqual(data["ai_model_used"], "anthropic.claude-3-haiku-20240307-v1:0")
                    mock_bedrock.converse.assert_called_once()

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

if __name__ == '__main__':
    unittest.main()
