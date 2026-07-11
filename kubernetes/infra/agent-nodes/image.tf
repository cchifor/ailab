# P2 — Kata/gVisor agent-node boot IMAGE (Talos Image Factory schematic).
#
# The Kata + gVisor runtimes ship as Talos SYSTEM EXTENSIONS baked into the boot image (NOT
# machine-config patches). This mirrors the CP root module's mechanism EXACTLY (kubernetes/infra/
# image.tf): a `talos_image_factory_schematic` built from `var.talos_extensions`, then a
# `talos_image_factory_urls` data source for the nocloud disk-image URL. The ONLY difference from
# the CPs is the extension LIST (this pool adds siderolabs/kata-containers + siderolabs/gvisor) — so
# the agent pool gets its OWN image while the CPs and every other pool stay on their current image.
#
# The staged file gets a DISTINCT basename (`local.agent_image_file`, "-agent-") so the Kata-enabled
# image coexists with the plain CP image on the SAME node's `local:import` datastore without clobbering
# it. bpg's download_file cannot decompress xz (the factory format), so the image is staged per node by
# scripts/stage-talos-image.sh (download + `xz -d`); the VM disk imports from that file (main.tf).
# Keep the script in sync via the `schematic_id` output (see outputs.tf) — same contract as the CPs.

resource "talos_image_factory_schematic" "agent" {
  schematic = yamlencode({
    customization = {
      systemExtensions = {
        officialExtensions = var.talos_extensions
      }
    }
  })
}

data "talos_image_factory_urls" "agent" {
  talos_version = var.talos_version
  schematic_id  = talos_image_factory_schematic.agent.id
  platform      = "nocloud"
  architecture  = "amd64"
}

locals {
  # import content needs .raw (not .img); "-agent-" keeps it distinct from the CP image on local:import.
  # Falling back to the PLAIN P1 pool = drop kata-containers/gvisor from var.talos_extensions and
  # re-stage — the filename is unchanged, only the baked contents differ (see variables.tf).
  agent_image_file = "talos-${var.talos_version}-agent-nocloud-amd64.raw"
}
