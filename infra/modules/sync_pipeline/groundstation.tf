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
        unvalidated_json = jsonencode({
          type = "QPSK"
          qpsk = {
            carrierFrequencyRecovery = {
              centerFrequency = {
                value = var.center_frequency_mhz
                units = "MHz"
              }
              range = {
                value = 250
                units = "kHz"
              }
            }
            symbolTimingRecovery = {
              symbolRate = {
                value = var.symbol_rate_msps
                units = "Msps"
              }
              range = {
                value = 0.75
                units = "ksps"
              }
              matchedFilter = {
                type          = "ROOT_RAISED_COSINE"
                rolloffFactor = 0.5
              }
            }
          }
        })
      }

      decode_config = {
        unvalidated_json = jsonencode({
          edges = [
            { from = "I-Ingress", to = "IQ-Recombiner" },
            { from = "Q-Ingress", to = "IQ-Recombiner" },
            { from = "IQ-Recombiner", to = "CcsdsViterbiDecoder" },
            { from = "CcsdsViterbiDecoder", to = "NrzmDecoder" },
            { from = "NrzmDecoder", to = "UncodedFramesEgress" },
          ]
          nodeConfigs = {
            I-Ingress = {
              type                = "CODED_SYMBOLS_INGRESS"
              codedSymbolsIngress = { source = "I" }
            }
            Q-Ingress = {
              type                = "CODED_SYMBOLS_INGRESS"
              codedSymbolsIngress = { source = "Q" }
            }
            IQ-Recombiner = {
              type = "IQ_RECOMBINER"
            }
            CcsdsViterbiDecoder = {
              type                      = "CCSDS_171_133_VITERBI_DECODER"
              ccsds171133ViterbiDecoder = { codeRate = "ONE_HALF" }
            }
            NrzmDecoder = {
              type = "NRZ_M_DECODER"
            }
            UncodedFramesEgress = {
              type = "UNCODED_FRAMES_EGRESS"
            }
          }
        })
      }
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
