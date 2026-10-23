---
name: rebuild-image-from-branch
description: Rebuild and push a container image (SDR pipeline or VIIRS render) via the image-build CodeBuild projects. Use after changing anything baked into an image (scripts/, Dockerfile, render_entrypoint.sh, the NASA render fixes) before validating a Batch job. Captures the source-ref trap that caused render jobs to fail with "No such file or directory".
---

# Rebuild a container image from the right branch

An AWS Batch container job runs the code BAKED INTO ITS IMAGE (via `COPY scripts/ /opt/scripts/`
in the Dockerfile), not the code in your working tree. So changing `scripts/`, a Dockerfile, or
`scripts/viirs/render_entrypoint.sh` has NO effect on a Batch job until the image is rebuilt and
pushed. "I changed the script" is necessary but not sufficient.

Two image-build CodeBuild projects, each builds from GitHub:
- `groundstation-noaa20-sdr-docker-build`  -> `...-sdr-pipeline:latest` (SatDump/RT-STPS/CSPP)
- `groundstation-noaa20-viirs-docker-build` -> `...-viirs-visualization:latest` (render image)

## THE TRAP (learned live 2026-10-22)

Both projects build from GitHub `sourceVersion = main`. If your change is on a FEATURE BRANCH
not yet merged to `main`, a default rebuild produces an image WITHOUT your change. Symptom: the
Batch job fails almost immediately with, e.g.:
`/bin/bash: /opt/scripts/viirs/render_entrypoint.sh: No such file or directory`.

## Procedure

1. Commit AND push your change to the branch the build will read (CodeBuild pulls from the
   remote, not your local clone).
2. Trigger the build. If the code is on `main`, a plain start-build is correct:
   ```
   aws codebuild start-build --project-name <image-build-project> --region eu-central-1
   ```
   If the code is on a FEATURE BRANCH, pin the build to it with a one-off override:
   ```
   aws codebuild start-build --project-name <image-build-project> \
     --source-version <branch> --region eu-central-1
   ```
3. Poll to completion (builds take ~4-8 min):
   ```
   aws codebuild batch-get-builds --ids <build-id> --region eu-central-1 \
     --query 'builds[0].{status:buildStatus,phase:currentPhase}'
   ```
4. CONFIRM the new image actually landed before validating -- check the ECR push time advanced:
   ```
   aws ecr describe-images --repository-name <repo> --region eu-central-1 \
     --image-ids imageTag=latest --query 'imageDetails[0].imagePushedAt'
   ```
5. Only now submit/validate the Batch job that consumes the image.

## Note

Unlike the aggregation entrypoint (which is pulled from S3 at job start -- see the
deploy-aggregation-entrypoint skill), the render entrypoint is IMAGE-BAKED, so it requires a
full image rebuild, not an S3 redeploy. Once your branch merges to `main`, the default
`start-build` (no `--source-version`) is correct again.
