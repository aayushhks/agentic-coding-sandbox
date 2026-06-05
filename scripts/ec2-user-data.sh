#!/bin/bash
# EC2 user-data bootstrap (Amazon Linux 2023) for a free-tier t3.micro.
#
# Paste this into the instance's "User data" field at launch. On first boot it installs Docker,
# builds the self-contained image (dashboard + API + baked SQLite data), and serves it on port
# 80. After a few minutes the dashboard is live at http://<instance-public-ip>.
#
# Assumes a PUBLIC repo. For a private repo, build the image elsewhere and `docker load` it here,
# or clone with a token. Logs go to /var/log/cloud-init-output.log.
set -euxo pipefail

REPO_URL="https://github.com/aayushhks/agentic-coding-sandbox.git"

# a 2G swapfile keeps the frontend/image build from running out of memory on a 1 GiB instance
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
fi

dnf install -y docker git
systemctl enable --now docker

cd /opt
[ -d agentic-coding-sandbox ] || git clone "$REPO_URL"
cd agentic-coding-sandbox

docker build -t agentic-coding-sandbox .
docker rm -f acs 2>/dev/null || true
docker run -d --name acs -p 80:8000 --restart unless-stopped agentic-coding-sandbox
