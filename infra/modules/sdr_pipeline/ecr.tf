resource "aws_ecr_repository" "sdr_pipeline" {
  name                 = "${var.project_name}-sdr-pipeline"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }

  tags = merge(var.tags, {
    Name    = "${var.project_name}-sdr-pipeline"
    Service = "sdr-pipeline"
  })
}

resource "aws_ecr_lifecycle_policy" "sdr_pipeline" {
  repository = aws_ecr_repository.sdr_pipeline.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep only the last 5 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 5
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
