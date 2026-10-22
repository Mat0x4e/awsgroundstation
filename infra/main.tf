provider "aws" {
  region  = var.region
  profile = var.aws_profile

  default_tags {
    tags = merge(var.tags, {
      Project         = var.project_name
      Environment     = var.environment
      ManagedBy       = "terraform"
      Objective       = var.objective
      Owner           = var.owner
      "Creation-Date" = var.creation_date
    })
  }
}

provider "awscc" {
  region  = var.region
  profile = var.aws_profile
}

data "aws_caller_identity" "current" {}

locals {
  common_tags = merge(var.tags, {
    Project         = var.project_name
    Environment     = var.environment
    ManagedBy       = "terraform"
    Objective       = var.objective
    Owner           = var.owner
    "Creation-Date" = var.creation_date
  })

  # Deterministic Batch ARNs built from known naming conventions.
  # These break the module.sdr_pipeline <-> module.batch_compute cycle:
  # module.batch_compute already reads module.sdr_pipeline outputs (ECR URL,
  # output bucket); wiring module.sdr_pipeline to module.batch_compute outputs
  # closes the cycle. Using plain string interpolation here removes that back-
  # reference entirely - no module output is read, only var.* and data.*.
  #
  # Job definition ARN is UNVERSIONED (no ":N" suffix). AWS submitJob resolves
  # the ARN to the latest ACTIVE revision at runtime, which is more robust than
  # pinning a revision number and avoids Terraform churn when the job def revs.
  # submitJob accepts name, unversioned ARN, or versioned ARN - all three forms
  # are valid per the Batch API contract.
  batch_fanout_job_queue_arn     = "arn:aws:batch:${var.region}:${data.aws_caller_identity.current.account_id}:job-queue/${var.project_name}-fanout-queue"
  batch_chunk_job_definition_arn = "arn:aws:batch:${var.region}:${data.aws_caller_identity.current.account_id}:job-definition/${var.project_name}-chunk"

  # Stage 2 aggregation Batch ARNs (same cycle-break pattern as Stage 1 above).
  # The on-demand CE + agg-queue + aggregation job def were deployed in task 5.1
  # and are owned by module.batch_compute. Deterministic naming: project_name-
  # agg-queue and project_name-aggregation (unversioned job def).
  batch_aggregation_job_queue_arn      = "arn:aws:batch:${var.region}:${data.aws_caller_identity.current.account_id}:job-queue/${var.project_name}-agg-queue"
  batch_aggregation_job_definition_arn = "arn:aws:batch:${var.region}:${data.aws_caller_identity.current.account_id}:job-definition/${var.project_name}-aggregation"

  # Stage 3 render Batch ARNs (same cycle-break pattern as Stage 1/2 above).
  # Render REUSES the on-demand aggregation queue (project_name-agg-queue) - it is
  # the SAME queue as aggregation, not a dedicated render queue. Render is a single
  # job (not a fan-out), so it rides the existing on-demand CE via that queue.
  batch_render_job_queue_arn = "arn:aws:batch:${var.region}:${data.aws_caller_identity.current.account_id}:job-queue/${var.project_name}-agg-queue"
  # Unversioned job-definition ARN (no ":N" suffix). submitJob resolves the ARN to
  # the latest ACTIVE revision at runtime, which is more robust than pinning a
  # revision number and avoids Terraform churn when the job def revs.
  batch_render_job_definition_arn = "arn:aws:batch:${var.region}:${data.aws_caller_identity.current.account_id}:job-definition/${var.project_name}-render"
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

  project_name  = var.project_name
  environment   = var.environment
  kms_key_arn   = module.security.kms_key_arn
  sns_topic_arn = module.security.sns_topic_arn
  tags          = local.common_tags
}

module "mission_profile" {
  count  = var.ground_station_enabled ? 1 : 0
  source = "./modules/mission_profile"

  project_name           = var.project_name
  environment            = var.environment
  satellite_norad_id     = var.satellite_norad_id
  satellite_onboarded    = var.satellite_onboarded
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
  satellite_arn       = "arn:aws:groundstation::${data.aws_caller_identity.current.account_id}:satellite/${var.satellite_id}"
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

module "sdr_pipeline" {
  count  = var.enable_sdr_pipeline ? 1 : 0
  source = "./modules/sdr_pipeline"

  project_name      = var.project_name
  account_id        = data.aws_caller_identity.current.account_id
  input_bucket_name = module.s3_delivery.bucket_name
  kms_key_arn       = module.security.kms_key_arn
  sns_topic_arn     = module.security.sns_topic_arn
  satellite_id      = var.satellite_id
  tags              = local.common_tags

  # clean-compute-pipeline Stage 1 strangler wiring.
  # Cycle broken via deterministic-ARN locals: module.batch_compute already reads
  # module.sdr_pipeline outputs (ECR URL, output bucket). Referencing
  # module.batch_compute outputs here would close the cycle. Instead, the job
  # queue and job definition ARNs are constructed from var.project_name + region
  # + account - pure string interpolation with no module dependency. The actual
  # Batch resources are still owned by module.batch_compute; sdr_pipeline's SFN
  # simply references them by their deterministic ARN.
  use_batch_fanout               = var.enable_batch_compute
  batch_fanout_job_queue_arn     = var.enable_batch_compute ? local.batch_fanout_job_queue_arn : ""
  batch_chunk_job_definition_arn = var.enable_batch_compute ? local.batch_chunk_job_definition_arn : ""

  # clean-compute-pipeline Stage 2 strangler wiring (aggregation -> Batch on-demand).
  # Same cycle-break pattern as Stage 1. Gated on enable_batch_compute so the
  # aggregation Batch path activates together with the fan-out Batch path.
  use_batch_aggregation                = var.enable_batch_compute
  batch_aggregation_job_queue_arn      = var.enable_batch_compute ? local.batch_aggregation_job_queue_arn : ""
  batch_aggregation_job_definition_arn = var.enable_batch_compute ? local.batch_aggregation_job_definition_arn : ""
}

module "viirs_visualization" {
  count  = var.enable_sdr_pipeline ? 1 : 0
  source = "./modules/viirs_visualization"

  project_name           = var.project_name
  account_id             = data.aws_caller_identity.current.account_id
  sdr_output_bucket_name = module.sdr_pipeline[0].output_bucket_name
  sdr_output_bucket_arn  = module.sdr_pipeline[0].output_bucket_arn
  kms_key_arn            = module.security.kms_key_arn
  kms_key_id             = module.security.kms_key_id
  sns_topic_arn          = module.security.sns_topic_arn
  tags                   = local.common_tags

  # Stage 3b strangler - flips the orchestrator Lambda to AWS Batch render when
  # enable_batch_compute is set; CodeBuild remains the fallback. Deterministic
  # ARNs (not module.batch_compute outputs) avoid a module cycle (batch_compute
  # already depends on viirs_visualization for the image URL).
  render_compute                  = var.enable_batch_compute ? "batch" : "codebuild"
  batch_render_job_queue_arn      = var.enable_batch_compute ? local.batch_render_job_queue_arn : ""
  batch_render_job_definition_arn = var.enable_batch_compute ? local.batch_render_job_definition_arn : ""
}

module "sync_pipeline" {
  count  = var.enable_sync_pipeline ? 1 : 0
  source = "./modules/sync_pipeline"

  project_name           = var.project_name
  environment            = var.environment
  account_id             = data.aws_caller_identity.current.account_id
  satellite_norad_id     = var.satellite_norad_id
  satellite_id           = var.satellite_id
  satellite_onboarded    = var.satellite_onboarded
  groundstation_role_arn = module.security.groundstation_role_arn
  kms_key_arn            = module.security.kms_key_arn
  kms_key_id             = module.security.kms_key_id
  sns_topic_arn          = module.security.sns_topic_arn
  # var.enable_sdr_pipeline must be true when enable_sync_pipeline is true (see
  # variables.tf) -- this reuses the SDR pipeline's ECR image rather than
  # building a second one, so there is nothing to fall back to if it's absent.
  sdr_pipeline_ecr_repository_url = var.enable_sdr_pipeline ? module.sdr_pipeline[0].ecr_repository_url : ""
  tags                            = local.common_tags

  # clean-compute-pipeline Stage 2 strangler wiring (sync aggregation -> Batch on-demand).
  # Same aggregation queue+jobdef as sdr_pipeline; RTSTPS=off selects the
  # CSPP-only branch in aggregation_entrypoint.sh.
  use_batch_aggregation                = var.enable_batch_compute
  batch_aggregation_job_queue_arn      = var.enable_batch_compute ? local.batch_aggregation_job_queue_arn : ""
  batch_aggregation_job_definition_arn = var.enable_batch_compute ? local.batch_aggregation_job_definition_arn : ""
}

###############################################################################
# Disposable experiment — Batch-on-EC2-Spot chunk fan-out bakeoff
# Gated by enable_compute_bakeoff (default false). See variables.tf for
# the teardown note and the enable_sdr_pipeline dependency.
###############################################################################

module "compute_bakeoff" {
  count  = var.enable_compute_bakeoff ? 1 : 0
  source = "./modules/compute_bakeoff"

  project_name = var.project_name
  account_id   = data.aws_caller_identity.current.account_id
  kms_key_arn  = module.security.kms_key_arn

  # Requires enable_sdr_pipeline = true — the module reuses the SDR ECR image and
  # output bucket. The ternary guards the [0] index; the module is count=0 when
  # enable_compute_bakeoff is false, so these expressions are never evaluated then.
  sdr_ecr_image_url  = var.enable_sdr_pipeline ? "${module.sdr_pipeline[0].ecr_repository_url}:latest" : ""
  output_bucket_name = var.enable_sdr_pipeline ? module.sdr_pipeline[0].output_bucket_name : ""
  output_bucket_arn  = var.enable_sdr_pipeline ? module.sdr_pipeline[0].output_bucket_arn : ""

  reception_bucket_name = module.s3_delivery.bucket_name
  reception_bucket_arn  = module.s3_delivery.bucket_arn

  tags = local.common_tags
}

###############################################################################
# Production Stage-1 fan-out — Batch-on-EC2-Spot chunk processing
# Gated by enable_batch_compute (default false). This is the long-lived
# replacement for the CodeBuild fan-out (clean-compute-pipeline strangler
# cutover), NOT the disposable bakeoff above. See variables.tf for the
# enable_sdr_pipeline dependency.
###############################################################################

module "batch_compute" {
  count  = var.enable_batch_compute ? 1 : 0
  source = "./modules/batch_compute"

  project_name = var.project_name
  account_id   = data.aws_caller_identity.current.account_id
  kms_key_arn  = module.security.kms_key_arn

  # Requires enable_sdr_pipeline = true — the module reuses the SDR ECR image and
  # output bucket. The ternary guards the [0] index; the module is count=0 when
  # enable_batch_compute is false, so these expressions are never evaluated then.
  sdr_ecr_image_url  = var.enable_sdr_pipeline ? "${module.sdr_pipeline[0].ecr_repository_url}:latest" : ""
  output_bucket_name = var.enable_sdr_pipeline ? module.sdr_pipeline[0].output_bucket_name : ""
  output_bucket_arn  = var.enable_sdr_pipeline ? module.sdr_pipeline[0].output_bucket_arn : ""

  # Stage 3 render job targets the DEDICATED viirs-visualization image, not the
  # sdr-pipeline image. Same ternary guard as sdr_ecr_image_url (module is count=0
  # when disabled, so the [0] is never evaluated). This creates a one-way
  # dependency batch_compute -> viirs_visualization which is acyclic
  # (viirs_visualization does not read batch_compute outputs), so no
  # deterministic-ARN cycle-break is needed here.
  viirs_ecr_image_url = var.enable_sdr_pipeline ? "${module.viirs_visualization[0].ecr_repository_url}:latest" : ""

  reception_bucket_name = module.s3_delivery.bucket_name
  reception_bucket_arn  = module.s3_delivery.bucket_arn

  tags = local.common_tags
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
