"""Nonsecret guest/launcher evidence that must exist BEFORE the S2 workload render. Fail closed."""
from __future__ import annotations

import datetime as dt
import re

from .operator_bundle import DIGEST_IMAGE, BundleError

SCHEMA = "ailab.relay-kubevirt-spike-guest-evidence/v1alpha1"
GI = 1024 ** 3
EPHEMERAL_LIMIT = 30 * GI     # VMI requests == limits (runbook)
EMPTY_DISK = 20 * GI          # Docker cache emptyDisk
LOG_RESERVE = 1 * GI          # launcher logs + KubeVirt's 50M ephemeral overhead + qcow2 overlay metadata
MAX_EVIDENCE_AGE = dt.timedelta(days=14)
FIELDS = {
    "schema": str, "measured_at": str, "measured_by": str,
    "guest_image": str, "guest_image_expanded_bytes": int, "guest_disk_virtual_bytes": int,
    "guest_disk_format": str, "launcher_image": str, "launcher_uid": int, "launcher_uid_source": str,
    "method": str,
}
UID_SOURCES = {"oci-config-user", "launcher-pod-securityContext"}


def template(guest_image: str, launcher_image: str) -> dict:
    """Skeleton the operator fills from real measurements; nulls never render."""
    return {"schema": SCHEMA, "measured_at": None, "measured_by": None, "guest_image": guest_image,
            "guest_image_expanded_bytes": None, "guest_disk_virtual_bytes": None, "guest_disk_format": None,
            "launcher_image": launcher_image, "launcher_uid": None, "launcher_uid_source": None,
            "method": None}


def validate(ev: dict, *, launcher_image: str, now: dt.datetime) -> dict:
    if not isinstance(ev, dict) or ev.get("schema") != SCHEMA:
        raise BundleError("guest evidence missing or wrong schema")
    unknown = sorted(set(ev) - set(FIELDS))
    if unknown:
        raise BundleError(f"guest evidence has unknown fields {unknown}")
    for key, typ in FIELDS.items():
        val = ev.get(key)
        if val is None or not isinstance(val, typ) or isinstance(val, bool) or val == "":
            raise BundleError(f"guest evidence field {key} is missing or not {typ.__name__}")
    if not DIGEST_IMAGE.match(ev["guest_image"]):
        raise BundleError("guest_image is not digest-pinned")
    if ev["launcher_image"] != launcher_image:
        raise BundleError("evidence launcher_image differs from the pinned VIRT_LAUNCHER_IMAGE")
    if ev["launcher_uid_source"] not in UID_SOURCES or not 1 <= ev["launcher_uid"] < 65536:
        raise BundleError("launcher_uid must be a non-root UID with a recognised source")
    if ev["guest_disk_format"] not in ("qcow2", "raw"):
        raise BundleError("guest_disk_format must be qcow2 or raw")
    try:
        at = dt.datetime.fromisoformat(ev["measured_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise BundleError("measured_at is not ISO-8601") from exc
    if at.tzinfo is None or at > now or now - at > MAX_EVIDENCE_AGE:
        raise BundleError("guest evidence is stale, in the future or has no timezone")
    if not re.fullmatch(r"[a-z0-9-]{3,64}", ev["method"]):
        raise BundleError("method must be a short identifier")
    allowance = EPHEMERAL_LIMIT - EMPTY_DISK - LOG_RESERVE
    # The copied base image and writable overlay coexist. Count both: the overlay
    # can grow to the virtual size even while the full base remains on disk.
    if ev["guest_image_expanded_bytes"] <= 0 or ev["guest_disk_virtual_bytes"] <= 0:
        raise BundleError("guest disk measurements must be positive")
    worst = ev["guest_image_expanded_bytes"] + ev["guest_disk_virtual_bytes"]
    if worst > allowance:
        raise BundleError(f"root containerDisk ({worst} B) exceeds the {allowance} B ephemeral allowance")
    return ev
