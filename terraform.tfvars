# Stage 1: Deploy foundation (no Ground Station access required)
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
