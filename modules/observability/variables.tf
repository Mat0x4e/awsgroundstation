variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
}

variable "environment" {
  description = "Deployment environment name"
  type        = string
}

variable "scheduler_log_group_name" {
  description = "Name of the CloudWatch Log Group for the scheduler Lambda"
  type        = string
}

variable "reception_bucket_name" {
  description = "Name of the S3 reception bucket"
  type        = string
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}
