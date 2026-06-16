terraform {
  required_version = ">= 1.6.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5" # v5 rewrite: cloudflare_zero_trust_access_* + cloudflare_dns_record
    }
  }
}
