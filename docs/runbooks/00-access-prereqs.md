# Runbook 00 — Access prerequisites

One-time setup so the IaaC can reach the machines. Done from **WSL2 Ubuntu**.

## 1. Control environment (WSL)
```bash
just bootstrap     # installs ansible, opentofu, collections (scripts/bootstrap-wsl.sh)
```
The repo lives on the Windows FS (`/mnt/c/Users/chifo/work/home/ailab`). The SSH private key
under `/mnt/c/Users/.ssh` cannot hold Unix 600 perms, so bootstrap copies it to `~/.ssh/` in WSL.

## 2. SSH to the Proxmox nodes (root)
Public key: `~/.ssh/id_ed25519.pub`. Add it to each node once:
```bash
for h in 192.168.0.2 192.168.0.3 192.168.0.4; do
  ssh-copy-id -i ~/.ssh/id_ed25519.pub root@$h    # prompts for the node root password once
done
# verify
ansible -i ../inventory/hosts.yml pve_nodes -m ping
```

## 3. Proxmox API token (for OpenTofu)
Create a dedicated user + token (run on any node, or via the UI: Datacenter → Permissions):
```bash
pveum user add tofu@pve
pveum role add Tofu -privs "VM.Allocate VM.Audit VM.Config.Disk VM.Config.CPUType \
  VM.Config.Memory VM.Config.Network VM.Config.Options VM.PowerMgmt \
  Datastore.Allocate Datastore.AllocateSpace Datastore.Audit Datastore.AllocateTemplate \
  Sys.Audit Sys.Console Sys.Modify SDN.Use Pool.Allocate"
pveum acl modify / -user tofu@pve -role Tofu
pveum user token add tofu@pve tofu --privsep 0
# copy the returned token id + secret into tofu/terraform.tfvars (gitignored)
```
> Some `bpg` ops (snippets, hardware mappings) additionally use root SSH; the provider is
> configured with both the API token and SSH (see `tofu/providers.tf`).

## 4. QNAP access
- Web admin: `http://ai-storage:8080` / `https://ai-storage` (`192.168.1.225`).
- Enable **SSH**: Control Panel → Telnet/SSH → Allow SSH (port 22), admin only.
- Confirm edition is **QuTS hero h5.1.0+** (required by the later CSI driver too).
- The QNAP storage build is semi-manual (`qnap-storage-setup.md`); read-only inventory is scripted
  in `scripts/qnap-api.sh` and `scripts/discover.sh`.

## 5. Secrets files (never committed)
```bash
cp tofu/terraform.tfvars.example tofu/terraform.tfvars     # fill PVE token, endpoint, node SSH
cp ansible/group_vars/vault.example.yml ansible/group_vars/vault.yml   # QNAP creds etc.
ansible-vault encrypt ansible/group_vars/vault.yml          # optional but recommended
```

## Checklist
- [ ] `just bootstrap` succeeds in WSL
- [ ] `ansible pve_nodes -m ping` → all green
- [ ] `tofu/terraform.tfvars` has a working API token (`tofu plan` authenticates)
- [ ] QNAP SSH reachable; edition/firmware confirmed
- [ ] physical node ↔ cable mapping recorded in `inventory/hosts.yml`
