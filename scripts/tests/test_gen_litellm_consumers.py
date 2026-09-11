#!/usr/bin/env python3
"""Unit tests for scripts/gen-litellm-consumers.py.

The subject is the derivation of the three consumer spans from litellm.yaml's model_list: the
Open WebUI `model_ids` allowlist, the dsh seed's `models:` rows, and the `checksum/config`
rollout annotation. Run:

    python -m unittest discover -s scripts/tests -p "test_*.py"

Every test runs against a SANDBOX built from the small fixtures under
fixtures/gen-litellm-consumers/ (never the real manifests), laid out at the same relative paths
the script expects under a throwaway repo root, and the CLI is driven through `--repo` so the
exit codes and messages tested here are the ones CI sees.

The module filename is hyphenated (repo convention), so it is loaded by path.
"""
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
_MOD_PATH = _SCRIPTS / "gen-litellm-consumers.py"
_CIH_PATH = _SCRIPTS / "check-inline-hashes.py"
_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "gen-litellm-consumers"
_RECONCILE_JS = _SCRIPTS.parent / "kubernetes/apps/apps/dsh/reconcile-provider.js"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # must NOT perform any I/O at import time
    return mod


glc = _load("gen_litellm_consumers", _MOD_PATH)
cih = _load("check_inline_hashes", _CIH_PATH)

LITELLM_REL = "kubernetes/apps/apps/ai/litellm.yaml"
OPEN_WEBUI_REL = "kubernetes/apps/apps/ai/open-webui.yaml"
SEED_REL = "kubernetes/apps/apps/dsh/settings.seed.yaml"
ALL_REL = (LITELLM_REL, OPEN_WEBUI_REL, SEED_REL)

#: What the fixture model_list must select, in model_list order, with the vision flag.
EXPECTED = [("alpha-cloud", True), ("delta-172", False), ("epsilon-local", False)]
EXPECTED_IDS = [name for name, _ in EXPECTED]

EXPECTED_WEBUI_LINE = (
    "            - { name: OPENAI_API_CONFIGS, value: '"
    '{"0":{"enable":true,"connection_type":"local",'
    '"model_ids":["alpha-cloud","delta-172","epsilon-local"],"tags":["keep me"]},'
    '"1":{"enable":true,"connection_type":"external"}}'
    "' }"
)

EXPECTED_SEED_ROWS = [
    "        - id: alpha-cloud",
    "          input: [text, image]",
    "        - id: delta-172",
    "        - id: epsilon-local",
]


class Sandbox:
    """A throwaway repo root holding copies of the three fixtures at their real relative paths."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        for rel, fixture in ((LITELLM_REL, "litellm.yaml"), (OPEN_WEBUI_REL, "open-webui.yaml"),
                             (SEED_REL, "settings.seed.yaml")):
            dst = self.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_FIXTURES / fixture, dst)
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False

    def read(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")

    def write(self, rel, text):
        (self.root / rel).write_text(text, encoding="utf-8", newline="")

    def run(self, *args):
        return subprocess.run(
            [sys.executable, str(_MOD_PATH), "--repo", str(self.root), *args],
            capture_output=True, text=True, encoding="utf-8",
        )


def _lines(text):
    return text.split("\n")


# ---------------------------------------------------------------------------
# the selection rule
# ---------------------------------------------------------------------------


class SelectionRule(unittest.TestCase):
    def test_fixture_selection_and_order(self):
        with Sandbox() as sb:
            models = glc.load_models(sb.root)
        self.assertEqual([(m.name, m.vision) for m in models], EXPECTED)

    def _visible(self, api_base=None, **model_info):
        entry = {"model_name": "x", "litellm_params": {"model": "openai/x", "api_key": "k"}}
        if api_base is not None:
            entry["litellm_params"]["api_base"] = api_base
        if model_info:
            entry["model_info"] = model_info
        return glc.consumer_visible(entry)

    def test_private_ipv4_ranges_included(self):
        for host in ("10.0.0.5", "10.255.255.254", "172.16.0.1", "172.31.255.254", "192.168.0.28"):
            with self.subTest(host=host):
                self.assertTrue(self._visible(f"http://{host}:8080/v1"))

    def test_cluster_local_service_included(self):
        self.assertTrue(self._visible("http://llm-node2.ai.svc.cluster.local:8080/v1"))
        self.assertTrue(self._visible("http://litellm.ai.svc.cluster.local/v1"))

    def test_public_and_non_rfc1918_hosts_excluded(self):
        for host in ("api.example.com", "172.32.0.1", "172.15.255.255", "100.64.0.1", "127.0.0.1",
                     "169.254.1.1", "11.0.0.1", "svc.cluster.local.example.com", "cluster.local"):
            with self.subTest(host=host):
                self.assertFalse(self._visible(f"http://{host}:8080/v1"))

    def test_no_api_base_excluded(self):
        self.assertFalse(self._visible())
        self.assertFalse(self._visible(mode="embedding"))

    def test_hidden_opt_out_excluded(self):
        self.assertFalse(self._visible("http://10.0.0.5:8080/v1", hidden=True))
        # A boolean false is the (redundant) default; it is not an opt-out.
        self.assertTrue(self._visible("http://10.0.0.5:8080/v1", hidden=False))

    def test_non_boolean_hidden_is_an_error_naming_the_entry(self):
        # "true" (a YAML string), 1, 0: a typo here must never silently mean "visible".
        for bad in ("true", "false", "yes", 1, 0, None, [True]):
            with self.subTest(hidden=bad):
                with self.assertRaises(glc.SourceError) as cm:
                    self._visible("http://10.0.0.5:8080/v1", hidden=bad)
                self.assertIn("'x'", str(cm.exception))
                self.assertIn("model_info.hidden", str(cm.exception))

    def test_non_boolean_hidden_fails_the_cli_naming_the_entry(self):
        with Sandbox() as sb:
            sb.write(LITELLM_REL, sb.read(LITELLM_REL).replace("{ hidden: true,", '{ hidden: "true",'))
            proc = sb.run("--check")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("ERROR", proc.stdout)
        self.assertIn("beta-hidden", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_non_chat_mode_excluded(self):
        base = "http://10.0.0.5:8080/v1"
        self.assertTrue(self._visible(base, mode="chat"))
        for mode in ("embedding", "rerank", "completion", "image_generation", "audio_transcription"):
            with self.subTest(mode=mode):
                self.assertFalse(self._visible(base, mode=mode))

    def test_self_hosted_embedding_route_is_not_listed(self):
        # The fixture carries zeta-embed-local: a PRIVATE api_base with model_info.mode: embedding.
        with Sandbox() as sb:
            self.assertIn("zeta-embed-local", sb.read(LITELLM_REL))
            names = [m.name for m in glc.load_models(sb.root)]
        self.assertNotIn("zeta-embed-local", names)
        self.assertEqual(names, EXPECTED_IDS)

    def _hide_every_route(self, sb):
        text = sb.read(LITELLM_REL)
        text = text.replace("max_input_tokens: 81920 }", "max_input_tokens: 81920, hidden: true }")
        text = text.replace("max_input_tokens: 114688 }", "max_input_tokens: 114688, hidden: true }")
        self.assertEqual(text.count("hidden: true"), 4, "every private-api_base fixture route is now hidden")
        sb.write(LITELLM_REL, text)

    def test_empty_selection_is_refused_in_both_modes(self):
        for mode in ("--check", "--write"):
            with self.subTest(mode=mode), Sandbox() as sb:
                self._hide_every_route(sb)
                before = {rel: sb.read(rel) for rel in ALL_REL}
                proc = sb.run(mode)
                self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("ERROR", proc.stdout)
                self.assertIn("no consumer-visible route in model_list; refusing to render empty consumer lists",
                              proc.stdout)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertNotIn("wrote", proc.stdout)
                for rel in ALL_REL:
                    self.assertEqual(sb.read(rel), before[rel], f"{rel} must not be touched")

    def test_vision_only_from_supports_vision_true(self):
        with Sandbox() as sb:
            by_name = {m.name: m for m in glc.load_models(sb.root)}
        self.assertTrue(by_name["alpha-cloud"].vision)
        self.assertFalse(by_name["delta-172"].vision)      # no supports_vision at all
        self.assertFalse(by_name["epsilon-local"].vision)  # supports_vision: false


# ---------------------------------------------------------------------------
# the rewrites
# ---------------------------------------------------------------------------


class OpenWebUIRewrite(unittest.TestCase):
    def test_only_the_model_ids_array_changes(self):
        with Sandbox() as sb:
            before = _lines(sb.read(OPEN_WEBUI_REL))
            self.assertEqual(sb.run("--write").returncode, 0)
            after = _lines(sb.read(OPEN_WEBUI_REL))
        self.assertEqual(len(before), len(after))
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(len(changed), 1, "exactly one line may change")
        self.assertEqual(after[changed[0]], EXPECTED_WEBUI_LINE)

    def test_json_keys_and_order_survive(self):
        with Sandbox() as sb:
            sb.run("--write")
            line = next(l for l in _lines(sb.read(OPEN_WEBUI_REL)) if "- { name: OPENAI_API_CONFIGS" in l)
        value = line.split("value: '", 1)[1].rsplit("' }", 1)[0]
        cfg = json.loads(value)
        self.assertEqual(list(cfg), ["0", "1"])
        self.assertEqual(list(cfg["0"]), ["enable", "connection_type", "model_ids", "tags"])
        self.assertEqual(cfg["0"]["model_ids"], EXPECTED_IDS)
        self.assertEqual(cfg["0"]["tags"], ["keep me"])
        self.assertEqual(cfg["1"], {"enable": True, "connection_type": "external"})

    def test_rewrite_that_yields_invalid_json_is_a_clean_error(self):
        # A committed model_ids string containing ']' ends the array match early, so the textual
        # rewrite leaves a dangling tail: the post-rewrite validation must report it, not crash.
        with Sandbox() as sb:
            sb.write(OPEN_WEBUI_REL, sb.read(OPEN_WEBUI_REL).replace('"stale-a"', '"sta]le-a"'))
            text = sb.read(OPEN_WEBUI_REL)
            with self.assertRaises(glc.SourceError) as cm:
                glc.render_open_webui(text, glc.load_models(sb.root))
            self.assertIn("rewritten OPENAI_API_CONFIGS is not valid JSON", str(cm.exception))
            for mode in ("--check", "--write"):
                with self.subTest(mode=mode):
                    proc = sb.run(mode)
                    self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                    self.assertIn("ERROR", proc.stdout)
                    self.assertIn("rewritten OPENAI_API_CONFIGS is not valid JSON", proc.stdout)
                    self.assertNotIn("Traceback", proc.stderr)
                    self.assertEqual(sb.read(OPEN_WEBUI_REL), text, "nothing is written")

    def test_comment_mentioning_the_env_name_is_not_the_span(self):
        with Sandbox() as sb:
            sb.run("--write")
            text = sb.read(OPEN_WEBUI_REL)
        self.assertIn("# a comment that mentions OPENAI_API_CONFIGS must not be mistaken", text)


class SeedRewrite(unittest.TestCase):
    def test_surrounding_lines_and_comments_are_byte_identical(self):
        with Sandbox() as sb:
            before = _lines(sb.read(SEED_REL))
            self.assertEqual(sb.run("--write").returncode, 0)
            after = _lines(sb.read(SEED_REL))
        first = before.index("        - id: stale-a")
        last = before.index("        - id: stale-b")
        self.assertEqual(after[:first], before[:first], "everything before the first row is untouched")
        self.assertEqual(after[first:first + len(EXPECTED_SEED_ROWS)], EXPECTED_SEED_ROWS)
        self.assertEqual(after[first + len(EXPECTED_SEED_ROWS):], before[last + 1:],
                         "everything after the models list is untouched")
        # The explanatory comment lines directly above the first row are part of the untouched prefix.
        self.assertEqual(after[first - 2], "        # explanatory comment BEFORE the first row: preserved byte for byte")
        self.assertEqual(after[first - 1], "        # (two lines of it, to prove the whole run survives)")

    def test_parsed_shape_is_what_reconcile_provider_needs(self):
        with Sandbox() as sb:
            sb.run("--write")
            text = sb.read(SEED_REL)
        doc = yaml.safe_load(text)
        litellm = doc["llm-pi-ai"]["providers"]["litellm"]
        self.assertEqual(litellm["models"], [
            {"id": "alpha-cloud", "input": ["text", "image"]},
            {"id": "delta-172"},
            {"id": "epsilon-local"},
        ])
        self.assertEqual(doc["llm-pi-ai"]["providers"]["other-provider"]["models"], [{"id": "untouched-model"}])
        lines = _lines(text)
        # Plain block headers at direct-child depth: the exact walk reconcile-provider.js performs.
        self.assertIn("llm-pi-ai:", lines)
        self.assertIn("  providers:", lines)
        self.assertIn("    litellm:", lines)
        self.assertIn("      models:", lines)

    @unittest.skipUnless(shutil.which("node"), "node is not installed here")
    def test_reconcile_provider_js_splices_the_generated_rows(self):
        with Sandbox() as sb:
            sb.run("--write")
            live = sb.root / "settings.yaml"
            live.write_text(
                "llm-pi-ai:\n"
                "  providers:\n"
                "    litellm:\n"
                "      api: openai-completions\n"
                "      models:\n"
                "        - id: gone-a\n"
                "    other-provider:\n"
                "      api: openai-responses\n"
                "      models:\n"
                "        - id: untouched-model\n"
                "ui:\n"
                "  theme: light\n",
                encoding="utf-8",
            )
            env = dict(os.environ, DSH_SETTINGS=str(live), DSH_SEED=str(sb.root / SEED_REL))
            proc = subprocess.run(["node", str(_RECONCILE_JS)], env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("now: alpha-cloud, delta-172, epsilon-local", proc.stdout)
            spliced = yaml.safe_load(live.read_text(encoding="utf-8"))
        self.assertEqual(spliced["llm-pi-ai"]["providers"]["litellm"]["models"], [
            {"id": "alpha-cloud", "input": ["text", "image"]},
            {"id": "delta-172"},
            {"id": "epsilon-local"},
        ])
        self.assertEqual(spliced["ui"], {"theme": "light"})


class Checksum(unittest.TestCase):
    def test_annotation_matches_check_inline_hashes_derivation(self):
        with Sandbox() as sb:
            sb.run("--write")
            saved = cih.REPO
            cih.REPO = sb.root
            try:
                site = cih.check_litellm_config_checksum()
            finally:
                cih.REPO = saved
            text = sb.read(LITELLM_REL)
        self.assertEqual(site.expected, site.actual)
        self.assertNotEqual(site.expected, "000000000000")
        self.assertEqual(glc.config_checksum(text), site.actual)
        self.assertIn(f'checksum/config: "{site.actual}"', text)

    def test_only_the_annotation_line_changes(self):
        with Sandbox() as sb:
            before = _lines(sb.read(LITELLM_REL))
            sb.run("--write")
            after = _lines(sb.read(LITELLM_REL))
        changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(len(before), len(after))
        self.assertEqual(len(changed), 1)
        self.assertIn('checksum/config: "', after[changed[0]])


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


class CheckMode(unittest.TestCase):
    def test_stale_tree_fails_with_one_line_per_span(self):
        with Sandbox() as sb:
            proc = sb.run("--check")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        drift = [l for l in proc.stdout.splitlines() if l.startswith("DRIFT ")]
        self.assertEqual(len(drift), 3, proc.stdout)
        self.assertTrue(any(OPEN_WEBUI_REL in l and "model_ids" in l for l in drift), proc.stdout)
        self.assertTrue(any(SEED_REL in l and "models" in l for l in drift), proc.stdout)
        self.assertTrue(any(LITELLM_REL in l and "checksum/config" in l for l in drift), proc.stdout)
        self.assertIn("--write", proc.stdout)

    def test_default_mode_is_check(self):
        with Sandbox() as sb:
            self.assertEqual(sb.run().returncode, 1)
            sb.run("--write")
            self.assertEqual(sb.run().returncode, 0)

    def test_check_names_only_the_drifted_span(self):
        with Sandbox() as sb:
            sb.run("--write")
            text = sb.read(OPEN_WEBUI_REL).replace('"model_ids":["alpha-cloud",', '"model_ids":[')
            sb.write(OPEN_WEBUI_REL, text)
            proc = sb.run("--check")
        self.assertEqual(proc.returncode, 1)
        drift = [l for l in proc.stdout.splitlines() if l.startswith("DRIFT ")]
        self.assertEqual(len(drift), 1, proc.stdout)
        self.assertIn(OPEN_WEBUI_REL, drift[0])
        self.assertIn("alpha-cloud", drift[0])

    def _formatting_drift(self, rel, before, after):
        with Sandbox() as sb:
            sb.run("--write")
            text = sb.read(rel)
            self.assertIn(before, text)
            sb.write(rel, text.replace(before, after, 1))
            proc = sb.run("--check")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            drift = [l for l in proc.stdout.splitlines() if l.startswith("DRIFT ")]
            self.assertEqual(len(drift), 1, proc.stdout)
            self.assertIn(rel, drift[0])
            self.assertIn("ids match", drift[0])
            self.assertIn("formatting", drift[0])
            self.assertIn("extra lines", drift[0])
            self.assertIn("--write", drift[0])
            self.assertNotIn("->", drift[0], "no committed -> derived pair: the ids are the same")
            # --write normalises it back to the canonical rendering.
            self.assertEqual(sb.run("--write").returncode, 0)
            self.assertEqual(sb.read(rel), text)
            self.assertEqual(sb.run("--check").returncode, 0)

    def test_seed_hand_added_comment_is_formatting_drift(self):
        self._formatting_drift(
            SEED_REL,
            "        - id: delta-172\n",
            "        - id: delta-172\n        # a hand-added comment inside the generated span\n",
        )

    def test_seed_extra_key_on_a_row_is_formatting_drift(self):
        self._formatting_drift(
            SEED_REL,
            "        - id: delta-172\n",
            "        - id: delta-172\n          temperature: 0.2\n",
        )

    def test_webui_spacing_is_formatting_drift(self):
        self._formatting_drift(
            OPEN_WEBUI_REL,
            '"model_ids":["alpha-cloud","delta-172",',
            '"model_ids":["alpha-cloud", "delta-172",',
        )

    def test_clean_tree_passes_and_check_does_not_write(self):
        with Sandbox() as sb:
            sb.run("--write")
            snapshot = {rel: sb.read(rel) for rel in (LITELLM_REL, OPEN_WEBUI_REL, SEED_REL)}
            proc = sb.run("--check")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout.count("OK "), 3, proc.stdout)
            # A stale tree is not touched by --check either.
            sb2_before = snapshot
            for rel in snapshot:
                self.assertEqual(sb.read(rel), sb2_before[rel])
        with Sandbox() as sb:
            before = {rel: sb.read(rel) for rel in (LITELLM_REL, OPEN_WEBUI_REL, SEED_REL)}
            sb.run("--check")
            for rel in before:
                self.assertEqual(sb.read(rel), before[rel])

    def test_unparseable_source_is_a_failure_not_a_skip(self):
        with Sandbox() as sb:
            sb.write(LITELLM_REL, sb.read(LITELLM_REL).replace("model_list:", "model_list: ["))
            proc = sb.run("--check")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ERROR", proc.stdout + proc.stderr)


class WriteMode(unittest.TestCase):
    def test_write_is_idempotent(self):
        with Sandbox() as sb:
            first = sb.run("--write")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            after_first = {rel: sb.read(rel) for rel in (LITELLM_REL, OPEN_WEBUI_REL, SEED_REL)}
            second = sb.run("--write")
            self.assertEqual(second.returncode, 0)
            after_second = {rel: sb.read(rel) for rel in (LITELLM_REL, OPEN_WEBUI_REL, SEED_REL)}
        self.assertEqual(after_first, after_second)
        self.assertEqual(first.stdout.count("wrote"), 3, first.stdout)
        self.assertNotIn("wrote", second.stdout)
        self.assertEqual(second.stdout.count("unchanged"), 3, second.stdout)

    def test_write_does_not_add_or_drop_lines(self):
        with Sandbox() as sb:
            before = {rel: sb.read(rel) for rel in (LITELLM_REL, OPEN_WEBUI_REL)}
            sb.run("--write")
            for rel, text in before.items():
                self.assertEqual(len(_lines(sb.read(rel))), len(_lines(text)), rel)
            # The seed grows by exactly (new rows) - (old rows): 4 lines replace 3.
            seed_before = _lines(_FIXTURES.joinpath("settings.seed.yaml").read_text(encoding="utf-8"))
            self.assertEqual(len(_lines(sb.read(SEED_REL))), len(seed_before) + 1)


class LineEndings(unittest.TestCase):
    """A CRLF checkout must be rewritten only in the owned spans and compared byte for byte."""

    @staticmethod
    def _to_crlf(sb):
        for rel in ALL_REL:
            p = sb.root / rel
            data = p.read_bytes()
            assert b"\r" not in data, "fixtures are LF"
            p.write_bytes(data.replace(b"\n", b"\r\n"))

    def test_write_keeps_crlf_and_matches_the_lf_rendering(self):
        with Sandbox() as lf:
            self.assertEqual(lf.run("--write").returncode, 0)
            expected = {rel: (lf.root / rel).read_bytes().replace(b"\n", b"\r\n") for rel in ALL_REL}
        with Sandbox() as sb:
            self._to_crlf(sb)
            proc = sb.run("--write")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout.count("wrote"), 3, proc.stdout)
            for rel in ALL_REL:
                got = (sb.root / rel).read_bytes()
                self.assertEqual(got.count(b"\n"), got.count(b"\r\n"), f"{rel}: every line break stays CRLF")
                self.assertNotIn(b"\r\r", got)
                self.assertEqual(got, expected[rel], rel)
            proc = sb.run("--check")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_checksum_is_the_same_for_crlf_and_lf(self):
        # The annotation describes the YAML-parsed ConfigMap value, whose line breaks YAML (and
        # check-inline-hashes' universal-newline read) normalise to LF.
        with Sandbox() as sb:
            lf = glc.config_checksum(sb.read(LITELLM_REL))
            self._to_crlf(sb)
            crlf_text = (sb.root / LITELLM_REL).read_bytes().decode("utf-8")
        self.assertIn("\r\n", crlf_text)
        self.assertEqual(glc.config_checksum(crlf_text), lf)

    def test_check_compares_real_bytes(self):
        with Sandbox() as sb:
            self._to_crlf(sb)
            sb.run("--write")
            p = sb.root / SEED_REL
            data = p.read_bytes()
            row = b"        - id: delta-172\r\n"
            self.assertIn(row, data)
            p.write_bytes(data.replace(row, b"        - id: delta-172\n", 1))
            proc = sb.run("--check")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        drift = [l for l in proc.stdout.splitlines() if l.startswith("DRIFT ")]
        self.assertEqual(len(drift), 1, proc.stdout)
        self.assertIn(SEED_REL, drift[0])


if __name__ == "__main__":
    unittest.main()
