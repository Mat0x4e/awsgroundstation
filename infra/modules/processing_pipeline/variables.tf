variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
}

variable "environment" {
  description = "Deployment environment name"
  type        = string
}

variable "reception_bucket_name" {
  description = "Name of the S3 reception bucket"
  type        = string
}

variable "reception_bucket_arn" {
  description = "ARN of the S3 reception bucket"
  type        = string
}

variable "kms_key_id" {
  description = "ID of the KMS key for encryption"
  type        = string
}

variable "kms_key_arn" {
  description = "ARN of the KMS key for encryption"
  type        = string
}

variable "lambda_role_arn" {
  description = "ARN of the IAM role for the processor Lambda"
  type        = string
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}
