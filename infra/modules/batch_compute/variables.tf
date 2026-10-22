# variables.tf - inputs for the production Batch compute module.
# This module is gated by enable_batch_compute (default false) in the root.
# Wave 0 (tasks 1.1, 1.2) provides the Spot CE + fan-out queue + chunk job definition.
# The on-demand CE and aggregation/render job definitions are added in waves 5 and 10.

variable "project_name" {
  description = "Project name used for resource naming and tagging (e.g. groundstation-noaa20)"
  type        = string
}

variable "account_id" {
  description = "AWS account ID - used to scope IAM trust policies and derive resource ARNs"
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

variable "viirs_ecr_image_url" {
  description = "Full ECR image URL (including :latest tag) for the dedicated VIIRS visualization container (Cartopy/GDAL/rasterio/h5py) - distinct from sdr_ecr_image_url. Example: 123456789012.dkr.ecr.eu-central-1.amazonaws.com/groundstation-noaa20-viirs-visualization:latest"
  type        = string
}

variable "reception_bucket_name" {
  description = "Name of the Ground Station reception S3 bucket containing the contact .pcap chunks (read-only for chunk jobs)"
  type        = string
}

variable "reception_bucket_arn" {
  description = "ARN of the Ground Station reception S3 bucket (used to scope the IAM read policy)"
  type        = string
}

variable "output_bucket_name" {
  description = "Name of the SDR pipeline output S3 bucket. Chunk jobs write to contacts/* only (never bakeoff/ or any throwaway prefix)."
  type        = string
}

variable "output_bucket_arn" {
  description = "ARN of the SDR pipeline output S3 bucket. IAM write policy is scoped to contacts/* only."
  type        = string
}

variable "max_vcpus" {
  description = "Maximum vCPUs for the Spot fan-out compute environment. Default 216 = 27 chunks x 8 vCPU, allowing all chunks of a typical contact to run concurrently (removes the CodeBuild account-build-cap throttle)."
  type        = number
  default     = 216
}

variable "aggregation_max_vcpus" {
  description = "Maximum vCPUs for the on-demand aggregation compute environment. Default 16 allows one or two concurrent aggregation jobs (8 vCPU each) without over-provisioning."
  type        = number
  default     = 16
}

variable "tags" {
  description = "Common tags to apply to all resources in this module. No Disposable=true - these are production resources."
  type        = map(string)
  default     = {}
}
