"""Explicit offline PG17 qualification: Docker required, no network, ports, or production credentials.

Run: python scripts/tests/integration/test_relay_operator_reaper_postgres.py
Uses the deployment's digest-pinned Postgres image; missing prerequisites fail, never skip.
"""
from pathlib import Path
import subprocess
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[3]
IMAGE = "postgres@sha256:f9cf3e1f0066f0eb0c48707d864f0ac5941e9ff454a072077d97a4d278d07d73"
SUBJECT = "https://id.chifor.me/realms/relay|72e99c0a-fce0-42f1-8e2a-af1363b5aa4f"


class PostgresDeadline(unittest.TestCase):
    def docker(self, *args, data=None, check=True):
        result = subprocess.run(["docker", *args], input=data, text=True, capture_output=True, timeout=45)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return result

    def sql(self, query, role="postgres", check=True):
        return self.docker("exec", "-i", self.name, "psql", "-X", "-tA", "-v", "ON_ERROR_STOP=1",
                           "-U", role, "-d", "relay", data=query, check=check)

    def counts(self):
        return self.sql("SELECT (SELECT count(*) FROM sessions), (SELECT count(*) FROM memberships);").stdout.strip()

    def setUp(self):
        self.name = "reaper-pg-qualification-" + uuid.uuid4().hex[:12]
        self.addCleanup(lambda: self.docker("rm", "-f", self.name, check=False))
        self.docker("run", "-d", "--name", self.name, "--pull", "never", "--network", "none", "--read-only",
                    "--user", "70:70", "--tmpfs", "/var/lib/postgresql/data:rw,nosuid,nodev,uid=70,gid=70,mode=0700",
                    "--tmpfs", "/var/run/postgresql:rw,nosuid,nodev,uid=70,gid=70,mode=0700",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                    "-e", "POSTGRES_DB=relay", "-e", "REAPER_PASSWORD=synthetic-reaper-test-only",
                    "--mount", f"type=bind,source={ROOT},target=/repo,readonly", IMAGE)
        limit = time.monotonic() + 20
        # initdb temporarily starts a server too. Wait for the entrypoint's final exec, not that socket.
        ready = '[ "$(cat /proc/1/comm)" = postgres ] && pg_isready -U postgres -d relay'
        while self.docker("exec", self.name, "sh", "-c", ready, check=False).returncode:
            self.assertLess(time.monotonic(), limit, "private PostgreSQL startup timed out")
            time.sleep(0.1)
        self.sql(f"""
          CREATE ROLE relay_owner NOLOGIN;
          CREATE TABLE users(id integer PRIMARY KEY, subject text NOT NULL);
          CREATE TABLE sessions(id integer PRIMARY KEY, user_id integer REFERENCES users(id));
          CREATE TABLE memberships(id integer PRIMARY KEY, user_id integer REFERENCES users(id));
          ALTER TABLE users OWNER TO relay_owner;
          ALTER TABLE sessions OWNER TO relay_owner;
          ALTER TABLE memberships OWNER TO relay_owner;
          INSERT INTO users VALUES (1,'{SUBJECT}'),(2,'synthetic-other-subject');
          INSERT INTO sessions VALUES(1,1),(2,2);
          INSERT INTO memberships VALUES(1,1),(2,2);
        """)
        # PR2063's predecessor already exposes a bigint argument named fence. PostgreSQL rejects
        # renaming it in CREATE OR REPLACE; setup must safely replace that existing signature.
        self.sql("""
          CREATE SCHEMA ops AUTHORIZATION relay_owner;
          CREATE FUNCTION ops.relay_operator_reap(fence bigint)
            RETURNS TABLE(sessions_deleted bigint, memberships_deleted bigint)
            LANGUAGE sql AS 'SELECT 0::bigint, 0::bigint';
          ALTER FUNCTION ops.relay_operator_reap(bigint) OWNER TO relay_owner;
        """)
        for _ in range(2):
            self.docker("exec", self.name, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "relay",
                        "-f", "/repo/kubernetes/apps/apps/relay/operator-reaper/setup.sql")

    def test_deadline_privileges_and_atomic_rollback(self):
        role = "relay_operator_reaper"
        settings = self.sql("SHOW statement_timeout; SHOW transaction_timeout;", role).stdout.strip().splitlines()
        self.assertEqual(settings, ["20s", "25s"])
        self.assertNotEqual(self.sql("SELECT * FROM sessions", role, check=False).returncode, 0)
        self.assertNotEqual(self.sql("DELETE FROM memberships", role, check=False).returncode, 0)
        self.assertNotEqual(self.sql("SELECT * FROM ops.relay_operator_reap()", role, check=False).returncode, 0)
        for deadline in ("NULL", "floor(extract(epoch FROM clock_timestamp()))::bigint - 1",
                         "floor(extract(epoch FROM clock_timestamp()))::bigint + 29",
                         "floor(extract(epoch FROM clock_timestamp()))::bigint + 211"):
            with self.subTest(deadline=deadline):
                result = self.sql(f"SELECT * FROM ops.relay_operator_reap({deadline});", role, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("reaper Lease deadline refused", result.stderr)
                self.assertEqual(self.counts(), "2|2")
        # Fixed expiry calculated before dispatch becomes stale while this same connection waits.
        delayed = self.sql(r"""
          SELECT floor(extract(epoch FROM clock_timestamp()))::bigint + 31 AS expiry \gset
          SELECT pg_sleep(2);
          SELECT * FROM ops.relay_operator_reap(:expiry);
        """, role, check=False)
        self.assertNotEqual(delayed.returncode, 0)
        self.assertIn("reaper Lease deadline refused", delayed.stderr)
        self.assertEqual(self.counts(), "2|2")
        # The first DELETE has happened when the second table's trigger blocks. A real timeout must
        # roll both deletions back. A shorter test timeout exercises the same PostgreSQL mechanism.
        self.sql("""
          CREATE FUNCTION public.test_reap_delay() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN PERFORM pg_sleep(2); RETURN OLD; END $$;
          CREATE TRIGGER delay_reap BEFORE DELETE ON memberships FOR EACH ROW EXECUTE FUNCTION test_reap_delay();
        """)
        failed = self.sql("SET statement_timeout='100ms'; SELECT * FROM ops.relay_operator_reap("
                          "floor(extract(epoch FROM clock_timestamp()))::bigint + 60);", role, check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("statement timeout", failed.stderr)
        self.assertEqual(self.counts(), "2|2")
        self.sql("DROP TRIGGER delay_reap ON memberships;")
        result = self.sql("SELECT * FROM ops.relay_operator_reap("
                          "floor(extract(epoch FROM clock_timestamp()))::bigint + 60);", role)
        self.assertEqual(result.stdout.strip(), "1|1")
        self.assertEqual(self.counts(), "1|1")
        self.assertEqual(self.sql("SELECT user_id FROM sessions UNION ALL SELECT user_id FROM memberships;").stdout.strip(),
                         "2\n2")
        self.assertEqual(self.sql("SELECT * FROM ops.relay_operator_reap("
                                 "floor(extract(epoch FROM clock_timestamp()))::bigint + 60);", role).stdout.strip(), "0|0")

    def test_original_expiry_with_database_clock_ten_seconds_behind(self):
        # A node-created 180s expiry appears 190s away on a DB clock 10s behind at take.
        # This exercises only the far-future sanity check, not a supported cross-system skew budget.
        result = self.sql("SELECT * FROM ops.relay_operator_reap("
                          "floor(extract(epoch FROM clock_timestamp()))::bigint + 190);", "relay_operator_reaper")
        self.assertEqual(result.stdout.strip(), "1|1")
        self.assertEqual(self.counts(), "1|1")


if __name__ == "__main__":
    unittest.main()
