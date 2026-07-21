# ec2.tf — EC2 aggregation instance, IAM instance profile, and security group
# Implements Requirements 1 and 2 of the ec2-aggregation spec.
#
# The instance is launched in stopped state. CSPP SDR, RT-STPS, and the
# SDR_4_1_DB LUT database are installed manually after first launch.
# SSM Agent (pre-installed on Amazon Linux 2023) is used for Run Command --
# no SSH keys and no inbound security group rules are needed.

###############################################################################
# AMI -- Amazon Linux 2023 (latest, deployment region)
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

###############################################################################
# Networking -- resolve default VPC and first available subnet
###############################################################################

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

###############################################################################
# Security Group -- outbound HTTPS only (SSM uses port 443 outbound)
###############################################################################

resource "aws_security_group" "aggregation_ec2" {
  name        = "${var.project_name}-aggregation-ec2"
  description = "EC2 aggregation instance -- no inbound rules; SSM outbound HTTPS only"
  vpc_id      = data.aws_vpc.default.id

  # No ingress rules -- SSM Run Command does not require inbound access

  egress {
    description = "HTTPS outbound for SSM and S3 access"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name    = "${var.project_name}-aggregation-ec2"
    Service = "sdr-pipeline"
  })
}

###############################################################################
# IAM Role and Instance Profile
###############################################################################

resource "aws_iam_role" "aggregation_ec2" {
  name        = "${var.project_name}-aggregation-ec2"
  description = "Allows the aggregation EC2 instance to access S3, KMS, and SSM"

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
    Name    = "${var.project_name}-aggregation-ec2"
    Service = "sdr-pipeline"
  })
}

# S3 read/write on the SDR output bucket + KMS access
resource "aws_iam_role_policy" "aggregation_ec2" {
  name = "${var.project_name}-aggregation-ec2-policy"
  role = aws_iam_role.aggregation_ec2.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadWriteOutputBucket"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = "${aws_s3_bucket.sdr_output.arn}/*"
      },
      {
        Sid      = "S3ListOutputBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.sdr_output.arn
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
      {
        Sid    = "ECRPull"
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
        ]
        Resource = "*"
      },
      {
        Sid    = "ECRPullImage"
        Effect = "Allow"
        Action = [
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
        ]
        Resource = aws_ecr_repository.sdr_pipeline.arn
      },
    ]
  })
}

# AmazonSSMManagedInstanceCore -- required for SSM Run Command and Session Manager
resource "aws_iam_role_policy_attachment" "aggregation_ec2_ssm" {
  role       = aws_iam_role.aggregation_ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "aggregation_ec2" {
  name = "${var.project_name}-aggregation-ec2"
  role = aws_iam_role.aggregation_ec2.name

  tags = merge(var.tags, {
    Name    = "${var.project_name}-aggregation-ec2"
    Service = "sdr-pipeline"
  })
}

###############################################################################
# EC2 Instance -- launched in stopped state
###############################################################################

# checkov:skip=CKV_AWS_8: No user data required -- CSPP SDR and RT-STPS are
# installed manually on the EBS volume after the first launch.
resource "aws_instance" "aggregation" {
  ami                    = data.aws_ami.amazon_linux_2023.id
  instance_type          = "r6i.xlarge"

  root_block_device {
    volume_size = 300
    volume_type = "gp3"
  }
  subnet_id              = data.aws_subnets.default.ids[0]
  vpc_security_group_ids = [aws_security_group.aggregation_ec2.id]
  iam_instance_profile   = aws_iam_instance_profile.aggregation_ec2.name

  # Launch in stopped state -- started on-demand by the Trigger Lambda via
  # ec2:StartInstances. Terraform does not natively manage stopped state;
  # the instance is never running during terraform apply.

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 100
    encrypted             = true
    kms_key_id            = var.kms_key_arn
    delete_on_termination = true

    tags = merge(var.tags, {
      Name    = "${var.project_name}-aggregation-root"
      Service = "sdr-pipeline"
    })
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    http_endpoint               = "enabled"
  }

  monitoring = true

  tags = merge(var.tags, {
    Name    = "${var.project_name}-aggregation"
    Service = "sdr-pipeline"
  })

  lifecycle {
    # Prevent Terraform from replacing the instance on AMI updates -- the EBS
    # volume with SDR_4_1_DB must be preserved.
    ignore_changes = [ami]
  }
}
