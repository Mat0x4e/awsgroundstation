output "dashboard_arn" {
  description = "ARN of the CloudWatch dashboard"
  value       = aws_cloudwatch_dashboard.groundstation.dashboard_arn
}

output "dashboard_url" {
  description = "URL of the CloudWatch dashboard"
  value       = "https://${data.aws_region.current.name}.console.aws.amazon.com/cloudwatch/home?region=${data.aws_region.current.name}#dashboards/dashboard/${aws_cloudwatch_dashboard.groundstation.dashboard_name}"
}

output "sns_alerts_topic_arn" {
  description = "ARN of the SNS topic for contact failure alerts"
  value       = aws_sns_topic.contact_failures.arn
}

output "contact_failure_alarm_arn" {
  description = "ARN of the CloudWatch alarm for failed contacts"
  value       = aws_cloudwatch_metric_alarm.contact_failed.arn
}
