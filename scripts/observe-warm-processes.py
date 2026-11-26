#!/usr/bin/env python3
"""Bounded /proc/stat metadata observations, never a provider-identity proof."""
import argparse
import collections
import datetime
import json
import math
import os
import re
import stat
import sys
import time

STAT_BYTES = 4096
MAX_PID = 2**31 - 1
MAX_TICKS = 2**64 - 1
MAX_PROCESSES = 16384
MAX_OUTPUT = 16 * 1024 * 1024
STOP_RESERVE = 1024
GAP_CODES = frozenset({
    'stat-invalid', 'stat-too-large', 'page-size-invalid', 'inventory-limit',
    'inventory-unavailable', 'stat-not-regular', 'stat-missing', 'stat-denied',
    'stat-read-error', 'root-unavailable', 'root-identity-changed', 'tracking-limit',
    'output-limit', 'invalid-arguments', 'duration-limit', 'sample-deadline',
    'observer-failed', 'platform-not-supported',
    'pid-reused', 'ancestry-time-order', 'sample-overrun',
})
FIELDS = ('pid', 'ppid', 'startTicks', 'rssBytes')


class Gap(Exception):
    """Only fixed codes from this module may cross the output boundary."""

    def __init__(self, code):
        super().__init__(code if code in GAP_CODES else 'observer-failed')


def unsigned(value, maximum):
    if not re.fullmatch(rb'[0-9]{1,20}', value):
        raise Gap('stat-invalid')
    number = int(value)
    if number > maximum:
        raise Gap('stat-invalid')
    return number


def parse_stat(data, pid, page_size):
    if len(data) > STAT_BYTES:
        raise Gap('stat-too-large')
    # comm can contain spaces, newlines, parentheses and non-UTF8 bytes. Discard
    # it as bytes; field 3 starts after the last closing parenthesis.
    prefix = str(pid).encode('ascii') + b' ('
    end = data.rfind(b')')
    if not data.startswith(prefix) or end < len(prefix) or data[end:end+2] != b') ':
        raise Gap('stat-invalid')
    fields = data[end+2:].split()
    if len(fields) < 22 or not re.fullmatch(rb'[A-Za-z]', fields[0]):
        raise Gap('stat-invalid')
    return {
        'pid': pid,
        'ppid': unsigned(fields[1], MAX_PID),
        'startTicks': unsigned(fields[19], MAX_TICKS),
        'rssBytes': unsigned(fields[21], MAX_TICKS // page_size) * page_size,
    }


class ProcReader:
    def __init__(self, root='/proc', page_size=None):
        self.root = root  # fixture injection only; CLI always uses /proc
        self.page_size = os.sysconf('SC_PAGE_SIZE') if page_size is None else page_size
        if not 1024 <= self.page_size <= 1024 * 1024:
            raise Gap('page-size-invalid')

    def pids(self, limit, check_deadline):
        result = []
        try:
            with os.scandir(self.root) as entries:
                for entry in entries:
                    check_deadline()
                    if re.fullmatch(r'[1-9][0-9]{0,9}', entry.name):
                        pid = int(entry.name)
                        if pid <= MAX_PID:
                            result.append(pid)
                            if len(result) > limit:
                                raise Gap('inventory-limit')
        except OSError:
            raise Gap('inventory-unavailable') from None
        return result

    def read(self, pid):
        fd = None
        try:
            # Nonblocking and regular-file checks also keep malformed fixtures
            # from blocking. Only the fixed numeric PID/stat path is opened.
            fd = os.open(os.path.join(self.root, str(pid), 'stat'),
                         os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Gap('stat-not-regular')
            data = b''
            while len(data) <= STAT_BYTES:
                part = os.read(fd, STAT_BYTES + 1 - len(data))
                if not part:
                    break
                data += part
            return parse_stat(data, pid, self.page_size)
        except (FileNotFoundError, ProcessLookupError):
            raise Gap('stat-missing') from None
        except PermissionError:
            raise Gap('stat-denied') from None
        except OSError:
            raise Gap('stat-read-error') from None
        finally:
            if fd is not None:
                os.close(fd)


def snapshot(reader, root_pid, root_start, tracked, limit, check_deadline,
             dormant=None, timestamp=None):
    dormant = set() if dormant is None else dormant
    timestamp = utc_now if timestamp is None else timestamp
    def root():
        check_deadline()
        try:
            row = reader.read(root_pid)
        except Gap:
            raise Gap('root-unavailable') from None
        if row['startTicks'] != root_start:
            raise Gap('root-identity-changed')
        return row

    records = {root_pid: root()}
    pids = set(reader.pids(limit, check_deadline)) | (set(tracked) - dormant)
    # Keep historical identities bounded too, including temporarily unreadable
    # reparented processes. Never silently truncate an inventory.
    if len(pids | set(tracked) | {root_pid}) > limit:
        raise Gap('inventory-limit')
    gaps, absent, transient = [], [], []
    for pid in sorted(pids - {root_pid}):
        check_deadline()
        try:
            records[pid] = reader.read(pid)
            dormant.discard(pid)
        except Gap as error:
            if str(error) == 'stat-missing' and pid in tracked:
                absent.append({'pid': pid, 'startTicks': tracked[pid],
                               'observedAt': timestamp(), 'status': 'absent-at-read'})
                dormant.add(pid)
            elif str(error) == 'stat-missing':
                # Ancestry was never read: do not call this unrelated churn or
                # claim it was outside the root subtree.
                transient.append({'pid': pid, 'observedAt': timestamp(),
                                  'status': 'inventory-transient'})
            else:
                gaps.append({'pid': pid, 'code': str(error)})
    records[root_pid] = root()  # refuse a root replacement during the scan
    known = dict(tracked)
    for pid, start in tracked.items():
        if pid in records and records[pid]['startTicks'] != start:
            gaps.append({'pid': pid, 'startTicks': start, 'code': 'pid-reused'})
            del known[pid]
    children = collections.defaultdict(list)
    for row in records.values():
        children[row['ppid']].append(row)
    descendants = {root_pid}
    queue = collections.deque([root_pid])
    while queue:
        check_deadline()
        parent = queue.popleft()
        for child in children[parent]:
            pid = child['pid']
            if pid in descendants:
                continue
            if child['startTicks'] < records[parent]['startTicks']:
                gaps.append({'pid': pid, 'code': 'ancestry-time-order'})
                continue
            descendants.add(pid)
            queue.append(pid)
    selected = descendants | {
        pid for pid, start in known.items()
        if pid in records and records[pid]['startTicks'] == start
    }
    for pid in selected:
        known[pid] = records[pid]['startTicks']
    if len(known) > limit:
        raise Gap('tracking-limit')
    return ([{key: records[pid][key] for key in FIELDS} for pid in sorted(selected)],
            gaps, known, absent, transient)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds')


class Output:
    def __init__(self, stream, limit):
        self.stream, self.limit, self.used = stream, limit, 0

    def emit(self, record, terminal=False):
        line = json.dumps(record, ensure_ascii=True, separators=(',', ':')) + '\n'
        size = len(line.encode('ascii'))
        ceiling = self.limit if terminal else self.limit - STOP_RESERVE
        if self.used + size > ceiling:
            raise Gap('output-limit')
        self.stream.write(line)
        self.stream.flush()
        self.used += size


def validate(args):
    if not (1 <= args.root_pid <= MAX_PID and 1 <= args.root_start_ticks <= MAX_TICKS
            and 1 <= args.duration <= 900 and math.isfinite(args.interval)
            and 0.25 <= args.interval <= 60
            and 1 <= args.max_processes <= MAX_PROCESSES
            and 4096 <= args.max_output_bytes <= MAX_OUTPUT):
        raise Gap('invalid-arguments')


def observe(args, reader, stream, monotonic=time.monotonic, sleep=time.sleep, timestamp=utc_now):
    validate(args)
    output = Output(stream, args.max_output_bytes)
    deadline = monotonic() + args.duration
    tracked = {}
    dormant = set()
    had_gaps = False

    def check_deadline():
        if monotonic() >= deadline:
            raise Gap('duration-limit')

    def finish(reason, status):
        output.emit({'type': 'stop', 'observedAt': timestamp(), 'reason': reason}, terminal=True)
        return status

    try:
        while True:
            check_deadline()
            started = monotonic()
            began = timestamp()
            try:
                rows, gaps, tracked, absent, transient = snapshot(
                    reader, args.root_pid, args.root_start_ticks, tracked,
                    args.max_processes, check_deadline, dormant, timestamp)
            except Gap as error:
                if str(error) == 'duration-limit':
                    raise Gap('sample-deadline') from None
                raise
            elapsed = monotonic() - started
            if elapsed > args.interval:
                gaps.append({'code': 'sample-overrun'})
            had_gaps = had_gaps or bool(gaps)
            output.emit({'type': 'sample', 'sampleStartedAt': began,
                         'observedAt': timestamp(), 'processes': rows,
                         'absent': absent, 'inventoryTransient': transient, 'gaps': gaps})
            # No catch-up burst after an overrun. Deadline checks occur between
            # bounded reads; this is not a hard real-time scheduler.
            sleep(max(0, min(args.interval, deadline - monotonic())))
    except Gap as error:
        reason = str(error)
        return finish(reason, 0 if reason == 'duration-limit' and not had_gaps else 1)
    except KeyboardInterrupt:
        return finish('interrupted', 1)
    except BrokenPipeError:
        return 1
    except Exception:
        return finish('observer-failed', 1)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Gap('invalid-arguments')


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        parser.add_argument('--root-pid', type=int, required=True)
        parser.add_argument('--root-start-ticks', type=int, required=True)
        parser.add_argument('--duration', type=int, default=120)
        parser.add_argument('--interval', type=float, default=1.0)
        parser.add_argument('--max-processes', type=int, default=4096)
        parser.add_argument('--max-output-bytes', type=int, default=8 * 1024 * 1024)
        args = parser.parse_args(argv)
        validate(args)
        if sys.platform != 'linux':
            raise Gap('platform-not-supported')
        return observe(args, ProcReader(), sys.stdout)
    except BrokenPipeError:
        return 1
    except Exception:
        # Never expose raw stat data, paths, exception text or traceback.
        try:
            print(json.dumps({'type': 'stop', 'observedAt': utc_now(), 'reason': 'startup-refused'}))
        except BrokenPipeError:
            return 1
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
