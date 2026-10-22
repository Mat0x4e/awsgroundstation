variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
}

variable "account_id" {
  description = "AWS account ID"
  type        = string
}

variable "input_bucket_name" {
  description = "Name of the Ground Station reception bucket"
  type        = string
}

variable "kms_key_arn" {
  description = "ARN of the KMS key for encryption"
  type        = string
}

variable "sns_topic_arn" {
  description = "ARN of the SNS topic for failure notifications"
  type        = string
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}

variable "satellite_id" {
  description = "NOAA-20 satellite UUID, used to build the S3 key prefix when listing a contact's .pcap chunks"
  type        = string
}

# -- clean-compute-pipeline Stage 1 strangler variables -----------------------
# These three variables wire the Step Functions Map to AWS Batch without
# creating a direct module-to-module dependency (batch_compute already depends
# on sdr_pipeline for the ECR image; a back-reference would cycle).
# The root module passes the values from module.batch_compute[0] outputs.

variable "use_batch_fanout" {
  description = "When true the Map routes each chunk to AWS Batch instead of CodeBuild (clean-compute-pipeline Stage 1 strangler cutover); default false keeps the CodeBuild path"
  type        = bool
  default     = false
}

variable "batch_fanout_job_queue_arn" {
  description = "ARN of the Batch fan-out job queue (from module.batch_compute[0].fanout_job_queue_arn). Leave empty string when use_batch_fanout is false."
  type        = string
  default     = ""
}

variable "batch_chunk_job_definition_arn" {
  description = "ARN of the Batch chunk job definition (from module.batch_compute[0].chunk_job_definition_arn). Leave empty string when use_batch_fanout is false."
  type        = string
  default     = ""
}

# -- clean-compute-pipeline Stage 2 strangler variables ----------------------
# These three variables wire the SFN aggregation step to the Batch on-demand
# aggregation job without creating a new module dependency. The root module
# passes deterministic ARNs (same pattern as the Stage 1 fan-out locals).

variable "use_batch_aggregation" {
  description = "When true the aggregation step routes to Batch (RTSTPS=on) instead of CodeBuild (clean-compute-pipeline Stage 2 strangler cutover); default false keeps the CodeBuild poll-loop path"
  type        = bool
  default     = false
}

variable "batch_aggregation_job_queue_arn" {
  description = "ARN of the Batch aggregation job queue (groundstation-noaa20-agg-queue). Leave empty string when use_batch_aggregation is false."
  type        = string
  default     = ""
}

variable "batch_aggregation_job_definition_arn" {
  description = "ARN of the Batch aggregation job definition (groundstation-noaa20-aggregation, unversioned). Leave empty string when use_batch_aggregation is false."
  type        = string
  default     = ""
}
