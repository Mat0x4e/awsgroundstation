output "kms_key_arn" {
  description = "ARN of the KMS encryption key"
  value       = aws_kms_key.groundstation.arn
}

output "kms_key_id" {
  description = "ID of the KMS encryption key"
  value       = aws_kms_key.groundstation.key_id
}

output "groundstation_role_arn" {
  description = "ARN of the IAM role for Ground Station service"
  value       = aws_iam_role.groundstation.arn
}

output "scheduler_role_arn" {
  description = "ARN of the IAM role for Contact Scheduler Lambda"
  value       = aws_iam_role.scheduler_lambda.arn
}

output "processor_role_arn" {
  description = "ARN of the IAM role for Processing Lambda"
  value       = var.enable_processing_pipeline ? aws_iam_role.processor_lambda[0].arn : ""
}

output "cloudtrail_arn" {
  description = "ARN of the CloudTrail trail"
  value       = aws_cloudtrail.groundstation.arn
}

output "sns_topic_arn" {
  description = "ARN of the SNS topic for contact notifications"
  value       = aws_sns_topic.contact_notifications.arn
}
