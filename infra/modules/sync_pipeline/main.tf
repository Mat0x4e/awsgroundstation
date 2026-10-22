# Sync Pipeline Module
#
# Implements the synchronous NOAA-20 demod/decode reception pipeline: a
# dedicated Ground Station mission profile and dataflow endpoint deliver
# demodulated/decoded data live to a receiver EC2 during the contact; a Step
# Functions state machine then hands off from the receiver (RT-STPS already
# ran there), runs CSPP in CodeBuild, and reuses the existing VIIRS
# visualization orchestrator. Fully separate from modules/mission_profile and
# modules/sdr_pipeline's async DigIF-to-S3 pipeline -- see groundstation.tf's
# header comment for why.
#
# Resources are split across: groundstation.tf, ec2.tf, s3.tf, iam.tf,
# lambda.tf, codebuild.tf, step_functions.tf, eventbridge.tf.
