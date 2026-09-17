# The publisher cannot read DSH's credential values or replace/delete the document.
# OpenBao ACLs apply per document: patch permits any field in this one document.
path "af/data/dsh/credentials" { capabilities = ["patch"] }
path "af/metadata/dsh/credentials" { capabilities = ["read"] }
