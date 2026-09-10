#!/usr/bin/env python3
"""Gate the dsh agent-team presets.

WHY THIS EXISTS AS A TEST RATHER THAN A REVIEW HABIT. A preset the roster cannot
load is reported as a BROKEN CARD in the UI, not as a failure anywhere an
operator watches -- dsh boots fine, the team is simply unusable, and nothing
goes red. Every invariant below is therefore silent in production.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEAMS = ROOT / "kubernetes" / "apps" / "apps" / "dsh" / "agent-teams"
KUSTOMIZATION = ROOT / "kubernetes" / "apps" / "apps" / "dsh" / "kustomization.yaml"
PATCH = ROOT / "kubernetes" / "apps" / "apps" / "dsh" / "cordis.patch.yml"

# The shipped root is PREPENDED before every configured root and wins duplicate
# ids, so a team taking one of these names would be silently unreachable.
SHIPPED_IDS = {"standard", "ptc", "cordis", "minimal"}

# Rows that hand an agent a delegation tool.
DELEGATION_ROWS = {
    "tool-subagent", "tool-subagent-fork", "tool-subagent-control",
    "tool-subagent-list-agents", "tool-subagent-codex", "tool-subagent-claude-code",
    "tool-workflow", "workflow-worker-thread", "tool-ralph",
}
# ACTIVATED. Delegation is live, so the "everything must be dormant" gate is gone
# and a different one takes its place: an enabled row must carry its BOUNDS.
# Dormancy was never the safety property -- it was a placeholder for bounds that
# had not been decided yet.
DELEGATION_ACTIVATED = True

# Bounds required on an ENABLED row, by row id. A row absent from this map needs
# none (tool-subagent-control and tool-subagent-list-agents take no config; the
# out-of-process providers reject a numeric maxDepth and must stay
# 'provider-managed', so requiring a number there would fail the mount).
REQUIRED_BOUNDS = {
    "tool-subagent": ["maxDepth"],
    "tool-subagent-fork": ["maxDepth"],
    "tool-ralph": ["maxRounds"],
    "workflow-worker-thread": ["maxConcurrentAgents", "maxTotalAgents"],
}
# tool-ralph ships maxRounds: 64 upstream, which is far too high for a shared
# 9-GPU estate; this is the ceiling this repo will accept.
MAX_RALPH_ROUNDS = 8


def teams():
    return sorted(p.name[: -len(".preset.yml")] for p in TEAMS.glob("*.preset.yml"))


def orphan_compositions():
    """Compositions with no matching preset.yml.

    Everything else here is driven by the *.preset.yml glob -- the same shape the
    initContainer's projection loop uses -- so a lone agent.cordis.yml is invisible
    to both: it ships in the ConfigMap, occupies a content hash, and mounts nothing,
    with no signal anywhere. An asymmetric guard only catches the half you thought of.
    """
    have = set(teams())
    return sorted(
        p.name[: -len(".agent.cordis.yml")]
        for p in TEAMS.glob("*.agent.cordis.yml")
        if p.name[: -len(".agent.cordis.yml")] not in have
    )


def rows(text):
    """(id, disabled) for every row, in file order."""
    found, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)- id: (\S+)\s*$", line)
        if not m:
            continue
        indent, rid = m.group(1), m.group(2)
        disabled = False
        for nxt in lines[i + 1 : i + 5]:
            # stop at the next row at the same or shallower indent
            if re.match(r"^\s*- id: ", nxt) or (nxt.strip() and not nxt.startswith(indent + " ")):
                break
            if re.match(r"^\s*disabled:\s*true\s*$", nxt):
                disabled = True
                break
        found.append((rid, disabled))
    return found


def _duplicate_keys(path):
    """Duplicate mapping keys anywhere in a composition, via a strict loader."""
    try:
        import yaml
    except ImportError:                                    # pragma: no cover
        return None
    found = []

    class Strict(yaml.SafeLoader):
        pass

    # `!!js` is dsh's own tag; the value is irrelevant here, only the shape.
    Strict.add_constructor("tag:yaml.org,2002:js", lambda l, n: None)

    def mapping(loader, node, deep=False):
        seen = set()
        for k, _ in node.value:
            key = loader.construct_object(k, deep=True)
            if key in seen:
                found.append(key)
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        yaml.load(path.read_text(encoding="utf-8"), Loader=Strict)
    except Exception:                                      # noqa: BLE001
        return None                                        # parse errors are another test's job
    return sorted(set(found))


def _row_config(text, row_id):
    """The `key: value` scalars in one row's config block, as a dict of strings."""
    lines, out, seen = text.splitlines(), {}, False
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)- id: (\S+)\s*$", line)
        if not m or m.group(2) != row_id:
            continue
        seen = True
        indent = m.group(1)
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= len(indent):
                break
            km = re.match(r"^\s+([A-Za-z][A-Za-z0-9_]*):\s*(\S.*)$", nxt)
            if km:
                out.setdefault(km.group(1), km.group(2).strip())
        break
    return out if seen else None


def _bounds_problems(team, text, found):
    """An ENABLED delegation row must carry the bounds this repo requires.

    Replaces the dormancy gate. Dormancy stopped fan-out by making it
    unreachable; now that it is reachable, what stops it running away is the
    bounds, and a bound nobody asserts is a bound that quietly disappears in a
    regeneration.
    """
    problems = []
    for rid, keys in REQUIRED_BOUNDS.items():
        enabled = [(r, dis) for r, dis in found if r == rid and not dis]
        if not enabled:
            continue                       # absent or dormant: nothing to bound
        cfg = _row_config(text, rid) or {}
        for key in keys:
            if key not in cfg:
                problems.append(f"{team}: row '{rid}' is ENABLED without a '{key}' bound")
        if rid == "tool-ralph" and "maxRounds" in cfg:
            try:
                if int(cfg["maxRounds"]) > MAX_RALPH_ROUNDS:
                    problems.append(
                        f"{team}: tool-ralph maxRounds={cfg['maxRounds']} exceeds "
                        f"{MAX_RALPH_ROUNDS} -- upstream ships 64, too high for this estate"
                    )
            except ValueError:
                problems.append(f"{team}: tool-ralph maxRounds is not a number")
    return problems


def check():
    fails = []
    names = teams()
    if not names:
        fails.append("no teams found -- did the directory move?")

    for orphan in orphan_compositions():
        fails.append(
            f"{orphan}: has {orphan}.agent.cordis.yml but no {orphan}.preset.yml -- it would "
            f"ship in the ConfigMap and mount nothing"
        )

    for team in names:
        preset = TEAMS / f"{team}.preset.yml"
        comp = TEAMS / f"{team}.agent.cordis.yml"

        if not comp.exists():
            fails.append(f"{team}: has {preset.name} but no {comp.name}")
            continue

        if not team.startswith("team-"):
            fails.append(f"{team}: team ids must be prefixed 'team-'")
        if team in SHIPPED_IDS:
            fails.append(f"{team}: collides with a shipped preset id; the shipped root wins")

        ptext = preset.read_text(encoding="utf-8")
        for field in ("name:", "description:"):
            if not re.search(rf"^{field}", ptext, re.M):
                fails.append(f"{team}: preset.yml has no {field.rstrip(':')} -- the picker card needs it")

        ctext = comp.read_text(encoding="utf-8")

        # DUPLICATE MAPPING KEYS. The generator appends config keys, and an
        # append beside an existing key rather than a replacement produced
        # `maxRounds: 64` followed by `maxRounds: 8` -- two values for one bound,
        # which YAML resolves last-wins and no reader notices. Cheap to assert,
        # and it is the shape a regeneration bug takes.
        dup = _duplicate_keys(comp)
        if dup:
            fails.append(f"{team}: duplicate mapping key(s) in the composition: {dup}")

        found = rows(ctext)
        if not found:
            fails.append(f"{team}: composition parses as no rows at all")
        ids = [r for r, _ in found]
        dupes = {r for r in ids if ids.count(r) > 1}
        if dupes:
            fails.append(f"{team}: duplicate row ids {sorted(dupes)}")

        if not DELEGATION_ACTIVATED:
            live = [r for r, dis in found if r in DELEGATION_ROWS and not dis]
            if live:
                fails.append(
                    f"{team}: delegation rows {sorted(live)} are ENABLED while "
                    f"DELEGATION_ACTIVATED is False. A preset is selectable the moment "
                    f"it merges -- this would expose fan-out before its bounds exist."
                )
        else:
            fails.extend(_bounds_problems(team, ctext, found))

        # Every row must name a module; a row with an id and no name mounts nothing.
        for i, line in enumerate(ctext.splitlines()):
            m = re.match(r"^(\s*)- id: (\S+)\s*$", line)
            if not m:
                continue
            following = ctext.splitlines()[i + 1 : i + 2]
            if not following or not re.match(r"^\s+(name|config|group|disabled):", following[0]):
                fails.append(f"{team}: row '{m.group(2)}' is followed by nothing usable")

        # Shipped in the content-hashed ConfigMap, or it never reaches the pod.
        ktext = KUSTOMIZATION.read_text(encoding="utf-8")
        for f in (f"agent-teams/{team}.preset.yml", f"agent-teams/{team}.agent.cordis.yml"):
            if f not in ktext:
                fails.append(f"{team}: {f} is not in kustomization.yaml's configMapGenerator")

    # The root has to be registered, and `default` restated -- a patch REPLACES config.
    dtext = PATCH.read_text(encoding="utf-8")
    if "path: /dsh-teams" not in dtext:
        fails.append("cordis.patch.yml does not register the /dsh-teams root")
    block = dtext.split("- id: agent-presets", 1)
    if len(block) < 2:
        fails.append("cordis.patch.yml has no agent-presets row")
    elif not re.search(r"^\s+default:\s*\S+", block[1], re.M):
        fails.append(
            "cordis.patch.yml's agent-presets row omits `default:` -- a patch REPLACES a "
            "row's config, so this erases the roster default rather than keeping it"
        )
    return fails


class AgentTeams(unittest.TestCase):
    """One assertion per invariant would re-walk the tree per test for no gain;
    check() already names the failing team and invariant in each message."""

    def test_every_team_is_loadable_and_correctly_gated(self):
        problems = check()
        self.assertEqual(problems, [], "\n  " + "\n  ".join(problems))


if __name__ == "__main__":
    unittest.main()
