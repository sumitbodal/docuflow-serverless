import sys
import os
import json
import importlib.util

os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:DocuFlowStateMachine"

import unittest
from unittest.mock import MagicMock, patch

MODULE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../functions/api_handler/app.py'))
spec = importlib.util.spec_from_file_location("api_handler_app", MODULE_PATH)
api_handler_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api_handler_app)

class TestApiHandler(unittest.TestCase):

    def test_cors_options_preflight(self):
        event = {
            "routeKey": "/api/upload-url",
            "requestContext": {"http": {"method": "OPTIONS"}}
        }
        res = api_handler_app.lambda_handler(event, None)
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(res["headers"]["Access-Control-Allow-Origin"], "*")

    def test_upload_url_generation_does_not_start_sfn(self):
        event = {
            "routeKey": "POST /api/upload-url",
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"filename": "invoice.pdf", "content_type": "application/pdf"})
        }
        with patch.object(api_handler_app, 'table'):
            with patch.object(api_handler_app, 's3_client') as mock_s3:
                with patch.object(api_handler_app, 'sfn_client') as mock_sfn:
                    mock_s3.generate_presigned_url.return_value = "https://mock-bucket.s3.amazonaws.com/uploads/xyz/invoice.pdf"
                    res = api_handler_app.lambda_handler(event, None)
                    self.assertEqual(res["statusCode"], 201)
                    data = json.loads(res["body"])
                    self.assertIn("document_id", data)
                    self.assertIn("upload_url", data)
                    # SFN must NOT be triggered until document is uploaded
                    mock_sfn.start_execution.assert_not_called()

    def test_process_document_starts_sfn_execution(self):
        event = {
            "routeKey": "POST /api/documents/doc-12345/process",
            "pathParameters": {"id": "doc-12345"},
            "requestContext": {"http": {"method": "POST"}}
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.get_item.return_value = {
                "Item": {
                    "document_id": "doc-12345",
                    "filename": "invoice.pdf",
                    "s3_key": "uploads/doc-12345/invoice.pdf",
                    "s3_bucket": "test-bucket"
                }
            }
            with patch.object(api_handler_app, 's3_client') as mock_s3:
                mock_s3.head_object.return_value = {"ContentLength": 1024}
                with patch.object(api_handler_app, 'sfn_client') as mock_sfn:
                    mock_sfn.start_execution.return_value = {"executionArn": "arn:aws:states:exec-12345"}
                    res = api_handler_app.lambda_handler(event, None)
                    self.assertEqual(res["statusCode"], 200)
                    mock_sfn.start_execution.assert_called_once()

    def test_process_document_fails_closed_when_s3_missing(self):
        event = {
            "routeKey": "POST /api/documents/doc-missing/process",
            "pathParameters": {"id": "doc-missing"},
            "requestContext": {"http": {"method": "POST"}}
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.get_item.return_value = {
                "Item": {
                    "document_id": "doc-missing",
                    "filename": "missing.pdf",
                    "s3_key": "uploads/doc-missing/missing.pdf",
                    "s3_bucket": "test-bucket"
                }
            }
            with patch.object(api_handler_app, 's3_client') as mock_s3:
                with patch.object(api_handler_app, 'sfn_client') as mock_sfn:
                    mock_s3.head_object.side_effect = Exception("404 NotFound")
                    res = api_handler_app.lambda_handler(event, None)
                    self.assertEqual(res["statusCode"], 400)
                    mock_sfn.start_execution.assert_not_called()

    def test_approve_document_resumes_step_functions_via_task_token(self):
        event = {
            "routeKey": "POST /api/documents/doc-12345/approve",
            "pathParameters": {"id": "doc-12345"},
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"action": "APPROVE", "reviewer": "Finance Director"})
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            # Emulate DynamoDB returning stored task token
            mock_table.get_item.return_value = {
                "Item": {
                    "document_id": "doc-12345",
                    "task_token": "MockTaskToken12345==",
                    "status": "PENDING_APPROVAL"
                }
            }
            with patch.object(api_handler_app, 'sfn_client') as mock_sfn:
                res = api_handler_app.lambda_handler(event, None)
                self.assertEqual(res["statusCode"], 200)
                data = json.loads(res["body"])
                self.assertTrue(data["step_functions_resumed"])
                mock_sfn.send_task_success.assert_called_once()
                call_args = mock_sfn.send_task_success.call_args[1]
                self.assertEqual(call_args["taskToken"], "MockTaskToken12345==")

    def test_approve_document_not_found_returns_404(self):
        event = {
            "routeKey": "POST /api/documents/non-existent-doc/approve",
            "pathParameters": {"id": "non-existent-doc"},
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"action": "APPROVE"})
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.get_item.return_value = {}  # Item not found
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 404)
            data = json.loads(res["body"])
            self.assertIn("not found", data["error"])

if __name__ == '__main__':
    unittest.main()
