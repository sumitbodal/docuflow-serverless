import sys
import os
import importlib.util

os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"

import unittest
from unittest.mock import MagicMock, patch

from io import BytesIO

MODULE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../functions/validator/app.py'))
spec = importlib.util.spec_from_file_location("validator_app", MODULE_PATH)
validator_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator_app)

class TestValidator(unittest.TestCase):

    def test_valid_pdf_extension_and_security_scan(self):
        event = {
            "document_id": "test-uuid-1234",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/test-uuid-1234/invoice.pdf",
            "filename": "invoice.pdf"
        }
        with patch.object(validator_app, 'table'):
            with patch.object(validator_app, 's3_client') as mock_s3:
                mock_s3.head_object.return_value = {
                    "ContentLength": 2048,
                    "ETag": '"abc123etag"'
                }
                mock_s3.get_object.return_value = {
                    "Body": BytesIO(b"%PDF-1.4 Mock Invoice Content")
                }
                result = validator_app.lambda_handler(event, None)
                self.assertIn("security_scan", result)
                sec = result["security_scan"]
                self.assertEqual(sec["status"], "VALIDATED")
                self.assertEqual(sec["file_size_bytes"], len(b"%PDF-1.4 Mock Invoice Content"))
                self.assertEqual(sec["extension"], ".pdf")
                self.assertTrue(sec["malware_scanned"])
                self.assertTrue(sec["compliance_cleared"])
                self.assertIn("sha256", sec)
                # SHA256 of b"%PDF-1.4 Mock Invoice Content"
                import hashlib
                expected_sha = hashlib.sha256(b"%PDF-1.4 Mock Invoice Content").hexdigest()
                self.assertEqual(sec["sha256"], expected_sha)

    def test_invalid_extension_raises_error(self):
        event = {
            "document_id": "test-uuid-1234",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/test-uuid-1234/malicious.sh",
            "filename": "malicious.sh"
        }
        with self.assertRaises(ValueError) as ctx:
            validator_app.lambda_handler(event, None)
        self.assertIn("Unsupported file format", str(ctx.exception))

if __name__ == '__main__':
    unittest.main()
