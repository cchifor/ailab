# Talos Image Factory schematic (system extensions baked in) + nocloud disk image per host.

resource "talos_image_factory_schematic" "this" {
  schematic = yamlencode({
    customization = {
      systemExtensions = {
        officialExtensions = var.talos_extensions
      }
    }
  })
}

data "talos_image_factory_urls" "this" {
  talos_version = var.talos_version
  schematic_id  = talos_image_factory_schematic.this.id
  platform      = "nocloud"
  architecture  = "amd64"
}

# NOTE: bpg's download_file cannot decompress xz (the format the Talos factory serves), so the
# nocloud image is staged on each node by scripts/stage-talos-image.sh (download + `xz -d`) into
# local:iso/talos-<ver>-nocloud-amd64.img. The VM disk imports from that file (see vms.tf).
# `schematic_id` + `talos_disk_image_url` outputs let you keep that script in sync with this config.
