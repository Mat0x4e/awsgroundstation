# Remote state backend — S3 with native lock file (Terraform >= 1.10)
# Bucket:  groundstation-noaa20-tfstate-471112743408 (eu-central-1)
#          versioning=ON, AES-256 encryption, all public access blocked
#
# Locking: use_lockfile=true uses S3 conditional writes — no DynamoDB table needed.
# The groundstation-noaa20-tfstate-lock DynamoDB table (created 2026-10-19) is no
# longer used and can be safely deleted when convenient.
#
# Bootstrap was done once via AWS CLI (2026-10-19). State migrated from local.
# To onboard a new operator: run `terraform init` — Terraform will pull state from S3.
# NEVER delete the bucket while live resources exist.

terraform {
  backend "s3" {
    bucket       = "groundstation-noaa20-tfstate-471112743408"
    key          = "infra/terraform.tfstate"
    region       = "eu-central-1"
    encrypt      = true
    use_lockfile = true
    profile      = "AWSAdminAccess-471112743408"
  }
}
