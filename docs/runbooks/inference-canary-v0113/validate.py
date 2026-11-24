#!/usr/bin/env python3
"""Validate the exact nonsecret canary inputs before review or staging."""
import hashlib
import json
from pathlib import Path
import uuid


def validate(root=Path(__file__).resolve().parent):
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    def read(name):
        return json.loads((root / name).read_text(encoding="utf-8"))

    profiles = read("profiles.json")
    definitions = read("launch-definitions.json")
    requests = read("binding-requests.json")
    policies = read("renderer-bindings.json")["relay_router_bindings"]
    inputs = read("holder-inputs.json")
    raw = (root / "profiles.json").read_bytes()
    require(b"\r" not in raw, "Profile bytes must use LF")
    require(hashlib.sha256(raw).hexdigest() == inputs["relay_holder_profiles_sha256"],
            "Profile byte pin differs")
    require(inputs["relay_holder_start"] is False, "Staging must not start services")
    require(len(profiles) == len(definitions) == len(requests) == len(policies) == 3,
            "Expected exactly three matching canary inputs")
    require({d["name"] for d in definitions} ==
            {"pi-canary-codex", "pi-canary-qwen", "pi-canary-claude"},
            "Canary names changed")
    for values, key in [(profiles, "id"), (requests, "bindingId"), (requests, "operationId")]:
        require(len({v[key] for v in values}) == 3, "Duplicate canary identity")
        for value in values:
            uuid.UUID(value[key])
    for profile, definition, request, policy in zip(profiles, definitions, requests, policies):
        require(definition["holder"] == profile, "Hub and holder definitions differ")
        require(profile["router"] == policy, "Renderer and holder policies differ")
        require(request["name"] == definition["name"], "Binding name differs")
        for key in ["bindingId", "connectionId", "hostId", "executionIdentity"]:
            require(request[key] == policy[key], "Binding authority differs: " + key)
        require(policy["selection"] == {"kind": "route", "alias":
                "canary-pi-" + definition["name"].removeprefix("pi-canary-")},
                "Profile route differs")
        require(request["grant"]["routes"] == [policy["selection"]["alias"]],
                "Binding route grant differs")
        require(request["grant"]["access"] ==
                (["local"] if definition["name"] == "pi-canary-qwen" else ["subscription"]),
                "Binding access differs")
        require(request["grant"]["lifetimeSeconds"] == 86400, "Grant lifetime changed")
        session = definition["session"]
        require(session["id"] == profile["id"] and session["revision"] == profile["revision"],
                "Session identity differs")
        require(session["inference"] == {"kind": "llm-router",
                "connectionId": policy["connectionId"], "credentialBindingId": policy["bindingId"],
                "protocol": policy["protocol"], "selection": policy["selection"]},
                "Session inference differs")
    return True


if __name__ == "__main__":
    validate()
    print("Three canary profiles, binding requests, renderer policies and byte pin match")
