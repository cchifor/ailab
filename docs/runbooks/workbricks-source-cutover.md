# Workbricks Flux source cutover

The ailab Flux source and root Kustomization are named `workbricks`; its
source is now `ssh://git@git.chifor.me/cchifor/workbricks.git`. The repository
is private and is read through the existing `workbricks-deploy-key` Secret.

Before merging the source change, an operator must update the encrypted
`known_hosts` value in `kubernetes/apps/workbricks-bootstrap/deploy-key.sops.yaml`.
It must contain the verified SSH host key for `git.chifor.me`; the former
GitHub host key is not valid for the new source. Obtain the key through the
forge's trusted operator procedure, edit the decrypted Secret with `sops edit`,
and re-encrypt it with the ailab age key. Never commit a plaintext Secret.

Validate the encrypted field without printing its value:

```bash
set -euo pipefail
export SOPS_AGE_KEY_FILE=kubernetes/infra/_out/age.agekey
known_hosts="$(sops decrypt kubernetes/apps/workbricks-bootstrap/deploy-key.sops.yaml \
  | yq -r '.data.known_hosts' | base64 --decode)"
if ! printf '%s\n' "$known_hosts" \
  | grep -Eq '(^|[[:space:],])\[?git\.chifor\.me\]?(:[0-9]+)?[[:space:]]+(ssh-|ecdsa-)'; then
  printf '%s\n' 'known_hosts has no git.chifor.me SSH host key entry' >&2
  exit 1
fi
if printf '%s\n' "$known_hosts" \
  | grep -Eq '(^|[[:space:],])github\.com([[:space:],]|$)'; then
  printf '%s\n' 'known_hosts still contains a github.com entry' >&2
  exit 1
fi
```

After the Workbricks deploy key has read access and the PR is merged, verify
that Flux fetches the Workbricks source and reports a ready artifact before
removing or rotating any old source access.
