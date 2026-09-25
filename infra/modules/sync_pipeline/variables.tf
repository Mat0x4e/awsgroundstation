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

variable "sdr_pipeline_ecr_repository_url" {
  description = <<-EOT
    URL of the existing sdr_pipeline ECR repository (modules/sdr_pipeline output
    ecr_repository_url). Reused rather than building a second image: it already
    carries CSPP 4.1.1, the J01 straylight LUTs, and RT-STPS -- everything the
    CSPP-only aggregation step here needs, and nothing about CSPP depends on
    which upstream pipeline produced its RDR input.
  EOT
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
