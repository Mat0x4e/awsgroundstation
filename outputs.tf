output "mission_profile_arn" {
  description = "ARN of the Ground Station mission profile"
  value       = module.mission_profile.mission_profile_arn
}

output "reception_bucket_name" {
  description = "Name of the S3 bucket for satellite data reception"
  value       = module.s3_delivery.bucket_name
}

output "reception_bucket_arn" {
  description = "ARN of the S3 bucket for satellite data reception"
  value       = module.s3_delivery.bucket_arn
}

output "sns_topic_arn" {
  description = "ARN of the SNS topic for contact notifications"
  value       = module.security.sns_topic_arn
}

output "kms_key_arn" {
  description = "ARN of the KMS key used for encryption"
  value       = module.security.kms_key_arn
}

output "scheduler_lambda_arn" {
  description = "ARN of the contact scheduler Lambda function"
  value       = module.contact_scheduler.lambda_function_arn
}

output "eventbridge_rule_arn" {
  description = "ARN of the EventBridge scheduling rule"
  value       = module.contact_scheduler.eventbridge_rule_arn
}

output "dashboard_url" {
  description = "URL of the CloudWatch dashboard"
  value       = module.observability.dashboard_url
}

output "output_bucket_name" {
  description = "Name of the processed data output bucket (when processing pipeline is enabled)"
  value       = var.enable_processing_pipeline ? module.processing_pipeline[0].output_bucket_name : null
}

output "processing_lambda_arn" {
  description = "ARN of the data processor Lambda function (when processing pipeline is enabled)"
  value       = var.enable_processing_pipeline ? module.processing_pipeline[0].lambda_function_arn : null
}
