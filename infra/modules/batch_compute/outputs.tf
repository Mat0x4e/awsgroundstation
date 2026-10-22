# outputs.tf - values consumed by the SFN wiring (task 3.1) and root module (task 1.3).
# On-demand CE and aggregation/render job definition outputs will be added in tasks 5/10.

output "fanout_job_queue_name" {
  description = "Name of the Spot fan-out Batch job queue - passed to the SFN SubmitBatchChunk state"
  value       = aws_batch_job_queue.fanout.name
}

output "fanout_job_queue_arn" {
  description = "ARN of the Spot fan-out Batch job queue"
  value       = aws_batch_job_queue.fanout.arn
}

output "chunk_job_definition_name" {
  description = "Name of the chunk Batch job definition (unqualified - always resolves to the latest revision)"
  value       = aws_batch_job_definition.chunk.name
}

output "chunk_job_definition_arn" {
  description = "ARN of the chunk Batch job definition (versioned ARN; use the name to reference latest)"
  value       = aws_batch_job_definition.chunk.arn
}

output "fanout_log_group_name" {
  description = "CloudWatch log group for fan-out chunk job execution logs"
  value       = aws_cloudwatch_log_group.fanout.name
}

###############################################################################
# Stage 2 - Aggregation on-demand outputs
###############################################################################

output "aggregation_job_queue_arn" {
  description = "ARN of the on-demand aggregation Batch job queue"
  value       = aws_batch_job_queue.aggregation.arn
}

output "aggregation_job_queue_name" {
  description = "Name of the on-demand aggregation Batch job queue - passed to the SFN aggregation submit states"
  value       = aws_batch_job_queue.aggregation.name
}

output "aggregation_job_definition_arn" {
  description = "ARN of the aggregation Batch job definition (versioned ARN; use the name to reference latest)"
  value       = aws_batch_job_definition.aggregation.arn
}

output "aggregation_job_definition_name" {
  description = "Name of the aggregation Batch job definition (unqualified - always resolves to the latest revision)"
  value       = aws_batch_job_definition.aggregation.name
}

output "aggregation_log_group_name" {
  description = "CloudWatch log group for aggregation job execution logs"
  value       = aws_cloudwatch_log_group.aggregation.name
}
