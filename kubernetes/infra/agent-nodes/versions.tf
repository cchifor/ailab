terraform {
  required_version = ">= 1.6.0"
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.111" # same pin as the Talos (infra/), ai-lxc, runners, and dev-workers modules
    }
    # Worker JOIN reuses the EXISTING cluster PKI: the talos provider here only renders the worker
    # machine-config + applies it. The machine_secrets themselves come from infra/ remote state
    # (talos-agent join reuses talos_machine_secrets.this) — see talos.tf. Pin == infra/versions.tf.
    talos = {
      source  = "siderolabs/talos"
      version = "~> 0.11"
    }
    # Applies the agent-pool node identity (label + taint) from the privileged CP kubeconfig — the
    # NodeRestriction admission plugin blocks a WORKER kubelet from self-setting arbitrary ailab.io/*
    # labels + non-standard taints via machine.nodeLabels/nodeTaints (worker.yaml.tftpl), so those are
    # silently dropped; the pool identity MUST be applied cluster-side (node-labels.tf).
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
  }
}
