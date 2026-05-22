output "bucket_name" {
  description = "Name of the reception S3 bucket"
  value       = aws_s3_bucket.reception.id
}

output "bucket_arn" {
  description = "ARN of the reception S3 bucket"
  value       = aws_s3_bucket.reception.arn
}

output "bucket_id" {
  description = "ID of the reception S3 bucket"
  value       = aws_s3_bucket.reception.id
}

output "logging_bucket_name" {
  description = "Name of the access logging S3 bucket"
  value       = aws_s3_bucket.logging.id
}
