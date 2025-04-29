import json
import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
import psycopg2
from openai import OpenAI

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
secrets_client = boto3.client("secretsmanager")

lk_timezone = ZoneInfo("Asia/Colombo")


def get_secret_credentials(secret_name):
    response = secrets_client.get_secret_value(SecretId=secret_name)
    secret = json.loads(response["SecretString"])
    return secret


def parse_transaction(sms_text, fallback_timestamp, OPENAI_API_KEY):
    """
    Use OpenAI to parse SMS into structured transaction JSON
    """
    logger.info(f"Parsing SMS with OpenAI: {sms_text}")

    system_prompt = """
    You are a financial SMS parser. Convert raw SMS notifications into structured JSON. 
    Use the provided timestamp only if the message lacks a date.

    Some rules to follow:
    - If the amount is exactly 4100 LKR, categorize it as "CIGARETTES".
    """

    user_prompt = f'SMS: "{sms_text}"\nCurrent timestamp: "{fallback_timestamp}"'

    try:
        # Initialize the OpenAI client with API key
        client = OpenAI(api_key=OPENAI_API_KEY)

        # Define the JSON schema for transaction data
        transaction_schema = {
            "type": "object",
            "properties": {
                "datetime": {
                    "type": "string",
                    "description": "The date and time of the transaction in ISO 8601 format."
                },
                "category": {
                    "type": "string",
                    "description": "The category that best describes the purpose of the transaction.",
                    "enum": [
                        "FOOD",
                        "TRAVEL",
                        "CIGARETTES",
                        "INCOMING TRANSFER",
                        "OUTGOING TRANSFER",
                        "PURCHASE",
                        "REFUND",
                        "UNKNOWN"
                    ]
                },
                "amount": {
                    "type": "number",
                    "description": "The monetary value of the transaction."
                },
                "description": {
                    "type": "string",
                    "description": "A brief text explaining or identifying the transaction."
                },
                "account_number": {
                    "type": ["string", "null"],
                    "description": "The account or card number associated with the transaction, or null if not available."
                },
                "transaction_type": {
                    "type": "string",
                    "description": "The direction of money flow in the transaction.",
                    "enum": ["CREDIT", "DEBIT"]
                }
            },
            "required": [
                "datetime",
                "category",
                "amount",
                "account_number",
                "description",
                "transaction_type"
            ],
            "additionalProperties": False,
        }

        # Call the OpenAI API with the current schema-based approach
        response = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "transaction",
                    "schema": transaction_schema,
                    "strict": True,
                }
            },
            temperature=0.0,
        )

        content = response.output_text
        logger.info(f"OpenAI returned: {content}")
        return json.loads(content)

    except Exception as e:
        logger.error("OpenAI API failed", exc_info=True)
        raise e


def insert_into_postgres(host, dbname, user, password, transaction_data, raw_data):
    """
    Insert parsed transaction into PostgreSQL
    """
    conn = None
    try:
        logger.info(f"Connecting to PostgreSQL at {host}")
        conn = psycopg2.connect(host=host, dbname=dbname, user=user, password=password)
        cur = conn.cursor()
        insert_query = """
        INSERT INTO public.transaction (
            datetime, 
            category, 
            amount, 
            description, 
            account_number, 
            transaction_type, 
            raw_data
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        values = (
            transaction_data.get("datetime"),
            transaction_data.get("category"),
            transaction_data.get("amount"),
            transaction_data.get("description"),
            transaction_data.get("account_number"),
            transaction_data.get("transaction_type"),
            raw_data,
        )
        cur.execute(insert_query, values)
        conn.commit()
        logger.info("Transaction inserted successfully")
        cur.close()
    except Exception as e:
        logger.error("Database insert failed", exc_info=True)
        raise e
    finally:
        if conn:
            conn.close()
            logger.info("Database connection closed")


def lambda_handler(event, context):
    try:
        logger.info(f"Received event: {event}")
        fallback_timestamp = datetime.now(lk_timezone).isoformat()

        # Parse event body
        body = json.loads(event.get("body", "{}"))
        sms_text = body.get("body", "")

        # Get DB credentials
        db_credentials = get_secret_credentials("prod/pgsql/credentials")
        logger.info("Successfully fetched DB credentials")

        # Get OpenAI API key from credentials
        openai_credentials = get_secret_credentials("prod/openai/api_credentials")
        OPENAI_API_KEY = openai_credentials.get("OPENAI_API_KEY")
        logger.info("Successfully fetched OPENAI credentials")

        # Parse transaction with OpenAI
        transaction_data = parse_transaction(
            sms_text, fallback_timestamp, OPENAI_API_KEY
        )

        # Insert into DB
        insert_into_postgres(
            host=db_credentials["DB_HOST"],
            dbname=db_credentials["DB_NAME"],
            user=db_credentials["DB_USER"],
            password=db_credentials["DB_PASSWORD"],
            transaction_data=transaction_data,
            raw_data=json.dumps(body),
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {
                    "message": "Transaction successfully recorded",
                    "transaction_type": transaction_data.get("transaction_type"),
                    "amount": transaction_data.get("amount"),
                    "timestamp": transaction_data.get("datetime"),
                }
            ),
        }

    except Exception as e:
        logger.error("Error in Lambda execution", exc_info=True)
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(
                {"error": "Failed to process transaction", "details": str(e)}
            ),
        }
