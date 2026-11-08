terraform {
  required_version = ">= 1.6.0"
  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.120" # same pin as infra/, agent-nodes, ai-lxc, runners and dev-workers
    }
    # The worker JOIN reuses the EXISTING cluster PKI: the talos provider here only renders the worker
    # machine config and applies it. Pin == infra/versions.tf and agent-nodes/versions.tf.
    talos = {
      source  = "siderolabs/talos"
      version = "~> 0.12"
    }
    # No kubernetes provider, unlike agent-nodes: the pool label and taint come from the machine
    # config. Talos 1.14 registers the kubelet with machine.nodeTaints (registerWithTaints) and owns
    # machine.nodeLabels on workers (the talos.dev/owned-labels and owned-taints annotations on
    # talos-agent-node-1, 2026-11-08), so the node is tainted from its first registration. A
    # kubernetes_node_taint resource could only run after the join, and would fail on a fresh apply
    # because the Node object does not exist yet.
  }
}
