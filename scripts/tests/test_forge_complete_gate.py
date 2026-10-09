"""Render the dedicated gate budgets and exercise the exclusive-host safety predicates."""
import pathlib
import unittest

import jinja2
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
ROLE = ROOT / "ansible/roles/gitea_runner"


def read_yaml(path):
    return yaml.safe_load(path.read_text())


def environment():
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = bool
    env.filters["ternary"] = lambda condition, yes, no: yes if condition else no
    return env


class CompleteGateConfiguration(unittest.TestCase):
    def setUp(self):
        self.defaults = read_yaml(ROLE / "defaults/main.yml")
        self.dedicated = {**self.defaults, **read_yaml(ROOT / "ansible/host_vars/ci-runner-9.yml")}

    def render(self, name, values):
        return environment().from_string((ROLE / "templates" / name).read_text()).render(**values)

    def test_dedicated_complete_job_budget_and_sole_label(self):
        config = yaml.safe_load(self.render("config.yaml.j2", self.dedicated))
        self.assertEqual(config["runner"]["timeout"], "10h")
        self.assertEqual(config["runner"]["shutdown_timeout"], "600m")
        self.assertEqual(config["runner"]["labels"], ["forge-complete:host"])
        self.assertEqual(config["runner"]["capacity"], 1)

    def test_ordinary_pool_budget_and_routing_remain_unchanged(self):
        config = yaml.safe_load(self.render("config.yaml.j2", self.defaults))
        self.assertEqual(config["runner"]["timeout"], "3h")
        self.assertEqual(config["runner"]["labels"], ["self-hosted-hv:host"])
        self.assertEqual(config["runner"]["shutdown_timeout"], "10m")
        self.assertFalse(self.defaults["gitea_runner_exclusive_host"])
        self.assertEqual(self.defaults["gitea_runner_cleanup_reap_age_sec"], 14400)

    def test_cleanup_and_shutdown_outlive_the_entire_job(self):
        service = self.render("gitea-act-runner.service.j2", self.dedicated)
        self.assertIn("KillMode=mixed\n", service)
        self.assertIn("TimeoutStopSec=601min\n", service)
        cleanup = self.render("gitea-runner-cleanup.env.j2", self.dedicated)
        self.assertIn("GITEA_CLEANUP_MIN_UNTIL_SEC=39600\n", cleanup)
        self.assertIn("GITEA_CLEANUP_REAP_AGE_SEC=39600\n", cleanup)
        for key in ("gitea_runner_cleanup_min_until_sec", "gitea_runner_cleanup_reap_age_sec"):
            self.assertGreater(self.dedicated[key], 600 * 60)
        self.assertGreater(self.dedicated["gitea_runner_cleanup_ws_prune_age_h"], 10)

    def test_global_server_ceiling_exceeds_daemon_budget(self):
        docs = yaml.safe_load_all((ROOT / "kubernetes/apps/apps/gitea/gitea.yaml").read_text())
        release = next(d for d in docs if d["kind"] == "HelmRelease")
        actions = release["spec"]["values"]["gitea"]["config"]["actions"]
        self.assertEqual(actions["ENDLESS_TASK_TIMEOUT"], "12h")
        self.assertNotIn("ZOMBIE_TASK_TIMEOUT", actions)

    def test_selected_host_is_an_existing_always_on_vm(self):
        inventory = read_yaml(ROOT / "inventory/hosts.yml")
        hosts = inventory["all"]["children"]["github_runners"]["hosts"]
        self.assertEqual(hosts["ci-runner-9"]["ansible_host"], "192.168.0.31")
        self.assertEqual(len(hosts), 8)  # seven ordinary workers remain after reserving this one
        tofu = (ROOT / "kubernetes/infra/runners/variables.tf").read_text()
        self.assertRegex(tofu, r'"ci-runner-9"\s*=\s*\{ node_name = "ai-node1", vm_id = 4109')
        exclusive = [p.name for p in (ROOT / "ansible/host_vars").glob("ci-runner-*.yml")
                     if read_yaml(p).get("gitea_runner_exclusive_host")]
        self.assertEqual(exclusive, ["ci-runner-9.yml"])


class ExclusiveHostGuards(unittest.TestCase):
    def setUp(self):
        self.tasks = read_yaml(ROLE / "tasks/exclusive-host.yml")
        self.env = environment()

    def test_guard_precedes_any_runner_mutation(self):
        tasks = read_yaml(ROLE / "tasks/main.yml")
        self.assertEqual(tasks[0]["ansible.builtin.include_tasks"], "exclusive-host.yml")
        self.assertEqual(tasks[0]["when"], "gitea_runner_exclusive_host | bool")
        self.assertIn("ansible.builtin.service_facts", self.tasks[1])
        self.assertIn("ansible.builtin.systemd_service", self.tasks[-1])
        retirement = self.tasks[-1]["ansible.builtin.systemd_service"]
        self.assertEqual(retirement["state"], "stopped")
        self.assertFalse(retirement["enabled"])
        self.assertNotIn("masked", retirement)  # the role owns a regular /etc unit, not a vendor unit

    def test_shared_or_multi_capacity_configuration_cannot_retire_a_service(self):
        predicates = self.tasks[0]["ansible.builtin.assert"]["that"]
        valid = {"github_runner_agent_enabled": False, "gitea_runner_label": "forge-complete",
                 "gitea_runner_capacity": 1}
        evaluate = lambda values: all(self.env.compile_expression(p)(**values) for p in predicates)
        self.assertTrue(evaluate(valid))
        for key, value in (("github_runner_agent_enabled", True), ("gitea_runner_label", "self-hosted-hv"),
                           ("gitea_runner_capacity", 2)):
            self.assertFalse(evaluate({**valid, key: value}), key)

    def test_running_or_unknown_service_state_blocks_maintenance(self):
        predicate = self.env.compile_expression(self.tasks[2]["ansible.builtin.assert"]["that"][0])
        for service in ("gitea-act-runner.service", "actions.runner.cchifor-platform.service"):
            for state in ("running", "unknown", "starting"):
                facts = {"services": {service: {"state": state}}}
                self.assertFalse(predicate(item=service, ansible_facts=facts), (service, state))
            facts = {"services": {service: {"state": "stopped"}}}
            self.assertTrue(predicate(item=service, ansible_facts=facts))
            self.assertTrue(predicate(item=service, ansible_facts={"services": {}}))

    def test_no_worker_is_not_inferred_from_a_failed_process_probe(self):
        probe = self.tasks[3]
        self.assertEqual(probe["ansible.builtin.command"]["argv"],
                         ["pgrep", "-x", "act_runner|Runner[.]Worker"])
        failed = self.env.compile_expression(probe["failed_when"])
        for rc in (0, 2, 127):
            self.assertTrue(failed(_exclusive_runner_processes={"rc": rc}), rc)
        self.assertFalse(failed(_exclusive_runner_processes={"rc": 1}))


if __name__ == "__main__":
    unittest.main()
