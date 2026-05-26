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

variable "satellite_norad_id" {
  description = "NORAD catalog ID for the target satellite"
  type        = number
  default     = 43013
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
