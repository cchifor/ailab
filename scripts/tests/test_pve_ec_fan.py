"""pve_ec_fan: the Bosgame M5 CPU-fan curve and its node_exporter textfile (ansible/roles/pve_ec_fan).

Drives the role's real scripts against a fake /sys/class/ec_su_axb35 tree (plain files), and renders its
real templates with the real role defaults. Pinned here:

  * ec-fan-profile `apply` writes each fan's curves BEFORE mode=curve, then the APU power mode (the driver picks the starting level
    from the rampup curve in effect when the mode switches), reads them back, and never touches fan3. fan3
    in the fan list is refused, and so is a curve the driver would step wrongly on (not five rising
    integers 0-100, or a rampdown value not below the rampup value of its level), before anything is
    written. A CPU fan not in the list is put back on auto (a fan dropped from the config);
  * `reset` (the unit's ExecStop) puts both CPU fans back to auto and sets the power mode to balanced (the
    factory EC default) whatever the config says, never touches fan3, and does nothing when the module is not
    loaded;
  * a power mode other than quiet, balanced or performance is refused before anything is written;
  * `check` (run by the role on each converge) writes nothing, exits 0 when the live state is the
    profile and 3 when it is not: a configured fan off the curve or on another curve, a dropped fan still
    on a curve, the driver gone;
  * ec-fan-textfile reports all three fans, the EC temperature, the power mode and which CPU fans the
    profile manages, counts a value it cannot read instead of publishing it, says driver_loaded 0 without
    the module, and replaces the file in one rename;
  * the rendered config is one the profile script accepts, the units say what the scripts expect, and
    ai-node1 and ai-node2 turn the role on, ai-node3 does not.

The Docker converge (ansible/roles/pve_ec_fan/tests/test-converge-docker.sh: DKMS against the Proxmox
headers, systemd, idempotence, --check, recovery, disable) needs the network and is not run here.

    python3 -m unittest discover -s scripts/tests -p "test_pve_ec_fan.py" -v
"""
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / 'ansible/roles/pve_ec_fan'
PROFILE = ROLE / 'files/ec-fan-profile.sh'
TEXTFILE = ROLE / 'files/ec-fan-textfile.sh'
BASH = shutil.which('bash')

# The role's default curve (the 2026-11-19 quiet one).
UP = '40,55,80,90,95'
DOWN = '35,50,75,85,90'
# The driver's own defaults (src/ec_su_axb35.c at the pin): what a freshly loaded module shows.
DRIVER_UP = {1: '60,70,83,95,97', 2: '60,70,83,95,97', 3: '20,60,83,95,97'}
DRIVER_DOWN = {1: '40,50,80,94,96', 2: '40,50,80,94,96', 3: '0,50,80,94,96'}


class FakeSysfs:
    """/sys/class/ec_su_axb35 as plain files, values with the driver's trailing newline."""

    def __init__(self, root: Path):
        self.root = root

    def create(self, mode='auto', rpm=(1540, 1560, 0), level=(1, 1, 0), temp=52, power='balanced'):
        for n in (1, 2, 3):
            d = self.root / f'fan{n}'
            d.mkdir(parents=True, exist_ok=True)
            self.put(f'fan{n}/mode', mode)
            self.put(f'fan{n}/rpm', rpm[n - 1])
            self.put(f'fan{n}/level', level[n - 1])
            self.put(f'fan{n}/rampup_curve', DRIVER_UP[n])
            self.put(f'fan{n}/rampdown_curve', DRIVER_DOWN[n])
        (self.root / 'temp1').mkdir(exist_ok=True)
        self.put('temp1/temp', temp)
        self.put('temp1/min', 40)
        self.put('temp1/max', 71)
        (self.root / 'apu').mkdir(exist_ok=True)
        self.put('apu/power_mode', power)
        # fan3 must never be written: give its files an old mtime to compare against.
        for f in (self.root / 'fan3').iterdir():
            os.utime(f, (1_000_000_000, 1_000_000_000))
        return self

    def put(self, rel, value, newline=True):
        (self.root / rel).write_text(f'{value}\n' if newline else str(value), newline='\n')

    def get(self, rel):
        return (self.root / rel).read_text().strip()

    def fan3_snapshot(self):
        return {f.name: (f.read_text(), f.stat().st_mtime) for f in sorted((self.root / 'fan3').iterdir())}


@unittest.skipUnless(BASH, 'bash is required')
class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sysfs = FakeSysfs(self.tmp / 'ec_su_axb35')

    def run_profile(self, action, fans='1 2', up=UP, down=DOWN, power='quiet', wait_s='2', timeout=30):
        env = dict(os.environ, EC_FAN_SYSFS=self.sysfs.root.as_posix(), EC_FAN_FANS=fans,
                   EC_FAN_RAMPUP_CURVE=up, EC_FAN_RAMPDOWN_CURVE=down, EC_FAN_POWER_MODE=power,
                   EC_FAN_WAIT_S=wait_s)
        return subprocess.run([BASH, PROFILE.as_posix(), action], env=env, capture_output=True,
                              text=True, timeout=timeout)

    def assert_untouched(self, before3):
        for n in (1, 2):
            self.assertEqual(self.sysfs.get(f'fan{n}/mode'), 'auto')
            self.assertEqual(self.sysfs.get(f'fan{n}/rampup_curve'), DRIVER_UP[n])
            self.assertEqual(self.sysfs.get(f'fan{n}/rampdown_curve'), DRIVER_DOWN[n])
        self.assertEqual(self.sysfs.get('apu/power_mode'), 'balanced')
        self.assertEqual(self.sysfs.fan3_snapshot(), before3)

    def test_apply_puts_the_cpu_fans_on_the_curve(self):
        self.sysfs.create()
        before3 = self.sysfs.fan3_snapshot()
        run = self.run_profile('apply')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        for n in (1, 2):
            self.assertEqual(self.sysfs.get(f'fan{n}/rampup_curve'), UP)
            self.assertEqual(self.sysfs.get(f'fan{n}/rampdown_curve'), DOWN)
            self.assertEqual(self.sysfs.get(f'fan{n}/mode'), 'curve')
        self.assertEqual(self.sysfs.get('apu/power_mode'), 'quiet')
        self.assertEqual(self.sysfs.fan3_snapshot(), before3)

    def test_apply_writes_the_power_mode_it_is_given(self):
        for mode in ('quiet', 'balanced', 'performance'):
            with self.subTest(mode=mode):
                self.sysfs.create(power='performance' if mode != 'performance' else 'quiet')
                run = self.run_profile('apply', power=mode)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertEqual(self.sysfs.get('apu/power_mode'), mode)

    def test_a_bad_power_mode_is_refused_before_any_write(self):
        for mode in ('', 'turbo', 'Quiet', 'quiet ', 'auto'):
            with self.subTest(mode=repr(mode)):
                self.sysfs.create()
                before3 = self.sysfs.fan3_snapshot()
                run = self.run_profile('apply', power=mode)
                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                self.assertIn('EC_FAN_POWER_MODE', run.stderr)
                self.assert_untouched(before3)
                self.assertEqual(self.run_profile('check', power=mode).returncode, 1)

    def test_apply_writes_the_curves_before_the_mode(self):
        self.sysfs.create()
        run = self.run_profile('apply')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        writes = re.findall(r'^ec-fan-profile: ((?:fan\d|apu)/\w+) <- ', run.stdout, re.M)
        self.assertEqual(writes, ['fan1/rampup_curve', 'fan1/rampdown_curve', 'fan1/mode',
                                  'fan2/rampup_curve', 'fan2/rampdown_curve', 'fan2/mode', 'apu/power_mode'])

    def test_apply_is_repeatable(self):
        self.sysfs.create(mode='curve')
        for _ in range(2):
            run = self.run_profile('apply')
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.sysfs.get('fan1/mode'), 'curve')

    def test_fan3_and_unknown_fans_are_refused_before_any_write(self):
        for fans in ('1 3', '3', '1 2 3', '4', '0', 'fan1', ''):
            with self.subTest(fans=fans):
                self.sysfs.create()
                before3 = self.sysfs.fan3_snapshot()
                run = self.run_profile('apply', fans=fans)
                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                self.assert_untouched(before3)
        self.assertIn('fan3 is not managed by this role yet', self.run_profile('apply', fans='1 3').stderr)

    def test_curves_the_driver_would_misstep_on_are_refused_before_any_write(self):
        bad = {
            'four values': ('35,50,60,68', DOWN),
            'six values': ('35,50,60,68,95,99', DOWN),
            'not rising': ('35,60,50,68,95', DOWN),
            'repeated value': ('35,50,50,68,95', DOWN),
            'above 100': ('35,50,60,68,101', DOWN),
            'leading zero': ('035,50,60,68,95', DOWN),
            'negative': (UP, '-1,45,55,63,90'),
            'spaces': ('40, 55, 80, 90, 95', DOWN),
            'empty': ('', DOWN),
            'rampdown equal to rampup': (UP, '35,50,80,85,90'),
            'rampdown above rampup': (UP, '35,50,75,92,94'),
        }
        for name, (up, down) in bad.items():
            with self.subTest(name):
                self.sysfs.create()
                before3 = self.sysfs.fan3_snapshot()
                run = self.run_profile('apply', up=up, down=down)
                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                self.assert_untouched(before3)

    def test_apply_waits_for_the_driver_to_appear(self):
        timer = threading.Timer(1.5, self.sysfs.create)
        timer.start()
        self.addCleanup(timer.cancel)
        run = self.run_profile('apply', wait_s='10')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.sysfs.get('fan2/mode'), 'curve')

    def test_apply_fails_when_the_driver_never_appears(self):
        run = self.run_profile('apply', wait_s='1')
        self.assertEqual(run.returncode, 1)
        self.assertIn('is ec_su_axb35 loaded?', run.stderr)

    def test_a_refused_write_fails_the_unit(self):
        # The kernel refuses a value with EINVAL; a directory where the attribute should be refuses any write,
        # as root too.
        self.sysfs.create()
        (self.sysfs.root / 'fan2/rampdown_curve').unlink()
        (self.sysfs.root / 'fan2/rampdown_curve').mkdir()
        run = self.run_profile('apply')
        self.assertEqual(run.returncode, 1)
        self.assertIn('fan2/rampdown_curve failed', run.stderr)
        self.assertEqual(self.sysfs.get('fan2/mode'), 'auto')

    def test_apply_hands_a_cpu_fan_dropped_from_the_list_back_to_auto(self):
        # systemd's ExecStop reads the NEW config, so only apply can reset a fan that left the list.
        self.sysfs.create(mode='curve')
        before3 = self.sysfs.fan3_snapshot()
        run = self.run_profile('apply', fans='1')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.sysfs.get('fan1/mode'), 'curve')
        self.assertEqual(self.sysfs.get('fan1/rampup_curve'), UP)
        self.assertEqual(self.sysfs.get('fan2/mode'), 'auto')
        self.assertEqual(self.sysfs.fan3_snapshot(), before3)
        writes = re.findall(r'^ec-fan-profile: ((?:fan\d|apu)/\w+) <- ', run.stdout, re.M)
        self.assertEqual(writes, ['fan1/rampup_curve', 'fan1/rampdown_curve', 'fan1/mode', 'fan2/mode',
                                  'apu/power_mode'])

    def test_reset_hands_both_cpu_fans_and_the_power_mode_back_and_never_fan3(self):
        self.sysfs.create(mode='curve', power='quiet')
        before3 = self.sysfs.fan3_snapshot()
        run = self.run_profile('reset', fans='1')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.sysfs.get('fan1/mode'), 'auto')
        self.assertEqual(self.sysfs.get('fan2/mode'), 'auto')
        self.assertEqual(self.sysfs.get('apu/power_mode'), 'balanced')
        self.assertEqual(self.sysfs.fan3_snapshot(), before3)

    def test_reset_ignores_a_broken_config(self):
        # A broken config must not keep the fans or the APU on the profile when the unit is stopped, nor
        # reach fan3.
        self.sysfs.create(mode='curve', power='performance')
        before3 = self.sysfs.fan3_snapshot()
        run = self.run_profile('reset', fans='1 3', up='nonsense', down='', power='turbo')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.sysfs.get('fan1/mode'), 'auto')
        self.assertEqual(self.sysfs.get('fan2/mode'), 'auto')
        self.assertEqual(self.sysfs.get('apu/power_mode'), 'balanced')
        self.assertEqual(self.sysfs.fan3_snapshot(), before3)

    def test_apply_after_reset_runs_the_profile_again(self):
        # a stop then a start (the role's restart): balanced in between, the configured mode after
        self.sysfs.create()
        self.assertEqual(self.run_profile('apply').returncode, 0)
        self.assertEqual(self.run_profile('reset').returncode, 0)
        self.assertEqual(self.sysfs.get('apu/power_mode'), 'balanced')
        self.assertEqual(self.run_profile('apply').returncode, 0)
        self.assertEqual(self.sysfs.get('apu/power_mode'), 'quiet')
        self.assertEqual(self.sysfs.get('fan1/mode'), 'curve')

    def test_reset_without_the_module_is_a_no_op(self):
        run = self.run_profile('reset')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn('module not loaded', run.stdout)

    def snapshot(self):
        return {f: (f.read_text(), f.stat().st_mtime_ns)
                for f in sorted(self.sysfs.root.rglob('*')) if f.is_file()}

    def test_check_passes_on_the_profile_and_writes_nothing(self):
        self.sysfs.create()
        self.assertEqual(self.run_profile('apply').returncode, 0)
        before = self.snapshot()
        run = self.run_profile('check')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.snapshot(), before)
        # one fan managed and the other back on auto is the profile too
        self.assertEqual(self.run_profile('apply', fans='2').returncode, 0)
        self.assertEqual(self.run_profile('check', fans='2').returncode, 0)

    def test_check_reports_every_way_off_the_profile(self):
        cases = {
            # a module loaded fresh under an active service: every fan auto, the driver's own curves
            'fresh module': lambda: None,
            'managed fan on auto': lambda: self.sysfs.put('fan2/mode', 'auto'),
            'managed fan on fixed': lambda: self.sysfs.put('fan1/mode', 'fixed'),
            'another rampup curve': lambda: self.sysfs.put('fan1/rampup_curve', '42,57,82,92,96'),
            'another rampdown curve': lambda: self.sysfs.put('fan2/rampdown_curve', '30,45,55,60,90'),
            'power mode back on balanced': lambda: self.sysfs.put('apu/power_mode', 'balanced'),
            'power mode on performance': lambda: self.sysfs.put('apu/power_mode', 'performance'),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.sysfs.create()
                if name != 'fresh module':
                    self.assertEqual(self.run_profile('apply').returncode, 0)
                change()
                before = self.snapshot()
                run = self.run_profile('check')
                self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
                self.assertRegex(run.stdout, r"^(fan[12]|apu): is '")
                self.assertEqual(self.snapshot(), before)

    def test_check_reports_a_dropped_fan_still_on_a_curve(self):
        self.sysfs.create()
        self.assertEqual(self.run_profile('apply').returncode, 0)
        run = self.run_profile('check', fans='1')
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        self.assertIn("fan2: is 'curve', want 'auto'", run.stdout)

    def test_check_reports_a_missing_driver(self):
        run = self.run_profile('check')
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)
        self.assertIn("fan1: is 'missing", run.stdout)

    def test_check_refuses_a_bad_config(self):
        self.sysfs.create()
        self.assertEqual(self.run_profile('check', fans='1 3').returncode, 1)
        self.assertEqual(self.run_profile('check', down=UP).returncode, 1)
        self.assertEqual(self.run_profile('check', power='loud').returncode, 1)

    def test_unknown_action(self):
        self.sysfs.create()
        self.assertEqual(self.run_profile('status').returncode, 1)


FAMILY = re.compile(r'^# (HELP|TYPE) ([a-z_]+) (.+)$')
SAMPLE = re.compile(r'^([a-z_]+)(\{[a-z_]+="[^"]*"(?:,[a-z_]+="[^"]*")*\})? (-?[0-9]+(?:\.[0-9]+)?)$')


def parse_exposition(text):
    """A strict reading of the text format as node_exporter's textfile collector needs it: HELP and TYPE
    once per family, before its samples, families contiguous. Returns {(name, labels): value}."""
    samples, seen, current = {}, set(), None
    for line in text.splitlines():
        m = FAMILY.match(line)
        if m:
            kind, name, rest = m.groups()
            if kind == 'HELP':
                if name in seen:
                    raise AssertionError(f'family {name} repeated')
                seen.add(name)
                current = name
            elif name != current or rest != 'gauge':
                raise AssertionError(f'TYPE line out of place: {line!r}')
            continue
        m = SAMPLE.match(line)
        if not m or m.group(1) != current:
            raise AssertionError(f'not a sample of {current}: {line!r}')
        samples[(m.group(1), m.group(2) or '')] = m.group(3)
    return samples


@unittest.skipUnless(BASH, 'bash is required')
class TextfileTests(unittest.TestCase):
    NOW = '1763550000'

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sysfs = FakeSysfs(self.tmp / 'ec_su_axb35')
        self.dir = self.tmp / 'textfile'
        self.dir.mkdir()

    def run_textfile(self, fans='1 2'):
        env = dict(os.environ, EC_FAN_SYSFS=self.sysfs.root.as_posix(),
                   EC_FAN_TEXTFILE_DIR=self.dir.as_posix(), EC_FAN_NOW=self.NOW)
        env.pop('EC_FAN_FANS', None)
        if fans is not None:
            env['EC_FAN_FANS'] = fans
        return subprocess.run([BASH, TEXTFILE.as_posix()], env=env, capture_output=True, text=True,
                              timeout=30)

    def prom(self):
        return (self.dir / 'ec_fan.prom').read_text()

    def test_a_loaded_driver_reports_every_fan_the_temperature_and_the_power_mode(self):
        self.sysfs.create(mode='curve', rpm=(4210, 4190, 0), level=(4, 4, 0), temp=71)
        self.sysfs.put('fan3/mode', 'auto')
        run = self.run_textfile()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(parse_exposition(self.prom()), {
            ('ec_fan_driver_loaded', ''): '1',
            ('ec_fan_managed', '{fan="1"}'): '1',
            ('ec_fan_managed', '{fan="2"}'): '1',
            ('ec_fan_rpm', '{fan="1"}'): '4210',
            ('ec_fan_rpm', '{fan="2"}'): '4190',
            ('ec_fan_rpm', '{fan="3"}'): '0',
            ('ec_fan_level', '{fan="1"}'): '4',
            ('ec_fan_level', '{fan="2"}'): '4',
            ('ec_fan_level', '{fan="3"}'): '0',
            ('ec_fan_mode_info', '{fan="1",mode="curve"}'): '1',
            ('ec_fan_mode_info', '{fan="2",mode="curve"}'): '1',
            ('ec_fan_mode_info', '{fan="3",mode="auto"}'): '1',
            ('ec_temp_celsius', ''): '71',
            ('ec_apu_power_mode_info', '{mode="balanced"}'): '1',
            ('ec_fan_read_errors', ''): '0',
            ('ec_fan_textfile_timestamp_seconds', ''): self.NOW,
        })

    def test_without_the_driver_it_says_so_and_still_stamps_the_file(self):
        run = self.run_textfile()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(parse_exposition(self.prom()), {
            ('ec_fan_driver_loaded', ''): '0',
            ('ec_fan_managed', '{fan="1"}'): '1',
            ('ec_fan_managed', '{fan="2"}'): '1',
            ('ec_fan_read_errors', ''): '0',
            ('ec_fan_textfile_timestamp_seconds', ''): self.NOW,
        })

    def test_an_unreadable_value_is_counted_not_published(self):
        self.sysfs.create()
        self.sysfs.put('fan1/rpm', 'garbage')
        self.sysfs.put('fan2/level', '9')
        self.sysfs.put('fan2/mode', 'unknown')
        (self.sysfs.root / 'temp1/temp').unlink()
        # The driver answers EINVAL for an unknown power mode; a directory fails the read the same way.
        (self.sysfs.root / 'apu/power_mode').unlink()
        (self.sysfs.root / 'apu/power_mode').mkdir()
        run = self.run_textfile()
        self.assertEqual(run.returncode, 0, run.stderr)
        samples = parse_exposition(self.prom())
        self.assertEqual(samples[('ec_fan_read_errors', '')], '5')
        self.assertNotIn(('ec_fan_rpm', '{fan="1"}'), samples)
        self.assertEqual(samples[('ec_fan_rpm', '{fan="2"}')], '1560')
        self.assertNotIn(('ec_fan_level', '{fan="2"}'), samples)
        self.assertEqual([k for k in samples if k[0] == 'ec_fan_mode_info'],
                         [('ec_fan_mode_info', '{fan="1",mode="auto"}'),
                          ('ec_fan_mode_info', '{fan="3",mode="auto"}')])
        self.assertFalse(any(k[0] in ('ec_temp_celsius', 'ec_apu_power_mode_info') for k in samples))

    def test_managed_follows_the_profiles_fan_list(self):
        self.sysfs.create()
        for fans, want in (('1', ('1', '0')), ('2', ('0', '1')), (None, ('0', '0'))):
            with self.subTest(fans=fans):
                run = self.run_textfile(fans=fans)
                self.assertEqual(run.returncode, 0, run.stderr)
                samples = parse_exposition(self.prom())
                self.assertEqual((samples[('ec_fan_managed', '{fan="1"}')],
                                  samples[('ec_fan_managed', '{fan="2"}')]), want)
                self.assertNotIn(('ec_fan_managed', '{fan="3"}'), samples)

    def test_a_value_without_a_trailing_newline_is_read(self):
        self.sysfs.create()
        self.sysfs.put('fan1/rpm', 1540, newline=False)
        run = self.run_textfile()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(parse_exposition(self.prom())[('ec_fan_rpm', '{fan="1"}')], '1540')

    def test_the_file_is_replaced_whole_and_no_temp_file_is_left(self):
        self.sysfs.create()
        (self.dir / 'ec_fan.prom').write_text('stale 1\n')
        (self.dir / 'other.prom').write_text('other 1\n')
        run = self.run_textfile()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn('stale', self.prom())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ['ec_fan.prom', 'other.prom'])

    def test_a_missing_textfile_dir_fails_the_unit(self):
        self.sysfs.create()
        shutil.rmtree(self.dir)
        run = self.run_textfile()
        self.assertNotEqual(run.returncode, 0)


def role_context(extra=None):
    """The role defaults with their own templates resolved, as Ansible would for these simple values."""
    ctx = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())
    ctx['ansible_kernel'] = '7.0.2-6-pve'
    ctx.update(extra or {})
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    for _ in range(5):
        for key, value in ctx.items():
            if isinstance(value, str) and '{{' in value:
                ctx[key] = env.from_string(value).render(**ctx)
            elif isinstance(value, list):
                ctx[key] = [env.from_string(v).render(**ctx) if isinstance(v, str) else v for v in value]
    return ctx


def render(name, extra=None):
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    return env.from_string((ROLE / 'templates' / name).read_text()).render(**role_context(extra))


def unit_lines(text):
    return [ln for ln in text.splitlines() if ln and not ln.startswith('#')]


class RoleRenderTests(unittest.TestCase):
    def test_defaults_pin_a_full_commit_and_a_valid_off_profile(self):
        ctx = role_context()
        self.assertIs(ctx['pve_ec_fan_enabled'], False)
        self.assertRegex(ctx['pve_ec_fan_commit'], r'^[0-9a-f]{40}$')
        self.assertEqual(ctx['pve_ec_fan_dkms_version'], '1.0+' + ctx['pve_ec_fan_commit'][:7])
        self.assertEqual(ctx['pve_ec_fan_src_dir'], '/usr/src/ec_su_axb35-' + ctx['pve_ec_fan_dkms_version'])
        self.assertEqual(ctx['pve_ec_fan_fans'], [1, 2])
        up, down = ctx['pve_ec_fan_rampup_curve'], ctx['pve_ec_fan_rampdown_curve']
        self.assertEqual(up, [40, 55, 80, 90, 95])
        self.assertEqual(down, [35, 50, 75, 85, 90])
        self.assertEqual(ctx['pve_ec_fan_power_mode'], 'quiet')
        for curve in (up, down):
            self.assertEqual(curve, sorted(set(curve)))
        self.assertTrue(all(d < u for d, u in zip(down, up)))
        self.assertIn('proxmox-headers-7.0.2-6-pve', ctx['pve_ec_fan_packages'])
        self.assertIn('proxmox-default-headers', ctx['pve_ec_fan_packages'])

    def test_the_textfile_dir_is_node_exporters(self):
        ne = yaml.safe_load((ROOT / 'ansible/roles/node_exporter/defaults/main.yml').read_text())
        self.assertEqual(role_context()['pve_ec_fan_textfile_dir'], ne['node_exporter_textfile_dir'])

    def test_the_rendered_config_is_one_the_profile_script_accepts(self):
        config = render('ec-fan-profile.env.j2')
        self.assertIn('EC_FAN_FANS="1 2"\n', config)
        self.assertIn(f'EC_FAN_RAMPUP_CURVE={UP}\n', config)
        self.assertIn(f'EC_FAN_RAMPDOWN_CURVE={DOWN}\n', config)
        self.assertIn('EC_FAN_POWER_MODE=quiet\n', config)
        if not BASH:
            self.skipTest('bash is required')
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = FakeSysfs(Path(tmp) / 'ec_su_axb35').create()
            env_file = Path(tmp) / 'ec-fan-profile'
            # systemd's EnvironmentFile and a shell agree on this simple KEY=value / KEY="a b" form.
            env_file.write_text(config.replace('EC_FAN_SYSFS=/sys/class/ec_su_axb35',
                                               f'EC_FAN_SYSFS={sysfs.root.as_posix()}'), newline='\n')
            run = subprocess.run([BASH, '-c', 'set -a; . "$1"; set +a; exec bash "$2" apply', '_',
                                  env_file.as_posix(), PROFILE.as_posix()],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(sysfs.get('fan1/mode'), 'curve')
            self.assertEqual(sysfs.get('fan2/rampdown_curve'), DOWN)
            self.assertEqual(sysfs.get('apu/power_mode'), 'quiet')

    def test_the_profile_unit_applies_after_the_module_loads_and_resets_on_stop(self):
        lines = unit_lines(render('ec-fan-profile.service.j2'))
        for expected in ('After=systemd-modules-load.service', 'Type=oneshot', 'RemainAfterExit=yes',
                         'EnvironmentFile=/etc/default/ec-fan-profile',
                         'ExecStart=/usr/local/sbin/ec-fan-profile apply',
                         'ExecStop=/usr/local/sbin/ec-fan-profile reset', 'WantedBy=multi-user.target'):
            self.assertIn(expected, lines)

    def test_the_textfile_unit_and_timer(self):
        service = unit_lines(render('ec-fan-textfile.service.j2'))
        self.assertIn('Environment=EC_FAN_TEXTFILE_DIR=/var/lib/prometheus/node-exporter', service)
        self.assertIn('ReadWritePaths=/var/lib/prometheus/node-exporter', service)
        self.assertIn('Environment=EC_FAN_SYSFS=/sys/class/ec_su_axb35', service)
        self.assertIn('ExecStart=/usr/local/sbin/ec-fan-textfile', service)
        # EC_FAN_FANS for ec_fan_managed; optional, so a missing config does not fail the metrics
        self.assertIn('EnvironmentFile=-/etc/default/ec-fan-profile', service)
        timer = unit_lines(render('ec-fan-textfile.timer.j2'))
        self.assertIn('OnUnitActiveSec=30', timer)
        self.assertIn('WantedBy=timers.target', timer)

    def test_dkms_conf(self):
        lines = unit_lines(render('dkms.conf.j2'))
        commit = role_context()['pve_ec_fan_commit']
        self.assertIn(commit, render('dkms.conf.j2'))
        for expected in ('PACKAGE_NAME="ec_su_axb35"', f'PACKAGE_VERSION="1.0+{commit[:7]}"',
                         'BUILT_MODULE_NAME[0]="ec_su_axb35"', 'DEST_MODULE_LOCATION[0]="/updates"',
                         'AUTOINSTALL="yes"', 'MAKE[0]="make KERNEL_BUILD=${kernel_source_dir} modules"',
                         'CLEAN="make KERNEL_BUILD=${kernel_source_dir} clean"'):
            self.assertIn(expected, lines)

    def test_all_three_nodes_turn_it_on_with_the_default_curve(self):
        # Secure Boot is off on all three nodes (node3 since 2026-11-19); each runs the role defaults' curve
        # (no override), so a host_vars curve or fan list here would be a deliberate, reviewed exception.
        host_vars = {h: yaml.safe_load((ROOT / f'ansible/host_vars/{h}.yml').read_text()) or {}
                     for h in ('ai-node1', 'ai-node2', 'ai-node3')}
        for h in ('ai-node1', 'ai-node2', 'ai-node3'):
            self.assertIs(host_vars[h].get('pve_ec_fan_enabled'), True, h)
        for h in ('ai-node1', 'ai-node2', 'ai-node3'):
            self.assertFalse([k for k in host_vars[h] if k.startswith('pve_ec_fan_') and k != 'pve_ec_fan_enabled'])

    def test_site_runs_the_role_ungated_so_switching_it_off_takes_it_down(self):
        play = yaml.safe_load((ROOT / 'ansible/site.yml').read_text())
        entry = [r for p in play for r in p.get('roles', []) if isinstance(r, dict) and r.get('role') == 'pve_ec_fan']
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0].get('tags'), ['ec_fan'])
        self.assertNotIn('when', entry[0])


if __name__ == '__main__':
    unittest.main()
