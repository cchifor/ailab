# P4 of ADR 0040: the per-hostname tunnel CNAMEs became registry-driven records
# (cloudflare_dns_record.edge, dns.tf). These blocks carry the existing records' state across, so
# the refactor is moves only: no record is recreated. Keep them until every operator checkout has
# applied past the refactor (state serial > 48 on 2026-10-27); removing one early would DESTROY and
# recreate that record.

moved {
  from = cloudflare_dns_record.tunnel["home"]
  to   = cloudflare_dns_record.edge["home"]
}
moved {
  from = cloudflare_dns_record.tunnel["sso"]
  to   = cloudflare_dns_record.edge["sso"]
}
moved {
  from = cloudflare_dns_record.tunnel["status"]
  to   = cloudflare_dns_record.edge["status"]
}
moved {
  from = cloudflare_dns_record.tunnel["chat"]
  to   = cloudflare_dns_record.edge["chat"]
}
moved {
  from = cloudflare_dns_record.tunnel["grafana"]
  to   = cloudflare_dns_record.edge["grafana"]
}
moved {
  from = cloudflare_dns_record.tunnel["api"]
  to   = cloudflare_dns_record.edge["api"]
}
moved {
  from = cloudflare_dns_record.tunnel["dw1"]
  to   = cloudflare_dns_record.edge["dw1"]
}
moved {
  from = cloudflare_dns_record.tunnel["dw2"]
  to   = cloudflare_dns_record.edge["dw2"]
}
moved {
  from = cloudflare_dns_record.tunnel["dw3"]
  to   = cloudflare_dns_record.edge["dw3"]
}
moved {
  from = cloudflare_dns_record.tunnel["dw4"]
  to   = cloudflare_dns_record.edge["dw4"]
}
moved {
  from = cloudflare_dns_record.tunnel["k8s"]
  to   = cloudflare_dns_record.edge["k8s"]
}
moved {
  from = cloudflare_dns_record.tunnel["hubble"]
  to   = cloudflare_dns_record.edge["hubble"]
}
moved {
  from = cloudflare_dns_record.tunnel["proxmox"]
  to   = cloudflare_dns_record.edge["proxmox"]
}
moved {
  from = cloudflare_dns_record.tunnel["qnap"]
  to   = cloudflare_dns_record.edge["qnap"]
}
moved {
  from = cloudflare_dns_record.tunnel["prometheus"]
  to   = cloudflare_dns_record.edge["prometheus"]
}
moved {
  from = cloudflare_dns_record.tunnel["alertmanager"]
  to   = cloudflare_dns_record.edge["alertmanager"]
}
moved {
  from = cloudflare_dns_record.tunnel["ntfy"]
  to   = cloudflare_dns_record.edge["ntfy"]
}
moved {
  from = cloudflare_dns_record.tunnel["git"]
  to   = cloudflare_dns_record.edge["git"]
}
moved {
  from = cloudflare_dns_record.tunnel["vault"]
  to   = cloudflare_dns_record.edge["vault"]
}
moved {
  from = cloudflare_dns_record.tunnel["agentforge"]
  to   = cloudflare_dns_record.edge["agentforge"]
}
moved {
  from = cloudflare_dns_record.tunnel["openbao"]
  to   = cloudflare_dns_record.edge["openbao"]
}
moved {
  from = cloudflare_dns_record.tunnel["dsh"]
  to   = cloudflare_dns_record.edge["dsh"]
}
moved {
  from = cloudflare_dns_record.tunnel["search"]
  to   = cloudflare_dns_record.edge["search"]
}
moved {
  from = cloudflare_dns_record.tunnel["router"]
  to   = cloudflare_dns_record.edge["router"]
}
moved {
  from = cloudflare_dns_record.tunnel["relay"]
  to   = cloudflare_dns_record.edge["relay"]
}
moved {
  from = cloudflare_dns_record.tunnel["muse"]
  to   = cloudflare_dns_record.edge["muse"]
}
moved {
  from = cloudflare_dns_record.tunnel["trueswarm"]
  to   = cloudflare_dns_record.edge["trueswarm"]
}
moved {
  from = cloudflare_dns_record.tunnel["id"]
  to   = cloudflare_dns_record.edge["id"]
}
