output "mission_profile_arn" {
  description = "ARN of the Ground Station mission profile"
  value       = var.ground_station_enabled ? module.mission_profile[0].mission_profile_arn : null
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

output "aggregation_instance_id" {
  description = "ID of the EC2 aggregation instance (when SDR pipeline is enabled)"
  value       = var.enable_sdr_pipeline ? module.sdr_pipeline[0].aggregation_instance_id : null
}

output "aggregation_trigger_lambda_arn" {
  description = "ARN of the aggregation Trigger Lambda function (when SDR pipeline is enabled)"
  value       = var.enable_sdr_pipeline ? module.sdr_pipeline[0].aggregation_trigger_lambda_arn : null
}

output "aggregation_trigger_lambda_function_name" {
  description = "Function name of the aggregation Trigger Lambda (when SDR pipeline is enabled)"
  value       = var.enable_sdr_pipeline ? module.sdr_pipeline[0].aggregation_trigger_lambda_function_name : null
}
