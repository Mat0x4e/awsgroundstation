output "ecr_repository_url" {
  description = "URL of the ECR repository for the SDR pipeline container image"
  value       = aws_ecr_repository.sdr_pipeline.repository_url
}

output "output_bucket_name" {
  description = "Name of the SDR pipeline output S3 bucket"
  value       = aws_s3_bucket.sdr_output.id
}

output "output_bucket_arn" {
  description = "ARN of the SDR pipeline output S3 bucket"
  value       = aws_s3_bucket.sdr_output.arn
}

# TODO: uncomment when aws_codebuild_project.chunk_processor is created in task 13.3
# output "codebuild_project_name" {
#   description = "Name of the CodeBuild project for chunk processing"
#   value       = aws_codebuild_project.chunk_processor.name
# }

output "state_machine_arn" {
  description = "ARN of the SDR pipeline Step Functions state machine"
  value       = aws_sfn_state_machine.sdr_pipeline.arn
}

output "aggregation_instance_id" {
  description = "ID of the EC2 aggregation instance"
  value       = aws_instance.aggregation.id
}

output "aggregation_trigger_lambda_arn" {
  description = "ARN of the aggregation Trigger Lambda function"
  value       = aws_lambda_function.aggregation_trigger.arn
}

output "aggregation_trigger_lambda_function_name" {
  description = "Function name of the aggregation Trigger Lambda"
  value       = aws_lambda_function.aggregation_trigger.function_name
}
