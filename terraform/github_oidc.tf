/*
 * GitHub Actions → AWS, without storing credentials.
 *
 * The workflow exchanges a short-lived GitHub OIDC token for temporary AWS
 * credentials. Nothing long-lived is stored in the repository: there is no
 * AWS_SECRET_ACCESS_KEY to leak, rotate, or find in a fork's logs.
 *
 * The trust policy is scoped to this repository specifically. Without the
 * `sub` condition, *any* GitHub repository in the world could assume this role.
 */

data "aws_caller_identity" "current" {}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  # GitHub's OIDC endpoint uses a well-known CA; AWS still requires a thumbprint.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1", "1c58a3a8518e8759bf075b76b750d4f2df264fcd", "1b511abead59c6ce207077c0bf0e0043b1382612"]

  tags = local.tags
}

locals {
  oidc_provider_arn = var.create_oidc_provider ? one(aws_iam_openid_connect_provider.github[*].arn) : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
}

resource "aws_iam_role" "github_deploy" {
  name = "${var.project}-github-deploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        # Only workflows in this repository, on any branch or tag.
        StringLike = {
          "token.actions.githubusercontent.com:sub" = ["repo:${var.github_repository}:*", "repo:${lower(var.github_repository)}:*"]
        }
      }
    }]
  })

  tags = local.tags
}

# The deploy workflow needs to do exactly two things: find the instance, and
# tell it to redeploy. Nothing broader.
resource "aws_iam_role_policy" "github_deploy" {
  name = "${var.project}-deploy"
  role = aws_iam_role.github_deploy.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "FindTheInstance"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Sid      = "RunTheRedeployDocument"
        Effect   = "Allow"
        Action   = ["ssm:SendCommand"]
        Resource = [
          "arn:aws:ssm:${var.aws_region}::document/AWS-RunShellScript",
          "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/*"
        ]
        Condition = {
          StringEquals = { "ssm:resourceTag/Project" = var.project }
        }
      },
      {
        Sid      = "ReadBackTheResult"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
        Resource = "*"
      }
    ]
  })
}
