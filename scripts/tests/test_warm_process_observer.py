import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    'warm_process_observer', pathlib.Path(__file__).parents[1] / 'observe-warm-processes.py')
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


def stat_bytes(pid=10, ppid=1, start=100, rss=3, comm=b'private command'):
    fields = [b'S'] + [b'0'] * 49
    fields[1], fields[19], fields[21] = str(ppid).encode(), str(start).encode(), str(rss).encode()
    return str(pid).encode() + b' (' + comm + b') ' + b' '.join(fields) + b'\n'


def row(pid, ppid, start):
    return {'pid': pid, 'ppid': ppid, 'startTicks': start, 'rssBytes': 4096}


class Reader:
    def __init__(self, rows):
        self.rows = {item['pid']: item for item in rows}
        self.errors = {}
        self.calls = []

    def pids(self, limit, check):
        check()
        return list(self.rows)

    def read(self, pid):
        self.calls.append(pid)
        if pid in self.errors:
            raise observer.Gap(self.errors[pid])
        if pid not in self.rows:
            raise observer.Gap('stat-missing')
        return dict(self.rows[pid])


def sample(reader, tracked=None, limit=100):
    return observer.snapshot(reader, 10, 100, tracked or {}, limit, lambda: None)[:3]


class Clock:
    def __init__(self):
        self.value = 0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def options(**changes):
    values = dict(root_pid=10, root_start_ticks=100, duration=2, interval=1,
                  max_processes=100, max_output_bytes=8192)
    values.update(changes)
    return SimpleNamespace(**values)


class WarmProcessObserverTest(unittest.TestCase):
    def test_parser_discards_parentheses_newlines_and_non_utf8_comm(self):
        data = stat_bytes(comm=b'SECRET ) (x)\nTOKEN\xff')
        self.assertEqual(observer.parse_stat(data, 10, 4096),
                         {'pid': 10, 'ppid': 1, 'startTicks': 100, 'rssBytes': 12288})

    def test_malformed_size_identity_and_numeric_bounds(self):
        values = [b'invalid SECRET', stat_bytes(pid=11), b'x' * 4097,
                  stat_bytes(rss=-1), stat_bytes(start=2**64), stat_bytes(ppid=2**31),
                  b'10 (secret) S 1 2', stat_bytes(rss=2**64)]
        for data in values:
            with self.subTest(data_size=len(data)), self.assertRaises(observer.Gap) as error:
                observer.parse_stat(data, 10, 4096)
            self.assertNotIn('SECRET', str(error.exception))

    def test_descendants_exclude_unrelated_and_project_only_metadata(self):
        reader = Reader([row(10, 1, 100), row(11, 10, 101), row(12, 11, 102), row(20, 1, 90)])
        reader.rows[11]['argv'] = 'SECRET'
        rows, gaps, known = sample(reader)
        self.assertEqual([r['pid'] for r in rows], [10, 11, 12])
        self.assertEqual(gaps, [])
        self.assertEqual(known, {10: 100, 11: 101, 12: 102})
        self.assertTrue(all(set(r) == set(observer.FIELDS) for r in rows))

    def test_reparented_observed_identity_is_retained_but_not_unknown_children(self):
        reader = Reader([row(10, 1, 100), row(11, 10, 101)])
        _, _, known = sample(reader)
        reader.rows[11]['ppid'] = 1
        reader.rows[12] = row(12, 11, 103)
        rows, gaps, known = sample(reader, known)
        self.assertEqual([r['pid'] for r in rows], [10, 11])
        self.assertEqual(gaps, [])

    def test_pid_reuse_is_reported_and_not_followed_as_old_process(self):
        reader = Reader([row(10, 1, 100), row(11, 10, 101)])
        _, _, known = sample(reader)
        reader.rows[11] = row(11, 1, 200)
        rows, gaps, known = sample(reader, known)
        self.assertEqual([r['pid'] for r in rows], [10])
        self.assertEqual(gaps, [{'pid': 11, 'startTicks': 101, 'code': 'pid-reused'}])
        self.assertNotIn(11, known)
        reader.rows[11]['ppid'] = 10
        self.assertEqual(sample(reader, known)[2][11], 200)

    def test_root_replacement_before_or_during_scan_stops(self):
        reader = Reader([row(10, 1, 999)])
        with self.assertRaisesRegex(observer.Gap, '^root-identity-changed$'):
            sample(reader)
        reader = Reader([row(10, 1, 100)])
        original = reader.read

        def replacing(pid):
            if reader.calls:
                reader.rows[10]['startTicks'] = 200
            return original(pid)
        reader.read = replacing
        with self.assertRaisesRegex(observer.Gap, '^root-identity-changed$'):
            sample(reader)

    def test_missing_or_denied_reads_are_explicit_and_root_read_failure_stops(self):
        reader = Reader([row(10, 1, 100), row(11, 10, 101), row(12, 10, 102)])
        _, _, known = sample(reader)
        del reader.rows[11]
        reader.errors[12] = 'stat-denied'
        rows, gaps, following = sample(reader, known)
        self.assertEqual([r['pid'] for r in rows], [10])
        self.assertEqual(gaps, [{'pid': 12, 'code': 'stat-denied'}])
        self.assertEqual(following, known)
        reader.errors[10] = 'stat-denied'
        with self.assertRaisesRegex(observer.Gap, '^root-unavailable$'):
            sample(reader, known)

    def test_inventory_bound_includes_missing_tracked_identities(self):
        reader = Reader([row(10, 1, 100)])
        with self.assertRaisesRegex(observer.Gap, '^inventory-limit$'):
            sample(reader, {11: 101}, limit=1)

    def test_parent_started_after_child_does_not_establish_ancestry(self):
        rows, gaps, _ = sample(Reader([row(10, 1, 100), row(11, 10, 99)]))
        self.assertEqual([r['pid'] for r in rows], [10])
        self.assertEqual(gaps, [{'pid': 11, 'code': 'ancestry-time-order'}])

    def run_observer(self, reader, args=None, clock=None):
        clock, out = clock or Clock(), io.StringIO()
        result = observer.observe(args or options(), reader, out, clock.now, clock.sleep,
                                  lambda: '2026-11-26T00:00:00.000+00:00')
        return result, [json.loads(line) for line in out.getvalue().splitlines()], out.getvalue()

    def test_duration_sampling_and_output_allowlist(self):
        status, records, raw = self.run_observer(Reader([row(10, 1, 100)]))
        self.assertEqual(status, 0)
        self.assertEqual([r['type'] for r in records], ['sample', 'sample', 'stop'])
        self.assertEqual(records[-1]['reason'], 'duration-limit')
        self.assertEqual(set(records[0]), {'type', 'sampleStartedAt', 'observedAt', 'processes', 'absent', 'gaps'})
        self.assertNotIn('comm', raw)

    def test_incomplete_deadline_sample_is_failure_not_clean_duration_end(self):
        clock = Clock()
        reader = Reader([row(10, 1, 100), row(11, 10, 101)])
        original = reader.read

        def slow(pid):
            clock.value += 1.1
            return original(pid)
        reader.read = slow
        status, records, _ = self.run_observer(reader, clock=clock)
        self.assertEqual(status, 1)
        self.assertEqual(records, [{'type': 'stop', 'observedAt': '2026-11-26T00:00:00.000+00:00', 'reason': 'sample-deadline'}])

    def test_sample_overrun_and_per_read_gaps_are_not_clean_success(self):
        clock = Clock()
        reader = Reader([row(10, 1, 100)])
        original = reader.read

        def slow(pid):
            clock.value += .2
            return original(pid)
        reader.read = slow
        status, records, _ = self.run_observer(reader, options(interval=.25), clock)
        self.assertEqual(status, 1)
        self.assertIn({'code': 'sample-overrun'}, records[0]['gaps'])

    def test_output_limit_reserves_fixed_stop_record(self):
        reader = Reader([row(10, 1, 100)] + [row(n, 10, 100+n) for n in range(11, 100)])
        status, records, raw = self.run_observer(reader, options(max_output_bytes=4096))
        self.assertEqual(status, 1)
        self.assertEqual(records[-1]['reason'], 'output-limit')
        self.assertLessEqual(len(raw.encode()), 4096)
        self.assertFalse(any(r['type'] == 'sample' for r in records))

    def test_unexpected_errors_and_unknown_gap_codes_are_sanitized(self):
        reader = Reader([row(10, 1, 100)])
        reader.read = lambda pid: (_ for _ in ()).throw(RuntimeError('SECRET raw payload'))
        status, records, raw = self.run_observer(reader)
        self.assertEqual(status, 1)
        self.assertEqual(records[-1]['reason'], 'observer-failed')
        self.assertNotIn('SECRET', raw)
        self.assertEqual(str(observer.Gap('SECRET')), 'observer-failed')

    def test_cli_invalid_arguments_do_not_open_proc_or_print_arguments(self):
        for tail in [['--duration', '901'], ['--interval', 'nan'], ['--interval', '.1'],
                     ['--max-processes', '0'], ['--root-start-ticks', '0'],
                     ['--max-output-bytes', str(observer.MAX_OUTPUT+1)], ['--SECRET']]:
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(tail=tail), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.object(observer, 'ProcReader') as reader:
                self.assertEqual(observer.main(['--root-pid', '10', '--root-start-ticks', '100'] + tail), 2)
                reader.assert_not_called()
            self.assertEqual(err.getvalue(), '')
            self.assertNotIn('SECRET', out.getvalue())
            self.assertEqual(json.loads(out.getvalue())['reason'], 'startup-refused')

    @unittest.skipUnless(os.name == 'posix', 'Linux file-open contract')
    def test_fixture_stat_only_no_symlink_fifo_or_oversized_reads(self):
        with tempfile.TemporaryDirectory() as folder:
            pid = pathlib.Path(folder) / '10'
            pid.mkdir()
            target = pid / 'stat'
            target.write_bytes(stat_bytes(comm=b'SECRET'))
            reader = observer.ProcReader(folder, 4096)
            self.assertEqual(reader.read(10)['rssBytes'], 12288)
            target.write_bytes(b'SECRET' * 1000)
            with self.assertRaisesRegex(observer.Gap, '^stat-too-large$'):
                reader.read(10)
            target.unlink()
            (pid / 'cmdline').write_text('SECRET command')
            target.symlink_to(pid / 'cmdline')
            with self.assertRaisesRegex(observer.Gap, '^stat-read-error$'):
                reader.read(10)
            target.unlink()
            os.mkfifo(target)
            with self.assertRaisesRegex(observer.Gap, '^stat-not-regular$'):
                reader.read(10)

    def test_directory_inventory_limit_is_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            for pid in (10, 11):
                (pathlib.Path(folder) / str(pid)).mkdir()
            with self.assertRaisesRegex(observer.Gap, '^inventory-limit$'):
                observer.ProcReader(folder, 4096).pids(1, lambda: None)

    def test_invalid_page_size_does_not_fall_back_or_read_host_configuration(self):
        with patch.object(observer.os, 'sysconf', create=True) as sysconf:
            with self.assertRaisesRegex(observer.Gap, '^page-size-invalid$'):
                observer.ProcReader('/unused-fixture', 0)
            sysconf.assert_not_called()

    def test_normal_child_exit_records_absence_once_without_failing_collection(self):
        clock = Clock()
        reader = Reader([row(10, 1, 100), row(11, 10, 101)])
        original_sleep = clock.sleep

        def child_exit(seconds):
            reader.rows.pop(11, None)
            original_sleep(seconds)
        clock.sleep = child_exit
        status, records, _ = self.run_observer(reader, options(duration=3), clock)
        self.assertEqual(status, 0)
        self.assertEqual(records[1]['absent'], [{
            'pid': 11, 'startTicks': 101, 'observedAt': '2026-11-26T00:00:00.000+00:00',
            'status': 'absent-at-read'}])
        self.assertEqual(records[2]['absent'], [])
        self.assertEqual(reader.calls.count(11), 2)
        self.assertTrue(all(not r['gaps'] for r in records if r['type'] == 'sample'))

    def test_absent_identity_reappears_or_reused_pid_is_distinguished(self):
        reader = Reader([row(10, 1, 100), row(11, 10, 101)])
        dormant, known = set(), {}

        def take():
            nonlocal known
            rows, gaps, known, absent = observer.snapshot(
                reader, 10, 100, known, 100, lambda: None, dormant, lambda: 'fixed-time')
            return rows, gaps, absent
        take()
        del reader.rows[11]
        self.assertEqual(take()[2][0]['status'], 'absent-at-read')
        reader.rows[11] = row(11, 1, 101)
        rows, gaps, absent = take()
        self.assertEqual([r['pid'] for r in rows], [10, 11])
        self.assertEqual((gaps, absent, dormant), ([], [], set()))
        del reader.rows[11]
        take()
        reader.rows[11] = row(11, 1, 999)
        rows, gaps, absent = take()
        self.assertEqual([r['pid'] for r in rows], [10])
        self.assertEqual(gaps, [{'pid': 11, 'startTicks': 101, 'code': 'pid-reused'}])
        self.assertEqual(absent, [])

    def test_unobserved_process_disappearing_mid_inventory_is_still_a_gap(self):
        reader = Reader([row(10, 1, 100), row(11, 10, 101)])
        reader.errors[11] = 'stat-missing'
        self.assertEqual(sample(reader)[1], [{'pid': 11, 'code': 'stat-missing'}])


if __name__ == '__main__':
    unittest.main()
