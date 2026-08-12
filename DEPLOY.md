# Deploying to AWS

A step-by-step guide to running the Agent Budget Controller on AWS, deployed
from GitHub, provisioned with Terraform.

Every step ends with a **check** — run it before moving on. Infrastructure
failures compound, and finding out at step 9 that step 3 was wrong is the
expensive way to do this.

> **None of this has been applied against a real AWS account from this
> machine.** The Terraform, workflows and compose file are written and
> syntax-checked, but `terraform`, `docker` and the `aws` CLI are not installed
> here, so nothing was `plan`ned, built or deployed. Treat the first
> `terraform plan` as the real review, and read it.

---

## What you are building

```
                 GitHub                          AWS (one region)
   ┌──────────────────────────────┐   ┌──────────────────────────────────────┐
   │ push to main                 │   │  VPC 10.20.0.0/16                    │
   │   │                          │   │   └─ public subnet + internet gateway│
   │   ├─ CI: 108 tests           │   │        │                             │
   │   ├─ build image             │   │        └─ EC2 t3.micro               │
   │   ├─ push → ghcr.io ─────────┼───┼──────────▶ docker compose:           │
   │   └─ SSM SendCommand ────────┼───┼──────────▶  caddy → proxy → mock     │
   │      (OIDC, no stored keys)  │   │              postgres  redis         │
   └──────────────────────────────┘   └──────────────────────────────────────┘
```

**One instance, on purpose.** RDS and ElastiCache have their own free-tier
allowances, but that is three services to keep inside three separate limits,
and the managed-service tiers are the ones most likely to have lapsed on a
newer account. One instance is one allowance and one thing to reason about.

### Deliberately absent

| Not used | Why |
|---|---|
| NAT Gateway | ~$32/month. The instance sits in a public subnet instead. This is the single most common surprise on a "free" AWS bill. |
| Load balancer | ~$16/month. Caddy on the instance terminates TLS. |
| RDS / ElastiCache | Separate allowances; containers instead. |
| ECR | Images live in GitHub Container Registry — free for public repos. |
| SSH / port 22 | Shell access is via SSM Session Manager: no open port, no key pair, no bastion, and audited in CloudTrail. |

### What this actually costs

**Check current pricing yourself.** AWS changed its free tier in July 2025:
accounts created after that get time-limited credits rather than the old
12-month allowances. The figures below are indicative, not a quote.

| Item | Free-tier allowance | If you exceed it |
|---|---|---|
| EC2 t3.micro | 750 h/month (12 mo) — one instance running 24/7 | ~$7.50/month |
| EBS gp3 20 GB | 30 GB (12 mo) | ~$1.60/month |
| Public IPv4 | 750 h/month (12 mo) | ~$3.60/month |
| Data transfer out | 100 GB/month | $0.09/GB |
| SSM Parameter Store (standard) | Always free | — |
| CloudWatch logs | 5 GB (always free) | $0.50/GB |

Realistic worst case if every allowance has lapsed: **roughly $13–15/month.**
Set the billing alarm in step 8 regardless.

---

## Prerequisites

- An **AWS account** with billing set up.
- A **GitHub account**, and this project pushed to a repository.
- Locally: `git`, `terraform` ≥ 1.6, the `aws` CLI v2. Docker is optional —
  GitHub Actions builds the image.

```bash
brew install terraform awscli gh    # macOS
```

**Check**

```bash
terraform version && aws --version && git --version
```

---

## Step 1 — Get the code into GitHub

This project is not yet a git repository.

```bash
cd "/Users/ganesharihanth/Personal/IDE Editor/aivar"

git init -b main
git add .
git status --short | grep -iE '\.env$|tfvars$|tfstate' && echo "STOP: a secret is staged" || echo "clean"
git commit -m "Agent Budget Controller"
```

Create the repository and push:

```bash
gh repo create aivar --public --source=. --push
# or: git remote add origin git@github.com:YOU/aivar.git && git push -u origin main
```

**Public vs private:** ghcr.io is free and unlimited for **public** images. A
private repo means private packages, which have a storage quota. Use public
unless you have a reason not to — nothing secret is in the image, and the
`.dockerignore` excludes `.env`.

**Check** — `.env`, `terraform.tfvars` and `*.tfstate` must be absent from
GitHub:

```bash
git ls-files | grep -E '\.env$|tfvars$|tfstate' && echo "PROBLEM" || echo "no secrets tracked"
```

---

## Step 2 — Credentials for Terraform

Terraform needs to create IAM roles and EC2 instances, so it needs
administrative access *once*. GitHub Actions will not use these — it gets a
scoped role via OIDC in step 5.

In the AWS console → IAM → Users → create a user (e.g. `terraform-admin`),
attach `AdministratorAccess`, and create an access key of type *Command Line
Interface*.

```bash
aws configure --profile budget-tf
export AWS_PROFILE=budget-tf
```

**Check**

```bash
aws sts get-caller-identity
```

You should see your account id. If this fails, nothing after it will work.

> Delete this access key when you are done provisioning. Day-to-day deploys do
> not need it.

---

## Step 3 — Generate the secrets

Three values, generated once.

```bash
# 1. API key pepper — NEVER change this later; it invalidates every agent key.
python3 -c "import secrets; print('api_key_pepper     =', repr(secrets.token_hex(32)))"

# 2. PostgreSQL password
python3 -c "import secrets; print('postgres_password  =', repr(secrets.token_urlsafe(24)))"

# 3. Dashboard password → bcrypt hash. Pick a real password first.
docker run --rm caddy:2-alpine caddy hash-password --plaintext 'your-strong-password'
```

No Docker locally? Generate the bcrypt hash in Python:

```bash
.venv/bin/pip install bcrypt -q
.venv/bin/python -c "import bcrypt; print(bcrypt.hashpw(b'your-strong-password', bcrypt.gensalt(rounds=14)).decode())"
```

### Why a dashboard password at all

The app's own `ADMIN_TOKEN` cannot be used here: the dashboard's JavaScript
never sends one, so switching it on would lock you out of your own UI. Leaving
it off on a public address would let **anyone freeze the fleet, grant budget or
delete agents** — `/admin/*` is unauthenticated by design for local use.

So authentication happens at the edge. Caddy puts basic auth in front of the
dashboard and `/admin/*`, and leaves two paths open:

- `/v1/chat/completions` — agents authenticate with their own `X-Agent-Key`.
- `/health` — so the deploy workflow can verify without credentials.

---

## Step 4 — Configure Terraform

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` with the three secrets, your `github_repository`
(`owner/repo`) and your `image` (`ghcr.io/owner/repo:latest`, lowercase).

Leave `site_address = ":80"` for now — HTTPS needs a domain pointing at an IP
that does not exist yet. Step 10 covers it.

**Check**

```bash
terraform init
terraform validate
terraform plan
```

Read the plan. It should create roughly **19 resources** and no `aws_nat_gateway`,
no `aws_lb`, no `aws_db_instance`:

```bash
terraform plan -no-color | grep -E "will be created" | wc -l
terraform plan -no-color | grep -E "nat_gateway|aws_lb|db_instance" && echo "STOP — that costs money" || echo "no expensive resources"
```

---

## Step 5 — Provision

```bash
terraform apply
```

Takes 2–3 minutes. Then:

```bash
terraform output
```

Note `public_ip`, `instance_id` and `github_actions_role_arn`.

**Check** — the instance must register with SSM before anything can deploy to
it. This takes a minute or two after boot:

```bash
aws ssm describe-instance-information \
  --query 'InstanceInformationList[].{Id:InstanceId,Ping:PingStatus}' --output table
```

Wait for `Online`. If it never appears, the instance cannot reach the SSM
endpoints — check that the subnet auto-assigns public IPs and the route table
has a default route to the internet gateway.

Watch the bootstrap finish (it installs Docker and writes the deploy scripts):

```bash
aws ssm start-session --target $(terraform output -raw instance_id)
# then, on the instance:
sudo tail -f /var/log/cloud-init-output.log
ls -l /var/lib/cloud/instance/bootstrap-complete   # exists when done
exit
```

---

## Step 6 — Let GitHub deploy

Terraform created a role GitHub can assume via OIDC. **No AWS keys are stored
in GitHub** — the workflow exchanges a short-lived GitHub token for temporary
AWS credentials, and the trust policy is scoped to your repository specifically.

```bash
gh secret set AWS_DEPLOY_ROLE --body "$(terraform output -raw github_actions_role_arn)"
gh secret set AWS_REGION      --body "us-east-1"
```

Or via the web UI: **Settings → Secrets and variables → Actions → New
repository secret**.

**Check**

```bash
gh secret list
```

---

## Step 7 — First deploy

```bash
cd ..
git commit --allow-empty -m "trigger deploy" && git push
```

Watch it:

```bash
gh run watch
```

The workflow builds the image, pushes it to ghcr.io, writes the commit-pinned
tag to SSM, runs `/usr/local/bin/redeploy` on the instance, then polls `/health`
until it reports `ok`.

**Check**

```bash
curl -s "http://$(cd terraform && terraform output -raw public_ip)/health"
```

Expect `{"status":"ok","redis":"ok","database":"ok",...}`.

> If the image is private, the instance cannot pull it. Either make the package
> public (**GitHub → your profile → Packages → package → Package settings →
> Change visibility**), or add a `docker login ghcr.io` with a read token to
> `redeploy`.

---

## Step 8 — Set a billing alarm before you forget

The single most valuable ten minutes in this guide.

1. Console → **Billing → Billing preferences** → enable *Receive Free Tier
   Alerts* and *Receive Billing Alerts*.
2. Console → **CloudWatch** (in **us-east-1**, where billing metrics live) →
   Alarms → Create alarm → *Billing → Total Estimated Charge* → threshold `5`
   USD → notify your email.
3. Confirm the SNS subscription email.

```bash
aws cloudwatch describe-alarms --alarm-name-prefix billing \
  --query 'MetricAlarms[].{Name:AlarmName,Threshold:Threshold}' --output table
```

---

## Step 9 — Seed it and get an agent key

The database is migrated but empty. Seed the demo fleet:

```bash
aws ssm start-session --target $(cd terraform && terraform output -raw instance_id)

cd /opt/app
sudo docker compose -f docker-compose.prod.yml exec proxy python -m scripts.seed
```

This prints twelve API keys **once**. Copy them now — they are stored only as
HMACs and cannot be recovered; you would have to rotate.

Open the dashboard at `http://<public_ip>/dashboard` and log in with the
username and password from step 3.

**Check** — point an agent at it:

```bash
curl "http://<public_ip>/v1/chat/completions" \
  -H "X-Agent-Key: sk-agent-..." \
  -H "X-Session-Id: smoke-test" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hello"}],"max_tokens":100}' -i
```

You should get a `200` with `X-Budget-Cost-USD` and `X-Budget-Agent-Remaining-USD`
headers, and the call should appear on the dashboard within a few seconds.

---

## Step 10 — HTTPS with a domain (recommended)

Plain HTTP sends your dashboard password and agent keys in the clear. If this
is anything more than a private demo, put a domain in front.

1. Create an **A record** pointing at the instance IP:

   ```bash
   cd terraform && terraform output dns_record_needed
   ```

2. Wait for DNS to propagate (`dig +short budget.example.com`).

3. Set the domain and re-apply:

   ```hcl
   # terraform.tfvars
   site_address = "budget.example.com"
   acme_email   = "you@example.com"
   ```

   ```bash
   terraform apply
   ```

4. Push any commit to trigger a redeploy so Caddy picks up the new
   `SITE_ADDRESS` from SSM. Caddy then obtains a Let's Encrypt certificate
   automatically on first request.

**Check**

```bash
curl -sI https://budget.example.com/health | head -1
```

> Port 80 must stay open — Let's Encrypt uses it for the ACME challenge.

---

## Going live against real providers

The deployment runs against the bundled mock provider by default, so the whole
demo works and costs nothing. To dispatch to real APIs:

```hcl
# terraform.tfvars
upstream_mode  = "live"
openai_api_key = "sk-..."
```

```bash
terraform apply     # writes the keys to SSM
git commit --allow-empty -m "go live" && git push
```

This is deliberately explicit. The app never switches to real endpoints just
because a key happens to be present in the environment — a tool built to
prevent surprise spend should not create any.

Before you do it, set real budgets. `make seed` creates a demo fleet with
generous limits.

---

## Operating it

```bash
cd terraform
INSTANCE=$(terraform output -raw instance_id)

# Shell on the box (no SSH, no key pair)
aws ssm start-session --target $INSTANCE

# On the instance:
cd /opt/app
sudo docker compose -f docker-compose.prod.yml ps
sudo docker compose -f docker-compose.prod.yml logs -f proxy
sudo /usr/local/bin/redeploy          # pull + restart + migrate
sudo /usr/local/bin/fetch-env         # re-read secrets from SSM

# Reconciliation — Redis counters vs the PostgreSQL ledger
sudo docker compose -f docker-compose.prod.yml exec proxy python -m scripts.reconcile

# Memory, which is the thing to watch on 1 GB
free -h && sudo docker stats --no-stream
```

### Redeploy without pushing code

```bash
aws ssm send-command --instance-ids $INSTANCE \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["/usr/local/bin/redeploy"]'
```

### Back up the database

Nothing here backs up automatically. The ledger is the audit record — losing it
loses your spend history.

```bash
sudo docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U budget budget_controller | gzip > /tmp/backup.sql.gz
```

Copy it off the instance (S3, or `aws ssm start-session` port forwarding).

### Tear it all down

```bash
cd terraform && terraform destroy
```

Removes everything Terraform created. It does **not** remove the ghcr.io image
or the GitHub secrets.

**Check** the bill is actually going to stop:

```bash
aws ec2 describe-instances \
  --filters "Name=tag:Project,Values=budget-controller" \
  --query 'Reservations[].Instances[].State.Name' --output text
aws ec2 describe-addresses --query 'Addresses[].PublicIp' --output text   # should be empty
```

---

## Troubleshooting

**`terraform apply` fails on the OIDC provider already existing.** It is
account-wide and can only exist once. Set `create_oidc_provider = false`.

**The deploy workflow cannot find the instance.** The filter is
`tag:Project = budget-controller`. If you changed `project` in tfvars, change
`PROJECT` at the top of `.github/workflows/deploy.yml` to match.

**Deploy succeeds but `/health` never goes green.** Shell in and look:
`sudo docker compose -f docker-compose.prod.yml logs proxy`. The usual causes
are a bad `POSTGRES_PASSWORD` in SSM, or PostgreSQL still starting — the proxy
waits for its healthcheck, so give it a minute.

**Containers being killed; `dmesg` shows the OOM killer.** 1 GB is tight.
Confirm swap is on (`free -h` should show 2 GB), and if it persists set
`instance_type = "t3.small"` — that leaves the free tier, so check the bill.

**Everything 503s with `dispatch_frozen`.** Someone (or a test) froze the
system. `GET /admin/freeze` shows who and why; lift it from the dashboard
header or `DELETE /admin/freeze`.

**Every agent key returns 401 after a redeploy.** `API_KEY_PEPPER` changed, so
every stored HMAC is unverifiable. Restore the original value in SSM, or rotate
every key.

**`docker pull` fails with `denied`.** The ghcr.io package is private. Make it
public, or add a registry login to `redeploy`.

**The dashboard asks for a password on every page load.** Expected — it is HTTP
basic auth. The browser will remember it for the session.

---

## Security notes

- **No long-lived AWS credentials in GitHub.** OIDC, scoped to your repository.
- **No SSH.** No port 22, no key pair. SSM only, audited by CloudTrail.
- **IMDSv2 required**, so an SSRF in the app cannot read instance credentials
  with a plain GET.
- **Secrets live in SSM Parameter Store** as `SecureString`, read at boot by an
  IAM role scoped to `/<project>/*`. They are **also in `terraform.tfstate` in
  plaintext** — keep state private, and use the S3 backend in `versions.tf` if
  more than one person runs `apply`.
- **The dashboard is behind basic auth**, which is only as private as your
  transport. Use a domain and HTTPS for anything real.
- **Redis and PostgreSQL are not published to the host** — they are reachable
  only inside the compose network.
- The bundled **mock provider is deployed too**. Harmless, but it is an open
  endpoint inside the network; drop the `mock` service from the compose file if
  you are running `upstream_mode = live` and do not want it.
