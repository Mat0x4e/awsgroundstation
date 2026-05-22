provider "aws" {
  region = var.region

  default_tags {
    tags = merge(var.tags, {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
    })
  }
}

provider "awscc" {
  region = var.region
}

locals {
  common_tags = merge(var.tags, {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  })
}

module "security" {
  source = "./modules/security"

  project_name               = var.project_name
  environment                = var.environment
  reception_bucket_arn       = module.s3_delivery.bucket_arn
  output_bucket_arn          = ""
  enable_processing_pipeline = var.enable_processing_pipeline
  tags                       = local.common_tags
}

module "s3_delivery" {
  source = "./modules/s3_delivery"

  project_name = var.project_name
  environment  = var.environment
  kms_key_arn  = module.security.kms_key_arn
  tags         = local.common_tags
}

module "mission_profile" {
  source = "./modules/mission_profile"

  project_name         = var.project_name
  environment          = var.environment
  satellite_norad_id   = var.satellite_norad_id
  reception_bucket_arn = module.s3_delivery.bucket_arn
  kms_key_arn          = module.security.kms_key_arn
  tags                 = local.common_tags
}

# Processing pipeline module (FEAT-002) - conditionally created
# module "processing_pipeline" {
#   count  = var.enable_processing_pipeline ? 1 : 0
#   source = "./modules/processing_pipeline"
#
#   project_name         = var.project_name
#   environment          = var.environment
#   reception_bucket_arn = module.s3_delivery.bucket_arn
#   kms_key_arn          = module.security.kms_key_arn
#   tags                 = local.common_tags
# }
