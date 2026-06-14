#!/usr/bin/env bash
# Install Ansible + OpenTofu + collections into WSL2 Ubuntu. Idempotent.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> apt dependencies"
sudo apt-get update -y
sudo apt-get install -y python3 python3-pip pipx curl gnupg jq nfs-common iperf3 unzip

echo "==> Ansible (via pipx)"
pipx install --include-deps ansible 2>/dev/null || pipx upgrade ansible || true
pipx ensurepath || true
export PATH="$HOME/.local/bin:$PATH"

echo "==> Ansible collections"
ansible-galaxy collection install -r "$REPO_DIR/ansible/requirements.yml" \
  -p "$REPO_DIR/ansible/collections"

echo "==> OpenTofu"
if ! command -v tofu >/dev/null 2>&1; then
  curl -fsSL https://get.opentofu.org/install-opentofu.sh -o /tmp/install-tofu.sh
  chmod +x /tmp/install-tofu.sh
  sudo /tmp/install-tofu.sh --install-method deb
  rm -f /tmp/install-tofu.sh
fi

echo "==> SSH key into WSL (~/.ssh needs 600; /mnt/c can't hold Unix perms)"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
WINKEY="/mnt/c/Users/chifo/.ssh/id_ed25519"
if [ -f "$WINKEY" ] && [ ! -f "$HOME/.ssh/id_ed25519" ]; then
  cp "$WINKEY" "$HOME/.ssh/id_ed25519"
  [ -f "$WINKEY.pub" ] && cp "$WINKEY.pub" "$HOME/.ssh/id_ed25519.pub"
  chmod 600 "$HOME/.ssh/id_ed25519"
  echo "   copied id_ed25519 into WSL ~/.ssh"
fi

echo "==> versions"
ansible --version | head -1
tofu version | head -1
echo "Bootstrap complete. Next: 'just discover' (after access set up — docs/runbooks/00-access-prereqs.md)."
