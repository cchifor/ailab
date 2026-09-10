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

# Rows that hand an agent a delegation tool. Until the plan's steps 2-5 land
# (bounds, night-window routing, workspace contract) every one of these must be
# dormant -- a preset has no off switch, so merging a team's files makes it
# selectable immediately.
DELEGATION_ROWS = {
    "tool-subagent", "tool-subagent-fork", "tool-subagent-control",
    "tool-subagent-list-agents", "tool-subagent-codex", "tool-subagent-claude-code",
    "tool-workflow", "workflow-worker-thread", "tool-ralph",
}
# Flip to True in the WP-1b PR that also removes the flags.
DELEGATION_ACTIVATED = False


def teams():
    return sorted(p.name[: -len(".preset.yml")] for p in TEAMS.glob("*.preset.yml"))


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


def check():
    fails = []
    names = teams()
    if not names:
        fails.append("no teams found -- did the directory move?")

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
