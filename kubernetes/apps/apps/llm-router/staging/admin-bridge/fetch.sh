#!/bin/sh
set -eu
umask 0002
release=/releases/router-0.1.0-20260927-admin-bridge
[ ! -f "$release/RELEASE.json" ] || exit 0
cp /deploy-key/identity /tmp/release-identity
chmod 600 /tmp/release-identity
# Bootstrap trust only on the cluster's Gitea service; the fetched Git object must
# also match the reviewed, full commit hash. Never forward this key to the internet.
for port in 2222 22; do
  ssh-keyscan -T 5 -p "$port" gitea-ssh.gitea.svc.cluster.local > /tmp/release-known-hosts 2>/dev/null || true
  [ ! -s /tmp/release-known-hosts ] || break
done
test -s /tmp/release-known-hosts
export GIT_SSH_COMMAND='ssh -F /dev/null -i /tmp/release-identity -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/tmp/release-known-hosts'
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
mkdir -p /stage/source
cd /stage/source
git init
git remote get-url origin >/dev/null 2>&1 || git remote add origin "ssh://git@gitea-ssh.gitea.svc.cluster.local:$port/dsh/llm-router.git"
git fetch --depth 1 origin a2a3b20e4dd7fa7f0e1a2350d5fe3a5ca495d48a
git checkout --detach FETCH_HEAD
test "$(git rev-parse HEAD)" = a2a3b20e4dd7fa7f0e1a2350d5fe3a5ca495d48a
