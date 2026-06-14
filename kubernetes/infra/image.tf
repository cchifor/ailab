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

# Download the decompressed Talos nocloud raw image to each host's image datastore.
resource "proxmox_download_file" "talos" {
  for_each = toset(distinct([for _, v in var.control_planes : v.host_node]))

  content_type            = "iso"
  datastore_id            = var.image_datastore
  node_name               = each.key
  file_name               = "talos-${var.talos_version}-nocloud-amd64.img"
  url                     = data.talos_image_factory_urls.this.urls.disk_image
  decompression_algorithm = "gz"
  overwrite               = false
}
