import base64, json, subprocess, io

KC = ["kubectl", "--context", "admin@ai", "-n", "testpool-spike"]
SCRATCH = r"C:/Users/chifo/AppData/Local/Temp/claude/C--Users-chifo-work-home-ailab--claude-worktrees-harmonic-wibbling-volcano/738feb38-9091-4a05-99a8-4052b7220936/scratchpad"

sec = json.loads(subprocess.run(KC + ["get", "secret", "tep-worker-spike-token", "-o", "json"],
                                capture_output=True, text=True).stdout)
token = base64.b64decode(sec["data"]["token"]).decode()
ca = sec["data"]["ca.crt"]  # already base64

kubeconfig = f"""apiVersion: v1
kind: Config
clusters:
- name: ai
  cluster:
    server: https://192.168.0.40:6443
    certificate-authority-data: {ca}
contexts:
- name: tep
  context:
    cluster: ai
    user: tep-worker
    namespace: testpool-spike
current-context: tep
users:
- name: tep-worker
  user:
    token: {token}
"""
io.open(SCRATCH + "/tep-kubeconfig", "w", encoding="utf-8", newline="\n").write(kubeconfig)
print("kubeconfig written; token length:", len(token))
