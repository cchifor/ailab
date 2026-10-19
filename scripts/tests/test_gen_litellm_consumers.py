#!/usr/bin/env python3
"""Unit tests for scripts/gen-litellm-consumers.py.

The subject is the derivation of the two consumer spans from litellm.yaml's model_list: the
Open WebUI `model_ids` allowlist and the `checksum/config` rollout annotation. Run:

    python -m unittest discover -s scripts/tests -p "test_*.py"

Every test runs against a SANDBOX built from the small fixtures under
fixtures/gen-litellm-consumers/ (never the real manifests), laid out at the same relative paths
the script expects under a throwaway repo root, and the CLI is driven through `--repo` so the
exit codes and messages tested here are the ones CI sees.

The module filename is hyphenated (repo convention), so it is loaded by path.
"""
import importlib.util
import json
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
ALL_REL = (LITELLM_REL, OPEN_WEBUI_REL)

#: What the fixture model_list must select, in model_list order.
EXPECTED_IDS = ["alpha-cloud", "delta-172", "epsilon-local"]

EXPECTED_WEBUI_LINE = (
    "            - { name: OPENAI_API_CONFIGS, value: '"
    '{"0":{"enable":true,"connection_type":"local",'
    '"model_ids":["alpha-cloud","delta-172","epsilon-local"],"tags":["keep me"]},'
    '"1":{"enable":true,"connection_type":"external"}}'
    "' }"
)


class Sandbox:
    """A throwaway repo root holding copies of the two fixtures at their real relative paths."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        for rel, fixture in ((LITELLM_REL, "litellm.yaml"), (OPEN_WEBUI_REL, "open-webui.yaml")):
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
            self.assertEqual(glc.load_models(sb.root), EXPECTED_IDS)

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

    def test_non_boolean_hidden_is_checked_on_every_entry_before_the_other_filters(self):
        # The rule's wording ("hidden must be a YAML boolean") holds for EVERY entry that carries
        # model_info, not only for the self-hosted chat routes the other filters let through: a
        # paid provider (no api_base), a public host, an embedding route.
        for label, api_base, extra in (
            ("no api_base", None, {}),
            ("public host", "https://api.example.com/v1", {}),
            ("embedding mode", "http://10.0.0.5:8080/v1", {"mode": "embedding"}),
        ):
            for bad in ("true", 1):
                with self.subTest(entry=label, hidden=bad):
                    with self.assertRaises(glc.SourceError) as cm:
                        self._visible(api_base, hidden=bad, **extra)
                    self.assertIn("'x'", str(cm.exception))
                    self.assertIn("model_info.hidden", str(cm.exception))

    def test_non_boolean_hidden_on_a_paid_provider_entry_fails_the_cli(self):
        # gpt-4.1 in the fixture has no api_base: it is never listed, and STILL a quoted "true" on
        # it is a failure naming the entry, not something the api_base filter skips over.
        with Sandbox() as sb:
            text = sb.read(LITELLM_REL)
            before = "          api_key: os.environ/OPENAI_API_KEY\n      - model_name: text-embedding-3-small\n"
            self.assertIn(before, text)
            sb.write(LITELLM_REL, text.replace(
                before,
                "          api_key: os.environ/OPENAI_API_KEY\n        model_info: { hidden: \"true\" }\n"
                "      - model_name: text-embedding-3-small\n", 1))
            proc = sb.run("--check")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("ERROR", proc.stdout)
        self.assertIn("gpt-4.1", proc.stdout)
        self.assertIn("model_info.hidden", proc.stdout)
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
            names = glc.load_models(sb.root)
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
                self.assertIn("no consumer-visible route in model_list; refusing to render an empty "
                              "Open WebUI Local group", proc.stdout)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertNotIn("wrote", proc.stdout)
                for rel in ALL_REL:
                    self.assertEqual(sb.read(rel), before[rel], f"{rel} must not be touched")


class DshOnlyIsInert(unittest.TestCase):
    """`model_info.dsh_only` was dsh's opt-in while dsh was a consumer (until 2026-10-19, ADR 0039).

    litellm.yaml still carries one `dsh_only: false`, and editing config.yaml rolls the shared
    LiteLLM pod, so the key stays and the generator ignores it: never an error, never a reason to
    list or withhold a route.
    """

    def test_any_value_selects_exactly_what_the_entry_would_without_it(self):
        for api_base, listed in (("http://10.0.0.5:8080/v1", True), (None, False)):
            for value in (True, False, "true", 1):
                with self.subTest(api_base=api_base, dsh_only=value):
                    entry = {"model_name": "x", "litellm_params": {"model": "openai/x", "api_key": "k"},
                             "model_info": {"dsh_only": value}}
                    if api_base:
                        entry["litellm_params"]["api_base"] = api_base
                    self.assertIs(glc.consumer_visible(entry), listed)
        # Nor does it contradict `hidden` any more: hidden alone decides.
        entry = {"model_name": "x", "litellm_params": {"model": "openai/x", "api_base": "http://10.0.0.5:8080/v1"},
                 "model_info": {"hidden": True, "dsh_only": True}}
        self.assertFalse(glc.consumer_visible(entry))

    def test_a_dsh_only_true_self_hosted_route_is_listed_like_any_other(self):
        with Sandbox() as sb:
            text = sb.read(LITELLM_REL)
            # delta-172 is self-hosted; this key used to hold it OUT of the Local group.
            text = text.replace("{ max_input_tokens: 81920 }", "{ max_input_tokens: 81920, dsh_only: true }", 1)
            # gpt-4.1 is paid (no api_base); this key used to offer it to dsh. It must not leak in.
            paid = "          model: openai/gpt-4.1\n          api_key: os.environ/OPENAI_API_KEY\n"
            self.assertIn(paid, text, "fixture shape changed")
            text = text.replace(paid, paid + "        model_info: { dsh_only: true }\n", 1)
            self.assertEqual(text.count("dsh_only: true"), 2)
            sb.write(LITELLM_REL, text)
            self.assertEqual(glc.load_models(sb.root), EXPECTED_IDS)
            proc = sb.run("--write")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(EXPECTED_WEBUI_LINE, _lines(sb.read(OPEN_WEBUI_REL)))
            self.assertEqual(sb.run("--check").returncode, 0)


class ModelNames(unittest.TestCase):
    """A model_name is written into the JSON inside Open WebUI's single-quoted YAML scalar. json.dumps
    escapes everything JSON needs, so the one name the rewrite cannot carry is one with a single
    quote. (The plain-YAML-scalar rule that also applied until 2026-10-19 existed for dsh's unquoted
    `- id: <name>` seed rows, which this script no longer writes.)"""

    @staticmethod
    def _config(name):
        # The name is JSON-quoted so it always reaches the parser as a STRING model_name.
        return (
            "model_list:\n"
            f"  - model_name: {json.dumps(name)}\n"
            "    litellm_params: { model: openai/x, api_base: http://10.0.0.5:8080/v1, api_key: k }\n"
        )

    def test_names_survive_the_open_webui_rewrite_verbatim(self):
        # Including shapes an unquoted YAML scalar could not carry: the name sits inside JSON.
        names = ["qwen3.5-122b-cloud", "a b", "123", "null", "true", "foo:", "x #y", "@x", "[a]",
                 'a"b', "a\\b", "a\tb", "café"]
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(glc.models_from_config(self._config(name)), [name])
        with Sandbox() as sb:
            span = glc.render_open_webui(sb.read(OPEN_WEBUI_REL), names)
        doc = yaml.safe_load(span.rendered)
        env = {e["name"]: e["value"] for e in doc["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(json.loads(env["OPENAI_API_CONFIGS"])["0"]["model_ids"], names)

    def test_an_empty_name_is_an_error(self):
        with self.assertRaises(glc.SourceError) as cm:
            glc.models_from_config(self._config(""))
        self.assertIn("model_name", str(cm.exception))

    def test_a_single_quote_cannot_sit_inside_the_open_webui_scalar(self):
        # "it's" is a fine YAML string, but the OPENAI_API_CONFIGS value is a single-quoted YAML
        # scalar and json.dumps does not escape a quote, so the name would end the scalar early.
        with self.assertRaises(glc.SourceError) as cm:
            glc.models_from_config(self._config("it's"))
        self.assertIn("it's", str(cm.exception))
        self.assertIn("single quote", str(cm.exception))

    def test_the_cli_names_the_entry_without_a_traceback(self):
        for mode in ("--check", "--write"):
            with self.subTest(mode=mode), Sandbox() as sb:
                sb.write(LITELLM_REL, sb.read(LITELLM_REL).replace("- model_name: alpha-cloud", '- model_name: "it\'s"'))
                before = {rel: sb.read(rel) for rel in ALL_REL}
                proc = sb.run(mode)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("ERROR", proc.stdout)
                self.assertIn("it's", proc.stdout)
                self.assertNotIn("Traceback", proc.stderr)
                for rel in ALL_REL:
                    self.assertEqual(sb.read(rel), before[rel], f"{rel} must not be touched")


class DuplicateModelNames(unittest.TestCase):
    """Several DEPLOYMENTS of one model_name -> exactly ONE Local-group id.

    This is how LiteLLM expresses a router-balanced pool (qwen3.8-27b-ailab runs on ai-node2 and
    ai-node3). A Local-group id is only the name, so the fold has nothing to choose between; the
    supports_vision agreement check that guarded dsh's `input: [text, image]` rows left with dsh.
    """

    DUP = ("      - model_name: alpha-cloud\n        litellm_params:\n          model: openai/alpha\n"
           "          api_base: http://10.0.0.99:8080/v1\n        model_info: { supports_vision: false }\n")

    def _with_dup(self, sb):
        text = sb.read(LITELLM_REL)
        anchor = "    litellm_settings:"
        self.assertIn(anchor, text, "fixture layout changed")
        sb.write(LITELLM_REL, text.replace(anchor, self.DUP + anchor, 1))

    def test_a_second_deployment_emits_one_id_not_two(self):
        with Sandbox() as sb:
            self._with_dup(sb)
            self.assertEqual(glc.load_models(sb.root), EXPECTED_IDS)
            proc = sb.run("--write")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(sb.read(OPEN_WEBUI_REL).count('"alpha-cloud"'), 1)

    def test_a_second_deployment_leaves_open_webui_byte_identical(self):
        # Adding a deployment is a ROUTING change: the Local group must not move at all.
        with Sandbox() as sb:
            sb.run("--write")
            before = sb.read(OPEN_WEBUI_REL)
            self._with_dup(sb)
            proc = sb.run("--write")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(sb.read(OPEN_WEBUI_REL), before)


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

    def test_a_committed_id_containing_a_bracket_is_simply_rewritten(self):
        # The rewrite is structural (json.loads, replace model_ids, json.dumps), so a stale id
        # with ']' in it is just a stale id: no array-matching regex to end early.
        with Sandbox() as sb:
            sb.write(OPEN_WEBUI_REL, sb.read(OPEN_WEBUI_REL).replace('"stale-a"', '"sta]le-a"'))
            proc = sb.run("--check")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("sta]le-a", proc.stdout)
            proc = sb.run("--write")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            line = next(l for l in _lines(sb.read(OPEN_WEBUI_REL)) if "- { name: OPENAI_API_CONFIGS" in l)
            self.assertEqual(line, EXPECTED_WEBUI_LINE)
            self.assertEqual(sb.run("--check").returncode, 0)

    def _webui_with_value(self, sb, value):
        lines = _lines(sb.read(OPEN_WEBUI_REL))
        i = next(k for k, l in enumerate(lines) if "- { name: OPENAI_API_CONFIGS" in l)
        lines[i] = f"            - {{ name: OPENAI_API_CONFIGS, value: '{value}' }}"
        sb.write(OPEN_WEBUI_REL, "\n".join(lines))
        return i

    def test_a_nested_decoy_in_another_connection_is_not_touched(self):
        # Connection "1" carries its own {"0":{"model_ids":[...]}} object: only the TOP-LEVEL "0"
        # is the span, whichever order the connections are written in.
        decoy = {"0": {"model_ids": ["decoy-a", "decoy-b"]}, "note": "keep"}
        for order in (("0", "1"), ("1", "0")):
            with self.subTest(order=order), Sandbox() as sb:
                cfg = {
                    "0": {"enable": True, "connection_type": "local", "model_ids": ["stale-a"], "tags": ["keep me"]},
                    "1": {"enable": True, "connection_type": "external", "nested": decoy},
                }
                cfg = {k: cfg[k] for k in order}
                i = self._webui_with_value(sb, json.dumps(cfg, separators=(",", ":")))
                proc = sb.run("--write")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                line = _lines(sb.read(OPEN_WEBUI_REL))[i]
                got = json.loads(line.split("value: '", 1)[1].rsplit("' }", 1)[0])
                self.assertEqual(list(got), list(order), "connection order is preserved")
                self.assertEqual(got["0"]["model_ids"], EXPECTED_IDS)
                self.assertEqual(list(got["0"]), ["enable", "connection_type", "model_ids", "tags"])
                self.assertEqual(got["1"]["nested"], decoy, "the decoy is byte-for-byte what it was")
                self.assertEqual(sb.run("--check").returncode, 0)

    def test_non_canonical_json_is_a_source_error(self):
        # The file's form is canonical compact JSON (json.dumps with separators (",", ":")); a value
        # written any other way is refused with an instruction, never normalised silently.
        msg = "OPENAI_API_CONFIGS is not canonical compact JSON; normalise it once by hand"
        for label, value in (
            ("space after a colon outside the array",
             '{"0":{"enable": true,"connection_type":"local","model_ids":["stale-a"]},"1":{"enable":true}}'),
            ("space inside the model_ids array",
             '{"0":{"enable":true,"connection_type":"local","model_ids":["stale-a", "stale-b"]},"1":{"enable":true}}'),
            ("trailing space before the closing brace",
             '{"0":{"enable":true,"connection_type":"local","model_ids":["stale-a"]},"1":{"enable":true} }'),
        ):
            for mode in ("--check", "--write"):
                with self.subTest(case=label, mode=mode), Sandbox() as sb:
                    self._webui_with_value(sb, value)
                    text = sb.read(OPEN_WEBUI_REL)
                    with self.assertRaises(glc.SourceError) as cm:
                        glc.render_open_webui(text, glc.load_models(sb.root))
                    self.assertIn(msg, str(cm.exception))
                    proc = sb.run(mode)
                    self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                    self.assertIn("ERROR", proc.stdout)
                    self.assertIn(msg, proc.stdout)
                    self.assertNotIn("Traceback", proc.stderr)
                    self.assertEqual(sb.read(OPEN_WEBUI_REL), text, "nothing is written")

    def test_connection_0_without_model_ids_is_a_source_error(self):
        for label, value in (
            ('"0" lacks model_ids', '{"0":{"enable":true,"connection_type":"local"},"1":{"enable":true}}'),
            ('"0" is not an object', '{"0":["stale-a"],"1":{"enable":true}}'),
            ('no "0" at all', '{"1":{"enable":true,"model_ids":["stale-a"]}}'),
            ('model_ids is not a list of strings', '{"0":{"enable":true,"model_ids":"stale-a"}}'),
        ):
            for mode in ("--check", "--write"):
                with self.subTest(case=label, mode=mode), Sandbox() as sb:
                    self._webui_with_value(sb, value)
                    text = sb.read(OPEN_WEBUI_REL)
                    proc = sb.run(mode)
                    self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                    self.assertIn("ERROR", proc.stdout)
                    self.assertIn("model_ids", proc.stdout)
                    self.assertNotIn("Traceback", proc.stderr)
                    self.assertEqual(sb.read(OPEN_WEBUI_REL), text, "nothing is written")

    def test_comment_mentioning_the_env_name_is_not_the_span(self):
        with Sandbox() as sb:
            sb.run("--write")
            text = sb.read(OPEN_WEBUI_REL)
        self.assertIn("# a comment that mentions OPENAI_API_CONFIGS must not be mistaken", text)


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

    REAL = '        checksum/config: "000000000000"'
    COMMENTED = '        # checksum/config: "deadbeefcafe"  (the previous value, kept as a note)'

    def test_a_commented_out_annotation_is_not_the_anchor(self):
        with Sandbox() as sb:
            lines = _lines(sb.read(LITELLM_REL))
            i = lines.index(self.REAL)
            lines.insert(i, self.COMMENTED)
            sb.write(LITELLM_REL, "\n".join(lines))
            proc = sb.run("--write")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            after = _lines(sb.read(LITELLM_REL))
            self.assertEqual(after[i], self.COMMENTED, "the commented line is untouched")
            self.assertRegex(after[i + 1], r'^        checksum/config: "[0-9a-f]{12}"$')
            self.assertNotIn("000000000000", after[i + 1])
            self.assertEqual(sb.run("--check").returncode, 0)

    def test_only_a_commented_annotation_is_an_error(self):
        for mode in ("--check", "--write"):
            with self.subTest(mode=mode), Sandbox() as sb:
                sb.write(LITELLM_REL, sb.read(LITELLM_REL).replace(self.REAL, "        # " + self.REAL.strip()))
                before = sb.read(LITELLM_REL)
                proc = sb.run(mode)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("ERROR", proc.stdout)
                self.assertIn("expected exactly one checksum/config annotation, found 0", proc.stdout)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(sb.read(LITELLM_REL), before)

    def test_two_uncommented_annotations_is_an_error(self):
        with Sandbox() as sb:
            sb.write(LITELLM_REL, sb.read(LITELLM_REL).replace(self.REAL, self.REAL + "\n" + self.REAL))
            proc = sb.run("--check")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("expected exactly one checksum/config annotation, found 2", proc.stdout)


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


class CheckMode(unittest.TestCase):
    def test_stale_tree_fails_with_one_line_per_span(self):
        with Sandbox() as sb:
            proc = sb.run("--check")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        drift = [l for l in proc.stdout.splitlines() if l.startswith("DRIFT ")]
        self.assertEqual(len(drift), 2, proc.stdout)
        self.assertTrue(any(OPEN_WEBUI_REL in l and "model_ids" in l for l in drift), proc.stdout)
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

    def test_webui_spacing_is_a_source_error_not_drift(self):
        # The OPENAI_API_CONFIGS value must be canonical compact JSON, model_ids array included:
        # spacing there is refused with an instruction (see OpenWebUIRewrite), not reported as
        # drift for --write to normalise.
        with Sandbox() as sb:
            sb.run("--write")
            text = sb.read(OPEN_WEBUI_REL)
            sb.write(OPEN_WEBUI_REL, text.replace('"model_ids":["alpha-cloud","delta-172",',
                                                  '"model_ids":["alpha-cloud", "delta-172",', 1))
            proc = sb.run("--check")
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertNotIn("DRIFT", proc.stdout)
            self.assertIn("ERROR", proc.stdout)
            self.assertIn("not canonical compact JSON", proc.stdout)

    def test_clean_tree_passes_and_check_does_not_write(self):
        with Sandbox() as sb:
            sb.run("--write")
            snapshot = {rel: sb.read(rel) for rel in ALL_REL}
            proc = sb.run("--check")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout.count("OK "), 2, proc.stdout)
            self.assertIn("both spans match", proc.stdout)
            for rel in snapshot:
                self.assertEqual(sb.read(rel), snapshot[rel])
        # A stale tree is not touched by --check either.
        with Sandbox() as sb:
            before = {rel: sb.read(rel) for rel in ALL_REL}
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
            after_first = {rel: sb.read(rel) for rel in ALL_REL}
            second = sb.run("--write")
            self.assertEqual(second.returncode, 0)
            after_second = {rel: sb.read(rel) for rel in ALL_REL}
        self.assertEqual(after_first, after_second)
        self.assertEqual(first.stdout.count("wrote"), 2, first.stdout)
        self.assertNotIn("wrote", second.stdout)
        self.assertEqual(second.stdout.count("unchanged"), 2, second.stdout)

    def test_write_does_not_add_or_drop_lines(self):
        with Sandbox() as sb:
            before = {rel: sb.read(rel) for rel in ALL_REL}
            sb.run("--write")
            for rel, text in before.items():
                self.assertEqual(len(_lines(sb.read(rel))), len(_lines(text)), rel)


class LineEndings(unittest.TestCase):
    """A CRLF checkout must be rewritten only in the owned spans and compared byte for byte."""

    #: The start of each span's anchor line, after its indentation.
    ANCHORS = {OPEN_WEBUI_REL: b"- { name: OPENAI_API_CONFIGS", LITELLM_REL: b'checksum/config: "'}

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
            self.assertEqual(proc.stdout.count("wrote"), 2, proc.stdout)
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

    def test_each_anchor_line_keeps_its_own_line_ending(self):
        # A rewrite replaces the VALUE on its anchor line and keeps that line's own terminator, so
        # one LF line in a CRLF checkout is rewritten as an LF line and nothing else moves.
        def lf_at(data, i):
            lines = data.split(b"\r\n")
            return b"\r\n".join(lines[:i + 1]) + b"\n" + b"\r\n".join(lines[i + 1:])

        with Sandbox() as lf:
            self.assertEqual(lf.run("--write").returncode, 0)
            rendered = {rel: (lf.root / rel).read_bytes().replace(b"\n", b"\r\n") for rel in ALL_REL}
        with Sandbox() as sb:
            self._to_crlf(sb)
            at = {}
            for rel, anchor in self.ANCHORS.items():
                p = sb.root / rel
                data = p.read_bytes()
                [at[rel]] = [k for k, l in enumerate(data.split(b"\r\n")) if l.lstrip().startswith(anchor)]
                p.write_bytes(lf_at(data, at[rel]))
            proc = sb.run("--write")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout.count("wrote"), 2, proc.stdout)
            for rel in ALL_REL:
                self.assertEqual((sb.root / rel).read_bytes(), lf_at(rendered[rel], at[rel]), rel)
            proc = sb.run("--check")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
