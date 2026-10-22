# ec2.tf — Dedicated synchronous receiver EC2 instance + dataflow endpoint group
#
# Unlike modules/sdr_pipeline's aggregation instance (launched stopped, started
# post-hoc by a Trigger Lambda after data already sits in S3), this instance
# must be RUNNING and its UDP listener ARMED before AOS: there is no S3 buffer
# in the synchronous path, so a late start loses the pass outright. Start
# automation is in lambda.tf (PREPASS trigger) and eventbridge.tf.

###############################################################################
# AMI, networking -- same pattern as modules/sdr_pipeline/ec2.tf
###############################################################################

data "aws_ami" "amazon_linux_2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# AWS Ground Station's managed prefix list -- scopes the UDP ingress rule to
# Ground Station's own address space instead of 0.0.0.0/0, per AWS's dataflow
# endpoint security group guidance.
data "aws_ec2_managed_prefix_list" "groundstation" {
  name = "com.amazonaws.global.groundstation"
}

###############################################################################
# Security Group
###############################################################################

resource "aws_security_group" "receiver" {
  name        = "${var.project_name}-sync-receiver"
  description = "Synchronous receiver EC2 -- UDP ingress from Ground Stations dataflow endpoint only; SSM outbound HTTPS only"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description     = "Ground Station demod/decode dataflow stream"
    from_port       = var.receiver_udp_port
    to_port         = var.receiver_udp_port
    protocol        = "udp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.groundstation.id]
  }

  egress {
    description = "HTTPS outbound for SSM and S3 access"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-receiver"
    Service = "sync-pipeline"
  })
}

###############################################################################
# IAM Role and Instance Profile
###############################################################################

resource "aws_iam_role" "receiver_ec2" {
  name        = "${var.project_name}-sync-receiver-ec2"
  description = "Allows the synchronous receiver EC2 instance to access its S3 bucket, KMS, and SSM"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "ec2.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-receiver-ec2"
    Service = "sync-pipeline"
  })
}

resource "aws_iam_role_policy" "receiver_ec2" {
  name = "${var.project_name}-sync-receiver-ec2-policy"
  role = aws_iam_role.receiver_ec2.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadWriteOutputBucket"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
        ]
        Resource = "${aws_s3_bucket.sync_output.arn}/*"
      },
      {
        Sid      = "S3ListOutputBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.sync_output.arn
      },
      {
        Sid    = "KMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey",
        ]
        Resource = var.kms_key_arn
      },
      # CloudWatch Metrics — emit OptDriftDetected from the SSM drift-check script
      # (ssm_drift_check.tf). Namespace-conditioned to SyncPipeline, matching the
      # PutMetricData pattern on aws_iam_role.codebuild in iam.tf.
      # Resource must be "*" — cloudwatch:PutMetricData does not support resource ARNs.
      {
        Sid      = "PutMetricDataDriftCheck"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "SyncPipeline"
          }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "receiver_ec2_ssm" {
  role       = aws_iam_role.receiver_ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "receiver_ec2" {
  name = "${var.project_name}-sync-receiver-ec2"
  role = aws_iam_role.receiver_ec2.name

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-receiver-ec2"
    Service = "sync-pipeline"
  })
}

###############################################################################
# EC2 Instance -- launched stopped; started pre-AOS by lambda.tf's arm trigger
###############################################################################

# checkov:skip=CKV_AWS_8: No user data required -- the live-UDP receiver,
# RT-STPS, and this instance's jpss1.xml (PnEncoded="true" variant, patched at
# runtime by scripts/sync_receiver_finish.sh, same pattern as
# scripts/aggregation.sh) are installed manually on the EBS volume, same as
# modules/sdr_pipeline's aggregation instance.
resource "aws_instance" "receiver" {
  ami           = data.aws_ami.amazon_linux_2023.id
  instance_type = var.receiver_instance_type

  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.receiver.id]
  iam_instance_profile   = aws_iam_instance_profile.receiver_ec2.name

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.receiver_root_volume_gb
    encrypted             = true
    kms_key_id            = var.kms_key_arn
    delete_on_termination = true

    tags = merge(var.tags, {
      Name    = "${var.project_name}-sync-receiver-root"
      Service = "sync-pipeline"
    })
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    http_endpoint               = "enabled"
  }

  monitoring = true

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-receiver"
    Service = "sync-pipeline"
  })

  lifecycle {
    # Same rationale as modules/sdr_pipeline/ec2.tf: RT-STPS and the live-UDP
    # receiver are installed by hand on the EBS volume, outside Terraform.
    ignore_changes = [ami, root_block_device]
  }
}

###############################################################################
# Task 9.3 (REL-6) — Fixed-IP secondary ENI
#
# The receiver EC2 is started and stopped per contact. A stop/start cycle can
# reassign the primary interface's (eth0) private IP, silently moving the
# address out from under the Ground Station dataflow endpoint group binding
# (which is fixed at apply time). REL-6 fix: create a secondary ENI whose
# private IP is assigned once by AWS and held stable for the lifetime of the
# ENI, independent of instance stop/start cycles.
#
# The dataflow endpoint group (below) binds to aws_network_interface.receiver
# .private_ip rather than aws_instance.receiver.private_ip, so the GS binding
# cannot be silently broken.
#
# The listener binds 0.0.0.0:<port> so it receives on all interfaces including
# the secondary ENI -- no listener configuration change is required.
###############################################################################

resource "aws_network_interface" "receiver" {
  subnet_id       = data.aws_subnets.default.ids[0]
  security_groups = [aws_security_group.receiver.id]
  description     = "Stable secondary ENI for Ground Station dataflow endpoint binding (REL-6) -- IP persists across instance stop/start"

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sync-receiver-eni"
    Service = "sync-pipeline"
  })
}

# Attach as device_index=1 (secondary). The instance's primary interface
# (device_index=0) is expressed via the top-level subnet_id +
# vpc_security_group_ids arguments on aws_instance.receiver. The AWS provider
# ~>5.0 does not allow mixing inline network_interface blocks with those
# top-level arguments, so the secondary attachment is expressed as a separate
# aws_network_interface_attachment resource rather than an inline block on the
# instance.
resource "aws_network_interface_attachment" "receiver" {
  instance_id          = aws_instance.receiver.id
  network_interface_id = aws_network_interface.receiver.id
  device_index         = 1
}

###############################################################################
# Dataflow Endpoint Group -- the live network endpoint Ground Station streams
# demod/decoded data to during the contact
###############################################################################

locals {
  dataflow_endpoint_name = "${var.project_name}-${var.environment}-sync-endpoint"
}

resource "awscc_groundstation_dataflow_endpoint_group" "receiver" {
  endpoint_details = [
    {
      endpoint = {
        name = local.dataflow_endpoint_name
        address = {
          # REL-6: bind to the secondary ENI's stable IP, not aws_instance
          # .receiver.private_ip. The ENI IP is assigned once and does not
          # change on instance stop/start, so the GS dataflow binding is safe.
          name = aws_network_interface.receiver.private_ip
          port = var.receiver_udp_port
        }
        mtu = 1500
      }
      security_details = {
        role_arn           = var.groundstation_role_arn
        security_group_ids = [aws_security_group.receiver.id]
        subnet_ids         = [data.aws_subnets.default.ids[0]]
      }
    }
  ]

  tags = [
    { key = "Name", value = local.dataflow_endpoint_name },
    { key = "Project", value = var.project_name },
  ]
}
