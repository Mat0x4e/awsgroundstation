#!/bin/bash
# Deploy scripts/aggregation_entrypoint.sh to S3 for the Batch aggregation job.
#
# The Batch aggregation job downloads this script from S3 at container startup,
# so a redeploy takes effect without rebuilding the SDR ECR image -- same
# no-image-rebuild property as the CodeBuild buildspec approach.
#
# Usage: ./scripts/deploy_aggregation_entrypoint.sh [output-bucket]
#
#   output-bucket  default: groundstation-noaa20-sdr-output-471112743408
#
# Requires: AWS credentials with s3:PutObject on software/* in the output bucket.
# The Batch job definition pulls from: software/aggregation_entrypoint.sh

set -euo pipefail

REGION="${AWS_REGION:-eu-central-1}"
BUCKET="${1:-groundstation-noaa20-sdr-output-471112743408}"
KMS_KEY_ID="${KMS_KEY_ID:-70451aac-a58c-4a93-be24-4587cd55a795}"
SRC="$(dirname "$0")/aggregation_entrypoint.sh"
S3_KEY="software/aggregation_entrypoint.sh"
S3_URI="s3://${BUCKET}/${S3_KEY}"

echo "==> Uploading ${SRC} to ${S3_URI}"
aws s3 cp "$SRC" "$S3_URI" --region "$REGION" \
    --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID"

echo "==> Deployed: ${S3_URI}"
echo "==> Batch job definition pulls entrypoint from: ${S3_KEY}"
echo "==> Local md5: $(md5sum "$SRC" | cut -d' ' -f1)"
