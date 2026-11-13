import os
import unittest
from decimal import Decimal
import datetime

os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"

from src.utils import DecimalEncoder, decimal_serialize, parse_dynamo_payload, dumps_api_response

class TestUtils(unittest.TestCase):

    def test_decimal_serialize_floats(self):
        input_data = {
            "subtotal": 1420.50,
            "tax": 255.69,
            "items": [
                {"unit_price": 50.25, "quantity": 2}
            ],
            "vendor": "Amazon Web Services"
        }
        res = decimal_serialize(input_data)
        self.assertIsInstance(res["subtotal"], Decimal)
        self.assertIsInstance(res["tax"], Decimal)
        self.assertIsInstance(res["items"][0]["unit_price"], Decimal)
        self.assertEqual(res["items"][0]["quantity"], 2)
        self.assertEqual(res["vendor"], "Amazon Web Services")

    def test_parse_dynamo_payload_converts_floats_to_decimal(self):
        json_payload = '{"invoice_num": "INV-100", "grand_total": 89500.50, "tax_rate": 0.18}'
        data = parse_dynamo_payload(json_payload)
        self.assertIsInstance(data["grand_total"], Decimal)
        self.assertIsInstance(data["tax_rate"], Decimal)
        self.assertEqual(data["grand_total"], Decimal("89500.50"))

    def test_dumps_api_response_formats_decimals_and_dates(self):
        body = {
            "document_id": "doc-999",
            "amount": Decimal("1420.50"),
            "count": Decimal("5.00"),
            "evaluated_at": datetime.datetime(2026, 11, 13, 12, 0, 0, tzinfo=datetime.timezone.utc)
        }
        json_str = dumps_api_response(body)
        self.assertIn('"amount": 1420.5', json_str)
        self.assertIn('"count": 5', json_str)
        self.assertIn('2026-11-13T12:00:00+00:00', json_str)

if __name__ == '__main__':
    unittest.main()
