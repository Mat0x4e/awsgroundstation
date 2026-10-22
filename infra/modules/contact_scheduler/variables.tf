variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
}

variable "environment" {
  description = "Deployment environment name"
  type        = string
}

variable "lambda_role_arn" {
  description = "ARN of the IAM role for the scheduler Lambda"
  type        = string
}

variable "mission_profile_arn" {
  description = "ARN of the Ground Station mission profile"
  type        = string
}

variable "satellite_arn" {
  description = "ARN of the target satellite"
  type        = string
}

variable "sns_topic_arn" {
  description = "ARN of the SNS topic for notifications"
  type        = string
}

variable "minimum_elevation_degrees" {
  description = "Minimum elevation angle in degrees for pass selection"
  type        = number
  default     = 10
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}
