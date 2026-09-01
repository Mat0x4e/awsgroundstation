variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
}

variable "account_id" {
  description = "AWS account ID"
  type        = string
}

variable "sdr_output_bucket_name" {
  description = "Name of the SDR output S3 bucket (source of visualization inputs)"
  type        = string
}

variable "sdr_output_bucket_arn" {
  description = "ARN of the SDR output S3 bucket"
  type        = string
}

variable "kms_key_arn" {
  description = "ARN of the KMS key used for encrypting outputs at rest"
  type        = string
}

variable "kms_key_id" {
  description = "ID of the KMS key used for SSE in CodeBuild S3 uploads"
  type        = string
}

variable "sns_topic_arn" {
  description = "ARN of the SNS topic for failure notifications"
  type        = string
}

variable "enable_geotiff" {
  description = "Whether to produce GeoTIFF outputs alongside annotated PNG images"
  type        = bool
  default     = true
}

variable "enable_destripe" {
  description = "Whether to apply inter-detector destriping correction on NASA SDR bands"
  type        = bool
  default     = true
}

variable "tle_url" {
  description = "CelesTrak URL for fetching the NOAA-20 TLE (used by BBoxCalculator TLE path)"
  type        = string
  default     = "https://celestrak.org/NORAD/elements/gp.php?CATNR=43013&FORMAT=3LE"
}

variable "tle_fallback" {
  description = "Inline fallback TLE string used when the CelesTrak endpoint is unreachable"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags to apply to all taggable resources"
  type        = map(string)
  default     = {}
}
