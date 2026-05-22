variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
}

variable "environment" {
  description = "Deployment environment name"
  type        = string
}

variable "reception_bucket_arn" {
  description = "ARN of the S3 reception bucket"
  type        = string
}

variable "output_bucket_arn" {
  description = "ARN of the S3 output bucket (optional, for processing pipeline)"
  type        = string
  default     = ""
}

variable "enable_processing_pipeline" {
  description = "Whether processing pipeline resources are enabled"
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}
