# Deploying to AWS

A step-by-step guide to running the Agent Budget Controller on AWS, deployed
from GitHub, provisioned with Terraform.

Every step ends with a **check** — run it before moving on. Infrastructure
failures compound, and finding out at step 9 that step 3 was wrong is the
expensive way to do this.

> This stack has been applied against a real AWS account and is running.
> The figures and resource counts below reflect that deployment. Read your
> own `terraform plan` before applying — it is the only thing that knows
> what your account will actually do.

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

If the project is not yet a git repository:

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

Two values, generated once.

```bash
# 1. API key pepper — NEVER change this later; it invalidates every agent key.
python3 -c "import secrets; print('api_key_pepper     =', repr(secrets.token_hex(32)))"

# 2. PostgreSQL password
python3 -c "import secrets; print('postgres_password  =', repr(secrets.token_urlsafe(24)))"
```

### There is no authentication in front of the dashboard

By decision, the dashboard and the whole admin API are open to anyone who can
reach the address. Be clear-eyed about what that means:

- anyone can read every team, agent, budget and event;
- anyone can create, edit, pause or delete agents, rotate keys, grant budget
  boosts, and freeze or resume all dispatch;
- agent API keys are *not* exposed — only their `sk-agent-xxxxxx` prefix. The
  raw key is returned exactly once, at creation.

Agent traffic on `/v1/chat/completions` is still authenticated by the agent's
own `X-Agent-Key`; that is independent of the above.

If you want to limit exposure without adding a login, set `allowed_cidrs` in
`terraform.tfvars` to your own address — that is the only control currently
restricting who can reach the admin API.

To add a login later, uncomment the `basic_auth` block in `deploy/Caddyfile`
and re-add the `DASHBOARD_USER` / `DASHBOARD_PASSWORD_HASH` parameters. Doing it
at the proxy covers the API as well as the UI, which is the point — auth inside
the app could only ever have guarded one of the two.

---

## Step 4 — Configure Terraform

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars` with the two secrets, your `github_repository`
(`owner/repo`) and your `image` (`ghcr.io/owner/repo:latest`, lowercase).

Leave `site_address = ":80"` for now — HTTPS needs a domain pointing at an IP
that does not exist yet. Step 10 covers it.

**Check**

```bash
terraform init
terraform validate
terraform plan
```

Read the plan. It should create roughly **25 resources** and no `aws_nat_gateway`,
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
gh secret set AWS_REGION      --body "ap-south-2"
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

Then restart the proxy:

```bash
sudo docker compose -f docker-compose.prod.yml restart proxy
```

This is required, not optional. The proxy keeps an in-process mirror of the
pricing catalog, loaded at startup and refreshed only when the catalog is
changed *through the admin API*. `seed.py` writes to the database directly, so a
proxy that was already running never learns about the models it just inserted —
and every call fails with `422 model_not_found` and an empty `available_models`
list, while `/admin/models` cheerfully shows all eleven entries. Restarting
reloads the mirror.

Open the dashboard at `http://<public_ip>/dashboard`. No login is required.

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

### Change the Caddyfile or the compose file

Edit `deploy/Caddyfile` or `deploy/docker-compose.prod.yml`, then:

```bash
cd terraform && terraform apply
```

That updates the SSM parameter holding the file. The next deploy — pushed or
run by hand — writes it to the instance and restarts the affected containers.
The instance is not replaced or rebooted.

These files used to live inside `user_data`, where they could not be changed at
all: `user_data` runs once at first boot and never again, so an edit forced a
stop/start and *still* left the old file in place. If you are working from an
instance created before that change, install the fetcher once:

```bash
aws ssm send-command --instance-ids $INSTANCE --document-name AWS-RunShellScript --parameters 'commands=["/usr/local/bin/fetch-files"]'
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

**Deploy fails at "Assume the AWS deploy role" with `Not authorized to perform
sts:AssumeRoleWithWebIdentity`.** The error says nothing about *which*
condition failed, and the usual cause is the subject pattern. GitHub issues the
OIDC subject in two shapes:

```
repo:OWNER/REPO:ref:refs/heads/main
repo:OWNER@123456/REPO@789012:ref:refs/heads/main
```

The second is the immutable-identifier form — numeric owner and repo ids
appended so a rename does not change the subject. Which one your repository
gets is not under your control, so `terraform/github_oidc.tf` trusts both.
To see what yours actually presents, read it out of CloudTrail:

```bash
aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventName,AttributeValue=AssumeRoleWithWebIdentity --max-results 1 --query 'Events[0].CloudTrailEvent' --output text | jq -r '.userIdentity.userName'
```

That field carries the exact subject the token presented. Do not "fix" this by
relaxing the pattern to `repo:*` — that lets any repository on GitHub assume
the role and run shell commands on your instance.

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
- **The dashboard and admin API are unauthenticated by decision.** `allowed_cidrs`
  is the only thing limiting who can reach them; narrow it if the instance does
  not need to be world-reachable.
- **Redis and PostgreSQL are not published to the host** — they are reachable
  only inside the compose network.
- The bundled **mock provider is deployed too**. Harmless, but it is an open
  endpoint inside the network; drop the `mock` service from the compose file if
  you are running `upstream_mode = live` and do not want it.
