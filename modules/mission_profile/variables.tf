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

variable "kms_key_arn" {
  description = "ARN of the KMS key for encryption"
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

variable "contact_max_duration_seconds" {
  description = "Maximum contact duration in seconds"
  type        = number
  default     = 720
}
