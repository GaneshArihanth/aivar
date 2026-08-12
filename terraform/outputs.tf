output "public_ip" {
  description = "The instance's public address."
  value       = aws_instance.app.public_ip
}

output "dashboard_url" {
  description = "Where the dashboard lives once the stack is up."
  value = var.site_address == ":80" ? "http://${aws_instance.app.public_ip}/dashboard" : "https://${trimprefix(var.site_address, ":")}/dashboard"
}

output "proxy_endpoint" {
  description = "Point your agents' base_url at this."
  value = var.site_address == ":80" ? "http://${aws_instance.app.public_ip}/v1" : "https://${trimprefix(var.site_address, ":")}/v1"
}

output "instance_id" {
  description = "For `aws ssm start-session --target <id>`."
  value       = aws_instance.app.id
}

output "github_actions_role_arn" {
  description = "Set this as the AWS_DEPLOY_ROLE secret (or variable) in GitHub."
  value       = aws_iam_role.github_deploy.arn
}

output "dns_record_needed" {
  description = "If you set a domain, point it here before the first HTTPS request."
  value = var.site_address == ":80" ? "n/a — serving plain HTTP on the public IP" : "A  ${trimprefix(var.site_address, ":")}  →  ${aws_instance.app.public_ip}"
}

output "next_steps" {
  value = <<-EOT
    1. Add this to GitHub → Settings → Secrets and variables → Actions:
         AWS_DEPLOY_ROLE = ${aws_iam_role.github_deploy.arn}
         AWS_REGION      = ${var.aws_region}
    2. Push to main. The deploy workflow builds, pushes to ghcr.io and runs
       `redeploy` on the instance over SSM.
    3. Shell in with:  aws ssm start-session --target ${aws_instance.app.id}
  EOT
}
