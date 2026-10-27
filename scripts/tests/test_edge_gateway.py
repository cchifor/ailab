"""Unit tests for scripts/edge/gateway.py against an in-memory UniFi controller.

    python3 -m unittest scripts.tests.test_edge_gateway

The fake holds the same objects the live controller showed on 2026-10-27 (the Nextcloud forwards,
*.lan records, the Home network, the DDNS entries) so ownership is tested against real neighbours.
"""
import copy
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "edge"))
import gateway  # noqa: E402
import render  # noqa: E402

HOME = "6584469af81ca8458f9a6af1"


def base_inventory():
    return {
        "portforward": [
            {"_id": "pf-nc", "name": "nextcloud", "enabled": True, "pfwd_interface": "wan", "src": "any",
             "dst_port": "9443", "fwd": "192.168.0.160", "fwd_port": "443", "proto": "tcp_udp", "log": True},
            {"_id": "pf-nc80", "name": "nextcloud-http", "enabled": True, "pfwd_interface": "wan", "src": "any",
             "dst_port": "80", "fwd": "192.168.0.160", "fwd_port": "80", "proto": "tcp_udp", "log": False},
        ],
        "networkconf": [
            {"_id": HOME, "name": "Home", "purpose": "corporate", "ip_subnet": "192.168.0.1/23",
             "dhcpd_enabled": True, "dhcpd_start": "192.168.0.52", "dhcpd_stop": "192.168.1.254", "vlan": 1,
             "domain_name": "", "x_something": "secret"},
            {"_id": "net-lab", "name": "Lab", "purpose": "corporate", "ip_subnet": "192.168.2.1/24"},
        ],
        "dynamicdns": [
            {"_id": "dd1", "service": "cloudflare", "host_name": "chifor.me", "interface": "wan",
             "login": "zone", "x_password": "TOKEN"},
            {"_id": "dd2", "service": "cloudflare", "host_name": "chifor.net", "interface": "wan",
             "login": "zone", "x_password": "TOKEN"},
        ],
        "static_dns": [
            {"_id": "dns-lan-1", "key": "dev-worker-1.lan", "value": "192.168.0.8", "record_type": "A",
             "enabled": True, "ttl": 0},
            {"_id": "dns-lan-2", "key": "ai-node1.lan", "value": "192.168.0.2", "record_type": "A",
             "enabled": True, "ttl": 0},
        ],
    }


class FakeUnifi:
    def __init__(self, inv=None):
        self.inv = inv or base_inventory()
        self.calls = []
        self.fail_next_create = False
        self.n = 0

    def inventory(self):
        return copy.deepcopy(self.inv)

    def _new_id(self, prefix):
        self.n += 1
        return f"{prefix}-{self.n}"

    def create_portforward(self, fields):
        self.calls.append(("create_portforward", fields["name"]))
        obj = dict(fields, _id=self._new_id("pf"))
        self.inv["portforward"].append(obj)
        return copy.deepcopy(obj)

    def update_portforward(self, oid, obj):
        self.calls.append(("update_portforward", oid))
        self.inv["portforward"] = [obj if r["_id"] == oid else r for r in self.inv["portforward"]]
        return obj

    def delete_portforward(self, oid):
        self.calls.append(("delete_portforward", oid))
        self.inv["portforward"] = [r for r in self.inv["portforward"] if r["_id"] != oid]

    def update_network(self, oid, obj):
        self.calls.append(("update_network", oid))
        self.inv["networkconf"] = [copy.deepcopy(obj) if r["_id"] == oid else r for r in self.inv["networkconf"]]
        return obj

    def create_dns(self, fields):
        self.calls.append(("create_dns", fields["key"]))
        obj = dict(fields, _id=self._new_id("dns"))
        self.inv["static_dns"].append(obj)
        if self.fail_next_create:
            self.fail_next_create = False
            raise gateway.GatewayError("connection reset (ambiguous outcome)")
        return copy.deepcopy(obj)

    def update_dns(self, oid, obj):
        self.calls.append(("update_dns", oid))
        self.inv["static_dns"] = [obj if r["_id"] == oid else r for r in self.inv["static_dns"]]
        return obj

    def delete_dns(self, oid):
        self.calls.append(("delete_dns", oid))
        self.inv["static_dns"] = [r for r in self.inv["static_dns"] if r["_id"] != oid]


class Base(unittest.TestCase):
    def setUp(self):
        self.g = gateway.load_gateway()
        self.reg = render.load()
        for h in self.reg["hosts"].values():
            h["lan_dns"] = False
        self.g["port_forward"]["enabled"] = False
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = pathlib.Path(self.tmp.name) / "state.yaml"
        self.state = gateway.empty_state(self.g)
        self.api = FakeUnifi()

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, **kw):
        return gateway.make_plan(self.g, self.reg, self.state, self.api.inventory(), **kw)

    def apply(self, plan, **kw):
        gateway.apply(self.api, self.g, self.reg, self.state, plan, state_path=self.state_path,
                      skip_vip_check=True, log=lambda *_: None, **kw)

    def lan_dns(self, *names, on=True):
        for n in names:
            self.reg["hosts"][n]["lan_dns"] = on


class Planning(Base):
    def test_committed_desired_state_matches_the_controller(self):
        plan = self.plan()
        self.assertEqual(plan["errors"], [])
        self.assertEqual(plan["ops"], [])

    def test_dns_create_apply_then_idempotent(self):
        self.lan_dns("router", "git")
        plan = self.plan()
        self.assertEqual([(o["action"], o["key"]) for o in plan["ops"]],
                         [("create", "git.chifor.me"), ("create", "router.chifor.me")])
        self.apply(plan)
        self.assertEqual(set(self.state["static_dns"]), {"git.chifor.me", "router.chifor.me"})
        self.assertEqual(self.state["intents"], [])
        self.assertEqual(self.plan()["ops"], [])
        rec = [r for r in self.api.inv["static_dns"] if r["key"] == "router.chifor.me"][0]
        self.assertEqual((rec["value"], rec["ttl"], rec["record_type"]), ("192.168.0.51", 60, "A"))

    def test_lan_records_are_never_touched(self):
        self.lan_dns("router")
        self.apply(self.plan())
        self.lan_dns("router", on=False)
        self.apply(self.plan())
        keys = {r["key"] for r in self.api.inv["static_dns"]}
        self.assertEqual(keys, {"dev-worker-1.lan", "ai-node1.lan"})

    def test_lan_dns_true_false_true(self):
        self.lan_dns("router")
        self.apply(self.plan())
        self.lan_dns("router", on=False)
        plan = self.plan()
        self.assertEqual([(o["action"], o["key"]) for o in plan["ops"]], [("delete", "router.chifor.me")])
        self.apply(plan)
        self.assertNotIn("router.chifor.me", self.state["static_dns"])
        self.lan_dns("router")
        plan = self.plan()
        self.assertEqual([(o["action"], o["key"]) for o in plan["ops"]], [("create", "router.chifor.me")])

    def test_unowned_record_in_the_way_aborts_then_adopt(self):
        self.api.inv["static_dns"].append({"_id": "dns-x", "key": "router.chifor.me", "value": "10.0.0.1",
                                           "record_type": "A", "enabled": True, "ttl": 0})
        self.lan_dns("router")
        plan = self.plan()
        self.assertTrue(any("not ours" in e for e in plan["errors"]))
        plan = self.plan(adopt=["router.chifor.me"])
        self.assertEqual(plan["errors"], [])
        self.assertEqual([(o["action"], o["key"]) for o in plan["ops"]],
                         [("adopt", "router.chifor.me"), ("update", "router.chifor.me")])
        self.apply(plan)
        self.assertEqual(self.state["static_dns"]["router.chifor.me"], "dns-x")
        self.assertEqual([r["value"] for r in self.api.inv["static_dns"] if r["_id"] == "dns-x"], ["192.168.0.51"])

    def test_owned_record_vanished_aborts_then_forget(self):
        self.lan_dns("router")
        self.apply(self.plan())
        oid = self.state["static_dns"]["router.chifor.me"]
        self.api.inv["static_dns"] = [r for r in self.api.inv["static_dns"] if r["_id"] != oid]
        self.assertTrue(any("is gone" in e for e in self.plan()["errors"]))
        plan = self.plan(forget=["router.chifor.me"])
        self.assertEqual(plan["errors"], [])
        self.assertEqual([(o["action"], o["key"]) for o in plan["ops"]],
                         [("forget", "router.chifor.me"), ("create", "router.chifor.me")])

    def test_interrupted_create_stops_the_next_run(self):
        self.lan_dns("router")
        self.api.fail_next_create = True
        with self.assertRaises(gateway.GatewayError):
            self.apply(self.plan())
        self.assertEqual([(i["kind"], i["key"]) for i in self.state["intents"]], [("dns", "router.chifor.me")])
        on_disk = gateway.load_state(self.g, self.state_path)
        self.assertEqual(len(on_disk["intents"]), 1)
        plan = self.plan()
        self.assertTrue(any("interrupted" in e for e in plan["errors"]))
        # the record did get created; naming it with --adopt is the recovery, and clears the intent
        self.state = on_disk
        plan = self.plan(adopt=["router.chifor.me"])
        self.assertEqual(plan["errors"], [])
        self.apply(plan)
        self.assertEqual(self.state["intents"], [])
        self.assertIn("router.chifor.me", self.state["static_dns"])
        self.assertEqual(self.plan()["ops"], [])

    def test_interrupted_create_that_never_happened_is_forgotten_and_retried(self):
        self.lan_dns("router")
        self.state["intents"].append({"kind": "dns", "key": "router.chifor.me", "at": 0})
        self.assertTrue(any("interrupted" in e for e in self.plan()["errors"]))
        plan = self.plan(forget=["router.chifor.me"])
        self.assertEqual(plan["errors"], [])
        self.assertEqual([(o["action"], o["key"]) for o in plan["ops"]],
                         [("forget", "router.chifor.me"), ("create", "router.chifor.me")])
        self.apply(plan)
        self.assertEqual(self.state["intents"], [])
        self.assertIn("router.chifor.me", self.state["static_dns"])

    def test_empty_registry_is_refused_before_planning(self):
        with self.assertRaises(render.RegistryError):
            reg = render.load()
            reg["hosts"] = {}
            errs = render.validate(reg)
            if errs:
                raise render.RegistryError("\n".join(errs))


class Forward(Base):
    def test_create_forward_leaves_nextcloud_alone(self):
        self.g["port_forward"]["enabled"] = True
        before = copy.deepcopy([r for r in self.api.inv["portforward"] if r["name"].startswith("nextcloud")])
        plan = self.plan()
        self.assertEqual([(o["action"], o["kind"]) for o in plan["ops"]], [("create", "forward")])
        self.apply(plan)
        after = [r for r in self.api.inv["portforward"] if r["name"].startswith("nextcloud")]
        self.assertEqual(before, after)
        mine = [r for r in self.api.inv["portforward"] if r["name"] == "ailab-edge-https"][0]
        self.assertEqual((mine["dst_port"], mine["fwd"], mine["fwd_port"], mine["proto"]),
                         ("443", "192.168.0.51", "443", "tcp"))
        self.assertEqual(self.plan()["ops"], [])

    def test_another_443_forward_is_a_conflict(self):
        self.g["port_forward"]["enabled"] = True
        self.api.inv["portforward"].append({"_id": "pf-old", "name": "k8s", "enabled": True,
                                            "pfwd_interface": "wan", "dst_port": "80,443", "fwd": "192.168.0.250",
                                            "fwd_port": "443", "proto": "tcp_udp"})
        plan = self.plan()
        self.assertTrue(any("already takes WAN port 443" in e for e in plan["errors"]))
        self.assertEqual(plan["ops"], [])

    def test_same_name_not_ours_aborts(self):
        self.g["port_forward"]["enabled"] = True
        self.api.inv["portforward"].append({"_id": "pf-x", "name": "ailab-edge-https", "enabled": False,
                                            "pfwd_interface": "wan", "dst_port": "8443", "fwd": "1.2.3.4",
                                            "fwd_port": "1", "proto": "tcp"})
        self.assertTrue(any("not ours" in e for e in self.plan()["errors"]))

    def test_disable_deletes_first(self):
        self.g["port_forward"]["enabled"] = True
        self.lan_dns("router")
        self.apply(self.plan())
        self.g["port_forward"]["enabled"] = False
        self.lan_dns("router", on=False)
        plan = self.plan()
        self.assertEqual([(o["action"], o["kind"]) for o in plan["ops"]], [("delete", "forward"), ("delete", "dns")])
        self.apply(plan)
        self.assertIsNone(self.state["port_forward"])
        self.assertFalse(any(r["name"] == "ailab-edge-https" for r in self.api.inv["portforward"]))

    def test_enable_creates_dns_before_the_forward(self):
        self.g["port_forward"]["enabled"] = True
        self.lan_dns("router")
        plan = self.plan()
        self.assertEqual([o["kind"] for o in plan["ops"]], ["dns", "forward"])


class Dhcp(Base):
    def test_drift_is_corrected_preserving_every_other_field(self):
        home = self.api.inv["networkconf"][0]
        home["dhcpd_start"] = "192.168.0.50"
        before = copy.deepcopy(home)
        plan = self.plan()
        self.assertEqual([(o["kind"], o["action"]) for o in plan["ops"]], [("dhcp", "update")])
        gateway.BACKUPS = pathlib.Path(self.tmp.name) / "backups"
        self.apply(plan)
        after = self.api.inv["networkconf"][0]
        self.assertEqual(after["dhcpd_start"], "192.168.0.52")
        for k, v in before.items():
            if k != "dhcpd_start":
                self.assertEqual(after[k], v, k)
        backup = list((pathlib.Path(self.tmp.name) / "backups").glob("*.json"))[0].read_text()
        self.assertNotIn("x_something", backup)

    def test_vip_inside_the_range_is_refused(self):
        self.g["lan"]["dhcp_start"] = "192.168.0.50"
        self.assertTrue(any("inside the desired DHCP range" in e for e in self.plan()["errors"]))

    def test_wrong_network_is_refused(self):
        self.api.inv["networkconf"][0]["ip_subnet"] = "192.168.0.1/24"
        self.assertTrue(any("not 'Home'" in e or "192.168.0.1/23" in e for e in self.plan()["errors"]))


class Binding(Base):
    def test_hash_covers_inventory_ops_and_options(self):
        h0 = self.plan()["hash"]
        self.assertEqual(h0, self.plan()["hash"])
        self.assertNotEqual(h0, self.plan(only=["dns"])["hash"])
        self.assertNotEqual(h0, self.plan(adopt=["x.chifor.me"])["hash"])
        self.api.inv["portforward"][0]["log"] = False  # an unrelated object changed
        self.assertNotEqual(h0, self.plan()["hash"])

    def test_secrets_do_not_change_the_hash_or_leak(self):
        h0 = self.plan()["hash"]
        self.api.inv["dynamicdns"][0]["x_password"] = "ROTATED"
        self.assertEqual(h0, self.plan()["hash"])
        self.assertNotIn("TOKEN", gateway.show(self.plan()))

    def test_object_changed_between_plan_and_apply(self):
        self.lan_dns("router")
        self.apply(self.plan())
        oid = self.state["static_dns"]["router.chifor.me"]
        self.lan_dns("router", on=False)
        plan = self.plan()
        for r in self.api.inv["static_dns"]:
            if r["_id"] == oid:
                r["value"] = "10.9.9.9"  # someone edited it in the UI after the plan
        with self.assertRaises(gateway.GatewayError):
            self.apply(plan)

    def test_state_from_another_controller_is_refused(self):
        self.state["controller"]["cert_sha256"] = "0" * 64
        gateway.save_state(self.state, self.state_path)
        with self.assertRaises(gateway.GatewayError):
            gateway.load_state(self.g, self.state_path)

    def test_partial_listing_is_refused(self):
        with self.assertRaises(gateway.GatewayError):
            gateway.UnifiClient._classic_list(200, {"meta": {"rc": "error", "msg": "api.err.LoginRequired"}}, "x")
        with self.assertRaises(gateway.GatewayError):
            gateway.UnifiClient._v2_list(500, None, "x")

    def test_missing_ddns_is_a_warning_not_a_change(self):
        self.api.inv["dynamicdns"] = self.api.inv["dynamicdns"][:1]
        plan = self.plan()
        self.assertEqual(plan["ops"], [])
        self.assertTrue(any("chifor.net" in w for w in plan["warnings"]))


if __name__ == "__main__":
    unittest.main()
