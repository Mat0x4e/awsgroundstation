variable "region" {
  description = "AWS region for Ground Station deployment"
  type        = string

  validation {
    condition = contains([
      "us-east-2",
      "us-west-2",
      "eu-north-1",
      "me-south-1",
      "ap-southeast-2",
      "af-south-1",
      "eu-west-1",
      "eu-central-1",
      "sa-east-1",
      "us-east-1"
    ], var.region)
    error_message = "Region must be a supported AWS Ground Station region."
  }
}

variable "environment" {
  description = "Deployment environment name"
  type        = string
  default     = "demo"
}

variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
  default     = "groundstation-noaa20"
}

variable "ground_station_enabled" {
  description = "Whether to create Ground Station resources (mission profile, tracking config). Set to true once Ground Station access is approved."
  type        = bool
  default     = false
}

variable "satellite_onboarded" {
  description = <<-EOT
    Confirms that the target satellite (NORAD ID 43013 — NOAA-20) has been onboarded
    into this AWS account via the Ground Station console or support request.
    Must be explicitly set to true alongside ground_station_enabled = true.
    Onboarding is a manual prerequisite: https://docs.aws.amazon.com/ground-station/latest/ug/getting-started.html
  EOT
  type        = bool
  default     = false
}

variable "enable_processing_pipeline" {
  description = "Whether to create the processing pipeline resources"
  type        = bool
  default     = false
}

variable "enable_sdr_pipeline" {
  description = "Whether to create the SDR pipeline resources (NOAA-20 CADU-to-TIFF)"
  type        = bool
  default     = false
}

variable "enable_sync_pipeline" {
  description = <<-EOT
    Whether to create the synchronous reception pipeline resources (dedicated
    demod/decode mission profile, receiver EC2, and post-contact processing).
    Fully separate from enable_sdr_pipeline's async DigIF pipeline -- both can
    be enabled simultaneously against the same satellite, each with its own
    mission profile. Requires ground_station_enabled = true and
    enable_sdr_pipeline = true (the sync pipeline reuses the SDR pipeline's
    ECR image for its CSPP-only CodeBuild step).
  EOT
  type        = bool
  default     = false
}

variable "satellite_norad_id" {
  description = "NORAD catalog ID for the target satellite"
  type        = number
  default     = 43013
}

variable "satellite_id" {
  description = "AWS Ground Station satellite UUID (from aws groundstation list-satellites). Required when ground_station_enabled = true."
  type        = string
  default     = ""
}

variable "objective" {
  description = "Project objective tag"
  type        = string
  default     = "demonstrator"
}

variable "owner" {
  description = "Project owner (must be an @soprasteria.com email per org tag policy)"
  type        = string
  default     = "mathieu.bonnet@soprasteria.com"
}

variable "creation_date" {
  description = "Project creation date (ISO format)"
  type        = string
  default     = "2025-05-22"
}

variable "enable_compute_bakeoff" {
  description = <<-EOT
    Set to true to create the disposable AWS Batch-on-EC2-Spot chunk fan-out bakeoff module.
    EXPERIMENT ONLY — defaults to false so it is never created by a normal apply.
    Requires enable_sdr_pipeline = true: the module reuses the SDR ECR image and output bucket.
    Tear down with: terraform destroy -target module.compute_bakeoff[0]
    See .kiro/specs/compute-bakeoff-batch-spot/ for cost and measurement notes.
  EOT
  type        = bool
  default     = false
}

variable "enable_batch_compute" {
  description = <<-EOT
    Set to true to create the production AWS Batch-on-EC2-Spot fan-out module
    (clean-compute-pipeline Stage 1). This module stands up ALONGSIDE the existing
    CodeBuild fan-out during the strangler-fig cutover and is the long-lived
    replacement, not a disposable experiment.
    Requires enable_sdr_pipeline = true: the module reuses the SDR ECR image and
    output bucket. Defaults to false so a normal apply does not create it; the
    operator flips this flag when ready to begin the Stage-1 cutover.
    See .kiro/specs/clean-compute-pipeline/ for cutover sequence and rollback notes.
  EOT
  type        = bool
  default     = false
}

variable "aws_profile" {
  description = "AWS CLI profile to use for authentication"
  type        = string
  default     = null
}

variable "tags" {
  description = "Common tags applied to all resources"
  type        = map(string)
  default     = {}
}
