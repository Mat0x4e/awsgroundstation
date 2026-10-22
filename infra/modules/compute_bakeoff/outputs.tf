# outputs.tf — values needed by the bakeoff harness to submit and measure jobs.

output "job_queue_name" {
  description = "Name of the Batch job queue — pass to the harness submit command"
  value       = aws_batch_job_queue.bakeoff.name
}

output "job_queue_arn" {
  description = "ARN of the Batch job queue"
  value       = aws_batch_job_queue.bakeoff.arn
}

output "job_definition_name" {
  description = "Name of the Batch job definition (unqualified — always latest revision)"
  value       = aws_batch_job_definition.bakeoff_chunk.name
}

output "job_definition_arn" {
  description = "ARN of the Batch job definition (versioned ARN; use the name for latest)"
  value       = aws_batch_job_definition.bakeoff_chunk.arn
}

output "compute_environment_arn" {
  description = "ARN of the Batch compute environment"
  value       = aws_batch_compute_environment.bakeoff.arn
}

output "log_group_name" {
  description = "CloudWatch log group name for Batch job logs — used by the harness to tail logs and for Cost Explorer tag filtering"
  value       = aws_cloudwatch_log_group.bakeoff.name
}
