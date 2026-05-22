data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Tracking configuration with autotrack
resource "awscc_groundstation_config" "tracking" {
  name = "${var.project_name}-${var.environment}-tracking"

  config_data = {
    tracking_config = {
      autotrack = "PREFERRED"
    }
  }

  tags = [
    {
      key   = "Name"
      value = "${var.project_name}-${var.environment}-tracking"
    },
    {
      key   = "Project"
      value = var.project_name
    },
    {
      key   = "Environment"
      value = var.environment
    }
  ]
}

# Antenna downlink configuration for X-band HRD
# NOAA-20 HRD: 7812 MHz center, 30 MHz bandwidth, QPSK modulation, RHCP polarization
resource "awscc_groundstation_config" "antenna_downlink" {
  name = "${var.project_name}-${var.environment}-antenna-downlink"

  config_data = {
    antenna_downlink_config = {
      spectrum_config = {
        bandwidth = {
          units = "MHz"
          value = 30
        }
        center_frequency = {
          units = "MHz"
          value = 7812
        }
        polarization = "RIGHT_HAND"
      }
    }
  }

  tags = [
    {
      key   = "Name"
      value = "${var.project_name}-${var.environment}-antenna-downlink"
    },
    {
      key   = "Frequency"
      value = "7812MHz"
    },
    {
      key   = "Bandwidth"
      value = "30MHz"
    },
    {
      key   = "Modulation"
      value = "QPSK"
    },
    {
      key   = "Polarization"
      value = "RHCP"
    }
  ]
}

# S3 recording configuration for data delivery
resource "awscc_groundstation_config" "s3_recording" {
  name = "${var.project_name}-${var.environment}-s3-recording"

  config_data = {
    s3_recording_config = {
      bucket_arn = var.reception_bucket_arn
      prefix     = "year={year}/month={month}/day={day}/satellite={satellite_id}"
      role_arn   = aws_iam_role.groundstation_delivery.arn
    }
  }

  tags = [
    {
      key   = "Name"
      value = "${var.project_name}-${var.environment}-s3-recording"
    },
    {
      key   = "Project"
      value = var.project_name
    }
  ]
}

# Ground Station Mission Profile for NOAA-20 HRD X-band
resource "awscc_groundstation_mission_profile" "noaa20_hrd" {
  name                                    = "${var.project_name}-${var.environment}-noaa20-hrd"
  minimum_viable_contact_duration_seconds = var.contact_min_duration_seconds
  contact_pre_pass_duration_seconds       = 120
  contact_post_pass_duration_seconds      = 120

  dataflow_edges = [
    {
      source      = awscc_groundstation_config.antenna_downlink.arn
      destination = awscc_groundstation_config.s3_recording.arn
    }
  ]

  tracking_config_arn = awscc_groundstation_config.tracking.arn

  tags = [
    {
      key   = "Name"
      value = "${var.project_name}-${var.environment}-noaa20-hrd"
    },
    {
      key   = "Satellite"
      value = "NOAA-20"
    },
    {
      key   = "NoradId"
      value = tostring(var.satellite_norad_id)
    },
    {
      key   = "Frequency"
      value = "7812MHz"
    },
    {
      key   = "Bandwidth"
      value = "30MHz"
    },
    {
      key   = "Modulation"
      value = "QPSK"
    },
    {
      key   = "Polarization"
      value = "RHCP"
    },
    {
      key   = "MinElevation"
      value = "${var.minimum_elevation_degrees}deg"
    }
  ]
}

# IAM role for Ground Station to deliver data to S3
resource "aws_iam_role" "groundstation_delivery" {
  name = "${var.project_name}-${var.environment}-gs-delivery-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "groundstation.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-gs-delivery-role"
  })
}

resource "aws_iam_role_policy" "groundstation_delivery" {
  name = "${var.project_name}-${var.environment}-gs-delivery-policy"
  role = aws_iam_role.groundstation_delivery.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetBucketLocation"
        ]
        Resource = [
          var.reception_bucket_arn,
          "${var.reception_bucket_arn}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "kms:Encrypt",
          "kms:GenerateDataKey*"
        ]
        Resource = [
          var.kms_key_arn
        ]
      }
    ]
  })
}
