# SDR Pipeline Module
#
# Implements the NOAA-20 CADU-to-TIFF processing pipeline. Resources are split
# across separate files: ecr.tf, s3.tf (task 13.1), iam.tf (task 13.2),
# codebuild.tf (task 13.3), step_functions.tf (task 11.1), eventbridge.tf.

locals {
  account_id = var.account_id
}
