#!/usr/bin/env python3
"""Generate the AI/GPU Grafana dashboard ConfigMap (deterministic, valid JSON).

Emits kubernetes/apps/infrastructure/monitoring/ai-llm-dashboard.yaml — a ConfigMap
labeled grafana_dashboard=1 so the kube-prometheus-stack Grafana sidecar auto-loads it.
Panels use the amdgpu_* (node_exporter textfile) + llamacpp:* (llama-server /metrics) series.

    python scripts/gen-ai-dashboard.py
"""
import json
import pathlib

DS = "${DS_PROMETHEUS}"  # datasource template variable (auto-selects the Prometheus DS)


def ts(pid, title, x, y, w, h, exprs, unit="short", legend="{{instance}}"):
    return {
        "id": pid,
        "type": "timeseries",
        "title": title,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": {"type": "prometheus", "uid": DS},
        "fieldConfig": {
            "defaults": {"unit": unit, "custom": {"drawStyle": "line", "fillOpacity": 10, "showPoints": "never"}},
            "overrides": [],
        },
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
        "targets": [
            {"refId": chr(65 + i), "datasource": {"type": "prometheus", "uid": DS}, "expr": e, "legendFormat": legend}
            for i, e in enumerate(exprs)
        ],
    }


panels = [
    ts(1, "iGPU utilization", 0, 0, 12, 8, ["amdgpu_gpu_busy_percent"], "percent"),
    ts(2, "VRAM used (per node)", 12, 0, 12, 8, ["amdgpu_vram_used_bytes"], "bytes"),
    ts(3, "GTT used (system-RAM spill)", 0, 8, 12, 8, ["amdgpu_gtt_used_bytes"], "bytes"),
    ts(4, "iGPU temperature", 12, 8, 6, 8, ["amdgpu_temp_millicelsius/1000"], "celsius"),
    ts(5, "iGPU power", 18, 8, 6, 8, ["amdgpu_power_microwatts/1000000"], "watt"),
    ts(6, "Decode throughput (tok/s)", 0, 16, 12, 8, ["llamacpp:predicted_tokens_seconds"], "none"),
    ts(7, "Prompt throughput (tok/s)", 12, 16, 12, 8, ["llamacpp:prompt_tokens_seconds"], "none"),
    ts(8, "KV-cache usage", 0, 24, 12, 8, ["llamacpp:kv_cache_usage_ratio"], "percentunit"),
    ts(9, "Requests: processing / deferred", 12, 24, 12, 8,
       ["llamacpp:requests_processing", "llamacpp:requests_deferred"], "short",
       legend="{{instance}} {{__name__}}"),
]

dashboard = {
    "title": "AI LLM — Strix Halo iGPU + llama.cpp",
    "uid": "ai-llm-strixhalo",
    "tags": ["ai", "gpu", "llama.cpp"],
    "timezone": "browser",
    "schemaVersion": 39,
    "refresh": "30s",
    "time": {"from": "now-3h", "to": "now"},
    "templating": {"list": [
        {"name": "DS_PROMETHEUS", "type": "datasource", "query": "prometheus",
         "current": {}, "hide": 0, "label": "Datasource", "refresh": 1}
    ]},
    "panels": panels,
}

configmap = {
    "apiVersion": "v1",
    "kind": "ConfigMap",
    "metadata": {
        "name": "ai-llm-dashboard",
        "namespace": "monitoring",
        "labels": {"grafana_dashboard": "1"},
    },
    "data": {"ai-llm.json": json.dumps(dashboard, indent=2)},
}

out = pathlib.Path(__file__).resolve().parents[1] / "kubernetes/apps/infrastructure/monitoring/ai-llm-dashboard.yaml"
# JSON is valid YAML 1.2, so a .yaml file containing JSON is accepted by kustomize/Flux.
out.write_text(json.dumps(configmap, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out}")
