variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
}

variable "environment" {
  description = "Deployment environment name"
  type        = string
}

variable "satellite_norad_id" {
  description = "NORAD catalog ID for the target satellite (NOAA-20 = 43013)"
  type        = number
  default     = 43013
}

variable "reception_bucket_arn" {
  description = "ARN of the S3 bucket for data reception"
  type        = string
}

variable "groundstation_role_arn" {
  description = "ARN of the IAM role for Ground Station service data delivery"
  type        = string
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}

variable "minimum_elevation_degrees" {
  description = "Minimum elevation angle in degrees for satellite contact"
  type        = number
  default     = 10
}

variable "contact_min_duration_seconds" {
  description = "Minimum viable contact duration in seconds"
  type        = number
  default     = 300
}

variable "satellite_onboarded" {
  description = <<-EOT
    Confirms that the target satellite (NORAD ID specified by satellite_norad_id) has been
    onboarded into this AWS account. This is a manual prerequisite — Ground Station resources
    cannot be scheduled for a satellite that is not registered in the account.
    See: https://docs.aws.amazon.com/ground-station/latest/ug/getting-started.html
  EOT
  type        = bool
  default     = false
}

