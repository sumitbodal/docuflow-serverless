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

    def test_upload_url_generation_fails_closed_when_s3_presign_errors(self):
        """Verifies upload-url returns HTTP 500 if S3 presigning fails, rejecting synthetic URLs."""
        event = {
            "routeKey": "POST /api/upload-url",
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"filename": "invoice.pdf", "content_type": "application/pdf"})
        }
        with patch.object(api_handler_app, 's3_client') as mock_s3:
            mock_s3.generate_presigned_url.side_effect = Exception("S3ClientError: AccessDenied")
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 500)
            data = json.loads(res["body"])
            self.assertIn("Failed to generate secure S3 presigned URL", data["error"])

    def test_upload_url_generation_fails_closed_when_dynamo_put_errors(self):
        """Verifies upload-url returns HTTP 500 if DynamoDB record initialization fails."""
        event = {
            "routeKey": "POST /api/upload-url",
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"filename": "invoice.pdf", "content_type": "application/pdf"})
        }
        with patch.object(api_handler_app, 's3_client') as mock_s3:
            with patch.object(api_handler_app, 'table') as mock_table:
                mock_s3.generate_presigned_url.return_value = "https://real-presigned.s3.amazonaws.com/uploads/xyz/invoice.pdf"
                mock_table.put_item.side_effect = Exception("DynamoDBProvisionedThroughputExceeded")
                res = api_handler_app.lambda_handler(event, None)
                self.assertEqual(res["statusCode"], 500)
                data = json.loads(res["body"])
                self.assertIn("Failed to initialize document record", data["error"])

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

    def test_process_document_fails_closed_when_sfn_start_errors(self):
        """Verifies process endpoint returns HTTP 500 when Step Functions execution fails to start."""
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
                    mock_sfn.start_execution.side_effect = Exception("StepFunctionsExecutionLimitExceeded")
                    res = api_handler_app.lambda_handler(event, None)
                    self.assertEqual(res["statusCode"], 500)
                    data = json.loads(res["body"])
                    self.assertIn("Failed to start Step Functions workflow execution", data["error"])
                    # Verify document is marked FAILED rather than stuck in PROCESSING
                    mock_table.update_item.assert_called()
                    call_kwargs = mock_table.update_item.call_args[1]
                    self.assertEqual(call_kwargs["ExpressionAttributeValues"][":st"], "FAILED")

    def test_approve_document_unauthorized_when_api_key_configured(self):
        """Verifies /approve returns 401 Unauthorized if DOCUFLOW_API_KEY is configured and missing from headers."""
        event_no_auth = {
            "routeKey": "POST /api/documents/doc-12345/approve",
            "pathParameters": {"id": "doc-12345"},
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"action": "APPROVE"})
        }
        with patch.object(api_handler_app, 'DOCUFLOW_API_KEY', 'secret-admin-key-999'):
            res = api_handler_app.lambda_handler(event_no_auth, None)
            self.assertEqual(res["statusCode"], 401)
            data = json.loads(res["body"])
            self.assertIn("Unauthorized", data["error"])

    def test_approve_document_authorized_when_valid_bearer_token(self):
        """Verifies /approve proceeds when valid Authorization Bearer token is supplied."""
        event_with_auth = {
            "routeKey": "POST /api/documents/doc-12345/approve",
            "pathParameters": {"id": "doc-12345"},
            "requestContext": {"http": {"method": "POST"}},
            "headers": {"Authorization": "Bearer secret-admin-key-999"},
            "body": json.dumps({"action": "APPROVE", "reviewer": "Finance Director"})
        }
        with patch.object(api_handler_app, 'DOCUFLOW_API_KEY', 'secret-admin-key-999'):
            with patch.object(api_handler_app, 'table') as mock_table:
                mock_table.get_item.return_value = {
                    "Item": {
                        "document_id": "doc-12345",
                        "task_token": "ValidTaskToken==",
                        "status": "PENDING_APPROVAL"
                    }
                }
                with patch.object(api_handler_app, 'sfn_client') as mock_sfn:
                    res = api_handler_app.lambda_handler(event_with_auth, None)
                    self.assertEqual(res["statusCode"], 200)
                    mock_sfn.send_task_success.assert_called_once()

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

    def test_process_document_fails_closed_when_file_exceeds_size_limit(self):
        """Verifies process endpoint rejects uploads exceeding 10MB limit before starting Step Functions."""
        event = {
            "routeKey": "POST /api/documents/doc-huge/process",
            "pathParameters": {"id": "doc-huge"},
            "requestContext": {"http": {"method": "POST"}}
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.get_item.return_value = {
                "Item": {
                    "document_id": "doc-huge",
                    "filename": "huge.pdf",
                    "s3_key": "uploads/doc-huge/huge.pdf",
                    "s3_bucket": "test-bucket"
                }
            }
            with patch.object(api_handler_app, 's3_client') as mock_s3:
                with patch.object(api_handler_app, 'sfn_client') as mock_sfn:
                    mock_s3.head_object.return_value = {"ContentLength": 20 * 1024 * 1024} # 20MB
                    res = api_handler_app.lambda_handler(event, None)
                    self.assertEqual(res["statusCode"], 400)
                    data = json.loads(res["body"])
                    self.assertIn("exceeds maximum limit", data["error"])
                    mock_sfn.start_execution.assert_not_called()

    def test_approve_document_resumes_step_functions_via_task_token(self):
        event = {
            "routeKey": "POST /api/documents/doc-12345/approve",
            "pathParameters": {"id": "doc-12345"},
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"action": "APPROVE", "reviewer": "Finance Director", "notes": "Approved after PO match"})
        }
        with patch.object(api_handler_app, 'table') as mock_table:
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
                # Verify that message field is included so subsequent ASL states do not fail
                payload = json.loads(call_args["output"])
                self.assertIn("message", payload)
                self.assertEqual(payload["status"], "APPROVED")
                self.assertEqual(payload["decision"], "MANUAL_APPROVED")
                self.assertEqual(payload["reviewer"], "Finance Director")
                mock_table.update_item.assert_called_once()

    def test_approve_document_missing_task_token_returns_400(self):
        event = {
            "routeKey": "POST /api/documents/doc-already-approved/approve",
            "pathParameters": {"id": "doc-already-approved"},
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"action": "APPROVE"})
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            # Document exists but is already approved (no task token)
            mock_table.get_item.return_value = {
                "Item": {
                    "document_id": "doc-already-approved",
                    "status": "APPROVED"
                }
            }
            with patch.object(api_handler_app, 'sfn_client') as mock_sfn:
                res = api_handler_app.lambda_handler(event, None)
                self.assertEqual(res["statusCode"], 400)
                data = json.loads(res["body"])
                self.assertIn("not awaiting approval", data["error"])
                mock_sfn.send_task_success.assert_not_called()
                mock_table.update_item.assert_not_called()

    def test_approve_document_fails_closed_when_sfn_resume_errors(self):
        event = {
            "routeKey": "POST /api/documents/doc-12345/approve",
            "pathParameters": {"id": "doc-12345"},
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"action": "APPROVE", "reviewer": "Finance Director"})
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.get_item.return_value = {
                "Item": {
                    "document_id": "doc-12345",
                    "task_token": "ExpiredTaskToken==",
                    "status": "PENDING_APPROVAL"
                }
            }
            with patch.object(api_handler_app, 'sfn_client') as mock_sfn:
                mock_sfn.send_task_success.side_effect = Exception("TaskDoesNotExist: Task token has expired or is invalid.")
                res = api_handler_app.lambda_handler(event, None)
                self.assertEqual(res["statusCode"], 500)
                data = json.loads(res["body"])
                self.assertIn("Failed to resume Step Functions execution", data["error"])
                # Fail closed: must NOT update document status to approved if Step Functions failed to resume
                mock_table.update_item.assert_not_called()

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

    def test_get_and_list_documents_sanitize_sensitive_task_token(self):
        """Verifies task_token is stripped from API responses to prevent unauthenticated approval spoofing."""
        event_get = {
            "routeKey": "GET /api/documents/doc-sensitive",
            "pathParameters": {"id": "doc-sensitive"},
            "requestContext": {"http": {"method": "GET"}}
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.get_item.return_value = {
                "Item": {
                    "document_id": "doc-sensitive",
                    "status": "PENDING_APPROVAL",
                    "task_token": "SecretSfnTaskToken123==",
                    "filename": "invoice.pdf"
                }
            }
            res = api_handler_app.lambda_handler(event_get, None)
            self.assertEqual(res["statusCode"], 200)
            data = json.loads(res["body"])
            self.assertEqual(data["document_id"], "doc-sensitive")
            self.assertNotIn("task_token", data)

        event_list = {
            "routeKey": "GET /api/documents",
            "requestContext": {"http": {"method": "GET"}}
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.query.return_value = {
                "Items": [
                    {
                        "document_id": "doc-sensitive",
                        "status": "PENDING_APPROVAL",
                        "task_token": "SecretSfnTaskToken123=="
                    }
                ]
            }
            res = api_handler_app.lambda_handler(event_list, None)
            self.assertEqual(res["statusCode"], 200)
            data = json.loads(res["body"])
            self.assertEqual(len(data["documents"]), 1)
            self.assertNotIn("task_token", data["documents"][0])

    def test_list_and_get_documents_unauthorized_when_api_key_configured(self):
        """Verifies read endpoints (list & get) return 401 Unauthorized if DOCUFLOW_API_KEY is configured."""
        event_list = {
            "routeKey": "GET /api/documents",
            "requestContext": {"http": {"method": "GET"}}
        }
        event_get = {
            "routeKey": "GET /api/documents/doc-12345",
            "pathParameters": {"id": "doc-12345"},
            "requestContext": {"http": {"method": "GET"}}
        }
        with patch.object(api_handler_app, 'DOCUFLOW_API_KEY', 'secret-admin-key-999'):
            res_list = api_handler_app.lambda_handler(event_list, None)
            self.assertEqual(res_list["statusCode"], 401)
            data_list = json.loads(res_list["body"])
            self.assertIn("Unauthorized", data_list["error"])

            res_get = api_handler_app.lambda_handler(event_get, None)
            self.assertEqual(res_get["statusCode"], 401)
            data_get = json.loads(res_get["body"])
            self.assertIn("Unauthorized", data_get["error"])

    def test_list_documents_fails_closed_when_both_query_and_scan_error(self):
        """Verifies list endpoint returns HTTP 500 when DynamoDB query and scan both fail."""
        event_list = {
            "routeKey": "GET /api/documents",
            "requestContext": {"http": {"method": "GET"}}
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.query.side_effect = Exception("DynamoDBQueryOutage")
            mock_table.scan.side_effect = Exception("DynamoDBScanOutage")
            res = api_handler_app.lambda_handler(event_list, None)
            self.assertEqual(res["statusCode"], 500)
            data = json.loads(res["body"])
            self.assertIn("Failed to retrieve documents from DynamoDB", data["error"])

    def test_process_document_fails_closed_when_dynamo_read_errors(self):
        """Verifies process endpoint returns HTTP 500 when DynamoDB get throws, without guessing S3 keys."""
        event = {
            "routeKey": "POST /api/documents/doc-db-err/process",
            "pathParameters": {"id": "doc-db-err"},
            "requestContext": {"http": {"method": "POST"}}
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.get_item.side_effect = Exception("DynamoDBConnectionTimeout")
            with patch.object(api_handler_app, 's3_client') as mock_s3:
                res = api_handler_app.lambda_handler(event, None)
                self.assertEqual(res["statusCode"], 500)
                data = json.loads(res["body"])
                self.assertIn("Database read failure", data["error"])
                mock_s3.head_object.assert_not_called()

    def test_process_document_not_found_returns_404_without_guessing_s3_key(self):
        """Verifies process endpoint returns HTTP 404 when document record does not exist."""
        event = {
            "routeKey": "POST /api/documents/doc-not-found/process",
            "pathParameters": {"id": "doc-not-found"},
            "requestContext": {"http": {"method": "POST"}}
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.get_item.return_value = {} # Record not found
            with patch.object(api_handler_app, 's3_client') as mock_s3:
                res = api_handler_app.lambda_handler(event, None)
                self.assertEqual(res["statusCode"], 404)
                data = json.loads(res["body"])
                self.assertIn("not found in database", data["error"])
                mock_s3.head_object.assert_not_called()

    def test_upload_url_generation_rejects_oversized_declared_size(self):
        """Verifies upload-url endpoint rejects declared file sizes exceeding 10MB before issuing presigned URL."""
        event = {
            "routeKey": "POST /api/upload-url",
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({
                "filename": "huge_invoice.pdf",
                "file_size": 25 * 1024 * 1024 # 25MB
            })
        }
        with patch.object(api_handler_app, 's3_client') as mock_s3:
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 400)
            data = json.loads(res["body"])
            self.assertIn("exceeds maximum allowable limit", data["error"])
            mock_s3.generate_presigned_url.assert_not_called()

    def test_upload_url_generation_rejects_unsupported_extension(self):
        """Verifies upload-url endpoint rejects unsupported file formats upfront."""
        event = {
            "routeKey": "POST /api/upload-url",
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"filename": "exploit.sh"})
        }
        with patch.object(api_handler_app, 's3_client') as mock_s3:
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 400)
            data = json.loads(res["body"])
            self.assertIn("Unsupported file extension", data["error"])
            mock_s3.generate_presigned_url.assert_not_called()

    def test_upload_url_generation_signs_content_length_when_declared(self):
        """Verifies presigned URL includes ContentLength parameter for upload-time S3 enforcement."""
        event = {
            "routeKey": "POST /api/upload-url",
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({
                "filename": "valid_invoice.pdf",
                "file_size": 45000
            })
        }
        with patch.object(api_handler_app, 'table'):
            with patch.object(api_handler_app, 's3_client') as mock_s3:
                mock_s3.generate_presigned_url.return_value = "https://s3.amazonaws.com/uploads/xyz/valid_invoice.pdf"
                res = api_handler_app.lambda_handler(event, None)
                self.assertEqual(res["statusCode"], 201)
                call_args = mock_s3.generate_presigned_url.call_args[1]
                self.assertEqual(call_args["Params"]["ContentLength"], 45000)

if __name__ == '__main__':
    unittest.main()
