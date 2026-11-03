# Implementation review — relay_agent_skill role and the relay_connector preflight (finalized after round 2)

<!-- codex-impl-review-status: finalized -->

This is the ailab side of Relay v0.9.0 (relay plan `docs/plans/2026-11-03-agent-bus-skill/plan.md`, Phase G):
- a `relay_agent_skill` role that installs the `relay-bus` skill for c4 on the dev-workers;
- a `relay_connector` preflight that refuses a connector pin of 0.9.0 or later while the old `inbox-hint` hook is
  still installed.

The role ships turned off: `relay_agent_skill_enabled: false`, and its sha256 pins are empty, which fails validation.
The v0.9.0 deploy PR fills in the pins and turns it on.

## Summary

- **Round 1** (reviewed 87c50fdb) found three important safety issues. They were fixed in 366cfdac.
- **Round 2** (reviewed 366cfdac) confirmed those three fixes and found two more important issues and a nit. These were
  fixed in 2c3841b0.

Both rounds were static reviews. Round 2 was the last, so the final fixes were verified by tests and by mutation
checks rather than by another review.

## Findings

### Privileged filesystem writes can follow paths changed after validation (R1)

**Severity:** important. **Resolved in 366cfdac.**

- Home writes and removals run as c4, through `files/skill_files.py`.
- The helper walks from the home one directory at a time, using directory descriptors opened without following
  links. A symlink anywhere from `~/.config`, `~/.claude` or `~/.codex` down is refused, including one that appears
  after validation.
- A directory or file owned by another user is refused, never repaired as root.
- `absent` removes only the two files the role installed, then the empty `relay-bus` directories.
- Round 2 confirmed this.

### Destination directories can be overwritten before the role detects the conflict (R2)

**Severity:** important. **Resolved in 366cfdac.**

- `validate.yml` dry-runs the helper before any change, in check mode too.
- If a directory, link or other kind of file sits where `SKILL.md`, `relay-bus.py` or `bus.json` goes, the run is
  refused and that path is left untouched.
- Round 2 confirmed this.

### Concurrent hook mergers can lose changes or restore the legacy hook (R3)

**Severity:** important. **Resolved in 366cfdac.**

- Both `claude_relay_hook.py` and `claude_skill_hook.py` hold one shared exclusive lock,
  `~/.claude/.settings.json.lock`, from the read through the rename. The lock file is 0600 and never opened through a
  link.
- Round 2 confirmed this.

### File preflight misses permissions required for creation and removal (round 2, N1)

**Severity:** important. **Resolved in 2c3841b0.**

- The dry run checks that every planned change can be made: each directory it will create in, remove from, or set a
  mode on must be writable and not on a read-only filesystem. Directories whose mode the role repairs count as
  writable.
- For `absent`, the dry run runs before the hook is removed.
- **Tests:** read-only directories, for `present` and `absent`, in apply and check mode, are refused with nothing
  changed.
- **Mutation check:** disabling the permission check makes the test fail.

### Check mode ignores lock paths that make apply fail (round 2, N2)

**Severity:** important. **Resolved in 2c3841b0.**

- Both hook helpers run the same `check_lock()` whenever a change is due, check mode included, and never create
  anything. An existing lock must be the user's regular file, readable and writable; a missing one must be
  creatable.
- Both roles run this before any file, binary, unit or service change.
- **Tests:** a lock that is a directory, a symlink or a read-only file, for both roles, in both modes.

### The concurrency regression does not guarantee overlapping commits (round 2, N3)

**Severity:** nit. **Resolved in 2c3841b0.**

- Explicit handshakes: writer 1 signals from inside `os.replace` while it holds the lock, and writer 2 starts only
  after that.
- The test asserts writer 2 cannot take the lock until writer 1 has renamed. It covers both orders.
- **Mutation check:** without `flock`, the test fails.

## Verification

Tests ran in Docker: `python:3.12` with ansible-core 2.20.0, as a non-root user.

| Suite | Result |
|---|---|
| `test_relay_agent_skill.py` | 51 run, 1 skipped (the opt-in real-helper case) |
| real-helper case, with the relay-skill branch's assets | 1 OK |
| `test_relay_connectors.py` | 47 run, 1 skipped (already skipped before this work) |

- **ansible-lint 25.9.2, `--offline`, production profile:** the new role and playbook have 0 failures. The
  `relay_connector` role keeps its 6 earlier findings, all in files this work does not touch.
- The CI job timeout is raised to 30 minutes: the two suites take about 20 minutes back to back.
