#!/bin/bash
# Deploy scripts/aggregation.sh to /opt/scripts/aggregation.sh on the EC2
# aggregation instance.
#
# The instance's software (/opt/rt-stps, /opt/SDR_4_1) is hand-provisioned on a
# preserved root volume, and modules/sdr_pipeline/ec2.tf carries no user_data --
# replacing the instance to add one would destroy that install. So the script is
# shipped out-of-band, via S3 + SSM, and this file is the repeatable form of that
# step. Re-run it after every change to scripts/aggregation.sh.
#
# Usage: ./scripts/deploy_aggregation.sh
#
# Requires: AWS_PROFILE with ec2:StartInstances, ssm:SendCommand, s3:PutObject.

set -euo pipefail

REGION="${AWS_REGION:-eu-central-1}"
INSTANCE_ID="${AGGREGATION_INSTANCE_ID:-i-01d21ecae10f99fbb}"
BUCKET="${SOFTWARE_BUCKET:-groundstation-noaa20-sdr-output-471112743408}"
KMS_KEY_ID="${KMS_KEY_ID:-70451aac-a58c-4a93-be24-4587cd55a795}"
SRC="$(dirname "$0")/aggregation.sh"
S3_URI="s3://${BUCKET}/software/aggregation.sh"

echo "==> Uploading ${SRC} to ${S3_URI}"
aws s3 cp "$SRC" "$S3_URI" --region "$REGION" \
    --sse aws:kms --sse-kms-key-id "$KMS_KEY_ID"

echo "==> Ensuring instance ${INSTANCE_ID} is running"
state=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --region "$REGION" \
    --query 'Reservations[].Instances[].State.Name' --output text)
if [ "$state" != "running" ]; then
    aws ec2 start-instances --instance-ids "$INSTANCE_ID" --region "$REGION" >/dev/null
    aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION"
fi

# The SSM agent registers a little after the instance reports running.
echo "==> Waiting for SSM agent"
for _ in $(seq 1 30); do
    ping=$(aws ssm describe-instance-information --region "$REGION" \
        --filters "Key=InstanceIds,Values=${INSTANCE_ID}" \
        --query 'InstanceInformationList[].PingStatus' --output text 2>/dev/null || true)
    [ "$ping" = "Online" ] && break
    sleep 5
done
[ "$ping" = "Online" ] || { echo "SSM agent not Online (last: ${ping:-none})" >&2; exit 1; }

echo "==> Installing to /opt/scripts/aggregation.sh"
cmd_id=$(aws ssm send-command --region "$REGION" \
    --instance-ids "$INSTANCE_ID" \
    --document-name AWS-RunShellScript \
    --comment "Deploy aggregation.sh" \
    --parameters "commands=[\
\"set -e\",\
\"mkdir -p /opt/scripts\",\
\"aws s3 cp ${S3_URI} /opt/scripts/aggregation.sh --region ${REGION}\",\
\"chmod +x /opt/scripts/aggregation.sh\",\
\"head -1 /opt/scripts/aggregation.sh\",\
\"md5sum /opt/scripts/aggregation.sh\"]" \
    --query 'Command.CommandId' --output text)

for _ in $(seq 1 30); do
    status=$(aws ssm get-command-invocation --region "$REGION" \
        --command-id "$cmd_id" --instance-id "$INSTANCE_ID" \
        --query Status --output text 2>/dev/null || echo Pending)
    case "$status" in Success|Failed|Cancelled|TimedOut) break;; esac
    sleep 3
done

aws ssm get-command-invocation --region "$REGION" \
    --command-id "$cmd_id" --instance-id "$INSTANCE_ID" \
    --query 'StandardOutputContent' --output text

if [ "$status" != "Success" ]; then
    echo "Deploy failed (${status}):" >&2
    aws ssm get-command-invocation --region "$REGION" \
        --command-id "$cmd_id" --instance-id "$INSTANCE_ID" \
        --query 'StandardErrorContent' --output text >&2
    exit 1
fi

echo "==> Deployed. Local md5: $(md5sum "$SRC" | cut -d' ' -f1)"
