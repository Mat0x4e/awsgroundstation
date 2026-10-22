output "lambda_function_arn" {
  description = "ARN of the contact scheduler Lambda function"
  value       = aws_lambda_function.contact_scheduler.arn
}

output "lambda_function_name" {
  description = "Name of the contact scheduler Lambda function"
  value       = aws_lambda_function.contact_scheduler.function_name
}

output "eventbridge_rule_arn" {
  description = "ARN of the EventBridge scheduling rule"
  value       = aws_cloudwatch_event_rule.scheduler_cron.arn
}

output "log_group_name" {
  description = "Name of the CloudWatch Log Group for the scheduler"
  value       = aws_cloudwatch_log_group.contact_scheduler.name
}
