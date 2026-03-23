import os
import json
import boto3
import logging
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2_client = boto3.client('ec2')
cloudwatch_client = boto3.client('cloudwatch')
secrets_client = boto3.client('secretsmanager')
sqs_client = boto3.client('sqs')

def get_secret(secret_id):
    try:
        response = secrets_client.get_secret_value(SecretId=secret_id)
        return json.loads(response['SecretString'])
    except Exception as e:
        logger.error(f"Failed to retrieve secret {secret_id}: {e}")
        return {}

def find_unattached_ebs_volumes():
    volumes = ec2_client.describe_volumes(Filters=[{'Name': 'status', 'Values': ['available']}])
    return [v['VolumeId'] for v in volumes.get('Volumes', [])]

def find_idle_ec2_instances():
    idle_instances = []
    instances = ec2_client.describe_instances(Filters=[{'Name': 'instance-state-name', 'Values': ['running']}])
    for reservation in instances.get('Reservations', []):
        for instance in reservation.get('Instances', []):
            instance_id = instance['InstanceId']
            # Get CPU utilization for the past week
            metrics = cloudwatch_client.get_metric_statistics(
                Namespace='AWS/EC2',
                MetricName='CPUUtilization',
                Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
                StartTime=datetime.now(timezone.utc) - timedelta(days=7),
                EndTime=datetime.now(timezone.utc),
                Period=86400,
                Statistics=['Average']
            )
            datapoints = metrics.get('Datapoints', [])
            # If all data points represent < 1% CPU usage
            if datapoints and all(dp['Average'] < 1.0 for dp in datapoints):
                idle_instances.append(instance_id)
    return idle_instances

def find_old_snapshots():
    old_snapshots = []
    paginator = ec2_client.get_paginator('describe_snapshots')
    # Use OwnerIds=['self'] to prevent scanning public snapshots
    for page in paginator.paginate(OwnerIds=['self']):
        for snap in page.get('Snapshots', []):
            if snap['StartTime'] < datetime.now(timezone.utc) - timedelta(days=90):
                old_snapshots.append(snap['SnapshotId'])
    return old_snapshots

def find_untagged_resources():
    untagged = []
    # Scanning only EC2 for simplicity depending on requirement
    instances = ec2_client.describe_instances()
    for reservation in instances.get('Reservations', []):
        for instance in reservation.get('Instances', []):
            if not instance.get('Tags'):
                untagged.append(instance['InstanceId'])
    return untagged

def enqueue_items(batch_id, resource_type, resource_ids):
    queue_url = os.environ.get('SQS_QUEUE_URL')
    if not queue_url or not resource_ids:
        return
    # Note: For scale > 10,000, we should use sqs_client.send_message_batch (10 at a time)
    # Shown individually for clarity.
    for res_id in resource_ids:
        message_body = {
            "batch_id": batch_id,
            "resource_type": resource_type,
            "resource_id": res_id
        }
        sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(message_body)
        )

def send_slack_notification(webhook_url, summary, batch_id):
    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🧹 HygieneBot: Action Required"}
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Found Zombies:*\n"
                            f"• {summary['ebs']} Unattached EBS Volumes\n"
                            f"• {summary['ec2']} Idle EC2 Instances (0% CPU)\n"
                            f"• {summary['snapshots']} Old Snapshots (> 90 days)\n"
                            f"• {summary['untagged']} Untagged Resources\n\n"
                            f"Batch ID: `{batch_id}`"
                }
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve Cleanup"},
                        "style": "danger",
                        "value": json.dumps({"batch_id": batch_id, "action": "approve"}),
                        "action_id": "approve_cleanup"
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "value": json.dumps({"batch_id": batch_id, "action": "deny"}),
                        "action_id": "deny_cleanup"
                    }
                ]
            }
        ]
    }
    req = urllib.request.Request(webhook_url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
    urllib.request.urlopen(req)

def lambda_handler(event, context):
    logger.info("Starting Scanner execution")
    batch_id = str(uuid.uuid4())
    
    ebs = find_unattached_ebs_volumes()
    ec2 = find_idle_ec2_instances()
    snapshots = find_old_snapshots()
    untagged = find_untagged_resources()
    
    # Push to SQS for Deleter Lambda processing (Scalability)
    enqueue_items(batch_id, "EBS", ebs)
    enqueue_items(batch_id, "EC2", ec2)
    enqueue_items(batch_id, "SNAPSHOT", snapshots)
    enqueue_items(batch_id, "UNTAGGED", untagged)
    
    summary = {
        "ebs": len(ebs),
        "ec2": len(ec2),
        "snapshots": len(snapshots),
        "untagged": len(untagged)
    }
    
    total = sum(summary.values())
    if total > 0:
        slack_secret = get_secret(os.environ.get('SLACK_SECRET_ID', 'hygienebot/slack'))
        webhook_url = slack_secret.get('webhook_url')
        if webhook_url:
            send_slack_notification(webhook_url, summary, batch_id)
            logger.info("Slack notification sent successfully.")
        else:
            logger.error("No Slack webhook URL configured.")
            
    return {
        'statusCode': 200,
        'body': json.dumps({'message': f'Scan complete. {total} resources found.'})
    }
