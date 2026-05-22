output "output_bucket_name" {
  description = "Name of the processed data output bucket"
  value       = aws_s3_bucket.output.id
}

output "output_bucket_arn" {
  description = "ARN of the processed data output bucket"
  value       = aws_s3_bucket.output.arn
}

output "sqs_queue_arn" {
  description = "ARN of the processing SQS queue"
  value       = aws_sqs_queue.processing.arn
}

output "dlq_arn" {
  description = "ARN of the dead letter queue"
  value       = aws_sqs_queue.dlq.arn
}

output "lambda_function_arn" {
  description = "ARN of the data processor Lambda function"
  value       = aws_lambda_function.data_processor.arn
}

output "sns_failure_topic_arn" {
  description = "ARN of the SNS topic for processing failure notifications"
  value       = aws_sns_topic.processing_failures.arn
}
