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

# Antenna downlink configuration for X-band HRD (DigIF S3 Data Delivery)
# NOAA-20 HRD: 7812 MHz center, 30 MHz bandwidth, RHCP polarization
# For S3 Data Delivery, we use antenna_downlink_config (not demod_decode).
# The data is captured as digitized RF (VITA-49 DigIF) and stored in S3 for
# offline demodulation/decoding (e.g. with MathWorks SDR or GNU Radio).
# See: https://docs.aws.amazon.com/ground-station/latest/ug/examples.pbs-to-s3.html
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
      role_arn   = var.groundstation_role_arn
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

  lifecycle {
    precondition {
      condition     = var.satellite_onboarded == true
      error_message = <<-EOT
        satellite_onboarded must be set to true before creating Ground Station resources.
        NOAA-20 (NORAD ID ${var.satellite_norad_id}) must be onboarded into this AWS account
        as a manual prerequisite — contacts cannot be scheduled for a satellite that is not
        registered. Follow the onboarding steps at:
        https://docs.aws.amazon.com/ground-station/latest/ug/getting-started.html
        Once confirmed, set satellite_onboarded = true in terraform.tfvars.
      EOT
    }
  }

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
      key   = "Polarization"
      value = "RHCP"
    },
    {
      key   = "DataFormat"
      value = "DigIF-VITA49"
    },
    {
      key   = "MinElevation"
      value = "${var.minimum_elevation_degrees}deg"
    }
  ]
}

