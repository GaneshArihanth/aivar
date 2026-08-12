/*
 * Agent Budget Controller — free-tier AWS footprint.
 *
 * Deliberately absent, because each is a classic surprise on a "free" account:
 *
 *   · NAT Gateway   ~$32/month. The instance sits in a public subnet with an
 *                   internet gateway instead. Nothing here needs egress from a
 *                   private subnet.
 *   · Load balancer ~$16/month. Caddy on the instance terminates TLS.
 *   · RDS / ElastiCache — separate free-tier allowances that may not apply to
 *                   newer accounts. PostgreSQL and Redis run in containers.
 *   · ECR           the image lives in GitHub Container Registry, which is free
 *                   for public repositories, so no AWS storage is consumed.
 *
 * What does cost money, even on the free tier:
 *   · the public IPv4 address (~$3.60/month) unless your account still has the
 *     12-month allowance. There is no way to have a public endpoint without it.
 *   · EBS beyond 30 GB, and data transfer beyond 100 GB/month out.
 */

data "aws_region" "current" {}

# Amazon Linux 2023, resolved at plan time so the AMI id is never hard-coded.
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64"
}

# ------------------------------------------------------------------ network

resource "aws_vpc" "main" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.tags, { Name = "${var.project}-vpc" })
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.tags, { Name = "${var.project}-igw" })
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.20.1.0/24"
  map_public_ip_on_launch = true
  availability_zone       = "${data.aws_region.current.name}a"
  tags                    = merge(local.tags, { Name = "${var.project}-public" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = merge(local.tags, { Name = "${var.project}-public-rt" })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ------------------------------------------------------------------ security

resource "aws_security_group" "instance" {
  name        = "${var.project}-sg"
  description = "Public HTTP/HTTPS only; no SSH - administration goes via SSM."
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP (also serves the ACME challenge)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }

  # No port 22 rule anywhere. Shell access is via SSM Session Manager, which
  # needs no inbound port, no key pair and no bastion, and is audited by
  # CloudTrail.

  egress {
    description = "Pull images, reach providers, talk to SSM"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${var.project}-sg" })
}

# --------------------------------------------------------------------- IAM

resource "aws_iam_role" "instance" {
  name = "${var.project}-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.tags
}

# Session Manager: shell access and remote command execution without SSH.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Read only the parameters belonging to this project, and decrypt them.
resource "aws_iam_role_policy" "read_secrets" {
  name = "${var.project}-read-secrets"
  role = aws_iam_role.instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
        Resource = [
          "arn:aws:ssm:${data.aws_region.current.name}:*:parameter/${var.project}",
          "arn:aws:ssm:${data.aws_region.current.name}:*:parameter/${var.project}/*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = "ssm.${data.aws_region.current.name}.amazonaws.com" }
        }
      }
    ]
  })
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.project}-instance"
  role = aws_iam_role.instance.name
  tags = local.tags
}

# ------------------------------------------------------------------ secrets
#
# Standard SSM parameters are free. Values are supplied by Terraform variables
# and marked sensitive, so they are never printed in plan output — but they DO
# land in Terraform state, so keep state private (see the S3 backend note in
# DEPLOY.md).

resource "aws_ssm_parameter" "pepper" {
  name        = "/${var.project}/API_KEY_PEPPER"
  description = "HMAC pepper for agent API keys. Changing it invalidates every key."
  type        = "SecureString"
  value       = var.api_key_pepper
  tags        = local.tags
}

resource "aws_ssm_parameter" "postgres_password" {
  name  = "/${var.project}/POSTGRES_PASSWORD"
  type  = "SecureString"
  value = var.postgres_password
  tags  = local.tags
}

resource "aws_ssm_parameter" "provider_keys" {
  for_each = {
    OPENAI_API_KEY    = var.openai_api_key
    ANTHROPIC_API_KEY = var.anthropic_api_key
    GEMINI_API_KEY    = var.gemini_api_key
  }

  name  = "/${var.project}/${each.key}"
  type  = "SecureString"
  value = each.value == "" ? "unset" : each.value
  tags  = local.tags
}

# Non-secret runtime configuration, so a deploy can change them without a
# rebuild.
resource "aws_ssm_parameter" "config" {
  for_each = {
    SITE_ADDRESS   = var.site_address
    UPSTREAM_MODE  = var.upstream_mode
    ACME_EMAIL     = var.acme_email
  }

  name      = "/${var.project}/${each.key}"
  type      = "String"
  value     = each.value
  overwrite = true
  tags      = local.tags
}

# IMAGE is deliberately separate. Terraform seeds it once so the very first
# boot has something to pull, and then stops managing its value — the deploy
# workflow owns it from that point, writing a commit-pinned tag on every push.
#
# Without ignore_changes the two fight: the next `terraform apply` would reset
# this to var.image and silently roll production back to whatever tag was
# hard-coded in the tfvars.
resource "aws_ssm_parameter" "image" {
  name      = "/${var.project}/IMAGE"
  type      = "String"
  value     = var.image
  overwrite = true
  tags      = local.tags

  lifecycle {
    ignore_changes = [value]
  }
}

# The compose file and the Caddyfile, shipped through SSM rather than baked
# into user_data.
#
# user_data runs exactly once, on first boot — cloud-init's scripts-user module
# is per-instance, so it does not re-run on stop/start. Baking these files in
# therefore made them unchangeable in practice: editing the Caddyfile changed
# user_data, which forced a stop/start (AWS requires a stopped instance to
# modify user_data) costing downtime and a new public IP, and *still* left the
# old file on disk, because nothing re-ran the script that writes it. The only
# way to apply a config change was to copy it over by hand.
#
# Through SSM instead: `terraform apply` updates the parameter, the next deploy
# writes it out and restarts. No bounce, no hand-copying. Nested one level
# under /files/ so the non-recursive get-parameters-by-path in fetch-env keeps
# ignoring them — they are files, not environment variables.
resource "aws_ssm_parameter" "files" {
  for_each = {
    compose = "${path.module}/../deploy/docker-compose.prod.yml"
    caddy   = "${path.module}/../deploy/Caddyfile"
  }

  name = "/${var.project}/files/${each.key}"
  type = "String"
  # gzip + base64, because the free Standard tier caps a value at 4 KB and the
  # compose file is already 3.4 KB — one more service would silently push it
  # over, and the Advanced tier that lifts the cap is $0.05/parameter/month,
  # which is a strange thing to start paying on a free-tier deployment.
  # Compressed it is well under a kilobyte. The cost is that the value is no
  # longer readable in the SSM console; `redeploy` decodes it on the way out.
  value     = base64gzip(file(each.value))
  overwrite = true
  tags      = local.tags
}

# ----------------------------------------------------------------- instance

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.instance.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

  # IMDSv2 only: a stolen SSRF on the app cannot read instance credentials with
  # a plain GET.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_size           = var.volume_size_gb
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  # Deliberately only the two values that identify *which* deployment this is.
  # Everything that changes over the life of the instance — secrets, config,
  # the compose file, the Caddyfile — comes from SSM at deploy time instead, so
  # this string stays constant and the instance stops being bounced by ordinary
  # edits. See aws_ssm_parameter.files for why.
  user_data = templatefile("${path.module}/user_data.sh", {
    project = var.project
    region  = data.aws_region.current.name
  })

  # Changing user_data on a running instance requires a stop/start, not a
  # replacement — a replacement would take the database with it, since
  # PostgreSQL's volume is this instance's root volume. Note that a stop/start
  # still costs a new public IP unless an Elastic IP or a domain is in play.
  user_data_replace_on_change = false

  tags = merge(local.tags, { Name = "${var.project}-app" })
}

locals {
  tags = {
    Project   = var.project
    ManagedBy = "terraform"
  }
}
