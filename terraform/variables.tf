variable "project" {
  description = "Name prefix for every resource, and the SSM parameter namespace."
  type        = string
  default     = "budget-controller"
}

variable "aws_region" {
  description = "Region to deploy into. us-east-1 is usually the cheapest."
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = <<-EOT
    t3.micro is the free-tier size in most regions (t2.micro in a few older
    ones). 1 GB of RAM is tight for five containers, so user_data adds 2 GB of
    swap. Step up to t3.small if you see the OOM killer in `dmesg`.
  EOT
  type        = string
  default     = "t3.micro"
}

variable "volume_size_gb" {
  description = "Root volume. The free tier covers 30 GB of gp3."
  type        = number
  default     = 20
}

variable "allowed_cidrs" {
  description = <<-EOT
    Who may reach ports 80/443. Leave open if agents call in from anywhere;
    narrow to your office/VPN CIDRs if not. There is no authentication in front
    of the dashboard or the admin API, so this list is the only thing that
    limits who can reach them.
  EOT
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "image" {
  description = "Container image to run, e.g. ghcr.io/you/agent-budget-controller:latest"
  type        = string
}

variable "site_address" {
  description = <<-EOT
    A domain (budget.example.com) gives automatic HTTPS via Let's Encrypt.
    Leave as ":80" to serve plain HTTP on the instance's public IP — acceptable
    for a private demo, not for anything carrying real API keys.
  EOT
  type        = string
  default     = ":80"
}

variable "acme_email" {
  description = "Contact address for Let's Encrypt. Only used with a real domain."
  type        = string
  default     = "admin@example.com"
}

variable "api_key_pepper" {
  description = <<-EOT
    HMAC pepper for agent API keys. Generate with:
      python -c "import secrets; print(secrets.token_hex(32))"
    Changing it invalidates every issued agent key, so set it once and keep it.
  EOT
  type        = string
  sensitive   = true
}

variable "postgres_password" {
  description = "Password for the in-container PostgreSQL role."
  type        = string
  sensitive   = true
}

variable "upstream_mode" {
  description = <<-EOT
    "mock" sends every call to the bundled fake provider — free, and the whole
    demo works. "live" dispatches to each model's real endpoint using the keys
    below, and spends real money.
  EOT
  type        = string
  default     = "mock"

  validation {
    condition     = contains(["mock", "live"], var.upstream_mode)
    error_message = "upstream_mode must be \"mock\" or \"live\"."
  }
}

variable "openai_api_key" {
  description = "Only consulted when upstream_mode = live."
  type        = string
  sensitive   = true
  default     = ""
}

variable "anthropic_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "gemini_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "github_repository" {
  description = <<-EOT
    owner/repo, used to scope the GitHub Actions OIDC trust policy so only
    workflows in this repository can assume the deploy role.
  EOT
  type        = string
}

variable "create_oidc_provider" {
  description = <<-EOT
    Set false if this AWS account already has the GitHub OIDC provider — it is
    account-wide and can only exist once.
  EOT
  type        = bool
  default     = true
}
