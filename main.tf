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

data "aws_caller_identity" "current" {}

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
  output_bucket_arn          = var.enable_processing_pipeline ? module.processing_pipeline[0].output_bucket_arn : ""
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
  count  = var.ground_station_enabled ? 1 : 0
  source = "./modules/mission_profile"

  project_name           = var.project_name
  environment            = var.environment
  satellite_norad_id     = var.satellite_norad_id
  reception_bucket_arn   = module.s3_delivery.bucket_arn
  groundstation_role_arn = module.security.groundstation_role_arn
  tags                   = local.common_tags
}

module "contact_scheduler" {
  source = "./modules/contact_scheduler"

  project_name        = var.project_name
  environment         = var.environment
  lambda_role_arn     = module.security.scheduler_role_arn
  mission_profile_arn = var.ground_station_enabled ? module.mission_profile[0].mission_profile_arn : ""
  satellite_arn       = "arn:aws:groundstation::${data.aws_caller_identity.current.account_id}:satellite/${var.satellite_norad_id}"
  sns_topic_arn       = module.security.sns_topic_arn
  tags                = local.common_tags
}

module "processing_pipeline" {
  count  = var.enable_processing_pipeline ? 1 : 0
  source = "./modules/processing_pipeline"

  project_name          = var.project_name
  environment           = var.environment
  reception_bucket_name = module.s3_delivery.bucket_name
  reception_bucket_arn  = module.s3_delivery.bucket_arn
  kms_key_id            = module.security.kms_key_id
  kms_key_arn           = module.security.kms_key_arn
  lambda_role_arn       = module.security.processor_role_arn
  tags                  = local.common_tags
}

module "observability" {
  source = "./modules/observability"

  project_name             = var.project_name
  environment              = var.environment
  scheduler_log_group_name = module.contact_scheduler.log_group_name
  reception_bucket_name    = module.s3_delivery.bucket_name
  kms_key_id               = module.security.kms_key_id
  tags                     = local.common_tags
}
