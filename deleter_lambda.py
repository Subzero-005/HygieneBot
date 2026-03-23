import os
import json
import boto3
import hmac
import hashlib
import time
import urllib.parse
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secrets_client = boto3.client('secretsmanager')
sqs_client = boto3.client('sqs')
ec2_client = boto3.client('ec2')

def get_slack_secret():
    secret_id = os.environ.get('SLACK_SECRET_ID', 'hygienebot/slack')
    try:
        response = secrets_client.get_secret_value(SecretId=secret_id)
        return json.loads(response['SecretString']).get('signing_secret', '')
    except Exception as e:
        logger.error(f"Failed to fetch Slack secret: {e}")
        return ""

def verify_slack_signature(headers, body, signing_secret):
    timestamp = headers.get('x-slack-request-timestamp', '')
    signature = headers.get('x-slack-signature', '')
    
    if not timestamp or not signature:
        return False
        
    if abs(time.time() - int(timestamp)) > 300: # 5 minutes
        return False
        
    sig_basestring = f"v0:{timestamp}:{body}"
    my_sig = 'v0=' + hmac.new(
        signing_secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(my_sig, signature)

def process_sqs_cleanup(batch_id):
    queue_url = os.environ.get('SQS_QUEUE_URL')
    while True:
        response = sqs_client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=1  # Short poll for lambda processing
        )
        
        messages = response.get('Messages', [])
        if not messages:
            break
            
        for msg in messages:
            body = json.loads(msg['Body'])
            # Verify batch ID matches the approved alert before deleting
            if body.get('batch_id') == batch_id:
                try:
                    res_type = body.get('resource_type')
                    res_id = body.get('resource_id')
                    
                    if res_type == 'EBS':
                        ec2_client.delete_volume(VolumeId=res_id)
                    elif res_type == 'EC2':
                        ec2_client.terminate_instances(InstanceIds=[res_id])
                    elif res_type == 'SNAPSHOT':
                        ec2_client.delete_snapshot(SnapshotId=res_id)
                    elif res_type == 'UNTAGGED':
                        ec2_client.terminate_instances(InstanceIds=[res_id])
                        
                    # Delete the message after acting on it
                    sqs_client.delete_message(
                        QueueUrl=queue_url,
                        ReceiptHandle=msg['ReceiptHandle']
                    )
                except Exception as e:
                    logger.error(f"Failed to delete {body}: {str(e)}")
                    # Move to DLQ or dead-letter handling in production

def clear_sqs(batch_id):
    """If user denies, we clear the SQS queue of items from that batch."""
    queue_url = os.environ.get('SQS_QUEUE_URL')
    # Similarly pop from SQS but DO NOT delete AWS resources
    # (Implementation omitted for brevity, would similarly consume messages)
    pass

def lambda_handler(event, context):
    headers = {k.lower(): v for k, v in event.get('headers', {}).items()}
    body = event.get('body', '')
    
    if event.get('isBase64Encoded'):
        import base64
        body = base64.b64decode(body).decode('utf-8')
        
    slack_signing_secret = get_slack_secret()
    if not verify_slack_signature(headers, body, slack_signing_secret):
        return {"statusCode": 401, "body": "Unauthorized - Signature Verification Failed"}

    parsed_body = urllib.parse.parse_qs(body)
    payload = json.loads(parsed_body.get('payload', [''])[0])
    
    action_info = payload['actions'][0]
    action_value = json.loads(action_info['value']) # Contains batch_id and action
    
    batch_id = action_value['batch_id']
    user_action = action_value['action']
    user_id = payload['user']['id']
    
    logger.info(f"User {user_id} trigged action {user_action} for batch {batch_id}")
    
    if user_action == 'approve':
        process_sqs_cleanup(batch_id)
        msg = "Cleanup Approved and Execution Started. 🚀"
    else:
        clear_sqs(batch_id)
        msg = "Cleanup Denied. Safely ignored. ❌"

    return {
        "statusCode": 200,
        "body": msg
    }
