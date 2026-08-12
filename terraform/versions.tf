terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Local state is fine for one person on one machine. For anything shared —
  # or for CI running `apply` — uncomment this and create the bucket first.
  # State contains your secrets in plaintext, so the bucket must be private
  # and encrypted.
  #
  # backend "s3" {
  #   bucket       = "your-tfstate-bucket"
  #   key          = "budget-controller/terraform.tfstate"
  #   region       = "us-east-1"
  #   encrypt      = true
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}
