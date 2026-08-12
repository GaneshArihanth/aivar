#!/bin/bash
# Instance bootstrap. Runs once, on first boot.
#
# Everything here is idempotent so the same script can be re-run by hand over
# SSM if a boot is interrupted.
set -euxo pipefail

PROJECT="${project}"
REGION="${region}"
APP_DIR=/opt/app

# --------------------------------------------------------------------- swap
# 1 GB of RAM is not quite enough for PostgreSQL, Redis, two Python services
# and Caddy. Without swap the OOM killer picks a victim under load — usually
# PostgreSQL, which is the worst possible choice.
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  # Prefer RAM, but use swap rather than killing a process.
  sysctl -w vm.swappiness=10
  echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf
fi

# ------------------------------------------------------------------- docker
dnf update -y
dnf install -y docker jq

systemctl enable --now docker
usermod -aG docker ec2-user

# Compose v2 as a CLI plugin.
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL \
  "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Cap the log volume: without this, JSON logs fill a 20 GB disk eventually and
# the first symptom is PostgreSQL refusing to write.
cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
systemctl restart docker

# ---------------------------------------------------------------- app files
mkdir -p "$APP_DIR"

cat > "$APP_DIR/docker-compose.prod.yml" <<'EOF'
${compose_file}
EOF

cat > "$APP_DIR/Caddyfile" <<'EOF'
${caddy_file}
EOF

# Pulls every parameter for this project out of SSM and writes the .env that
# docker compose reads. Kept as a script so a redeploy can refresh secrets
# without replacing the instance.
cat > /usr/local/bin/fetch-env <<EOF
#!/bin/bash
set -euo pipefail
aws ssm get-parameters-by-path \
  --path "/$PROJECT" \
  --with-decryption \
  --region "$REGION" \
  --query 'Parameters[].{Name:Name,Value:Value}' \
  --output json \
| jq -r '.[] | "\(.Name | split("/") | last)=\(.Value)"' \
> $APP_DIR/.env.tmp
mv $APP_DIR/.env.tmp $APP_DIR/.env
chmod 600 $APP_DIR/.env
EOF
chmod +x /usr/local/bin/fetch-env

# Redeploy: refresh secrets, pull the current image, restart, migrate.
cat > /usr/local/bin/redeploy <<EOF
#!/bin/bash
set -euo pipefail
cd $APP_DIR
/usr/local/bin/fetch-env
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d --remove-orphans
# Migrations run after the container is up, against the same image that will
# serve traffic — so the schema can never be newer or older than the code.
docker compose -f docker-compose.prod.yml exec -T proxy alembic upgrade head
docker image prune -f
EOF
chmod +x /usr/local/bin/redeploy

# A marker the deploy workflow waits on, so it does not race the bootstrap.
touch /var/lib/cloud/instance/bootstrap-complete
