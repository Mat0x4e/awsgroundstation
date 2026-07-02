#!/usr/bin/env bash
# build.sh — Build and push the SDR pipeline Docker image to ECR
#
# Usage:
#   ./docker/sdr-pipeline/build.sh
#
# Prerequisites (in nasa_software/):
#   - satdump_1.2.2_ubuntu_22.04_amd64.deb
#   - RT-STPS_7.1/RT-STPS_7.0.tar.gz
#   - RT-STPS_7.1/RT-STPS_7.0_PATCH_1.tar.gz
#   - RT-STPS_7.1/CSPP_SDR_V4.1.1_patch.tar.gz
#   - CSPP_SDR_V4.1.tar.gz
#   - CSPP_SDR_V4.1_straylight_luts_j01.tar.gz
#   - CSPP_SDR_V4.1_static_tiles.tar.gz
#   - scripts/ directory with pipeline Python/shell scripts
#
# Environment variables (optional overrides):
#   AWS_REGION        — AWS region (default: eu-central-1)
#   AWS_ACCOUNT_ID    — AWS account ID (auto-detected if not set)
#   AWS_PROFILE       — AWS CLI profile (default: AWSAdminAccess-471112743408)
#   ECR_REPO_NAME     — ECR repository name (default: groundstation-noaa20-sdr-pipeline)
#   DOCKER_BUILDKIT   — Enable BuildKit (default: 1)

set -euo pipefail

# --- Configuration ---
AWS_REGION="${AWS_REGION:-eu-central-1}"
AWS_PROFILE="${AWS_PROFILE:-AWSAdminAccess-471112743408}"
ECR_REPO_NAME="${ECR_REPO_NAME:-groundstation-noaa20-sdr-pipeline}"
DOCKER_BUILDKIT="${DOCKER_BUILDKIT:-1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
NASA_SOFTWARE="${PROJECT_ROOT}/nasa_software"
SCRIPTS_DIR="${PROJECT_ROOT}/scripts"

# --- Verify prerequisites ---
echo "[build] Checking prerequisites..."
for f in \
    "${NASA_SOFTWARE}/satdump_1.2.2_ubuntu_22.04_amd64.deb" \
    "${NASA_SOFTWARE}/RT-STPS_7.1/RT-STPS_7.0.tar.gz" \
    "${NASA_SOFTWARE}/RT-STPS_7.1/RT-STPS_7.0_PATCH_1.tar.gz" \
    "${NASA_SOFTWARE}/RT-STPS_7.1/CSPP_SDR_V4.1.1_patch.tar.gz" \
    "${NASA_SOFTWARE}/CSPP_SDR_V4.1.tar.gz" \
    "${NASA_SOFTWARE}/CSPP_SDR_V4.1_straylight_luts_j01.tar.gz" \
    "${NASA_SOFTWARE}/CSPP_SDR_V4.1_static_tiles.tar.gz"; do
    if [ ! -f "$f" ]; then
        echo "[ERROR] Missing prerequisite: $f"
        exit 1
    fi
done

if [ ! -d "${SCRIPTS_DIR}" ]; then
    echo "[ERROR] scripts/ directory not found at ${SCRIPTS_DIR}"
    exit 1
fi

echo "[build] All prerequisites found."

# --- Copy files to Docker build context ---
echo "[build] Preparing Docker build context..."
BUILD_CONTEXT=$(mktemp -d)
trap "rm -rf ${BUILD_CONTEXT}" EXIT

cp "${SCRIPT_DIR}/Dockerfile" "${BUILD_CONTEXT}/"
cp "${NASA_SOFTWARE}/satdump_1.2.2_ubuntu_22.04_amd64.deb" "${BUILD_CONTEXT}/"
cp "${NASA_SOFTWARE}/RT-STPS_7.1/RT-STPS_7.0.tar.gz" "${BUILD_CONTEXT}/"
cp "${NASA_SOFTWARE}/RT-STPS_7.1/RT-STPS_7.0_PATCH_1.tar.gz" "${BUILD_CONTEXT}/"
cp "${NASA_SOFTWARE}/RT-STPS_7.1/CSPP_SDR_V4.1.1_patch.tar.gz" "${BUILD_CONTEXT}/"
cp "${NASA_SOFTWARE}/CSPP_SDR_V4.1.tar.gz" "${BUILD_CONTEXT}/"
cp "${NASA_SOFTWARE}/CSPP_SDR_V4.1_straylight_luts_j01.tar.gz" "${BUILD_CONTEXT}/"
cp "${NASA_SOFTWARE}/CSPP_SDR_V4.1_static_tiles.tar.gz" "${BUILD_CONTEXT}/"
cp -r "${SCRIPTS_DIR}" "${BUILD_CONTEXT}/scripts"

echo "[build] Build context size: $(du -sh ${BUILD_CONTEXT} | cut -f1)"

# --- Detect AWS Account ID ---
if [ -z "${AWS_ACCOUNT_ID:-}" ]; then
    echo "[build] Detecting AWS account ID..."
    AWS_ACCOUNT_ID="$(aws sts get-caller-identity --profile "${AWS_PROFILE}" --query Account --output text)"
fi

ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}"

# --- Determine image tags ---
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")"
IMAGE_TAG_SHA="${ECR_URI}:${GIT_SHA}"
IMAGE_TAG_LATEST="${ECR_URI}:latest"

echo "[build] ECR URI:    ${ECR_URI}"
echo "[build] Git SHA:    ${GIT_SHA}"
echo "[build] Tags:       ${GIT_SHA}, latest"

# --- Login to ECR ---
echo "[build] Logging in to ECR..."
aws ecr get-login-password --region "${AWS_REGION}" --profile "${AWS_PROFILE}" \
    | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# --- Build Docker image ---
echo "[build] Building Docker image..."
export DOCKER_BUILDKIT
docker build \
    -t "${IMAGE_TAG_SHA}" \
    -t "${IMAGE_TAG_LATEST}" \
    -f "${BUILD_CONTEXT}/Dockerfile" \
    "${BUILD_CONTEXT}"

# --- Push to ECR ---
echo "[build] Pushing ${IMAGE_TAG_SHA}..."
docker push "${IMAGE_TAG_SHA}"

echo "[build] Pushing ${IMAGE_TAG_LATEST}..."
docker push "${IMAGE_TAG_LATEST}"

echo "[build] Done. Image pushed as:"
echo "        ${IMAGE_TAG_SHA}"
echo "        ${IMAGE_TAG_LATEST}"
