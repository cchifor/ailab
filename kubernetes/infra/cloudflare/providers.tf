# The API token is read from the CLOUDFLARE_API_TOKEN environment variable (export it from .env) so the
# secret never lands in a tfvars file or in state inputs. The token must be scoped to DNS + Access ONLY:
#   Account -> Access: Apps and Policies -> Edit
#   Account -> Access: Service Tokens   -> Edit
#   Account -> Access: Organizations, Identity Providers, and Groups -> Edit
#   Zone    -> DNS                       -> Edit   (Zone Resources: chifor.me)
# The Organizations/IdPs/Groups scope is required by the Authelia identity provider in access.tf. It is
# easy to miss because READ on it is enough to plan: a token without it plans the IdP clean and then
# fails at apply.
# (NOT Cloudflare Tunnel — the tunnel is locally-managed in kubernetes/apps/apps/edge/cloudflared.yaml.)
provider "cloudflare" {}
