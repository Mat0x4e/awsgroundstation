# outputs.tf — Module outputs

output "lambda_function_arn" {
  description = "ARN of the VIIRS visualization orchestrator Lambda function"
  value       = aws_lambda_function.viirs_orchestrator.arn
}

output "lambda_function_name" {
  description = "Name of the VIIRS visualization orchestrator Lambda function"
  value       = aws_lambda_function.viirs_orchestrator.function_name
}

output "codebuild_project_name" {
  description = "Name of the CodeBuild project that renders VIIRS visualizations"
  value       = aws_codebuild_project.viirs_visualization.name
}

output "ecr_repository_url" {
  description = "URL of the ECR repository hosting the visualization Docker image"
  value       = aws_ecr_repository.visualization.repository_url
}
