# Stage 1: Deploy foundation (no Ground Station access required)
# Deployment region: eu-central-1 (Frankfurt)
# Ground station contacts are scheduled cross-region via Ohio 1, Oregon 1, or Stockholm 1.
# NOAA-20 (NORAD 43013) available stations: Cape Town 1, Hawaii 1, Ohio 1, Oregon 1, Stockholm 1
# Ireland 1 does NOT support NOAA-20 — confirmed 2026-06-18.
region                     = "eu-central-1"
environment                = "demo"
project_name               = "groundstation-noaa20"
ground_station_enabled     = false
satellite_onboarded        = false # Set to true only after NOAA-20 (NORAD 43013) is onboarded in this account
enable_processing_pipeline = false
aws_profile                = "AWSAdminAccess-471112743408"

# Default tags
objective     = "demonstrator"
owner         = "mathieu.bonnet@soprasteria.com"
creation_date = "2025-05-22"
