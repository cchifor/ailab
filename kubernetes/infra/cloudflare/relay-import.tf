# Adopt the Relay DNS record created by the operator on 2026-10-01 (PR #1010). The one adoption still
# pending at P4: it applies with P4's first apply, after which DELETE this file (next PR). An import
# block whose target later leaves state (an interrupted CNAME/A replacement) would try to re-import
# this deleted record id and block every plan; the other adopted records' blocks went with P4.
import {
  to = cloudflare_dns_record.edge["relay"]
  id = "c967ce7dbbf43b1d7599eb4d213efa57/d01f099abc5e5a85c6c5bef5791c8e41"
}
