import sys
import os
import importlib.util

os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"

import unittest
from unittest.mock import MagicMock, patch

MODULE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../functions/risk_scorer/app.py'))
spec = importlib.util.spec_from_file_location("risk_scorer_app", MODULE_PATH)
risk_scorer_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(risk_scorer_app)

class TestRiskScorer(unittest.TestCase):

    def test_parallel_state_merged_input(self):
        # Emulates Step Functions CalculateRiskScore receiving {"parallel_results": [branch1, branch2]}
        # (Parallel state wraps output in Payload key; ASL passes as {"parallel_results.$": "$"})
        parallel_event = {
            "document_id": "doc-parallel-123",
            "filename": "AWS_Monthly.pdf",
            "s3_bucket": "test-bucket",
            "s3_key": "uploads/doc-parallel-123/AWS_Monthly.pdf",
            "parallel_results": [
                {
                    "document_id": "doc-parallel-123",
                    "extracted_data": {
                        "vendor": "Amazon Web Services EMEA",
                        "tax_id": "LU26375245",
                        "vendor_trusted": True,
                        "vendor_registry_match": True,
                        "vendor_identity_verified": True,
                        "grand_total": 1420.50
                    }
                },
                {
                    "document_id": "doc-parallel-123",
                    "security_scan": {
                        "status": "VALIDATED",
                        "integrity_verified": True,
                        "format_whitelisted": True
                    }
                }
            ]
        }
        with patch.object(risk_scorer_app, 'table'):
            result = risk_scorer_app.lambda_handler(parallel_event, None)
            self.assertEqual(result["document_id"], "doc-parallel-123")
            self.assertEqual(result["filename"], "AWS_Monthly.pdf")
            self.assertIn("risk", result)
            self.assertEqual(result["risk"]["level"], "LOW")
            self.assertEqual(result["risk"]["recommendation"], "AUTO_APPROVE")

    def test_high_risk_flag_review(self):
        event = {
            "document_id": "doc-test-2222",
            "extracted_data": {
                "vendor": "Apex Global Consultancy Offshore Ltd",
                "vendor_trusted": False,
                "grand_total": 85000.00
            },
            "security_scan": {"integrity_verified": True, "format_whitelisted": True}
        }
        with patch.object(risk_scorer_app, 'table'):
            result = risk_scorer_app.lambda_handler(event, None)
            self.assertEqual(result["risk"]["level"], "HIGH")
            self.assertEqual(result["risk"]["recommendation"], "FLAG_FOR_MANUAL_REVIEW")
            self.assertGreaterEqual(result["risk"]["score"], 70)

    def test_extraction_failed_elevates_risk(self):
        event = {
            "document_id": "doc-failed-extract",
            "extracted_data": {
                "vendor": "Unextracted (Binary Document)",
                "vendor_trusted": False,
                "grand_total": 0.0,
                "extraction_status": "EXTRACTION_FAILED"
            },
            "security_scan": {"integrity_verified": True, "format_whitelisted": True}
        }
        with patch.object(risk_scorer_app, 'table'):
            result = risk_scorer_app.lambda_handler(event, None)
            self.assertEqual(result["risk"]["level"], "HIGH")
            self.assertEqual(result["risk"]["recommendation"], "FLAG_FOR_MANUAL_REVIEW")

    def test_model_unverified_vendor_claims_trust_treated_as_untrusted(self):
        """Verifies risk scorer treats vendor as untrusted if not verified in server-side registry."""
        event = {
            "document_id": "doc-fake-trust",
            "extracted_data": {
                "vendor": "Unverified Vendor claiming trust",
                "vendor_trusted": True,
                "vendor_registry_match": False,  # Server-side check failed
                "grand_total": 1200.0
            },
            "security_scan": {"format_whitelisted": True, "size_within_limits": True}
        }
        with patch.object(risk_scorer_app, 'table'):
            result = risk_scorer_app.lambda_handler(event, None)
            # Must be flagged as untrusted (+45 risk score)
            self.assertIn("Untrusted or unregistered offshore entity", " ".join(result["risk"]["factors"]))
            self.assertGreaterEqual(result["risk"]["score"], 50)

    def test_tax_id_mismatch_triggers_critical_identity_spoofing_risk(self):
        """Verifies tax_id_mismatch flag adds critical +65 penalty and flags for manual review."""
        event = {
            "document_id": "doc-spoof-attempt",
            "extracted_data": {
                "vendor": "Amazon Web Services EMEA",
                "vendor_trusted": False,
                "vendor_registry_match": False,
                "tax_id_mismatch": True,
                "grand_total": 500.0  # Even for small amounts, spoofing must block auto-approval
            },
            "security_scan": {"format_whitelisted": True, "size_within_limits": True}
        }
        with patch.object(risk_scorer_app, 'table'):
            result = risk_scorer_app.lambda_handler(event, None)
            risk = result["risk"]
            self.assertGreaterEqual(risk["score"], 70)
            self.assertEqual(risk["level"], "HIGH")
            self.assertEqual(risk["recommendation"], "FLAG_FOR_MANUAL_REVIEW")
            self.assertTrue(any("Identity Spoofing Detected" in f for f in risk["factors"]))

    def test_substring_vendor_name_rejected_by_exact_trusted_set(self):
        """Verifies substring containing trusted name ('AWS Hacker Org') is rejected by exact trusted set."""
        event = {
            "document_id": "doc-substring-spoof",
            "extracted_data": {
                "vendor": "AWS Phishing Service Inc",
                "vendor_trusted": True,  # Attacker injected True into payload
                "grand_total": 100.0
            },
            "security_scan": {"format_whitelisted": True, "size_within_limits": True}
        }
        with patch.object(risk_scorer_app, 'table'):
            result = risk_scorer_app.lambda_handler(event, None)
            risk = result["risk"]
            # Must be penalized +45 for untrusted entity despite vendor_trusted: True
            self.assertGreaterEqual(risk["score"], 50)
            self.assertTrue(any("Untrusted or unregistered offshore entity" in f for f in risk["factors"]))

    def test_uncorroborated_registered_vendor_claims_prevented_from_auto_approval(self):
        """Verifies a forged invoice with exact registered name but no Tax ID/domain is routed to manual review."""
        event = {
            "document_id": "doc-forged-aws-no-corroboration",
            "extracted_data": {
                "vendor": "Amazon Web Services EMEA",
                "vendor_registry_match": True,
                "vendor_identity_verified": False,  # Missing Tax ID and domain corroboration
                "vendor_trusted": False,            # Extractor rejected trust
                "grand_total": 1200.00              # Low amount that previously could have been auto-approved!
            },
            "security_scan": {"format_whitelisted": True, "size_within_limits": True}
        }
        with patch.object(risk_scorer_app, 'table'):
            result = risk_scorer_app.lambda_handler(event, None)
            risk = result["risk"]
            # Auto-approval MUST be prevented:
            self.assertFalse(risk["vendor_trusted"])
            self.assertFalse(risk["vendor_identity_verified"])
            self.assertTrue(risk["requires_manual_review"])
            self.assertGreaterEqual(risk["score"], 70)
            self.assertEqual(risk["level"], "HIGH")
            self.assertEqual(risk["recommendation"], "FLAG_FOR_MANUAL_REVIEW")
            self.assertTrue(any("Uncorroborated vendor claim" in f for f in risk["factors"]))
            self.assertIn("DEMO_DATA_ONLY", risk["registry_notice"])

    def test_corroborated_vendor_with_tax_id_eligible_for_auto_approval(self):
        """Verifies an invoice with exact name AND verified Tax ID corroboration is trusted and eligible for auto-approval."""
        event = {
            "document_id": "doc-valid-aws",
            "extracted_data": {
                "vendor": "Amazon Web Services EMEA",
                "tax_id": "LU26375245",
                "vendor_domain": "aws.amazon.com",
                "vendor_registry_match": True,
                "vendor_identity_verified": True,
                "vendor_trusted": True,
                "grand_total": 1200.00
            },
            "security_scan": {"format_whitelisted": True, "size_within_limits": True}
        }
        with patch.object(risk_scorer_app, 'table'):
            result = risk_scorer_app.lambda_handler(event, None)
            risk = result["risk"]
            self.assertTrue(risk["vendor_trusted"])
            self.assertTrue(risk["vendor_identity_verified"])
            self.assertFalse(risk["requires_manual_review"])
            self.assertLess(risk["score"], 35)
            self.assertEqual(risk["level"], "LOW")
            self.assertEqual(risk["recommendation"], "AUTO_APPROVE")

    def test_review_handler_waitForTaskToken(self):
        event = {
            "document_id": "doc-test-3333",
            "task_token": "AAAAKgAAAAIAAAAAAAAMockTaskToken==",
            "risk": {"score": 85, "level": "HIGH"}
        }
        with patch.object(risk_scorer_app, 'table') as mock_table:
            res = risk_scorer_app.review_handler(event, None)
            self.assertEqual(res["statusCode"], 200)
            mock_table.update_item.assert_called_once()

    def test_review_handler_fails_closed_when_dynamo_errors(self):
        """Verifies review_handler raises exception if task_token persistence fails."""
        event = {
            "document_id": "doc-test-fail-save",
            "task_token": "UnsavedTaskToken==",
            "risk": {"score": 85, "level": "HIGH"}
        }
        with patch.object(risk_scorer_app, 'table') as mock_table:
            mock_table.update_item.side_effect = Exception("DynamoDBProvisionedThroughputExceeded")
            with self.assertRaises(Exception):
                risk_scorer_app.review_handler(event, None)

    def test_review_handler_fails_closed_when_sns_publish_errors(self):
        """Verifies review_handler raises RuntimeError if SNS publish fails."""
        event = {
            "document_id": "doc-test-fail-sns",
            "task_token": "ValidTaskToken==",
            "risk": {"score": 85, "level": "HIGH"}
        }
        with patch.object(risk_scorer_app, 'table'):
            with patch.dict(os.environ, {"SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:test-topic"}):
                with patch('boto3.client') as mock_boto_client:
                    mock_sns = MagicMock()
                    mock_sns.publish.side_effect = Exception("SNSAccessDenied")
                    mock_boto_client.return_value = mock_sns
                    with self.assertRaises(RuntimeError) as ctx:
                        risk_scorer_app.review_handler(event, None)
                    self.assertIn("Failed to publish high-risk approval alert to SNS topic", str(ctx.exception))

    def test_risk_scorer_fails_closed_when_dynamo_errors(self):
        """Verifies risk_scorer lambda_handler raises RuntimeError if DynamoDB update fails."""
        event = {
            "document_id": "doc-test-risk-db-fail",
            "extracted_data": {"vendor": "AWS", "grand_total": 100.0, "vendor_trusted": True},
            "security_scan": {"format_whitelisted": True, "size_within_limits": True}
        }
        with patch.object(risk_scorer_app, 'table') as mock_table:
            mock_table.update_item.side_effect = Exception("DynamoDBUpdateError")
            with self.assertRaises(RuntimeError) as ctx:
                risk_scorer_app.lambda_handler(event, None)
            self.assertIn("Failed to persist risk score to DynamoDB", str(ctx.exception))

    def test_notifier_handler(self):
        event = {
            "document_id": "doc-test-4444",
            "status": "APPROVED",
            "decision": "AUTO_APPROVED",
            "message": "Auto approved"
        }
        with patch.object(risk_scorer_app, 'table'):
            res = risk_scorer_app.notifier_handler(event, None)
            self.assertEqual(res["statusCode"], 200)
            self.assertEqual(res["final_status"], "APPROVED")

    def test_notifier_handler_fails_closed_when_dynamo_errors(self):
        """Verifies notifier_handler raises RuntimeError if DynamoDB update fails."""
        event = {
            "document_id": "doc-test-notifier-fail",
            "status": "APPROVED",
            "decision": "AUTO_APPROVED"
        }
        with patch.object(risk_scorer_app, 'table') as mock_table:
            mock_table.update_item.side_effect = Exception("DynamoDBWriteError")
            with self.assertRaises(RuntimeError) as ctx:
                risk_scorer_app.notifier_handler(event, None)
            self.assertIn("Failed to persist final document status to DynamoDB", str(ctx.exception))

    def test_notifier_handler_callback_path(self):
        # Emulates sendTaskSuccess callback: reviewer + notes, no 'message' field
        event = {
            "document_id": "doc-test-5555",
            "status": "APPROVED",
            "decision": "MANUAL_APPROVED",
            "reviewer": "finance-lead@company.com",
            "notes": "Vendor verified via procurement portal",
            "approved_at": "2026-11-13T12:00:00+00:00"
        }
        with patch.object(risk_scorer_app, 'table'):
            res = risk_scorer_app.notifier_handler(event, None)
            self.assertEqual(res["statusCode"], 200)
            self.assertEqual(res["final_status"], "APPROVED")
            self.assertIn("finance-lead@company.com", res["message"])

if __name__ == '__main__':
    unittest.main()
