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

output "dashboard_url" {
  description = "URL of the CloudWatch dashboard"
  value       = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards"
}

output "kms_key_arn" {
  description = "ARN of the KMS key used for encryption"
  value       = module.security.kms_key_arn
}

output "output_bucket_name" {
  description = "Name of the processed data output bucket (when processing pipeline is enabled)"
  value       = var.enable_processing_pipeline ? "" : null
}
