# Grafana Monitoring Improvements — Implementation Plan

## Codex Review

- Status: needs corrections before implementation; the overall architecture is sound, but several KSM/Flux/Grafana details must be made exact.
- KSM CRS must include `metricNamePrefix: gotk`, correct `labelsFromPath`, Flux plural RBAC resources, and must not disable default KSM collectors needed by the Kubernetes dashboard.
- Flux PodMonitor port `http-prom` and namespace targeting are correct for the local Flux manifests, but the plan should explicitly use `spec.podMetricsEndpoints` and an `app In (...)` pod selector.
- Dashboard risks: AI row trim conflicts with the AI-CPU fix, k8s dashboard says 15 panels but defines 17, and several PromQL/LogQL expressions need zero-target and syntax fixes.
- Sources checked: https://github.com/kubernetes/kube-state-metrics/blob/main/docs/metrics/extend/customresourcestate-metrics.md, https://github.com/fluxcd/flux2-monitoring-example, https://prometheus-operator.dev/docs/api-reference/api/, https://grafana.com/docs/loki/latest/query/metric_queries/, https://grafana.com/docs/grafana/latest/datasources/loki/template-variables/

Goal: Add runner/worker resource sections to AI Lab Fleet dashboard, add new Kubernetes releases dashboard, fix dashboard audit issues.

Architecture: Kubernetes/Flux GitOps, kube-prometheus-stack, Loki, PromQL/LogQL. New files under kubernetes/apps/infrastructure/monitoring/, relabel in kubernetes/apps/apps/ai/monitoring.yaml. Dashboard JSON via parse-mutate-minify scripts.

Tech Stack: Talos, Flux, kube-prometheus-stack, Loki, Grafana v39 schema.

## Global Constraints

- GitOps reconcile: Flux on merge to main, pre-merge validation local
- Every ServiceMonitor/PodMonitor: release: kube-prometheus-stack label
- Every dashboard ConfigMap: grafana_dashboard: "1" label
- Datasources: ${DS_PROMETHEUS}, ${DS_LOKI} template vars only
- Cluster context: kubectl --context admin@ai
- Kustomization: register all new files
- Fleet layout: 24-column grid, y>=86, x+w<=24/band, ids>=41 unique
<!-- codex: Add script-level assertions for every generated dashboard: unique ids, no overlaps, x+w<=24, and final Fleet maxY=124. Manual visual validation is not enough for this plan's grid requirements. -->

---

### Task 1: Dev-worker node_exporter scrape

Create dev-workers-node.yaml (mirror ci-runners-node.yaml for .37/.38/.39:9100), register in kustomization, validate with kustomize build.

Produces: node_*{job="dev-worker-node"} for 3 instances.

---

### Task 2: Flux metrics collection (PodMonitor + KSM custom-resource-state)

Create flux-monitoring.yaml PodMonitor selecting flux-system pods (source/kustomize/helm/notification controllers) on port http-prom with release: kube-prometheus-stack label and job: flux relabel.
<!-- codex: Put the PodMonitor in namespace monitoring, use spec.namespaceSelector.matchNames: [flux-system], use spec.selector.matchExpressions on key app In [source-controller,kustomize-controller,helm-controller,notification-controller], and use spec.podMetricsEndpoints (not endpoints). The local Flux pods expose container port name http-prom. -->

Add kube-state-metrics: block to kube-prometheus-stack.yaml spec.values with:
<!-- codex: This must be nested under spec.values["kube-state-metrics"]. For gotk_resource_info, each configured resource needs metricNamePrefix: gotk plus metric name resource_info; otherwise the default metric name will not match the planned queries. Keep default KSM collectors enabled; do not copy Flux upstream collectors: [] or --custom-resource-state-only=true because Task 4 needs default Kubernetes metrics. -->
- rbac.extraRules granting list/watch on Flux CRDs
<!-- codex: extraRules must grant list/watch on plural Flux resources such as kustomizations, helmreleases, gitrepositories, helmrepositories, and ocirepositories in their API groups. The chart adds customresourcedefinitions RBAC when customResourceState.enabled is true only if its normal RBAC creation remains enabled. -->
- customResourceState.enabled: true with config for Kustomization/HelmRelease/GitRepository/HelmRepository/OCIRepository
<!-- codex: Use exact groupVersionKind values: kustomize.toolkit.fluxcd.io/v1 Kustomization, helm.toolkit.fluxcd.io/v2 HelmRelease, and source.toolkit.fluxcd.io/v1 GitRepository/HelmRepository/OCIRepository. -->
- Per-kind revision paths: Kustomization=lastAppliedRevision, HelmRelease=lastAttemptedRevision, GitRepository/etc=artifact.revision
<!-- codex: HelmRelease lastAttemptedRevision exists, but Flux upstream monitoring uses status.history[0].chartVersion/chartName/appVersion. lastAttemptedRevision can represent a failed attempt rather than the deployed ready release; choose and label this semantics explicitly. -->
- Labels: exported_namespace, ready, suspended, revision
<!-- codex: ready values come from Flux condition status and are "True"/"False"/"Unknown"; suspended comes from boolean spec.suspend and is "true"/"false" when present. Queries must use this casing and tolerate missing suspended labels. -->

Register flux-monitoring.yaml in kustomization after alloy.yaml.
Cross-check revision paths against https://github.com/fluxcd/flux2-monitoring-example upstream.
<!-- codex: Upstream also includes Bucket, HelmChart, notification, and image resources. Omitting them is fine only if dashboard queries restrict customresource_kind to the five configured kinds. -->

Produces: gotk_reconcile_* (controllers), gotk_resource_info{customresource_kind, name, exported_namespace, ready, suspended, revision} (KSM).
<!-- codex: gotk_reconcile_* comes from controller scraping via PodMonitor; gotk_resource_info comes only from KSM CRS. Validate both independently. -->

---

### Task 3: AI Lab Fleet — Runners + Workers sections + AI fixes

File: reporting-dashboard.yaml (minified JSON ConfigMap), ai/monitoring.yaml (relabel).

Fix A (AI scrape relabel): In ai/monitoring.yaml ai-llm-node ServiceMonitor (line ~69-71), add relabelings block with targetLabel: job, replacement: ai-llm-node.
<!-- codex: Put relabelings under spec.endpoints[] for the ServiceMonitor. After adding this, filter all amdgpu_* dashboard queries with job="ai-llm-node" to prevent future non-AI node_exporter textfile metrics from leaking into the AI row. -->

Fix B (AI row trim): Transform script edit_reporting.py to:
<!-- codex: The repo currently uses scripts/gen-reporting-dashboard.py, not edit_reporting.py. Use the existing generator or a deterministic JSON parse/mutate/minify script, and fail if JSON cannot round-trip. -->
1. Fix AI-CPU panel: replace instance=~"192.168.0.4[456]:9100" with job="ai-llm-node"
<!-- codex: This conflicts with the next step if AI Node CPU is removed from the kept panel list. Either keep AI Node CPU as a fifth panel or drop this edit and its validation. -->
2. Trim AI row: keep only iGPU Utilization, VRAM (Used vs Total), Decode Throughput, iGPU Temperature in 2x2 layout (x 0/12, w 12, y 50-70 band); remove GTT, Prompt Throughput, Busy Slots, Power, Requests
<!-- codex: The kept panel list excludes AI Node CPU. If intentional, remove the AI-CPU fix from the task; if not, add AI Node CPU to the kept layout. -->
3. Append Runners section (row id=41 y=86): 9 panels (ids 42-50)
   - 42-46: stat tiles (Runners Up green>=5, Cores, Memory, Fleet CPU%, Fleet Mem%)
   - 47-50: timeseries (CPU per runner, Mem per runner, Root Disk %, Network RX+/TX-)
<!-- codex: Up/count stat queries should use "or vector(0)" so zero targets render 0 instead of No data. Percent queries should guard denominators, for example clamp_min(sum(...), 1), to avoid division-by-zero/NaN panels. -->
4. Append Workers section (row id=51 y=105): 9 panels (ids 52-60) identical to Runners with job=dev-worker-node, Up green>=3, y+19 offset
<!-- codex: Apply the same zero-target and denominator guards as the Runners panels. The dev-worker-node ServiceMonitor should mirror ci-runners-node's relabeling pattern exactly. -->

Validate: datasource consistency (all ${DS_PROMETHEUS}), no hardcoded IPs (192.168.0.4*), no gridPos overlaps, unique ids, 6 rows total, maxY=124.
<!-- codex: Also assert Fleet contiguity: existing Storage ends at y=86, Runners row starts y=86, Workers row starts y=105, and every new panel satisfies x+w<=24. -->

Produces: Fleet dashboard with 2 new rows + trimmed AI + IP-free AI-CPU query.
<!-- codex: If AI Node CPU is removed, this output statement is inaccurate. -->

---

### Task 4: New dashboard "Kubernetes — Releases & Workloads"

File: k8s-releases-dashboard.yaml (new ConfigMap).
<!-- codex: Explicitly register k8s-releases-dashboard.yaml in kubernetes/apps/infrastructure/monitoring/kustomization.yaml; otherwise Flux will not apply it. -->

Build script build_k8s_releases.py assembles 3 rows with 15 panels:
<!-- codex: The rows below define 17 non-row panels: 8 Flux + 6 resource usage + 3 logs. Fix the count or remove two panels before using "15 panels" as a validation invariant. -->

Row 1 (Flux releases, id=1 y=0):
- ids 2-7: stat tiles (HelmReleases Ready, Kustomizations Ready, Sources Ready, Suspended yellow>=1, Not Ready red>=1, Reconcile p99 histogram_quantile)
<!-- codex: Ready/Suspended/Not Ready queries must use ready="True", ready!="True", and suspended="true" casing. Add "or vector(0)" for zero matching resources. Sources Ready should restrict customresource_kind to GitRepository|HelmRepository|OCIRepository. -->
- id 8: table (not-ready/suspended resources, instant true, transforms labelsToFields+organize, columns: kind/name/namespace/ready/suspended/revision)
<!-- codex: gotk_resource_info labels are customresource_kind and exported_namespace, not kind and namespace. Rename them in transforms or query aliases. -->
- id 9: timeseries (reconcile duration p50/p99 by controller, histogram_quantile 2 targets, unit s)
<!-- codex: Use histogram_quantile over sum by (le, controller) (rate(gotk_reconcile_duration_seconds_bucket[$__rate_interval])) or equivalent. Without bucket aggregation, p50/p99 will be wrong or empty. -->

Row 2 (resource usage, id=20 y=19):
- ids 21-22: timeseries (CPU by namespace sum by(namespace), Memory working-set by namespace)
- ids 23-24: timeseries (Top 10 pods CPU, Top 10 pods memory topk queries)
<!-- codex: Aggregate before topk, e.g. topk(10, sum by (namespace,pod) (rate(container_cpu_usage_seconds_total{container!="",pod!=""}[$__rate_interval]))); raw topk ranks container/image series, not pods. -->
- id 25: timeseries (Pod restarts 1h increase)
<!-- codex: Use kube_pod_container_status_restarts_total and sum by(namespace,pod,container)(increase(...[1h])); decide whether to show zero via or vector(0) when no restarts exist. -->
- id 26: stat (Pods not ready kube_pod_status_ready{condition="true"}==0, red>=1)
<!-- codex: This expression returns one series per not-ready pod, not a scalar. For a stat use sum(kube_pod_status_ready{condition="true"} == 0) or vector(0), and usually exclude Succeeded pods with kube_pod_status_phase{phase=~"Pending|Running|Unknown"}. -->

Row 3 (errors/warnings logs, id=30 y=41):
- id 31: logs panel (datasource ${DS_LOKI}, expr {namespace=~"$namespace"} |~ "(?i)(error|warn|fatal|panic|exception|fail)", showTime/wrapLogMessage/sortOrder Descending)
<!-- codex: The current Alloy labels include namespace and pod, so this selector is valid. If a pod variable is added, include pod=~"$pod" consistently. -->
- id 32: timeseries (error/warn rate by namespace count_over_time{...} |~ pattern [$__auto], datasource ${DS_LOKI})
<!-- codex: Use valid LogQL syntax: sum by (namespace) (count_over_time({namespace=~"$namespace"} |~ "(?i)(error|warn|fatal|panic|exception|fail)"[$__auto])). The range selector belongs inside count_over_time. $__auto is supported for Loki metric queries. -->
- id 33: timeseries (Top 10 noisiest error/warn pods topk count_over_time)
<!-- codex: Use topk(10, sum by (namespace,pod) (count_over_time({namespace=~"$namespace"} |~ "...regex..."[$__auto]))); otherwise topk ranks individual log streams rather than pods. -->

Template vars: DS_PROMETHEUS, DS_LOKI, namespace query label_values(kube_namespace_status_phase).
<!-- codex: Prometheus namespace variable syntax should be label_values(kube_namespace_status_phase, namespace). If multi/all is enabled, set allValue to a PromQL-safe regex such as .*. -->

Validate: grafana_dashboard: "1" label, datasource vars present, all panels (non-row) have correct datasource uids (${DS_PROMETHEUS} or ${DS_LOKI}), kustomize build.
<!-- codex: Add explicit k8s-releases grid validation; the plan gives row y values but not panel x/y/w/h, so "no overlaps" cannot be verified from this spec alone. -->

Produces: k8s-releases-dashboard.yaml ConfigMap with minified JSON.

---

### Task 5: Open PR

Push branch, gh pr create with title/body.

---

### Task 6: Post-merge live verification

Check: targets up (dev-worker-node 3 series =1), Flux metrics present (gotk_resource_info count, gotk_reconcile_duration_seconds), dashboards render (Fleet + k8s-releases no "No data"), no regressions (existing Fleet rows, AI LLM/Loki/Trivy unchanged).
<!-- codex: Add edge-case checks: one AI node down still shows remaining AI series, zero dev-worker/Flux targets render 0 not No data, Fleet remains contiguous to maxY=124, and missing Flux resources/CRDs do not break dashboard variables or stat panels. -->

---

## Self-Review

Spec coverage: Runners → Task 3, Workers → Task 1+3, k8s dashboard → Task 2+4, Fixes → Task 3.
Placeholder scan: revision paths explicit upstream cross-check; all queries concrete.
<!-- codex: HelmRelease revision semantics are not resolved yet because lastAttemptedRevision differs from Flux upstream's status.history[0].chartVersion. -->
Type/label consistency: job labels consistent (ci-runner-node, dev-worker-node, ai-llm-node); gotk_resource_info labels consistent; panel ids unique.
<!-- codex: gotk_resource_info consistency depends on implementing labelsFromPath exactly; dashboard queries should use customresource_kind/exported_namespace unless transforms rename them. -->

<!-- codex-review-status: finalized -->