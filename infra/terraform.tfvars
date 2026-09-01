# Stage 1: Deploy foundation (no Ground Station access required)
# Deployment region: eu-central-1 (Frankfurt)
# Ground station contacts are scheduled cross-region via Ohio 1, Oregon 1, or Stockholm 1.
# NOAA-20 (NORAD 43013) available stations: Cape Town 1, Hawaii 1, Ohio 1, Oregon 1, Stockholm 1
# Ireland 1 does NOT support NOAA-20 — confirmed 2026-06-18.
region                     = "eu-central-1"
environment                = "demo"
project_name               = "groundstation-noaa20"
ground_station_enabled     = true
satellite_onboarded        = true # NOAA-20 (NORAD 43013) confirmed onboarded 2026-06-19
enable_processing_pipeline = false
enable_sdr_pipeline        = true
aws_profile                = "AWSAdminAccess-471112743408"
satellite_id               = "33f035e1-73f7-47a5-9df8-fbc48636dca8" # NOAA-20 UUID from list-satellites

# Default tags
objective     = "demonstrator"
owner         = "mathieu.bonnet@soprasteria.com"
creation_date = "2025-05-22"
