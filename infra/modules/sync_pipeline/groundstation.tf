# groundstation.tf — Ground Station resources for synchronous NOAA-20 reception
#
# Fully separate from modules/mission_profile (async DigIF -> S3). This mission
# profile uses AntennaDownlinkDemodDecodeConfig (AWS demodulates + FEC-decodes
# before delivery) and a DataflowEndpointConfig pointing at a dataflow endpoint
# on the receiver EC2's ENI -- i.e. live/synchronous delivery during the
# contact, not S3RecordingConfig.
#
# DemodulationConfig / DecodeConfig JSON below is the AWS-validated JPSS-1
# config from aws-samples/aws-groundstation-s3-data-delivery
# (cfn/jpss1-gs-to-s3.yml), at this project's exact frequency (7812 MHz /
# 30 MHz / RHCP). DecodeConfig's egress node is UncodedFramesEgress: QPSK
# demod + CCSDS Viterbi 1/2 decode + NRZ-M decode, but NEITHER Reed-Solomon
# correction NOR PN derandomization -- RT-STPS performs both internally
# (confirmed from .build/rt-stps/config/jpss1.xml: frame_sync -> pn ->
# reed_solomon), which is why the receiver's RT-STPS config must run with
# PnEncoded="true" (see scripts/sync_receiver_finish.sh), the opposite of the
# existing SatDump-fed pipeline where SatDump already derandomizes upstream.

resource "awscc_groundstation_config" "tracking" {
  name = "${var.project_name}-${var.environment}-sync-tracking"

  config_data = {
    tracking_config = {
      autotrack = "PREFERRED"
    }
  }

  tags = [
    { key = "Name", value = "${var.project_name}-${var.environment}-sync-tracking" },
    { key = "Project", value = var.project_name },
    { key = "Environment", value = var.environment },
  ]
}

resource "awscc_groundstation_config" "demod_decode" {
  name = "${var.project_name}-${var.environment}-sync-demod-decode"

  config_data = {
    antenna_downlink_demod_decode_config = {
      spectrum_config = {
        bandwidth = {
          units = "MHz"
          value = var.bandwidth_mhz
        }
        center_frequency = {
          units = "MHz"
          value = var.center_frequency_mhz
        }
        polarization = "RIGHT_HAND"
      }

      demodulation_config = {
        # AWS Ground Station matches the unvalidatedJSON string with key order
        # preserved from the original AWS-validated template (AquaSnppJpss.yml,
        # SnppJpssDownlinkDemodDecodeAntennaConfig). Using templatefile() keeps
        # the original key order ("type" before "qpsk", "value" before "units",
        # "symbolRate" before "range" before "matchedFilter") which is required
        # for the profile to receive contact offers (trial 8, 2026-11-18).
        # jsonencode() was removed: it sorts object keys alphabetically, which
        # produces a string the backend rejects (0 contacts offered).
        # Reformatting this file is a functional change (guarded by the sha256 precondition below).
        unvalidated_json = templatefile("${path.module}/files/jpss1_demodulation.json.tpl", {
          center_frequency_mhz = var.center_frequency_mhz
          symbol_rate_msps     = var.symbol_rate_msps
        })
      }

      decode_config = {
        # Same rationale as demodulation_config above: verbatim AWS sample JSON
        # with "type" as the first key in each nodeConfig (not alphabetically
        # sorted). Static file -- no variable substitution needed.
        # Reformatting this file is a functional change (guarded by the sha256 precondition below).
        unvalidated_json = file("${path.module}/files/jpss1_decode.json")
      }
    }
  }

  lifecycle {
    precondition {
      condition = (
        filesha256("${path.module}/files/jpss1_demodulation.json.tpl") == "7c34e9a88efd4d4972844f003a25e24470064c03b5b83b94935bc35e2e8d3e17" &&
        filesha256("${path.module}/files/jpss1_decode.json") == "c15a81ed204acf54b2aecab13fd339e5d3753377cdf299a59921979f97b07d21"
      )
      error_message = "files/jpss1_demodulation.json.tpl or files/jpss1_decode.json changed. These are sent to AWS as exact strings; reformatting them (CRLF, key order, whitespace) is a functional change that silently brings back 0 contact offers. Verify with scripts/gs_list_sync_contacts.ps1 after apply and update the hashes in this precondition only then."
    }
  }

  tags = [
    { key = "Name", value = "${var.project_name}-${var.environment}-sync-demod-decode" },
    { key = "Frequency", value = "${var.center_frequency_mhz}MHz" },
    { key = "Bandwidth", value = "${var.bandwidth_mhz}MHz" },
    { key = "Polarization", value = "RHCP" },
    { key = "DataFormat", value = "DemodDecode-UncodedFrames" },
  ]
}

# References the endpoint named in the dataflow endpoint group below (ec2.tf).
# Ground Station will not offer any contacts for this mission profile until an
# endpoint with this exact name exists in a dataflow endpoint group.
resource "awscc_groundstation_config" "dataflow_endpoint" {
  name = "${var.project_name}-${var.environment}-sync-dataflow-endpoint"

  config_data = {
    dataflow_endpoint_config = {
      dataflow_endpoint_name = local.dataflow_endpoint_name
      # dataflowEndpointRegion is marked optional in the AWS API reference but is
      # required in practice: a DataflowEndpointConfig without it gets 0 contacts
      # offered on all stations (trial 7 Q1 vs Q2, 2026-11-18).
      dataflow_endpoint_region = data.aws_region.current.id
    }
  }

  tags = [
    { key = "Name", value = "${var.project_name}-${var.environment}-sync-dataflow-endpoint" },
  ]
}

resource "awscc_groundstation_mission_profile" "noaa20_sync" {
  name                                    = "${var.project_name}-${var.environment}-noaa20-sync"
  minimum_viable_contact_duration_seconds = var.contact_min_duration_seconds
  contact_pre_pass_duration_seconds       = var.contact_pre_pass_duration_seconds
  contact_post_pass_duration_seconds      = 120

  dataflow_edges = [
    {
      # Demod/decode configs expose multiple named egress nodes; the edge must
      # name the one to use (see DataflowEdges in the AWS sample template).
      source      = "${awscc_groundstation_config.demod_decode.arn}/UncodedFramesEgress"
      destination = awscc_groundstation_config.dataflow_endpoint.arn
    }
  ]

  tracking_config_arn = awscc_groundstation_config.tracking.arn

  lifecycle {
    precondition {
      condition     = var.satellite_onboarded == true
      error_message = <<-EOT
        satellite_onboarded must be set to true before creating Ground Station resources.
        NOAA-20 (NORAD ID ${var.satellite_norad_id}) must be onboarded into this AWS account
        as a manual prerequisite. See modules/mission_profile for the same precondition on
        the existing async pipeline.
      EOT
    }
  }

  tags = [
    { key = "Name", value = "${var.project_name}-${var.environment}-noaa20-sync" },
    { key = "Satellite", value = "NOAA-20" },
    { key = "NoradId", value = tostring(var.satellite_norad_id) },
    { key = "DataFormat", value = "DemodDecode-Synchronous" },
    { key = "Frequency", value = "${var.center_frequency_mhz}MHz" },
    { key = "Bandwidth", value = "${var.bandwidth_mhz}MHz" },
    { key = "Polarization", value = "RHCP" },
  ]
}
