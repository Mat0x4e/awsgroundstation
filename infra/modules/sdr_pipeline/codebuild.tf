# codebuild.tf — SDR Pipeline shared CodeBuild project (retired, task 8)
#
# aws_codebuild_project.sdr_pipeline, aws_cloudwatch_log_group.codebuild, and
# aws_iam_role.codebuild / aws_iam_role_policy.codebuild were removed in task 8
# of the clean-compute-pipeline spec (Stage 2). Both the fan-out Map and the
# async aggregation now run on Batch; the shared CodeBuild project is fully dead.
#
# The image-build project lives in codebuild_image.tf (aws_codebuild_project.sdr_docker_build).
# The LUT cache refresh project lives in lut_cache.tf (aws_codebuild_project.lut_cache_refresh).
# The sync pipeline CSPP CodeBuild project lives in modules/sync_pipeline/codebuild.tf -- HELD
# (not yet validated on a real sync contact).
