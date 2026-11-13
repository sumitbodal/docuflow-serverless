import json
import datetime
from decimal import Decimal

class DecimalEncoder(json.JSONEncoder):
    """
    Encodes Decimal objects to float (or int if whole) and datetimes to ISO strings
    for clean API Gateway JSON responses.
    """
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        if isinstance(obj, (datetime.date, datetime.datetime)):
            return obj.isoformat()
        return super().default(obj)

def decimal_serialize(obj):
    """
    Recursively converts float values to Decimal for safe, loss-less Amazon DynamoDB storage.
    """
    if isinstance(obj, float):
        return Decimal(str(round(obj, 4)))
    elif isinstance(obj, dict):
        return {k: decimal_serialize(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [decimal_serialize(v) for v in obj]
    return obj

def parse_dynamo_payload(json_str: str):
    """
    Reads incoming JSON payloads (e.g. from Bedrock or API) directly converting float
    representations into Decimal for DynamoDB storage.
    """
    return json.loads(json_str, parse_float=Decimal)

def dumps_api_response(body: dict) -> str:
    """
    Serializes data structures containing Decimal or datetime into valid JSON strings.
    """
    return json.dumps(body, cls=DecimalEncoder)
