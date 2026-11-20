variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
}

variable "environment" {
  description = "Deployment environment name"
  type        = string
}

variable "account_id" {
  description = "AWS account ID"
  type        = string
}

variable "satellite_norad_id" {
  description = "NORAD catalog ID for the target satellite (NOAA-20 = 43013)"
  type        = number
  default     = 43013
}

variable "satellite_id" {
  description = "AWS Ground Station satellite UUID (from aws groundstation list-satellites)"
  type        = string
}

variable "satellite_onboarded" {
  description = "Confirms the satellite has been onboarded into this AWS account (see modules/mission_profile)"
  type        = bool
  default     = false
}

variable "groundstation_role_arn" {
  description = "ARN of the IAM role for Ground Station service data delivery (from modules/security)"
  type        = string
}

variable "kms_key_arn" {
  description = "ARN of the shared KMS key (from modules/security)"
  type        = string
}

variable "kms_key_id" {
  description = "ID of the shared KMS key (from modules/security)"
  type        = string
}

variable "sns_topic_arn" {
  description = "ARN of the shared SNS topic for failure notifications (from modules/security)"
  type        = string
}

# ---------------------------------------------------------------------------
# Demod/Decode antenna configuration
# ---------------------------------------------------------------------------
# Defaults match modules/mission_profile's existing DigIF antenna_downlink_config
# (7812 MHz / 30 MHz / RHCP) and the validated JPSS-1 QPSK demod/decode JSON
# published in aws-samples/aws-groundstation-s3-data-delivery
# (cfn/jpss1-gs-to-s3.yml). See infra/modules/sync_pipeline/groundstation.tf for
# how these feed the DemodulationConfig / DecodeConfig unvalidatedJSON.

variable "center_frequency_mhz" {
  description = "Downlink center frequency in MHz"
  type        = number
  default     = 7812
}

variable "bandwidth_mhz" {
  description = "Downlink bandwidth in MHz"
  type        = number
  default     = 30
}

variable "symbol_rate_msps" {
  description = "QPSK symbol rate in Msps"
  type        = number
  default     = 15
}

# ---------------------------------------------------------------------------
# Receiver EC2 / networking
# ---------------------------------------------------------------------------

variable "receiver_udp_port" {
  description = "UDP port the receiver listens on for the AWS Ground Station dataflow endpoint"
  type        = number
  default     = 55888
}

variable "receiver_instance_type" {
  description = "EC2 instance type for the synchronous receiver"
  type        = string
  default     = "c6i.xlarge"
}

variable "receiver_root_volume_gb" {
  description = "Root EBS volume size in GB for the receiver instance"
  type        = number
  default     = 100
}

variable "contact_min_duration_seconds" {
  description = "Minimum viable contact duration in seconds"
  type        = number
  default     = 300
}

variable "contact_pre_pass_duration_seconds" {
  description = <<-EOT
    Lead time before AOS, in seconds. Longer than the existing async profile's
    120s -- the receiver EC2 must be running AND its UDP listener armed before
    Ground Station starts streaming, with no S3 buffer to fall back on if it
    isn't ready in time.

    600s covers the receiver_arm Lambda's worst-case budget: up to 240s
    waiting for EC2 to reach running state, plus up to 120s waiting for the
    SSM agent to register Online (see lambdas/receiver_arm/handler.py), plus
    margin for the SSM send_command round trip and clock skew before AOS.
  EOT
  type        = number
  default     = 600
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}

# -- Shared Batch aggregation chain -------------------------------------------
# Architecture principle: sync and async converge at the first common artifact
# (the frame capture in S3) and share everything after it. The aggregation is
# therefore ALWAYS the shared Batch job (modules/batch_compute); there is no
# CodeBuild fallback. use_batch_aggregation is kept as an explicit guard so a
# misconfigured root module fails at plan time with a clear message instead of
# silently producing a state machine with empty queue/job-definition ARNs.

variable "use_batch_aggregation" {
  description = "Must be true: the sync pipeline's aggregation is the shared Batch job (no CodeBuild fallback). The root module wires this from enable_batch_compute, so enable_sync_pipeline requires enable_batch_compute = true."
  type        = bool

  validation {
    condition     = var.use_batch_aggregation
    error_message = "The sync pipeline requires the shared Batch aggregation chain: set enable_batch_compute = true when enable_sync_pipeline = true (the sync CodeBuild fallback was retired)."
  }
}

variable "batch_aggregation_job_queue_arn" {
  description = "ARN of the shared Batch aggregation job queue (groundstation-noaa20-agg-queue). Required."
  type        = string

  validation {
    condition     = length(var.batch_aggregation_job_queue_arn) > 0
    error_message = "batch_aggregation_job_queue_arn must be set: the sync pipeline requires the shared Batch aggregation chain (enable_batch_compute = true)."
  }
}

variable "batch_aggregation_job_definition_arn" {
  description = "ARN of the shared Batch aggregation job definition (groundstation-noaa20-aggregation, unversioned). Required."
  type        = string

  validation {
    condition     = length(var.batch_aggregation_job_definition_arn) > 0
    error_message = "batch_aggregation_job_definition_arn must be set: the sync pipeline requires the shared Batch aggregation chain (enable_batch_compute = true)."
  }
}

variable "assets_bucket_name" {
  description = "Name of the SDR output bucket, which holds software/ (CSPP J01 LUT tarball) and lut-cache/. Passed to the shared aggregation job as ASSETS_BUCKET; the sync bucket has neither prefix."
  type        = string
}