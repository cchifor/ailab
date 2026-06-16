output "tunnel_cname_records" {
  description = "Managed proxied CNAMEs (hostname -> record name)."
  value       = { for k, r in cloudflare_dns_record.tunnel : k => r.name }
}
