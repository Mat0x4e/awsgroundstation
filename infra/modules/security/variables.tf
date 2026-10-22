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

variable "monthly_budget_usd" {
  description = "Monthly cost budget ceiling in USD. A budget alarm fires at 80% and 100% of this amount, guarding against accidental spend (e.g. a re-enabled contact scheduler booking paid Ground Station passes)."
  type        = string
  default     = "500"
}
