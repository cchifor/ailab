terraform {
  required_version = ">= 1.6.0"

  required_providers {
    proxmox = {
      source = "bpg/proxmox"
      # Provides proxmox_storage_nfs (newer short-named storage resources).
      # Verify after init: `tofu providers schema -json | jq '.provider_schemas[].resource_schemas | keys[]' | grep storage`
      version = "~> 0.109"
    }
  }
}
