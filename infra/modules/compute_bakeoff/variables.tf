# variables.tf — inputs for the disposable Batch-on-EC2-Spot bakeoff module.
# This module is gated by enable_compute_bakeoff (default false) in the root.
# It is never created by a normal apply and can be torn down independently.

variable "project_name" {
  description = "Project name used for resource naming and tagging (e.g. groundstation-noaa20)"
  type        = string
}

variable "account_id" {
  description = "AWS account ID — used to scope IAM trust policies and derive resource ARNs"
  type        = string
}

variable "kms_key_arn" {
  description = "ARN of the KMS CMK used for S3 SSE-KMS and CloudWatch Logs encryption"
  type        = string
}

variable "sdr_ecr_image_url" {
  description = "Full ECR image URL (including :latest tag) for the SDR pipeline container. Example: 123456789012.dkr.ecr.eu-central-1.amazonaws.com/groundstation-noaa20-sdr-pipeline:latest"
  type        = string
}

variable "reception_bucket_name" {
  description = "Name of the Ground Station reception S3 bucket containing the contact .pcap chunks (read-only for bakeoff jobs)"
  type        = string
}

variable "reception_bucket_arn" {
  description = "ARN of the Ground Station reception S3 bucket (used to scope the IAM read policy)"
  type        = string
}

variable "output_bucket_name" {
  description = "Name of the SDR pipeline output S3 bucket. Bakeoff jobs write to the bakeoff/* prefix only — production contacts/ is never touched."
  type        = string
}

variable "output_bucket_arn" {
  description = "ARN of the SDR pipeline output S3 bucket. IAM write policy is scoped to the bakeoff/* prefix only."
  type        = string
}

variable "max_vcpus" {
  description = "Maximum vCPUs for the Batch compute environment. Default 240 is sufficient for ~30 chunks × 8 vCPU = 240, removing the CodeBuild concurrency throttle under test."
  type        = number
  default     = 240
}

variable "tags" {
  description = "Common tags to merge onto all resources. Objective=compute-bakeoff and Disposable=true are always added on top."
  type        = map(string)
  default     = {}
}
