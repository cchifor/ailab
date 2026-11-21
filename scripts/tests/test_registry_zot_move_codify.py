#!/usr/bin/env python3
"""Unit tests for the zot move's codified live state (2026-11-21).

Subject: ansible/group_vars/registry.yml, ansible/roles/registry_zot/{defaults/main.yml,tasks/main.yml,
templates/config.json.j2}.

  1. the registry group's values ARE the live state: the strive retention hold (100000 / 87600h), retention
     dryRun true, and the running binary pinned to v2.1.18 WITHOUT a renovate annotation;
  2. the binary-change guard: an unpinned version fails, matching bytes pass, an upgrade or a downgrade fails
     without registry_zot_allow_binary_change and passes with it, a first install passes; get_url verifies the
     checksum from the same map. (The guard's input comes from `stat`, which also runs in --check mode.)
  3. the writer fence: registry_zot_read_only=true leaves no action other than "read" anywhere in
     http.accessControl (anonymous, ci, pull, every scoped push user, the admin group, adminPolicy), the default
     render keeps every write grant, and toggling it is classified as a RELOAD by the apply helper.

Run: python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import importlib.util
import json
import pathlib
import unittest

import jinja2
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
ROLE = REPO / "ansible" / "roles" / "registry_zot"
GROUP_VARS = REPO / "ansible" / "group_vars" / "registry.yml"
EXTRA = {"registry_ci_password": "test-render-only", "registry_oidc_client_secret": "test-render-only"}


def _bool(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.strip().lower()
    return value in ("yes", "on", "1", "true", 1)


def _env():
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["to_json"] = json.dumps
    env.filters["bool"] = _bool
    return env


def _context(overrides=None, with_group=True):
    ctx = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    if with_group:
        ctx.update(yaml.safe_load(GROUP_VARS.read_text()))
    ctx.update(EXTRA)
    ctx.update(overrides or {})
    return ctx


def _render(overrides=None, with_group=True):
    tpl = _env().from_string((ROLE / "templates" / "config.json.j2").read_text())
    return json.loads(tpl.render(**_context(overrides, with_group)))


def _actions(cfg):
    ac = cfg["http"]["accessControl"]
    out = []
    for repo, pol in ac["repositories"].items():
        out.append((f"anonymous {repo}", pol.get("anonymousPolicy", [])))
        # zot grants defaultPolicy to any authenticated user no policy matches: a write there would bypass the fence.
        out.append((f"default {repo}", pol.get("defaultPolicy", [])))
        for p in pol.get("policies", []):
            out.append((f"{repo} {p.get('users') or p.get('groups')}", p["actions"]))
    if "adminPolicy" in ac:
        out.append(("adminPolicy", ac["adminPolicy"]["actions"]))
    return out


class LiveStateCodified(unittest.TestCase):
    def test_group_values_are_the_live_hold_dryrun_and_binary(self):
        g = yaml.safe_load(GROUP_VARS.read_text())
        self.assertEqual(g["registry_zot_strive_keep_recent"], 100000)
        self.assertEqual(g["registry_zot_strive_keep_within"], "87600h")
        self.assertIs(g["registry_zot_retention_dry_run"], True)
        self.assertNotIn("registry_zot_version", g, "the v2.1.18 pin is removed after the cutover")

    def test_the_pin_is_invisible_to_renovate(self):
        # renovate's regex manager keys on the literal "renovate:" marker: none may appear anywhere in the file.
        self.assertNotIn("renovate:", GROUP_VARS.read_text())

    def test_the_role_default_is_the_installed_v2_1_23(self):
        d = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        self.assertEqual(d["registry_zot_version"], "v2.1.23")

    def test_rendered_retention_holds_and_is_dry_run(self):
        ret = _render()["storage"]["retention"]
        self.assertIs(ret["dryRun"], True)
        strive = next(p for p in ret["policies"] if p["repositories"] == ["strive/**"])
        sha = next(k for k in strive["keepTags"] if k["patterns"] == ["^sha-.*"])
        self.assertEqual((sha["mostRecentlyPushedCount"], sha["pushedWithin"]), (100000, "87600h"))


class BinaryGuard(unittest.TestCase):
    def setUp(self):
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
        names = [t.get("name", "") for t in tasks]
        self.stat_i = next(i for i, n in enumerate(names) if n == "Read the installed zot binary's checksum")
        self.guard_i = next(i for i, n in enumerate(names) if n.startswith("Refuse an unpinned version"))
        self.get_i = next(i for i, n in enumerate(names) if n == "Install the zot binary")
        self.guard = tasks[self.guard_i]["ansible.builtin.assert"]["that"]
        self.get_url = tasks[self.get_i]["ansible.builtin.get_url"]
        self.defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())

    def _passes(self, version, installed, allow=False, arch="amd64", sha_map=None):
        ctx = {"registry_zot_version": version, "registry_zot_arch": arch,
               "registry_zot_sha256": sha_map if sha_map is not None else self.defaults["registry_zot_sha256"],
               "registry_zot_allow_binary_change": allow,
               "registry_zot_installed": {"stat": {"exists": installed is not None, "checksum": installed}}}
        env = _env()
        return all(env.compile_expression(expr)(**ctx) for expr in self.guard)

    def test_order_stat_guard_get_url(self):
        self.assertLess(self.stat_i, self.guard_i)
        self.assertLess(self.guard_i, self.get_i)

    def test_get_url_verifies_the_same_checksum(self):
        self.assertEqual(self.get_url["checksum"],
                         "sha256:{{ registry_zot_sha256[registry_zot_version][registry_zot_arch] }}")
        self.assertIn("{{ registry_zot_arch }}", self.get_url["url"])

    def test_defaults(self):
        d = self.defaults
        self.assertIs(d["registry_zot_allow_binary_change"], False)
        self.assertIn(d["registry_zot_version"], d["registry_zot_sha256"])
        self.assertIn(d["registry_zot_arch"], d["registry_zot_sha256"][d["registry_zot_version"]])
        self.assertIn("v2.1.18", d["registry_zot_sha256"])

    def test_checksum_is_selected_by_arch(self):
        fake = {"v1": {"amd64": "a" * 64, "arm64": "b" * 64}}
        self.assertTrue(self._passes("v1", "b" * 64, arch="arm64", sha_map=fake), "arm64 bytes match arm64 entry")
        self.assertFalse(self._passes("v1", "a" * 64, arch="arm64", sha_map=fake), "amd64 bytes on arm64")
        self.assertFalse(self._passes("v2.1.23", None, arch="arm64"), "no arm64 entry fails closed")
        self.assertFalse(self._passes("v2.1.23", None, arch="arm64", allow=True), "no entry even with opt-in")

    def test_scenarios(self):
        sha = {v: by_arch["amd64"] for v, by_arch in self.defaults["registry_zot_sha256"].items()}
        self.assertTrue(self._passes("v2.1.18", sha["v2.1.18"]), "matching bytes")
        self.assertFalse(self._passes("v2.1.23", sha["v2.1.18"]), "upgrade without opt-in")
        self.assertFalse(self._passes("v2.1.18", sha["v2.1.23"]), "downgrade without opt-in")
        self.assertTrue(self._passes("v2.1.23", sha["v2.1.18"], allow=True), "explicit opt-in")
        self.assertFalse(self._passes("v9.9.9", sha["v2.1.18"]), "missing checksum key")
        self.assertFalse(self._passes("v9.9.9", sha["v2.1.18"], allow=True), "missing key even with opt-in")
        self.assertTrue(self._passes("v2.1.23", None), "first install")


class NewCtTrimExclusion(unittest.TestCase):
    """ai-node3's nightly LXC sweep must leave the new registry CT 5005 alone (the pve_base role consumes the var)."""

    def test_node3_env_excludes_5005(self):
        base = REPO / "ansible" / "roles" / "pve_base"
        ctx = yaml.safe_load((base / "defaults" / "main.yml").read_text())
        ctx.update(yaml.safe_load((REPO / "ansible" / "host_vars" / "ai-node3.yml").read_text()))
        env = jinja2.Environment(undefined=jinja2.Undefined)
        env.filters["int"] = lambda v: int(v)
        rendered = env.from_string((base / "templates" / "pve-lxc-fstrim.env.j2").read_text()).render(**ctx)
        line = next(l for l in rendered.splitlines() if l.startswith("PVE_LXC_FSTRIM_EXCLUDE="))
        self.assertIn("5005", line.split("=", 1)[1].strip('"').split())

    def test_the_sweep_script_honours_the_exclude_list(self):
        script = (REPO / "ansible" / "roles" / "pve_base" / "files" / "pve-lxc-fstrim.sh").read_text()
        self.assertIn('EXCLUDE="${PVE_LXC_FSTRIM_EXCLUDE:-}"', script)
        self.assertRegex(script, r'in_list "\$ct" \$EXCLUDE')


class WriterFence(unittest.TestCase):
    def test_default_keeps_write_grants(self):
        acts = dict(_actions(_render()))
        self.assertTrue(any("create" in a for a in acts.values()))
        self.assertIn("delete", acts["adminPolicy"])

    def test_fence_leaves_only_read_everywhere(self):
        acts = _actions(_render({"registry_zot_read_only": True}))
        self.assertGreater(len(acts), 5)
        bad = [(w, a) for w, a in acts if set(a) - {"read"}]
        self.assertEqual(bad, [])

    def test_fence_changes_only_access_control_and_is_a_reload(self):
        path = ROLE / "files" / "zot-config-apply.py"
        spec = importlib.util.spec_from_file_location("zot_config_apply", path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        mode, paths, _ = helper.classify(_render(), _render({"registry_zot_read_only": True}))
        self.assertEqual(mode, "reload")
        self.assertTrue(paths and all(p.startswith("http.accessControl") for p in paths), paths)


class ActiveRegistryAfterCutover(unittest.TestCase):
    """After the cutover the registry_* outputs follow CT 5005; the old resource stays bound to CT 5004."""

    TF = REPO / "kubernetes" / "infra" / "registry-lxc"

    def _block(self, text, header):
        start = text.index(header)
        depth, i = 0, text.index("{", start)
        for j in range(i, len(text)):
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            if depth == 0:
                return text[start:j + 1]
        self.fail(f"unterminated block {header}")

    def test_active_outputs_point_at_the_new_ct(self):
        out = (self.TF / "outputs.tf").read_text()
        for name, attr in (("registry_ip", "ip"), ("registry_vmid", "vm_id"), ("registry_hostname", "hostname"),
                           ("registry_node", "node_name")):
            self.assertIn(f"var.registry_lxc_n3.{attr}", self._block(out, f'output "{name}"'), name)
        self.assertIn("var.registry_lxc.vm_id", self._block(out, 'output "registry_retired_vmid"'))
        self.assertIn("var.registry_lxc.node_name", self._block(out, 'output "registry_retired_node"'))

    def test_old_resource_keeps_its_binding_and_ignores_run_state(self):
        main = (self.TF / "main.tf").read_text()
        old = self._block(main, 'resource "proxmox_virtual_environment_container" "registry" ')
        self.assertRegex(old, r"node_name\s*=\s*var\.registry_lxc\.node_name")
        self.assertRegex(old, r"vm_id\s*=\s*var\.registry_lxc\.vm_id")
        self.assertRegex(old, r"ignore_changes\s*=\s*\[started, start_on_boot\]")
        self.assertRegex(old, r"prevent_destroy\s*=\s*true")
        new = self._block(main, 'resource "proxmox_virtual_environment_container" "registry_n3" ')
        self.assertRegex(new, r"ignore_changes\s*=\s*\[started\]")
        self.assertRegex(new, r"start_on_boot\s*=\s*true")
        self.assertRegex(new, r"prevent_destroy\s*=\s*true")


if __name__ == "__main__":
    unittest.main()
