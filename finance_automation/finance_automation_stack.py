from aws_cdk import (
    Duration,
    Stack,
    aws_lambda as _lambda,
    aws_apigateway as api_gateway,
    aws_iam as iam,
    RemovalPolicy,
    aws_s3 as s3,
    aws_lambda_event_sources as lambda_event_sources,
    aws_ec2 as ec2
)
from cdk_klayers import Klayers

from constructs import Construct

class SmsStorageStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        runtime = _lambda.Runtime.PYTHON_3_12

        # Initialize Klayers 
        klayers = Klayers(
            self,
            python_version = runtime,
            region = "ap-southeast-1"
        )
    
        # get the latest layer version for the requests package
        psycopg2_layer = klayers.layer_version(self, "psycopg2-binary")

        openai_layer = _lambda.LayerVersion.from_layer_version_arn(
                    self, "OpenAILayer", 
                    layer_version_arn="arn:aws:lambda:ap-southeast-1:339712959705:layer:openai_layer:1" 
        )
        
        # Reference the existing bucket instead of creating a new one
        sms_bucket = s3.Bucket.from_bucket_name(
            self, 
            id="sms_storage_bucket",
            bucket_name="sms-storage-data"
        )
        
        # Custom role for Lambda
        sms_lambda_role = iam.Role(
            self,
            "sms_lambda_role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name('service-role/AWSLambdaBasicExecutionRole'),
                iam.ManagedPolicy.from_aws_managed_policy_name('service-role/AWSLambdaVPCAccessExecutionRole')
            ]
        )
        
        # Add specific permissions for S3 access
        sms_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=['s3:GetObject', 's3:PutObject', 's3:ListBucket'],
                resources=[
                    f"{sms_bucket.bucket_arn}",
                    f"{sms_bucket.bucket_arn}/*"
                ]
            )
        )
        
        sms_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ec2:CreateNetworkInterface",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DeleteNetworkInterface",
                ],
                resources=["*"],
            )
        )

        sms_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                ],
                resources=["arn:aws:secretsmanager:ap-southeast-1:339712959705:secret:prod/pgsql/credentials-5KdWfJ"],
            )
        )

        sms_lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "*",
                ],
                resources=["*"],
            )
        )

        # Lambda Function to process SMS and store in S3
        sms_processor_lambda = _lambda.Function(
            self,
            id="sms_processor_lambda",
            function_name="sms-processor",
            runtime=runtime,
            role=sms_lambda_role,
            handler="get_transactions.lambda_handler",
            code=_lambda.Code.from_asset('lambda'),
            timeout=Duration.minutes(3),
            layers=[psycopg2_layer, openai_layer],
            memory_size=128,
            description="Processes SMS messages and appends them to a JSONL file in S3",
        )

        # Lambda Function to process SMS and store in S3
        sms_processor_lambda = _lambda.Function(
            self,
            id="test_internet_lambda",
            function_name="test-internet",
            runtime=runtime,
            role=sms_lambda_role,
            handler="test_internet.lambda_handler",
            code=_lambda.Code.from_asset('lambda'),
            timeout=Duration.minutes(3),
            layers=[psycopg2_layer],
            memory_size=128,
            description="Testing internet",
        )
        
        # API Gateway to trigger the Lambda function
        api = api_gateway.RestApi(
            self,
            id="SmsProcessorApi",
            rest_api_name="SMS Processor API",
            description="API for receiving SMS JSON payloads and storing them in S3"
        )

        # Create an endpoint for the SMS processing
        sms_resource = api.root.add_resource("sms")
        integration = api_gateway.LambdaIntegration(
            sms_processor_lambda,
            proxy=True
        )
        sms_resource.add_method("POST", integration)  # Allow POST requests with SMS payload