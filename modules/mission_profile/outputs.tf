output "mission_profile_arn" {
  description = "ARN of the Ground Station mission profile"
  value       = awscc_groundstation_mission_profile.noaa20_hrd.arn
}

output "mission_profile_id" {
  description = "ID of the Ground Station mission profile"
  value       = awscc_groundstation_mission_profile.noaa20_hrd.id
}
