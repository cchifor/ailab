# Bounded warm-process metadata observation

`scripts/observe-warm-processes.py` is an operator-run Linux collector for an
already authorized, exclusive warm-session window. It never launches a provider,
changes configuration, sends a prompt, discovers an account, or selects a root.
The operator supplies the known root PID **and its start ticks** from the same
PID namespace and boot. An unknown root identity leaves the observation pending.
Keep the existing warm/capacity budgets and owner/operator handoffs in
[WARM-ACCEPTANCE.md](inference-canary-v0113/WARM-ACCEPTANCE.md).

Example after those prerequisites, with operator-supplied numeric variables and
a fresh output path:

```sh
umask 077
set -C
python3 scripts/observe-warm-processes.py \
  --root-pid "$ROOT_PID" --root-start-ticks "$ROOT_START_TICKS" \
  --duration 120 --interval 1 > warm-processes-new.jsonl
```

The shell refuses an existing output file. Use the same PID namespace and
visibility as the reviewed root; a container sees only the `/proc` entries its
namespace and permissions expose. Run without elevated access when the existing
operator identity already has the needed visibility. No additional host access
or inspection of argv, environment, executable paths or process names is needed.

Each JSONL sample contains timestamps, process rows (`pid`, `ppid`, `startTicks`,
`rssBytes`), absence observations and fixed-code gaps. The reader opens only numeric `/proc/PID/stat`
files, at most 4,096 bytes each. It discards the parenthesized command field as
bytes, including embedded parentheses, newlines or invalid UTF-8. RSS is field
24 multiplied by the system page size; start ticks are field 22. No names,
commands, environment, paths, exception text or model content are emitted.
Control records use only fixed `type`, `code` and `reason` values.

The root identity is checked before and after each scan. Observed descendants
are selected from PPID links in that scan, with parent/child start-order checks.
Previously observed `(PID, startTicks)` identities remain tracked if reparented.
A reused PID is reported and is never treated as the old process; its new
identity is selected only if independently present in the current root subtree.
New children of an already detached process are not inferred to be descendants.
Root replacement or an unreadable root stops collection.

For a previously observed identity, `ENOENT` or `ESRCH` (including reaping between
opening and reading stat) produces one `absent` observation
with its PID, start ticks, timestamp and fixed status `absent-at-read`. This is
normal observational data, not a collector failure or proof of reaping. That
identity becomes dormant and is checked again only if its numeric PID returns
in the inventory. Its bounded identity record remains available to distinguish
the same reparented process from PID reuse. For a PID listed in the inventory but
never successfully observed, those errors produce an `inventoryTransient` row
containing only the PID, timestamp and fixed status `inventory-transient`. Its
ancestry is unknown: it may have been inside or outside the root subtree. These
rows are informational and do not force exit 1. Denied reads, malformed stat data
and other read failures remain explicit gaps. Preserve transient observations
when assessing coverage; do not dismiss them as unrelated background processes.

Bounds are enforced: duration 1–900 seconds; interval 0.25–60 seconds; at most
16,384 inventory/tracked PIDs (default 4,096); at most 16 MiB of output (default
8 MiB). `--max-processes` and `--max-output-bytes` may lower those ceilings.
The interval is the pause after a completed scan, not a promised start-to-start
sampling period; use the recorded timestamps for actual timing. Inventory,
tracking, output and read limits never silently truncate a sample.
The deadline is checked between reads and during ancestry traversal; it is not a
hard real-time guarantee against a stalled kernel. Slow samples report an
overrun; SIGINT/deadline-incomplete samples stop explicitly. No catch-up
burst is performed. A final fixed-code stop record is reserved within the output
budget. Exit 0 means the requested duration ended without recorded gaps, and may
include known-identity absences or unknown-ancestry inventory transients; exit 1
means a gap or early stop; exit 2 means startup was refused. These are collector
statuses, not warm-session acceptance verdicts.
Require the final stop record. External termination, a broken output stream or
power loss can leave an incomplete file, which is not a completed observation.

This is sampled, non-atomic metadata. It cannot prove that no process existed
between samples, discover children reparented before observation, establish
provider/account identity, or prove that a missing PID was reaped rather than
unreadable. PID presence also does not distinguish a running process from a
zombie awaiting reaping; the collector intentionally does not emit process state.
Absence is scoped to its read timestamp and current visibility. RSS is not
unique memory or an aggregate memory limit. Correlate the observations with the
operator's known root, exclusive window, source/configuration receipts and
request times; retain gaps and stop records in the acceptance report. Do not
repeat workloads or infer a PASS to conceal incomplete observations.
