# budget.tf — AWS Budgets guardrail for Ground Station spend (COST-1, R9-AC1).
#
# Scope choice: account-wide (not service-scoped to "AWS Ground Station").
# A working account-wide budget is more useful than a mis-scoped budget that
# silently misses charges.  To narrow the scope to Ground Station only once the
# exact Cost Explorer service name is confirmed, uncomment the cost_filter block
# below and verify the value matches the entry shown in the Cost Explorer
# "Service" dimension for this account.
#
# SNS / KMS wiring: AWS Budgets publishes as the budgets.amazonaws.com service
# principal.  Two grants are required for the KMS-encrypted shared SNS topic
# (aws_sns_topic.contact_notifications) to accept those publishes:
#   1. SNS topic policy  — AllowBudgetsPublish statement (security/main.tf)
#   2. KMS key policy    — AllowBudgetsEncryption statement (security/main.tf)
# Without both, budget notifications silently fail to reach the topic.
# Both are added alongside the existing CloudWatch grants in main.tf rather than
# here, so the full policy stays in one place per resource.

resource "aws_budgets_budget" "ground_station" {
  name       = "${var.project_name}-${var.environment}-monthly"
  account_id = data.aws_caller_identity.current.account_id

  budget_type  = "COST"
  time_unit    = "MONTHLY"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"

  # Account-wide scope (see file header).  Uncomment to narrow to Ground Station:
  # cost_filter {
  #   name   = "Service"
  #   values = ["AWS Ground Station"]
  #   # Verify the exact string in Cost Explorer > Costs by Service before enabling.
  # }

  # 80 % of budgeted — early warning before a contact over-run becomes a surprise.
  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 80
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.contact_notifications.arn]
  }

  # 100 % of budgeted — ceiling breached (actual spend).
  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.contact_notifications.arn]
  }

  # 100 % of budgeted — forecasted to breach (gives lead time before actual).
  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "FORECASTED"
    subscriber_sns_topic_arns = [aws_sns_topic.contact_notifications.arn]
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-${var.environment}-monthly-budget"
  })
}
