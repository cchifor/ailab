"""Read-only, anonymous HTTPS re-check of pinned tag -> index digests. No credentials, no blobs."""
from __future__ import annotations

import hashlib
import urllib.request

USER_AGENT = "ailab-relay-kubevirt-spike/0.1 (read-only pin check)"
ACCEPT = ("application/vnd.oci.image.index.v1+json,"
          "application/vnd.docker.distribution.manifest.list.v2+json")
MAX_MANIFEST = 64 * 1024


def _split(ref: str) -> tuple[str, str, str]:
    host, rest = ref.split("/", 1)
    repo, sep, tail = rest.rpartition("@" if "@" in rest else ":")
    return host, repo, tail


def fetch_index_digest(tag_ref: str, opener=urllib.request.urlopen) -> str:
    host, repo, tag = _split(tag_ref)
    if host != "quay.io":
        raise ValueError(f"only quay.io is supported anonymously: {tag_ref}")
    req = urllib.request.Request(f"https://{host}/v2/{repo}/manifests/{tag}",
                                 headers={"User-Agent": USER_AGENT, "Accept": ACCEPT})
    with opener(req, timeout=30) as resp:
        body = resp.read(MAX_MANIFEST + 1)
        header = resp.headers.get("Docker-Content-Digest", "")
    if len(body) > MAX_MANIFEST:
        raise ValueError(f"{tag_ref}: manifest larger than {MAX_MANIFEST} bytes")
    body_digest = "sha256:" + hashlib.sha256(body).hexdigest()
    if header and header != body_digest:
        raise ValueError(f"{tag_ref}: registry digest header disagrees with the body")
    return body_digest


def check(bundle: dict, opener=urllib.request.urlopen) -> list[dict]:
    pairs = [(c["tag_checked"], c["image"]) for c in bundle["components"]]
    pairs += [(bundle[k]["tag_checked"], bundle[k]["image"]) for k in ("guest_candidate", "compose_smoke_image")]
    rows = []
    for tag_ref, pinned in pairs:
        try:
            got = fetch_index_digest(tag_ref, opener)
            status = "pass" if pinned.endswith("@" + got) else "fail"
        except Exception as exc:  # network/HTTP errors are reported, never treated as pass
            got, status = f"error: {type(exc).__name__}", "unknown"
        rows.append({"tag": tag_ref, "pinned": pinned.rpartition("@")[2], "observed": got, "status": status})
    return rows
