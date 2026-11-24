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
    archives = inputs["relay_holder_archives"]
    require(sorted(a["component"] for a in archives) == ["holder", "pi-linux-x64"],
            "Expected exactly holder and Pi archives")
    pi_pin = next(a["manifestSha256"] for a in archives if a["component"] == "pi-linux-x64")
    runtime = "/opt/relay-pi/releases/" + pi_pin
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
        require(profile["native"]["runtimeDirectory"] == runtime and
                profile["artifacts"].get(runtime + "/manifest.json") == pi_pin,
                "Pi archive and profile runtime pins differ")
        require(profile["executable"] == runtime + "/node" and
                profile["communication"]["executable"] == runtime + "/node" and
                profile["communication"]["args"] == [runtime + "/relay-agent-tools.mjs"] and
                set(profile["artifacts"]) == {runtime + "/" + name for name in
                    ["relay-pi.mjs", "pi-worker.mjs", "relay-pi-policy.mjs",
                     "relay-agent-tools.mjs", "bin/pi", "bin/npm", "manifest.json"]},
                "Pi component paths differ from the pinned runtime")
        require(definition["holder"] == profile, "Hub and holder definitions differ")
        require(profile["router"] == policy, "Renderer and holder policies differ")
        require(definition["connectorId"] == policy["hostId"], "Connector differs")
        require(request["name"] == definition["name"], "Binding name differs")
        for key in ["bindingId", "connectionId", "hostId", "executionIdentity"]:
            require(request[key] == policy[key], "Binding authority differs: " + key)
        require(policy["selection"] == {"kind": "model", "model":
                "canary-pi-" + definition["name"].removeprefix("pi-canary-")},
                "Profile route differs")
        require(request["grant"]["routes"] == [policy["selection"]["model"]],
                "Binding route grant differs")
        require(request["grant"]["access"] ==
                (["local"] if definition["name"] == "pi-canary-qwen" else ["subscription"]),
                "Binding access differs")
        require(request["grant"]["lifetimeSeconds"] == 86400, "Grant lifetime changed")
        session = definition["session"]
        require(session["id"] == profile["id"] and session["revision"] == profile["revision"],
                "Session identity differs")
        require(session["executionIdentity"] == policy["executionIdentity"] and
                session["maxRequestSeconds"] == profile["maxRequestSeconds"] and
                session["harness"] == profile["native"]["kind"] and
                session["adapterDigest"] == profile["artifacts"][runtime + "/relay-pi.mjs"],
                "Session runtime identity or deadline differs")
        require(session["inference"] == {"kind": "llm-router",
                "connectionId": policy["connectionId"], "credentialBindingId": policy["bindingId"],
                "protocol": policy["protocol"], "selection": policy["selection"]},
                "Session inference differs")
    return True


if __name__ == "__main__":
    validate()
    print("Three canary profiles, binding requests, renderer policies and byte pin match")
