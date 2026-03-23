# HygieneBot Implementation Plan

## Overview
HygieneBot automates AWS resource cleanup by detecting unused ("Zombie") resources, requesting human approval via Slack, and proceeding with deletion once authorized.

## 1. Architecture Design
- **Scanner Lambda:** Triggered weekly by an EventBridge rule. Scans AWS environment for unattached EBS volumes, idle EC2 instances (0% CPU), untagged resources, and old snapshots. Findings are pushed to an SQS Queue, and a summarized notification is sent to Slack.
- **SQS Queue (Pending Deletions):** Holds the list of resources to be deleted to handle massive scales (e.g., 10,000+ snapshots) asynchronously.
- **Deleter Lambda:** Triggered via API Gateway when a Slack Quick Action button (Approve/Deny) is pressed. Processes SQS messages on approval, or clears them on denial.
- **API Gateway:** Provides a webhook endpoint for Slack Interactive Messages.
- **AWS Secrets Manager:** Securely stores Slack Webhook URLs and Signing Secrets.

## 2. Step-by-Step Implementation

### Phase 1: Preparation & IAM
*   **Create Secrets:** Add Slack Webhook URL and Slack Signing Secret to AWS Secrets Manager.
*   **Define IAM Roles:** Set up minimum viable permissions:
    *   *Scanner Role:* `ec2:Describe*`, `cloudwatch:GetMetricStatistics`, `sqs:SendMessage`, `secretsmanager:GetSecretValue`.
    *   *Deleter Role:* `ec2:DeleteVolume`, `ec2:TerminateInstances`, `ec2:DeleteSnapshot`, `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `secretsmanager:GetSecretValue`.

### Phase 2: Infrastructure as Code (Terraform)
*   **Provision AWS Resources:** Write `main.tf` to define Lambdas, SQS, API Gateway, IAM Roles, EventBridge schedules, and Secrets Manager references.
*   **Apply Terraform:** Deploy the stack to generate the API Gateway URL.

### Phase 3: Slack App Setup
*   **Create Slack App:** Go to api.slack.com -> Create App.
*   **Configure Webhooks:** Enable incoming webhooks and copy the URL to Secrets Manager.
*   **Enable Interactivity:** Add the API Gateway URL as the Slack Interactivity Request URL.

### Phase 4: Lambda Code Implementation
*   **Scanner Lambda:** Implement Boto3 logic to scan for zombie resources. Send individual items to SQS in background while triggering Slack webhook with summary metrics.
*   **Deleter Lambda:** Implement Slack signature verification manually using the stored Signing Secret. Drain SQS queue and execute Boto3 delete commands strictly based on the approved action.

### Phase 5: Testing
*   **Unit Tests:** Create mock AWS environments using `moto` to test detection logic.
*   **Dry-run:** Manually invoke Scanner Lambda. Verify SQS items and Slack message. Click "Approve" and verify API Gateway passes the payload accurately. 

## 3. Operations & Auditing
*   **CloudWatch Logs:** All Lambda invocations are logged for auditing (who pressed "Approve" via Slack ID).
*   **DLQ (Dead Letter Queue):** Attach a DLQ to the deletion SQS queue for items that fail to delete.
