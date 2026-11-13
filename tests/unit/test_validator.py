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
                self.assertTrue(sec["sha256_computed"])
                self.assertTrue(sec["format_whitelisted"])
                self.assertTrue(sec["size_within_limits"])
                self.assertNotIn("integrity_verified", sec)
                self.assertNotIn("compliance_cleared", sec)
                self.assertIn("sha256", sec)
                # SHA256 of b"%PDF-1.4 Mock Invoice Content"
                import hashlib
                expected_sha = hashlib.sha256(b"%PDF-1.4 Mock Invoice Content").hexdigest()
                self.assertEqual(sec["sha256"], expected_sha)

    def test_missing_s3_coordinates_raises_error(self):
        """Verifies validator requires s3_bucket and s3_key and rejects synthetic fallback."""
        event = {
            "document_id": "test-uuid-no-coords",
            "filename": "invoice.pdf"
        }
        with self.assertRaises(ValueError) as ctx:
            validator_app.lambda_handler(event, None)
        self.assertIn("Missing required S3 coordinates", str(ctx.exception))

    def test_oversized_s3_file_fails_closed_before_download(self):
        """Verifies validator checks head_object size before reading full payload, rejecting >10MB files."""
        event = {
            "document_id": "test-uuid-oversized",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/test-uuid-oversized/huge.pdf",
            "filename": "huge.pdf"
        }
        with patch.object(validator_app, 's3_client') as mock_s3:
            mock_s3.head_object.return_value = {"ContentLength": 15 * 1024 * 1024} # 15MB
            with self.assertRaises(ValueError) as ctx:
                validator_app.lambda_handler(event, None)
            self.assertIn("exceeds maximum limit", str(ctx.exception))
            # Critical: must NOT download full body if head_object reported oversized
            mock_s3.get_object.assert_not_called()

    def test_missing_s3_object_fails_closed(self):
        """Verifies fail-closed behavior when S3 object cannot be found."""
        event = {
            "document_id": "test-uuid-missing",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/test-uuid-missing/missing.pdf",
            "filename": "missing.pdf"
        }
        with patch.object(validator_app, 's3_client') as mock_s3:
            mock_s3.head_object.side_effect = Exception("NoSuchKey")
            with self.assertRaises(FileNotFoundError):
                validator_app.lambda_handler(event, None)

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

    def test_validator_fails_closed_when_dynamo_errors(self):
        """Verifies validator raises RuntimeError if DynamoDB update fails."""
        event = {
            "document_id": "test-uuid-fail-db",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/test-uuid-fail-db/invoice.pdf",
            "filename": "invoice.pdf"
        }
        with patch.object(validator_app, 'table') as mock_table:
            with patch.object(validator_app, 's3_client') as mock_s3:
                mock_s3.head_object.return_value = {"ContentLength": 1024}
                mock_s3.get_object.return_value = {"Body": BytesIO(b"%PDF-1.4 sample content")}
                mock_table.update_item.side_effect = Exception("DynamoDBWriteError")
                with self.assertRaises(RuntimeError) as ctx:
                    validator_app.lambda_handler(event, None)
                self.assertIn("Failed to persist security validation results to DynamoDB", str(ctx.exception))

if __name__ == '__main__':
    unittest.main()
