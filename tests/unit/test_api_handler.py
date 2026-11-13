import sys
import os
import json
import importlib.util

os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["STATE_MACHINE_ARN"] = "arn:aws:states:us-east-1:123456789012:stateMachine:DocuFlowStateMachine"
os.environ["DOCUFLOW_API_KEY"] = "test-secret-admin-key"

import unittest
from unittest.mock import MagicMock, patch

MODULE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../functions/api_handler/app.py'))
spec = importlib.util.spec_from_file_location("api_handler_app", MODULE_PATH)
api_handler_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api_handler_app)

AUTH_HEADERS = {"Authorization": "Bearer test-secret-admin-key"}

class TestApiHandler(unittest.TestCase):

    def test_cors_options_preflight(self):
        event = {
            "routeKey": "/api/upload-url",
            "requestContext": {"http": {"method": "OPTIONS"}}
        }
        res = api_handler_app.lambda_handler(event, None)
        self.assertEqual(res["statusCode"], 200)
        self.assertEqual(res["headers"]["Access-Control-Allow-Origin"], "*")

    def test_authorization_fails_closed_by_default_when_key_unset(self):
        """Verifies authorization is strictly fail-closed: if DOCUFLOW_API_KEY is unset, requests are rejected with 401."""
        event = {
            "routeKey": "GET /api/documents",
            "requestContext": {"http": {"method": "GET"}}
        }
        with patch.dict(os.environ, {"DOCUFLOW_API_KEY": "", "ADMIN_API_KEY": ""}):
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 401)
            data = json.loads(res["body"])
            self.assertIn("Unauthorized", data["error"])

    def test_authorization_allows_cognito_jwt_claims(self):
        """Verifies API Gateway Cognito JWT authorizer claims grant authorization without API key."""
        event = {
            "routeKey": "GET /api/documents",
            "requestContext": {
                "http": {"method": "GET"},
                "authorizer": {
                    "jwt": {
                        "claims": {"sub": "user-uuid-1234", "email": "reviewer@company.com"}
                    }
                }
            }
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.query.return_value = {"Items": []}
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 200)

    def test_authorization_allows_lambda_authorizer_context(self):
        """Verifies API Gateway Lambda authorizer context grants authorization."""
        event = {
            "routeKey": "GET /api/documents",
            "requestContext": {
                "http": {"method": "GET"},
                "authorizer": {
                    "lambda": {"user": "admin", "role": "reviewer"}
                }
            }
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.query.return_value = {"Items": []}
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 200)

    def test_upload_url_generation_does_not_start_sfn(self):
        event = {
            "routeKey": "POST /api/upload-url",
            "headers": AUTH_HEADERS,
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"filename": "invoice.pdf", "content_type": "application/pdf", "file_size": 5000})
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
                    self.assertEqual(data["max_size_bytes"], 10 * 1024 * 1024)
                    mock_sfn.start_execution.assert_not_called()

    def test_upload_url_generation_rejects_missing_file_size(self):
        """Verifies upload-url endpoint rejects requests missing file_size to prevent boundary bypass."""
        event = {
            "routeKey": "POST /api/upload-url",
            "headers": AUTH_HEADERS,
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"filename": "invoice.pdf", "content_type": "application/pdf"})
        }
        with patch.object(api_handler_app, 's3_client') as mock_s3:
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 400)
            data = json.loads(res["body"])
            self.assertIn("Missing mandatory field 'file_size'", data["error"])
            mock_s3.generate_presigned_url.assert_not_called()

    def test_upload_url_generation_fails_closed_when_s3_presign_errors(self):
        """Verifies upload-url returns HTTP 500 if S3 presigning fails, rejecting synthetic URLs."""
        event = {
            "routeKey": "POST /api/upload-url",
            "headers": AUTH_HEADERS,
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"filename": "invoice.pdf", "content_type": "application/pdf", "file_size": 2048})
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
            "headers": AUTH_HEADERS,
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"filename": "invoice.pdf", "content_type": "application/pdf", "file_size": 2048})
        }
        with patch.object(api_handler_app, 's3_client') as mock_s3:
            with patch.object(api_handler_app, 'table') as mock_table:
                mock_s3.generate_presigned_url.return_value = "https://real-presigned.s3.amazonaws.com/uploads/xyz/invoice.pdf"
                mock_table.put_item.side_effect = Exception("DynamoDBProvisionedThroughputExceeded")
                res = api_handler_app.lambda_handler(event, None)
                self.assertEqual(res["statusCode"], 500)
                data = json.loads(res["body"])
                self.assertIn("Failed to initialize document record", data["error"])

    def test_upload_url_generation_rejects_oversized_declared_size(self):
        """Verifies upload-url endpoint rejects declared file sizes exceeding 10MB before issuing presigned URL."""
        event = {
            "routeKey": "POST /api/upload-url",
            "headers": AUTH_HEADERS,
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({
                "filename": "huge_invoice.pdf",
                "file_size": 25 * 1024 * 1024
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
            "headers": AUTH_HEADERS,
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"filename": "exploit.sh", "file_size": 500})
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
            "headers": AUTH_HEADERS,
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

    def test_process_document_starts_sfn_execution(self):
        event = {
            "routeKey": "POST /api/documents/doc-12345/process",
            "headers": AUTH_HEADERS,
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
            "headers": AUTH_HEADERS,
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
                    mock_table.update_item.assert_called()
                    call_kwargs = mock_table.update_item.call_args[1]
                    self.assertEqual(call_kwargs["ExpressionAttributeValues"][":st"], "FAILED")

    def test_process_document_fails_closed_when_s3_missing(self):
        event = {
            "routeKey": "POST /api/documents/doc-missing/process",
            "headers": AUTH_HEADERS,
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
            "headers": AUTH_HEADERS,
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
                    mock_s3.head_object.return_value = {"ContentLength": 20 * 1024 * 1024}
                    res = api_handler_app.lambda_handler(event, None)
                    self.assertEqual(res["statusCode"], 400)
                    data = json.loads(res["body"])
                    self.assertIn("exceeds maximum limit", data["error"])
                    mock_sfn.start_execution.assert_not_called()

    def test_process_document_fails_closed_when_dynamo_read_errors(self):
        """Verifies process endpoint returns HTTP 500 when DynamoDB get throws, without guessing S3 keys."""
        event = {
            "routeKey": "POST /api/documents/doc-db-err/process",
            "headers": AUTH_HEADERS,
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
            "headers": AUTH_HEADERS,
            "pathParameters": {"id": "doc-not-found"},
            "requestContext": {"http": {"method": "POST"}}
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.get_item.return_value = {}
            with patch.object(api_handler_app, 's3_client') as mock_s3:
                res = api_handler_app.lambda_handler(event, None)
                self.assertEqual(res["statusCode"], 404)
                data = json.loads(res["body"])
                self.assertIn("not found in database", data["error"])
                mock_s3.head_object.assert_not_called()

    def test_approve_document_resumes_step_functions_via_task_token(self):
        event = {
            "routeKey": "POST /api/documents/doc-12345/approve",
            "headers": AUTH_HEADERS,
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
                payload = json.loads(call_args["output"])
                self.assertIn("message", payload)
                self.assertEqual(payload["status"], "APPROVED")
                self.assertEqual(payload["decision"], "MANUAL_APPROVED")
                self.assertEqual(payload["reviewer"], "Finance Director")
                mock_table.update_item.assert_called_once()

    def test_approve_document_missing_action_returns_400(self):
        """Verifies /approve rejects missing action with HTTP 400 instead of defaulting to APPROVE."""
        event = {
            "routeKey": "POST /api/documents/doc-12345/approve",
            "headers": AUTH_HEADERS,
            "pathParameters": {"id": "doc-12345"},
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"reviewer": "admin"})
        }
        res = api_handler_app.lambda_handler(event, None)
        self.assertEqual(res["statusCode"], 400)
        data = json.loads(res["body"])
        self.assertIn("Missing mandatory field 'action'", data["error"])

    def test_approve_document_invalid_action_returns_400(self):
        """Verifies /approve rejects non-APPROVE/REJECT actions with HTTP 400."""
        event = {
            "routeKey": "POST /api/documents/doc-12345/approve",
            "headers": AUTH_HEADERS,
            "pathParameters": {"id": "doc-12345"},
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"action": "MAYBE"})
        }
        res = api_handler_app.lambda_handler(event, None)
        self.assertEqual(res["statusCode"], 400)
        data = json.loads(res["body"])
        self.assertIn("Invalid action", data["error"])

    def test_approve_document_malformed_json_returns_400(self):
        """Verifies /approve rejects malformed JSON request bodies with HTTP 400."""
        event = {
            "routeKey": "POST /api/documents/doc-12345/approve",
            "headers": AUTH_HEADERS,
            "pathParameters": {"id": "doc-12345"},
            "requestContext": {"http": {"method": "POST"}},
            "body": "{not-valid-json"
        }
        res = api_handler_app.lambda_handler(event, None)
        self.assertEqual(res["statusCode"], 400)
        data = json.loads(res["body"])
        self.assertIn("Invalid JSON", data["error"])

    def test_approve_document_missing_task_token_returns_400(self):
        event = {
            "routeKey": "POST /api/documents/doc-already-approved/approve",
            "headers": AUTH_HEADERS,
            "pathParameters": {"id": "doc-already-approved"},
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"action": "APPROVE"})
        }
        with patch.object(api_handler_app, 'table') as mock_table:
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
            "headers": AUTH_HEADERS,
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
                mock_sfn.send_task_success.side_effect = Exception("TaskDoesNotExist")
                res = api_handler_app.lambda_handler(event, None)
                self.assertEqual(res["statusCode"], 500)
                data = json.loads(res["body"])
                self.assertIn("Failed to resume Step Functions execution", data["error"])
                mock_table.update_item.assert_not_called()

    def test_approve_document_not_found_returns_404(self):
        event = {
            "routeKey": "POST /api/documents/non-existent-doc/approve",
            "headers": AUTH_HEADERS,
            "pathParameters": {"id": "non-existent-doc"},
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps({"action": "APPROVE"})
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.get_item.return_value = {}
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 404)
            data = json.loads(res["body"])
            self.assertIn("not found", data["error"])

    def test_get_and_list_documents_sanitize_sensitive_task_token(self):
        """Verifies task_token is stripped from API responses to prevent unauthenticated approval spoofing."""
        event_get = {
            "routeKey": "GET /api/documents/doc-sensitive",
            "headers": AUTH_HEADERS,
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
            "headers": AUTH_HEADERS,
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

    def test_list_and_get_documents_unauthorized_when_wrong_key(self):
        """Verifies read endpoints (list & get) return 401 Unauthorized if invalid key is sent."""
        event_list = {
            "routeKey": "GET /api/documents",
            "headers": {"Authorization": "Bearer wrong-key"},
            "requestContext": {"http": {"method": "GET"}}
        }
        res_list = api_handler_app.lambda_handler(event_list, None)
        self.assertEqual(res_list["statusCode"], 401)

    def test_list_documents_fails_closed_when_both_query_and_scan_error(self):
        """Verifies list endpoint returns HTTP 500 when DynamoDB query and scan both fail."""
        event_list = {
            "routeKey": "GET /api/documents",
            "headers": AUTH_HEADERS,
            "requestContext": {"http": {"method": "GET"}}
        }
        with patch.object(api_handler_app, 'table') as mock_table:
            mock_table.query.side_effect = Exception("DynamoDBQueryOutage")
            mock_table.scan.side_effect = Exception("DynamoDBScanOutage")
            res = api_handler_app.lambda_handler(event_list, None)
            self.assertEqual(res["statusCode"], 500)
            data = json.loads(res["body"])
            self.assertIn("Failed to retrieve documents from DynamoDB", data["error"])

    def test_approve_document_rejects_non_reviewer_role_with_403(self):
        """Verifies users lacking the 'Reviewers' or 'Admins' group are strictly rejected with 403 Forbidden."""
        event = {
            "routeKey": "POST /api/documents/{id}/approve",
            "pathParameters": {"id": "doc-test-123"},
            "body": json.dumps({"action": "APPROVE"}),
            "requestContext": {
                "http": {"method": "POST"},
                "authorizer": {
                    "jwt": {
                        "claims": {
                            "sub": "user-regular-uuid",
                            "email": "employee@company.com",
                            "cognito:groups": ["Employees"]
                        }
                    }
                }
            }
        }
        with patch.dict(os.environ, {"ADMIN_API_KEY": "", "DOCUFLOW_API_KEY": ""}):
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 403)
            data = json.loads(res["body"])
            self.assertIn("Forbidden", data["error"])
            self.assertIn("Reviewers", data["error"])

    def test_approve_document_allows_user_in_reviewers_group(self):
        """Verifies users belonging to the 'Reviewers' Cognito group can approve documents."""
        event = {
            "routeKey": "POST /api/documents/{id}/approve",
            "pathParameters": {"id": "doc-test-456"},
            "body": json.dumps({"action": "APPROVE", "notes": "Approved by compliance"}),
            "requestContext": {
                "http": {"method": "POST"},
                "authorizer": {
                    "jwt": {
                        "claims": {
                            "sub": "reviewer-uuid-789",
                            "email": "reviewer@company.com",
                            "cognito:groups": ["Reviewers"]
                        }
                    }
                }
            }
        }
        with patch.dict(os.environ, {"ADMIN_API_KEY": "", "DOCUFLOW_API_KEY": ""}), \
             patch.object(api_handler_app, 'table') as mock_table, \
             patch.object(api_handler_app, 'sfn_client') as mock_sfn:
            mock_table.get_item.return_value = {
                "Item": {
                    "document_id": "doc-test-456",
                    "status": "PENDING_APPROVAL",
                    "task_token": "ValidTaskToken=="
                }
            }
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 200)
            data = json.loads(res["body"])
            self.assertEqual(data["status"], "APPROVED")
            mock_sfn.send_task_success.assert_called_once()

    def test_list_documents_isolates_non_reviewer_tenant_data(self):
        """Verifies non-reviewers only receive documents they own."""
        event = {
            "routeKey": "GET /api/documents",
            "requestContext": {
                "http": {"method": "GET"},
                "authorizer": {
                    "jwt": {
                        "claims": {
                            "sub": "user-alice",
                            "email": "alice@company.com"
                        }
                    }
                }
            }
        }
        with patch.dict(os.environ, {"ADMIN_API_KEY": "", "DOCUFLOW_API_KEY": ""}), \
             patch.object(api_handler_app, 'table') as mock_table:
            mock_table.query.return_value = {
                "Items": [
                    {"document_id": "doc-1", "owner": "user-alice", "filename": "alice.pdf"},
                    {"document_id": "doc-2", "owner": "user-bob", "filename": "bob.pdf"}
                ]
            }
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 200)
            data = json.loads(res["body"])
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["documents"][0]["document_id"], "doc-1")

    def test_get_document_denies_other_user_document_with_403(self):
        """Verifies non-reviewers cannot view another user's document."""
        event = {
            "routeKey": "GET /api/documents/{id}",
            "pathParameters": {"id": "doc-bob"},
            "requestContext": {
                "http": {"method": "GET"},
                "authorizer": {
                    "jwt": {
                        "claims": {
                            "sub": "user-alice",
                            "email": "alice@company.com"
                        }
                    }
                }
            }
        }
        with patch.dict(os.environ, {"ADMIN_API_KEY": "", "DOCUFLOW_API_KEY": ""}), \
             patch.object(api_handler_app, 'table') as mock_table:
            mock_table.get_item.return_value = {
                "Item": {
                    "document_id": "doc-bob",
                    "owner": "user-bob",
                    "uploaded_by": "bob@company.com"
                }
            }
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 403)
            data = json.loads(res["body"])
            self.assertIn("Forbidden", data["error"])

    def test_get_document_denies_anonymous_or_unassigned_document_with_403(self):
        """Verifies non-reviewers cannot view records with owner: 'anonymous' or missing owner."""
        event = {
            "routeKey": "GET /api/documents/{id}",
            "pathParameters": {"id": "doc-unassigned"},
            "requestContext": {
                "http": {"method": "GET"},
                "authorizer": {
                    "jwt": {
                        "claims": {
                            "sub": "user-alice",
                            "email": "alice@company.com"
                        }
                    }
                }
            }
        }
        with patch.dict(os.environ, {"ADMIN_API_KEY": "", "DOCUFLOW_API_KEY": ""}), \
             patch.object(api_handler_app, 'table') as mock_table:
            mock_table.get_item.return_value = {
                "Item": {
                    "document_id": "doc-unassigned",
                    "owner": "anonymous",
                    "uploaded_by": "anonymous"
                }
            }
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 403)
            data = json.loads(res["body"])
            self.assertIn("Forbidden", data["error"])

    def test_process_document_denies_anonymous_or_unassigned_document_with_403(self):
        """Verifies non-reviewers cannot process records with owner: 'anonymous' or missing owner."""
        event = {
            "routeKey": "POST /api/documents/{id}/process",
            "pathParameters": {"id": "doc-unassigned"},
            "requestContext": {
                "http": {"method": "POST"},
                "authorizer": {
                    "jwt": {
                        "claims": {
                            "sub": "user-alice",
                            "email": "alice@company.com"
                        }
                    }
                }
            }
        }
        with patch.dict(os.environ, {"ADMIN_API_KEY": "", "DOCUFLOW_API_KEY": ""}), \
             patch.object(api_handler_app, 'table') as mock_table:
            mock_table.get_item.return_value = {
                "Item": {
                    "document_id": "doc-unassigned",
                    "owner": "anonymous",
                    "uploaded_by": "anonymous"
                }
            }
            res = api_handler_app.lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 403)
            data = json.loads(res["body"])
            self.assertIn("Forbidden", data["error"])

if __name__ == '__main__':
    unittest.main()
