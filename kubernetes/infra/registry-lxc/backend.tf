terraform {
  # Local state (kubernetes/infra/registry-lxc/terraform.tfstate, gitignored). Separate root
  # module => the registry LXC can be built/destroyed without touching Talos / ai-lxc / runners.
  backend "local" {}
}
