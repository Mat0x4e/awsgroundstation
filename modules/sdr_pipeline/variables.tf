variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
}

variable "account_id" {
  description = "AWS account ID"
  type        = string
}

variable "input_bucket_name" {
  description = "Name of the Ground Station reception bucket"
  type        = string
}

variable "kms_key_arn" {
  description = "ARN of the KMS key for encryption"
  type        = string
}

variable "sns_topic_arn" {
  description = "ARN of the SNS topic for failure notifications"
  type        = string
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}
