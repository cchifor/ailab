#!/usr/bin/env python3
"""Wiring gates for dsh's models, which all come from llm-router since 2026-10-19 (ADR 0039).

WHY THIS EXISTS. A model route in dsh is named in five places that nothing else ties together:
the provider blocks in settings.seed.yaml, the reconcile lines in deployment.yaml's
seed-settings step, the default route (settings.seed.yaml AND the agent-default-model row in
cordis.patch.yml), the Conductor's role routes in conductor.runtime.json, and the worker route in
the team-conductor composition. A name that drifts in any one of them is not a load error -- dsh
boots and the picker looks fine -- it is a failure on the first turn that uses it. These tests
make each reference resolve against the seed.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / "kubernetes" / "apps" / "apps" / "dsh"
ROUTER = "https://router.chifor.me/v1"
KEY_REF = "LLM_ROUTER_API_KEY"
RETIRED = {"litellm", "openai-codex", "openai-codex-realjaynesage", "deepseek-official"}


class _TolerantLoader(yaml.SafeLoader):
    """cordis.patch.yml carries `!!js` tags this test has no need to evaluate."""


_TolerantLoader.add_multi_constructor("", lambda loader, suffix, node: None)


def _seed():
    return yaml.safe_load((APP / "settings.seed.yaml").read_text(encoding="utf-8"))


def _providers():
    return _seed()["llm-pi-ai"]["providers"]


def _models(provider):
    return [m["id"] for m in _providers()[provider]["models"]]


def _seed_settings_script():
    spec = yaml.safe_load((APP / "deployment.yaml").read_text(encoding="utf-8"))["spec"]["template"]["spec"]
    init = next(c for c in spec["initContainers"] if c["name"] == "seed-settings")
    return init["args"][0]


def _patch_rows():
    rows = yaml.load((APP / "cordis.patch.yml").read_text(encoding="utf-8"), Loader=_TolerantLoader)
    return [r for r in rows if isinstance(r, dict)]


class SeedProviders(unittest.TestCase):
    def test_only_router_providers_are_seeded(self):
        self.assertEqual(sorted(_providers()), ["llm-router-claude", "llm-router-codex", "llm-router-local"])

    def test_every_provider_uses_the_router_and_its_key(self):
        for name, p in _providers().items():
            with self.subTest(provider=name):
                self.assertEqual(p["baseURL"], ROUTER)
                self.assertEqual(p["apiKeyEnv"], KEY_REF)
                self.assertTrue(p["models"], "a hand-declared route needs a non-empty models list")

    def test_codex_goes_through_the_smart_pool_on_the_responses_protocol(self):
        p = _providers()["llm-router-codex"]
        self.assertEqual(p["api"], "openai-responses")
        self.assertEqual(p["headers"], {"X-Router-Route": "pool-codex"})
        # Both measured defects of the Responses path reproduce through the router (seed header).
        self.assertEqual(p["compat"], {"supportsStrictMode": True})
        for m in p["models"]:
            with self.subTest(model=m["id"]):
                self.assertNotIn("off", m["reasoningEfforts"], "an `off` row sends reasoning.effort none -> 400")

    def test_claude_and_local_speak_chat_completions(self):
        # /v1/responses answers response.failed for both families through the router.
        for name in ("llm-router-claude", "llm-router-local"):
            self.assertEqual(_providers()[name]["api"], "openai-completions")

    def test_image_input_only_where_the_router_accepts_images(self):
        vision = [m["id"] for p in _providers().values() for m in p["models"] if "image" in (m.get("input") or [])]
        self.assertEqual(vision, ["gpt-6-luna"])

    def test_no_image_generation_model_is_offered_as_a_chat_model(self):
        ids = [m for p in _providers() for m in _models(p)]
        self.assertFalse([i for i in ids if i.startswith("qwen-image")])


class DeploymentReconciles(unittest.TestCase):
    def test_every_seeded_provider_is_reconciled_each_boot(self):
        script = _seed_settings_script()
        for name in _providers():
            with self.subTest(provider=name):
                self.assertRegex(script, rf"(?m)^\s*DSH_PROVIDER={re.escape(name)} node /seed/reconcile-provider\.js\s*$")

    def test_retired_settings_providers_are_unwritten_not_reconciled(self):
        script = _seed_settings_script()
        for name in RETIRED - {"deepseek-official"}:
            with self.subTest(provider=name):
                self.assertIn(f"DSH_PROVIDER={name} DSH_PROVIDER_REMOVE=1 node /seed/reconcile-provider.js", script)
                self.assertNotRegex(script, rf"(?m)^\s*DSH_PROVIDER={re.escape(name)} node ")
        # The bare invocation defaults to `litellm`; it must not come back as a reconcile.
        self.assertNotRegex(script, r"(?m)^\s*node /seed/reconcile-provider\.js\s*$")

    def test_default_model_reconcile_names_every_retired_provider(self):
        script = _seed_settings_script()
        m = re.search(r"DSH_RETIRED_PROVIDERS=([A-Za-z0-9,-]+)", script)
        self.assertIsNotNone(m)
        self.assertEqual(set(m.group(1).split(",")), RETIRED)
        self.assertIn("node /seed/reconcile-default-model.js", script)

    def test_reconcile_script_ships_in_the_seed_configmap(self):
        kust = yaml.safe_load((APP / "kustomization.yaml").read_text(encoding="utf-8"))
        relay = next(g for g in kust["configMapGenerator"] if g["name"] == "dsh-relay")
        self.assertIn("reconcile-default-model.js", relay["files"])

    def test_the_litellm_master_key_is_not_in_the_pod(self):
        text = (APP / "deployment.yaml").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?m)^\s*- name: LITELLM_API_KEY\s*$")
        self.assertNotIn("name: dsh-litellm", (APP / "openbao-eso.yaml").read_text(encoding="utf-8"))


class DefaultRoute(unittest.TestCase):
    def test_seed_default_is_a_seeded_model(self):
        d = _seed()["agent-default-model"]
        self.assertIn(d["model"], _models(d["provider"]))

    def test_composition_default_matches_the_seed(self):
        rows = [r for r in _patch_rows() if r.get("id") == "agent-default-model"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "@deepseek-ai/dsh-agent-default-model")
        self.assertEqual(rows[0]["config"], _seed()["agent-default-model"])

    def test_shipped_deepseek_adapter_is_disabled_by_the_documented_row_form(self):
        rows = [r for r in _patch_rows() if r.get("id") == "llm-deepseek"]
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0].get("disabled"), True)
        # The include plugin SKIPS a row whose name does not match the shipped one, silently.
        self.assertEqual(rows[0]["name"], "@deepseek-ai/dsh-llm-deepseek")
        self.assertNotIn("config", rows[0])


class ConductorRoutes(unittest.TestCase):
    def test_every_role_route_is_a_seeded_model_with_an_admission_bound(self):
        config = json.loads((APP / "conductor.runtime.json").read_text(encoding="utf-8"))
        for role, route in config["models"].items():
            with self.subTest(role=role):
                self.assertIn(route["model"], _models(route["provider"]))
                self.assertIn(f"{route['provider']}/{route['model']}", config["admission"]["routes"])
        for key in config["admission"]["routes"]:
            provider, model = key.split("/", 1)
            self.assertIn(model, _models(provider), key)

    def test_team_compositions_route_children_to_seeded_models(self):
        # Rows nest inside group rows, so walk the whole tree for every agentOptions block.
        def routes(node):
            if isinstance(node, dict):
                if isinstance(node.get("agentOptions"), dict):
                    yield node["agentOptions"]
                for v in node.values():
                    yield from routes(v)
            elif isinstance(node, list):
                for v in node:
                    yield from routes(v)

        found = 0
        for path in sorted((APP / "agent-teams").glob("*.agent.cordis.yml")):
            for opts in routes(yaml.load(path.read_text(encoding="utf-8"), Loader=_TolerantLoader)):
                found += 1
                with self.subTest(file=path.name, route=opts):
                    self.assertIn(opts["model"], _models(opts["provider"]))
        self.assertGreater(found, 0, "team-conductor routes its workers explicitly; none found")


@unittest.skipUnless(shutil.which("node"), "node is not installed here")
class ReconcileDefaultModel(unittest.TestCase):
    SCRIPT = APP / "reconcile-default-model.js"

    def _run(self, live_text, retired=",".join(sorted(RETIRED))):
        with tempfile.TemporaryDirectory() as d:
            live = pathlib.Path(d) / "settings.yaml"
            # newline="": the pod's file is LF, and write_text would make it CRLF on Windows.
            live.write_text(live_text, encoding="utf-8", newline="")
            env = dict(os.environ, DSH_SETTINGS=str(live), DSH_SEED=str(APP / "settings.seed.yaml"),
                       DSH_RETIRED_PROVIDERS=retired)
            proc = subprocess.run(["node", str(self.SCRIPT)], env=env, capture_output=True, text=True)
            return proc, live.read_bytes().decode("utf-8")

    def test_a_retired_default_is_repointed_and_the_rest_kept(self):
        live = ("llm-pi-ai:\n  providers:\n    x:\n      api: a\n"
                "agent-default-model:\n  provider: litellm\n  model: qwen3.8-flash-next-cloud\n"
                "agent-presets:\n  default: team-conductor\n")
        proc, out = self._run(live)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = yaml.safe_load(out)
        self.assertEqual(doc["agent-default-model"], _seed()["agent-default-model"])
        self.assertEqual(doc["agent-presets"], {"default": "team-conductor"})
        self.assertEqual(doc["llm-pi-ai"], {"providers": {"x": {"api": "a"}}})

    def test_a_current_default_is_left_alone(self):
        live = "agent-default-model:\n  provider: llm-router-codex\n  model: gpt-6-sol\n"
        proc, out = self._run(live)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out, live)

    def test_flow_form_and_crlf_are_handled(self):
        live = "ui: 1\r\nagent-default-model: { provider: deepseek-official, model: deepseek-v4-flash }\r\n"
        proc, out = self._run(live)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("deepseek-official", out)
        self.assertNotIn("\n", out.replace("\r\n", ""), "the file's CRLF endings must be kept")
        self.assertEqual(yaml.safe_load(out)["agent-default-model"], _seed()["agent-default-model"])

    def test_a_comment_is_never_read_as_the_active_provider(self):
        # A comment naming a retired provider above a valid key must not trigger a rewrite...
        live = ("agent-default-model:\n  # previous provider: litellm\n  # provider: litellm\n"
                "  provider: llm-router-codex\n  model: gpt-6-sol\n")
        proc, out = self._run(live)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out, live)
        # ...and a comment naming a current provider must not mask a retired active key.
        live = "agent-default-model:\n  # provider: llm-router-codex\n  provider: litellm\n  model: x\n"
        proc, out = self._run(live)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(yaml.safe_load(out)["agent-default-model"], _seed()["agent-default-model"])

    def test_only_a_direct_child_provider_key_counts(self):
        live = ("agent-default-model:\n  meta:\n    provider: litellm\n"
                "  provider: llm-router-local\n  model: qwen3.8-27b\n")
        proc, out = self._run(live)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(out, live)

    def test_absent_default_and_unreadable_shapes_never_fail_the_boot(self):
        for live in ("ui: 1\n", "agent-default-model: litellm/x\nagent-default-model: y\n",
                     "agent-default-model: litellm\n", "agent-default-model: &d { provider: litellm }\n"):
            with self.subTest(live=live):
                proc, out = self._run(live)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(out, live)


if __name__ == "__main__":
    unittest.main()
