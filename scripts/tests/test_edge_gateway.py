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
        self.clients = []

    def active_clients(self):
        return copy.deepcopy(self.clients)

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
        # the WAN gate needs a live VIP; tests that exercise it pass skip_vip_check=False
        kw.setdefault("skip_vip_check", True)
        return gateway.make_plan(self.g, self.reg, self.state, self.api.inventory(), **kw)

    def apply(self, plan, **kw):
        gateway.apply(self.api, self.g, self.reg, self.state, plan, state_path=self.state_path,
                      log=lambda *_: None, **kw)

    def ops(self, plan):
        return [(o["action"], o["kind"], o["key"]) for o in plan["ops"]]

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
        self.assertEqual(self.state["intents"][0]["fields"]["value"], "192.168.0.51")
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

    def test_adopting_an_intent_needs_the_fields_it_sent(self):
        # an object carrying the key but not the create's fields is somebody else's
        self.lan_dns("router")
        self.state["intents"].append({"kind": "dns", "key": "router.chifor.me", "at": 0, "fields": {
            "key": "router.chifor.me", "value": "192.168.0.51", "record_type": "A", "enabled": True, "ttl": 60}})
        self.api.inv["static_dns"].append({"_id": "dns-x", "key": "router.chifor.me", "value": "10.0.0.1",
                                           "record_type": "A", "enabled": True, "ttl": 0})
        plan = self.plan(adopt=["router.chifor.me"])
        self.assertTrue(any("not that create's" in e for e in plan["errors"]), plan["errors"])
        self.assertEqual(plan["ops"], [])

    def test_owned_record_with_a_duplicate_key_aborts(self):
        self.lan_dns("router")
        self.apply(self.plan())
        self.api.inv["static_dns"].append({"_id": "dns-dup", "key": "router.chifor.me", "value": "10.0.0.1",
                                           "record_type": "A", "enabled": True, "ttl": 0})
        plan = self.plan()
        self.assertTrue(any("other record(s) carry this key" in e for e in plan["errors"]), plan["errors"])

    def test_a_collision_after_the_plan_stops_the_apply(self):
        self.lan_dns("router")
        plan = self.plan()
        self.api.inv["static_dns"].append({"_id": "dns-late", "key": "router.chifor.me", "value": "10.0.0.1",
                                           "record_type": "A", "enabled": True, "ttl": 0})
        with self.assertRaises(gateway.GatewayError):
            self.apply(plan)
        self.assertNotIn(("create_dns", "router.chifor.me"), self.api.calls)

    def test_mixed_operations_follow_the_phases(self):
        # owned: the Home network, router's record, the forward; then withdraw both, move the range, add git
        self.g["port_forward"]["enabled"] = True
        self.lan_dns("router")
        self.apply(self.plan(adopt=["Home"]))
        self.g["port_forward"]["enabled"] = False
        self.lan_dns("router", on=False)
        self.lan_dns("git")
        self.g["lan"]["dhcp_start"] = "192.168.0.53"
        self.assertEqual(self.ops(self.plan()), [("delete", "forward", "ailab-edge-https"),
                                                 ("delete", "dns", "router.chifor.me"),
                                                 ("update", "dhcp", "Home"),
                                                 ("create", "dns", "git.chifor.me")])

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

    def owned_forward(self):
        self.g["port_forward"]["enabled"] = True
        self.apply(self.plan())
        return self.state["port_forward"]["id"]

    def test_a_reused_or_renamed_forward_id_stops_the_run(self):
        oid = self.owned_forward()
        for r in self.api.inv["portforward"]:
            if r["_id"] == oid:
                r["name"] = "someone-else"
        plan = self.plan()
        self.assertTrue(any("may have been reused" in e for e in plan["errors"]), plan["errors"])
        self.assertEqual(plan["ops"], [])
        plan = self.plan(forget=["ailab-edge-https"])
        self.assertEqual(self.ops(plan)[0], ("forget", "forward", "ailab-edge-https"))

    def test_an_owned_forward_with_a_new_443_neighbour_aborts(self):
        self.owned_forward()
        self.api.inv["portforward"].append({"_id": "pf-new", "name": "game", "enabled": True,
                                            "pfwd_interface": "wan", "dst_port": "400-500", "fwd": "192.168.0.99",
                                            "fwd_port": "443", "proto": "tcp"})
        plan = self.plan()
        self.assertTrue(any("already takes WAN port 443" in e for e in plan["errors"]), plan["errors"])
        self.assertEqual(plan["ops"], [])

    def test_the_wan_gate_also_guards_an_update(self):
        oid = self.owned_forward()
        for r in self.api.inv["portforward"]:
            if r["_id"] == oid:
                r["enabled"] = False  # disabled by hand in the UI
        plan = self.plan(skip_vip_check=False)
        self.assertEqual(self.ops(plan), [("update", "forward", "ailab-edge-https")])
        real = gateway.vip_answers
        try:
            gateway.vip_answers = lambda *a: False
            with self.assertRaises(gateway.GatewayError):
                self.apply(plan)
            self.assertNotIn(("update_portforward", oid), self.api.calls)
            gateway.vip_answers = lambda *a: True
            self.apply(plan)
        finally:
            gateway.vip_answers = real
        self.assertEqual(self.plan()["ops"], [])

    def gate_that_changes_the_controller(self, change):
        real = gateway.vip_answers

        def gate(*_a):
            change()
            return True
        gateway.vip_answers = gate
        return real

    def test_a_rename_during_the_gate_stops_the_update(self):
        oid = self.owned_forward()
        for r in self.api.inv["portforward"]:
            if r["_id"] == oid:
                r["enabled"] = False
        plan = self.plan(skip_vip_check=False)

        def rename():
            for r in self.api.inv["portforward"]:
                if r["_id"] == oid:
                    r["name"] = "renamed-in-the-ui"
        real = self.gate_that_changes_the_controller(rename)
        try:
            with self.assertRaises(gateway.GatewayError):
                self.apply(plan)
        finally:
            gateway.vip_answers = real
        self.assertNotIn(("update_portforward", oid), self.api.calls)

    def test_a_443_forward_appearing_during_the_gate_stops_the_create(self):
        self.g["port_forward"]["enabled"] = True
        plan = self.plan(skip_vip_check=False)

        def add():
            self.api.inv["portforward"].append({"_id": "pf-late", "name": "late", "enabled": True,
                                                "pfwd_interface": "wan", "dst_port": "443", "fwd": "192.168.0.9",
                                                "fwd_port": "443", "proto": "tcp"})
        real = self.gate_that_changes_the_controller(add)
        try:
            with self.assertRaises(gateway.GatewayError):
                self.apply(plan)
        finally:
            gateway.vip_answers = real
        self.assertNotIn(("create_portforward", "ailab-edge-https"), self.api.calls)
        self.assertEqual(self.state["intents"], [])

    def test_recovery_after_a_rename_forgets_the_recorded_name(self):
        oid = self.owned_forward()
        for r in self.api.inv["portforward"]:
            if r["_id"] == oid:
                r["name"] = "ailab-edge-https-v2"
        self.g["port_forward"]["name"] = "ailab-edge-https-v2"
        self.assertTrue(any("may have been reused" in e for e in self.plan()["errors"]))
        plan = self.plan(forget=["ailab-edge-https"], adopt=["ailab-edge-https-v2"])
        self.assertEqual(plan["errors"], [])
        self.assertEqual(self.ops(plan), [("forget", "forward", "ailab-edge-https"),
                                          ("adopt", "forward", "ailab-edge-https-v2")])
        self.apply(plan)
        self.assertEqual(self.state["port_forward"], {"id": oid, "name": "ailab-edge-https-v2"})
        self.assertEqual(self.plan()["ops"], [])

    def test_unowned_objects_changed_during_an_apply_fail_it(self):
        self.g["port_forward"]["enabled"] = True
        plan = self.plan()
        real = self.api.create_portforward

        def create_and_disturb(fields):
            for r in self.api.inv["portforward"]:
                if r["_id"] == "pf-nc":
                    r["fwd"] = "192.168.0.99"  # someone retargets Nextcloud meanwhile
            return real(fields)
        self.api.create_portforward = create_and_disturb
        with self.assertRaises(gateway.GatewayError) as cm:
            self.apply(plan)
        self.assertIn("portforward pf-nc: changed", str(cm.exception))

    def test_the_gate_parses_http_framing(self):
        m = b"edge-canary ok peer="
        ok = gateway.canary_ok
        self.assertTrue(ok(b"HTTP/1.1 200 OK\r\nContent-Length: 27\r\n\r\nedge-canary ok peer=1.2.3.4", m))
        # the server closed before the declared length
        self.assertFalse(ok(b"HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\nedge-canary ok peer=1.2.3.4", m))
        # chunked, the marker split across chunks: decoded, it is there
        self.assertTrue(ok(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                           b"8\r\nedge-can\r\nc\r\nary ok peer=\r\n0\r\n\r\n", m))
        # chunked, empty body, the marker only in a trailer
        self.assertFalse(ok(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                            b"0\r\nX-Note: edge-canary ok peer=\r\n\r\n", m))
        # the marker in a header only
        self.assertFalse(ok(b"HTTP/1.1 200 OK\r\nX-Note: edge-canary ok peer=\r\nContent-Length: 2\r\n\r\nhi", m))
        self.assertFalse(ok(b"garbage", m))
        self.assertFalse(ok(b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * (gateway.CANARY_LIMIT + 1), m))

    def test_the_gate_wants_the_canarys_own_answer(self):
        marker = gateway.canary_marker(self.reg)
        self.assertEqual(marker, b"edge-canary ok peer=")
        self.assertTrue(gateway.canary_ok(b"HTTP/1.1 200 OK\r\nServer: x\r\n\r\nedge-canary ok peer=1.2.3.4", marker))
        self.assertFalse(gateway.canary_ok(b"HTTP/1.1 200 OK\r\n\r\n<html>some other site</html>", marker))
        self.assertFalse(gateway.canary_ok(b"HTTP/1.1 403 Forbidden\r\n\r\nedge-canary ok peer=", marker))


class Dhcp(Base):
    def test_drift_is_corrected_preserving_every_other_field(self):
        home = self.api.inv["networkconf"][0]
        home["dhcpd_start"] = "192.168.0.50"
        before = copy.deepcopy(home)
        plan = self.plan(adopt=["Home"])
        self.assertEqual([(o["kind"], o["action"]) for o in plan["ops"]], [("dhcp", "adopt"), ("dhcp", "update")])
        gateway.BACKUPS = pathlib.Path(self.tmp.name) / "backups"
        self.apply(plan)
        after = self.api.inv["networkconf"][0]
        self.assertEqual(after["dhcpd_start"], "192.168.0.52")
        for k, v in before.items():
            if k != "dhcpd_start":
                self.assertEqual(after[k], v, k)
        backup = list((pathlib.Path(self.tmp.name) / "backups").glob("*.json"))[0].read_text()
        self.assertNotIn("x_something", backup)

    def test_an_unowned_network_is_never_changed(self):
        self.api.inv["networkconf"][0]["dhcpd_start"] = "192.168.0.50"
        plan = self.plan()
        self.assertTrue(any("not ours" in e for e in plan["errors"]), plan["errors"])
        self.assertEqual(plan["ops"], [])

    def test_state_owning_another_network_is_refused(self):
        self.state["dhcp_network"] = {"id": "net-lab", "name": "Lab"}
        self.assertTrue(any("but gateway.yaml names" in e for e in self.plan()["errors"]))
        plan = self.plan(forget=["Home"])  # the forget names gateway.yaml's network: no effect on Lab's entry
        self.assertTrue(any("but gateway.yaml names" in e for e in plan["errors"]))
        plan = self.plan(forget=["Lab"])  # the owned entry's own name releases it
        self.assertEqual(self.ops(plan), [("forget", "dhcp", "Lab")])
        self.assertEqual(plan["errors"], [])

    def lease_plan(self, **kw):
        self.api.inv["networkconf"][0]["dhcpd_start"] = "192.168.0.50"
        self.state["dhcp_network"] = {"id": HOME, "name": "Home"}
        plan = self.plan(**kw)
        gateway.check_leases(plan, self.api.active_clients())
        return plan

    def test_taking_back_an_active_clients_address_is_refused(self):
        self.api.clients = [{"ip": "192.168.0.51", "network_id": HOME, "hostname": "phone", "mac": "aa"},
                            {"ip": "192.168.0.80", "network_id": HOME, "mac": "bb"}]
        plan = self.lease_plan()
        self.assertTrue(any("192.168.0.51 (phone)" in e for e in plan["errors"]), plan["errors"])
        self.assertFalse(any("192.168.0.80" in e for e in plan["errors"]))
        acked = self.lease_plan(ack_dhcp_leases=True)
        self.assertEqual(acked["errors"], [])
        self.assertNotEqual(plan["hash"], acked["hash"])

    def test_a_fixed_ip_client_is_not_a_conflict(self):
        self.api.clients = [{"ip": "192.168.0.50", "network_id": HOME, "use_fixedip": True,
                             "fixed_ip": "192.168.0.50", "mac": "cc"},
                            {"ip": "192.168.0.51", "network_id": "net-lab", "mac": "dd"}]
        self.assertEqual(self.lease_plan()["errors"], [])

    def test_a_client_without_a_network_id_counts(self):
        self.api.clients = [{"ip": "192.168.0.51", "mac": "ff"}]
        self.assertTrue(any("192.168.0.51" in e for e in self.lease_plan()["errors"]))

    def test_ipv6_clients_do_not_break_the_check(self):
        self.api.clients = [{"ip": "fe80::1", "network_id": HOME, "mac": "11"},
                            {"ip": "2001:db8::5", "mac": "22"}]
        self.assertEqual(self.lease_plan()["errors"], [])
        self.assertFalse(any("DHCP" in w for w in self.lease_plan(ack_dhcp_leases=True)["warnings"]))

    def test_a_client_arriving_after_the_plan_stops_the_write(self):
        plan = self.lease_plan()
        self.assertEqual(plan["errors"], [])
        self.api.clients = [{"ip": "192.168.0.50", "network_id": HOME, "mac": "ee"}]
        gateway.BACKUPS = pathlib.Path(self.tmp.name) / "backups"
        with self.assertRaises(gateway.GatewayError):
            self.apply(plan)
        self.assertNotIn(("update_network", HOME), self.api.calls)

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
        self.assertNotEqual(h0, self.plan(skip_vip_check=False)["hash"])
        self.assertNotEqual(h0, self.plan(ack_dhcp_leases=True)["hash"])
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

    def test_a_paged_listing_is_refused(self):
        ok = {"meta": {"rc": "ok"}, "data": [{"_id": "a"}]}
        self.assertEqual(gateway.UnifiClient._classic_list(200, ok, "x"), [{"_id": "a"}])
        for paged in ({"meta": {"rc": "ok", "count": 500}, "data": [{"_id": "a"}]},
                      {"meta": {"rc": "ok"}, "data": [{"_id": "a"}], "offset": 0},
                      {"meta": {"rc": "ok", "next": "cursor"}, "data": []}):
            with self.assertRaises(gateway.GatewayError):
                gateway.UnifiClient._classic_list(200, paged, "x")

    def test_missing_ddns_is_a_warning_not_a_change(self):
        self.api.inv["dynamicdns"] = self.api.inv["dynamicdns"][:1]
        plan = self.plan()
        self.assertEqual(plan["ops"], [])
        self.assertTrue(any("chifor.net" in w for w in plan["warnings"]))


if __name__ == "__main__":
    unittest.main()
