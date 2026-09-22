#!/usr/bin/env python3
"""Wiring gates for the dsh `claude-cli` model provider (ADR 0030).

WHY THIS EXISTS. This route has three properties that are load-bearing, invisible
in the thing that carries them, and individually easy to "tidy up" into a defect:

  1. `--disallowed-tools '*'` is what makes it a TEXT tier. `isolateTools: true`
     looks like it does that job -- upstream's README says it strips Claude's
     tooling -- but it only passes --strict-mcp-config, which removes MCP servers
     and leaves every BUILT-IN tool in place: Bash, Read, Edit, inside a pod whose
     ServiceAccount is cluster-admin. Measured in the live pod 2026-09-22: with
     both flags the CLI's init event reports "tools":[]; with only the first it
     reports its full built-in set. Deleting one line in a YAML comment block
     silently converts a reasoning route into a shell.

  2. The row's `command` must be the WRAPPER, not a bare binary. The wrapper is
     where the subscription credential is read and where the two variables that
     outrank it are unset. Pointing `command` straight at `claude` would produce a
     route that authenticates with whatever the pod happens to have.

  3. The provider is THREE files named by ONE row, installed by seed-settings from
     the dsh-relay ConfigMap. A file missing from the generator is not a degraded
     feature: a row whose module cannot resolve throws at profile load, and on a
     1-replica Recreate Deployment that is the web UI going down.

Everything here is a static check of the manifests, so it costs nothing and runs
where the cluster does not.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
DSH = ROOT / "kubernetes" / "apps" / "apps" / "dsh"
PATCH = DSH / "cordis.patch.yml"
JOB = DSH / "install-job.yaml"
DEPLOY = DSH / "deployment.yaml"
KUST = DSH / "kustomization.yaml"
WRAPPER = DSH / "claude-cli.sh"
PROVIDER = DSH / "claude-cli-provider.mjs"

# The three modules the row needs and the wrapper it spawns. All four must be in the
# dsh-relay generator AND installed by seed-settings, or the row cannot resolve.
PROVIDER_FILES = (
    "claude-cli-provider.mjs",
    "claude-cli-translate.mjs",
    "claude-cli-images.mjs",
)
# Deliberately NOT under /dsh-home/.local/bin, which is the first entry of the dsh container's
# PATH: a wrapper there is one word away from the agent's own shell.
WRAPPER_PATH = "/dsh-home/.claude-cli/bin/claude-cli"
PATH_DIR = "/dsh-home/.local/bin"

# The flags that make this a text tier, measured in the live pod 2026-09-22 by reading the tool
# list out of the CLI's own init event: no flags 22 tools, --strict-mcp-config alone 22 tools,
# an explicit deny list 14 tools, each of these 0.
TOOL_POLICY_FLAGS = ("--tools", "--disallowed-tools", "--setting-sources")

# Claude Fable 5.1 is refused by older CLI builds; the docs state 2.1.255 as the floor.
FABLE_MIN_CLI = (2, 1, 255)


class _TolerantLoader(yaml.SafeLoader):
    """cordis.patch.yml carries `!!js` tags this test has no need to evaluate."""


_TolerantLoader.add_multi_constructor("", lambda loader, suffix, node: None)


def _patch_rows():
    doc = yaml.load(PATCH.read_text(encoding="utf-8"), Loader=_TolerantLoader)
    return [r for r in doc if isinstance(r, dict)]


def _claude_row():
    """The provider row, found the way the loader finds it: inside an `insert:`."""
    for row in _patch_rows():
        for entry in row.get("insert") or []:
            if isinstance(entry, dict) and entry.get("id") == "claude-cli-provider":
                return entry
    return None


def _wrapper_code():
    """The wrapper with its comments stripped.

    The comments legitimately name the anti-patterns they forbid ("NEVER add --bare"), so a
    substring check over the whole file would fail on its own documentation. Same discipline as
    test_dsh_install_job.py.
    """
    return "\n".join(
        line for line in WRAPPER.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


def _job_env():
    doc = yaml.safe_load(JOB.read_text(encoding="utf-8"))
    container = doc["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value") for e in container["env"]}


def _dsh_container_env():
    doc = yaml.safe_load(DEPLOY.read_text(encoding="utf-8"))
    containers = doc["spec"]["template"]["spec"]["containers"]
    dsh = next(c for c in containers if c["name"] == "dsh")
    return {e["name"]: e.get("value") for e in dsh["env"] if "value" in e}


class TheRowIsAnInsert(unittest.TestCase):
    """A NEW row must be inserted; an override of an absent id is a silent no-op."""

    def test_the_provider_row_exists_as_an_insert(self):
        self.assertIsNotNone(
            _claude_row(),
            "claude-cli-provider must be introduced with `insert:` -- an override of an id no "
            "shipped bundle declares is skipped with one stderr line while the pod boots fine",
        )

    def test_it_names_the_vendored_file_relatively(self):
        # A bare package specifier resolves from the profile directory's symlink farm, which holds
        # dsh's own closure only -- MODULE_NOT_FOUND at boot, and boot() rethrows.
        self.assertEqual(_claude_row().get("name"), "./claude-cli-provider.mjs")


class TheTextOnlyTierIsPinned(unittest.TestCase):
    """Both flags, not just the one that looks like it does the job."""

    def setUp(self):
        self.config = (_claude_row() or {}).get("config") or {}

    def test_isolate_tools_is_on(self):
        self.assertIs(self.config.get("isolateTools"), True)

    def test_the_policy_is_enforced_in_the_wrapper_not_only_in_the_row(self):
        # Flags in the row apply to ONE call site. The agent has a shell in this container, so
        # `claude-cli -p ...` from its own Bash must get the same policy -- otherwise the text
        # tier is a property of how dsh calls the CLI, not of the route.
        code = _wrapper_code()
        for flag in TOOL_POLICY_FLAGS:
            self.assertIn(
                flag, code,
                f"{flag} must be applied by the wrapper, so every invocation carries it",
            )

    def test_the_wrapper_empties_the_tool_list_both_ways(self):
        code = _wrapper_code()
        # The positive form (an empty allowlist) and the negative one. Each measured to zero on
        # its own; both are sent because they fail differently.
        self.assertRegex(code, r'--tools\s+""')
        self.assertRegex(code, r'--disallowed-tools\s+"\*"')

    def test_settings_files_are_not_loaded(self):
        # Claude Code reads .claude/settings.json from its cwd, which can carry PreToolUse HOOKS:
        # arbitrary shell inside a process holding the subscription token.
        self.assertRegex(_wrapper_code(), r'--setting-sources\s+""')

    def test_the_working_directory_is_not_the_agent_writable_tree(self):
        cwd = self.config.get("cwd")
        self.assertIsNotNone(cwd, "an unset cwd defaults to /workspace, which the agent may write")
        self.assertFalse(
            cwd.startswith("/workspace"),
            "/workspace is the sandbox root model-authored code writes; CLAUDE.md and "
            ".claude/settings.json would be read from it on the next Claude turn",
        )

    def test_images_are_coupled_to_the_tool_policy(self):
        # With no tools the model cannot open a materialised attachment, so declaring image
        # support would be a capability claim the route cannot honour. These two move together:
        # flipping `images` back on while the tools stay denied recreates the exact failure the
        # deviation exists to prevent.
        tools_denied = '--tools ""' in _wrapper_code().replace("'", '"')
        if tools_denied:
            self.assertIs(
                self.config.get("images"), False,
                "images must stay false while the wrapper denies every tool",
            )

    def test_bare_mode_is_never_used(self):
        # --bare does NOT read CLAUDE_CODE_OAUTH_TOKEN; it would de-authenticate the route while
        # looking like a startup optimisation.
        self.assertNotIn("--bare", self.config.get("extraArgs") or [])
        # Judge the CODE, not the comments that explain it -- the wrapper deliberately NAMES this
        # anti-pattern in a warning, the same way test_dsh_install_job.py has to ignore comments
        # that name `corepack enable`.
        self.assertNotIn("--bare", _wrapper_code())


class TheCredentialGoesThroughTheWrapper(unittest.TestCase):
    def test_command_is_the_wrapper(self):
        self.assertEqual(
            (_claude_row() or {}).get("config", {}).get("command"), WRAPPER_PATH,
            "pointing `command` at a bare binary skips the credential and the precedence unsets",
        )

    def test_seed_settings_installs_the_wrapper_executable(self):
        text = DEPLOY.read_text(encoding="utf-8")
        self.assertRegex(
            text, rf"install -m 0755 /seed/claude-cli\.sh {re.escape(WRAPPER_PATH)}\b",
            "the wrapper must be installed 0755 at the path the row names",
        )

    def test_the_wrapper_is_not_on_the_agents_path(self):
        self.assertFalse(
            WRAPPER_PATH.startswith(PATH_DIR + "/"),
            f"{PATH_DIR} is the first PATH entry of the dsh container, so a wrapper there is one "
            "word away from model-authored code. Friction, not a boundary -- but keep it.",
        )

    def test_the_child_environment_is_an_allowlist_not_a_denylist(self):
        # Upstream spawns the child with the whole of process.env, so without a scrub the Claude
        # Code process inherits LITELLM_API_KEY, CODEX_API_KEY and every DSH_* variable.
        #
        # It must be an ALLOWLIST. The denylist this was written as first -- dsh-subprocess's own
        # KEY/PASSWORD/SECRET/TOKEN plus DSH_* -- was exercised in the pod with planted variables
        # and GITEA_PAT walked straight through it. A denylist has to guess every shape a
        # credential name can take; this estate already has one that fits none of them.
        code = _wrapper_code()
        self.assertRegex(
            code, r'\*\)\s*unset "\$_n"',
            "the default branch of the scrub must UNSET, so anything not named is dropped",
        )
        keep = re.search(r"^\s*(PATH\|[A-Z_|]+)\)\s*;;", code, re.M)
        self.assertIsNotNone(keep, "expected an explicit keep-list branch naming PATH first")
        kept = set(keep.group(1).split("|"))
        self.assertTrue(
            {"PATH", "HOME"} <= kept, "PATH and HOME are needed for the CLI to run at all"
        )
        # Anything credential-shaped in the keep-list defeats the point.
        for name in kept:
            self.assertFalse(
                re.search(r"KEY|TOKEN|SECRET|PASSWORD|PAT$", name),
                f"{name} is credential-shaped and must not be in the keep-list",
            )

    def test_the_wrapper_unsets_the_two_higher_precedence_variables(self):
        code = _wrapper_code()
        for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            # re.M, because `^`/`$` anchor the whole string otherwise and this is a line match.
            self.assertRegex(
                code, re.compile(rf"^unset {var}$", re.M),
                msg=f"{var} outranks CLAUDE_CODE_OAUTH_TOKEN; unset it rather than blanking it",
            )

    def test_the_adapter_itself_carries_no_token_logic(self):
        # The adapter runs inside the process that executes model-authored tool calls. Credential
        # handling belongs in the wrapper; this keeps that seam from eroding.
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", PROVIDER.read_text(encoding="utf-8"))


class TheVendoredFilesResolve(unittest.TestCase):
    def test_the_provider_imports_its_renamed_siblings(self):
        text = PROVIDER.read_text(encoding="utf-8")
        self.assertIn("'./claude-cli-translate.mjs'", text)
        self.assertIn("'./claude-cli-images.mjs'", text)
        # Upstream's own specifiers would resolve to files this repo does not ship.
        self.assertNotIn("'./translate.js'", text)
        self.assertNotIn("'./images.js'", text)

    def test_every_file_is_in_the_relay_configmap(self):
        doc = yaml.safe_load(KUST.read_text(encoding="utf-8"))
        relay = next(g for g in doc["configMapGenerator"] if g["name"] == "dsh-relay")
        for name in PROVIDER_FILES + ("claude-cli.sh",):
            self.assertIn(
                name, relay["files"],
                f"{name} is not in the dsh-relay generator, so seed-settings would install "
                "nothing and the row would be a MODULE_NOT_FOUND crash-loop at boot",
            )

    def test_seed_settings_installs_every_module(self):
        text = DEPLOY.read_text(encoding="utf-8")
        for name in PROVIDER_FILES:
            self.assertIn(f"install -m 0644 /seed/{name} /dsh-home/profiles/web/{name}", text)


class TheCliPinIsCoherent(unittest.TestCase):
    def setUp(self):
        self.env = _job_env()

    def test_both_pins_are_present_and_well_formed(self):
        version = self.env.get("CLAUDE_CODE_VERSION")
        sha = self.env.get("CLAUDE_CODE_SHA256")
        self.assertIsNotNone(version, "the Job must pin the CLI version")
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertIsNotNone(sha, "an unverified 217 MB download is not a supply chain")
        self.assertRegex(sha, r"^[0-9a-f]{64}$")

    def test_the_pin_can_actually_serve_the_models_offered(self):
        version = tuple(int(p) for p in self.env["CLAUDE_CODE_VERSION"].split("."))
        ids = [m["id"] for m in (_claude_row() or {}).get("config", {}).get("models", [])]
        if any(i.startswith("claude-fable-5-1") for i in ids):
            self.assertGreaterEqual(
                version, FABLE_MIN_CLI,
                "Claude Fable 5.1 needs Claude Code >= 2.1.255; this pin would refuse it",
            )

    def test_the_deployment_agrees_with_the_job(self):
        # kustomize derives the Deployment's copy from the Job, so a drifted literal is not what
        # ships -- but it is what a reader believes, and what a bare `kubectl apply` would use.
        self.assertEqual(
            _dsh_container_env().get("CLAUDE_CODE_VERSION"), self.env["CLAUDE_CODE_VERSION"]
        )

    def test_the_download_is_verified_before_it_is_published(self):
        code = "\n".join(
            line for line in JOB.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        self.assertIn("$CLAUDE_CODE_SHA256", code, "the checksum must be compared, not just stored")
        # Publish only via a verified temp file: install to .new then mv, never curl straight to
        # the destination, or an interrupted download leaves a half-file that looks installed.
        self.assertRegex(code, r'install -m 0755 /tmp/claude "\$CC_BIN\.new"')
        self.assertRegex(code, r'mv -f "\$CC_BIN\.new" "\$CC_BIN"')
        # glibc build: the runtime image is Debian. The musl build installs fine and fails at exec.
        self.assertIn("/linux-x64/claude", code)


class TheJobRenameIsComplete(unittest.TestCase):
    """A Job's pod template is immutable: its contents and its name move together, and every
    kustomize replacement that names it has to move too or the derived values silently stop."""

    def test_every_replacement_names_the_current_job(self):
        job_name = yaml.safe_load(JOB.read_text(encoding="utf-8"))["metadata"]["name"]
        doc = yaml.safe_load(KUST.read_text(encoding="utf-8"))
        sources = [r["source"] for r in doc["replacements"]]
        job_sources = [s for s in sources if s.get("kind") == "Job"]
        self.assertTrue(job_sources, "expected the install Job to be a replacement source")
        for src in job_sources:
            self.assertEqual(
                src["name"], job_name,
                "a replacement naming a Job that no longer exists is a SILENT no-op: the "
                "Deployment keeps whatever literal it was written with",
            )

    def test_the_version_is_derived_into_the_deployment(self):
        doc = yaml.safe_load(KUST.read_text(encoding="utf-8"))
        wanted = "spec.template.spec.containers.[name=dsh].env.[name=CLAUDE_CODE_VERSION].value"
        derived = [
            path
            for r in doc["replacements"]
            if r["source"].get("fieldPath", "").endswith("[name=CLAUDE_CODE_VERSION].value")
            for t in r["targets"]
            for path in t["fieldPaths"]
        ]
        self.assertIn(
            wanted, derived,
            "claude-cli.sh builds the binary path from CLAUDE_CODE_VERSION; if it is not derived "
            "from the Job, a bump stages one version and execs another",
        )

    def test_the_job_keeps_its_flux_force_annotation(self):
        # scripts/pin-image-digests.py refuses to pin ANY digest in the estate while a
        # Flux-applied Job lacks this, so dropping it turns into a repo-wide deploy block.
        doc = yaml.safe_load(JOB.read_text(encoding="utf-8"))
        self.assertEqual(
            doc["metadata"]["annotations"].get("kustomize.toolkit.fluxcd.io/force"), "enabled"
        )


class TheDefaultModelIsUnchanged(unittest.TestCase):
    def test_claude_cli_is_not_the_agent_default(self):
        # A model that can never emit a tool call cannot drive dsh's agent loop. Upstream's README
        # sets agent-default-model to this provider; copying that would break every session.
        for row in _patch_rows():
            if row.get("id") == "agent-default-model":
                self.assertNotEqual((row.get("config") or {}).get("provider"), "claude-cli")
            for entry in row.get("insert") or []:
                if isinstance(entry, dict) and entry.get("id") == "agent-default-model":
                    self.assertNotEqual((entry.get("config") or {}).get("provider"), "claude-cli")


if __name__ == "__main__":
    unittest.main()
