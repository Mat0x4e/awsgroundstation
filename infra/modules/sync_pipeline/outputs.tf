output "output_bucket_name" {
  description = "Name of the sync pipeline output S3 bucket"
  value       = aws_s3_bucket.sync_output.id
}

output "output_bucket_arn" {
  description = "ARN of the sync pipeline output S3 bucket"
  value       = aws_s3_bucket.sync_output.arn
}

output "mission_profile_arn" {
  description = "ARN of the synchronous NOAA-20 Ground Station mission profile"
  value       = awscc_groundstation_mission_profile.noaa20_sync.arn
}

output "dataflow_endpoint_group_arn" {
  description = "ARN of the receiver's Ground Station dataflow endpoint group"
  value       = awscc_groundstation_dataflow_endpoint_group.receiver.arn
}

output "receiver_instance_id" {
  description = "ID of the sync receiver EC2 instance"
  value       = aws_instance.receiver.id
}

output "state_machine_arn" {
  description = "ARN of the sync pipeline Step Functions state machine"
  value       = aws_sfn_state_machine.sync_pipeline.arn
}

output "receiver_arm_lambda_arn" {
  description = "ARN of the Receiver Arm Lambda function"
  value       = aws_lambda_function.receiver_arm.arn
}

output "receiver_arm_lambda_function_name" {
  description = "Function name of the Receiver Arm Lambda"
  value       = aws_lambda_function.receiver_arm.function_name
}

output "receiver_public_ip" {
  description = "Elastic IP on the sync receiver primary ENI (SSM and S3 egress)"
  value       = aws_eip.receiver.public_ip
}

output "network_canary_function_name" {
  description = "Function name of the temporary network canary Lambda (null when disabled)"
  value       = var.enable_network_canary ? aws_lambda_function.network_canary[0].function_name : null
}
