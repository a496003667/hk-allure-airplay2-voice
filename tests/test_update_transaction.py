"""Run the real updater against a private fake runtime and staged files."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import shutil
import signal

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / 'airplay2/hk-ap2-replace.sh'


class TransactionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hk-update-')
        self.root = Path(self.temp.name).resolve()
        self.base = self.root / 'vendor/airplay2'
        self.base.mkdir(parents=True)
        self.target = self.base / 'settings'
        self.target.write_text('old')
        self.staged = self.base / 'settings.new'
        self.staged.write_text('candidate')
        self.staged.chmod(0o644)
        self.manifest = self.root / 'manifest'
        self.write_manifest()
        (self.root / 'mounts').touch()
        ctl = self.base / 'hk-ap2-prodctl'
        ctl.write_text('''#!/bin/sh
printf '%s\n' "$1" >>"$FIXTURE_ROOT/calls"
case "$1" in
 verify)
    test ! -f "$FIXTURE_ROOT/fail-restore" || exit 1
    test "$(cat "$HK_AP2_BASE/settings")" != bad || exit 1
    ;;
esac
exit 0
''')
        ctl.chmod(0o755)
        hook = self.base / 'hk-ap2-audio-hook.sh'
        hook.write_text('#!/bin/sh\necho mute >>"$FIXTURE_ROOT/calls"\n')
        hook.chmod(0o755)
        (self.root / 'bin').mkdir()
        sleeper = self.root / 'bin/sleep'
        sleeper.write_text('#!/bin/sh\nexit 0\n')
        sleeper.chmod(0o755)
        self.env = os.environ | {
            'FIXTURE_ROOT': str(self.root), 'HK_AP2_BASE': str(self.base),
            'HK_AP2_RUNDIR': str(self.root / 'runtime'),
            'HK_AP2_MOUNTS': str(self.root / 'mounts'),
            'HK_AP2_HOLD_MUTE_FILE': str(self.root / 'hold'),
            'HK_AP2_UPDATE_LOCK': str(self.root / 'lock'),
            'HK_AP2_UPDATE_LOG': str(self.root / 'update.log'),
            'HK_AP2_UPDATE_WAIT_SECONDS': '2',
            'PATH': str(self.root / 'bin') + ':' + os.environ['PATH'],
        }

    def tearDown(self):
        self.temp.cleanup()

    def write_manifest(self):
        self.manifest.write_text(f'{hashlib.sha256(self.staged.read_bytes()).hexdigest()} 644 {self.target}\n')

    def run_update(self, *args):
        return subprocess.run(['sh', str(UPDATER), *(args or ('--manifest', str(self.manifest)))],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def test_commit_requires_verify_and_retains_hold_and_backup(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_text(), 'candidate')
        self.assertTrue((self.root / 'hold').exists())
        tx = next((self.base / '.updates').glob('*/state'))
        self.assertEqual(tx.read_text().strip(), 'committed')
        self.assertEqual((tx.parent / '1.bak').read_text(), 'old')
        self.assertIn('verify', (self.root / 'calls').read_text())
        self.assertFalse((self.root / 'lock').exists())

    def test_bad_hash_aborts_before_mute_or_stop(self):
        self.staged.write_text('tampered')
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_text(), 'old')
        self.assertFalse((self.root / 'calls').exists())

    def test_low_memory_update_releases_staging_only_after_install_before_start(self):
        self.env['HK_AP2_UPDATE_RELEASE_STAGING'] = '1'
        ctl = self.base / 'hk-ap2-prodctl'
        ctl.write_text('''#!/bin/sh
case "$1" in
 start) [ ! -e "$HK_AP2_BASE/settings.new" ] || exit 1;;
esac
exit 0
''')
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_text(), 'candidate')
        self.assertFalse(self.staged.exists())
        tx = next((self.base / '.updates').glob('*/state'))
        self.assertEqual(tx.read_text().strip(), 'committed')
        self.assertEqual((tx.parent / '1.bak').read_text(), 'old')

    def test_wrong_mode_aborts_before_mutation(self):
        self.staged.chmod(0o755)
        self.assertNotEqual(self.run_update().returncode, 0)
        self.assertEqual(self.target.read_text(), 'old')

    def test_update_and_rollback_work_without_stat_applet(self):
        unavailable = self.root / 'bin/stat'
        unavailable.write_text('#!/bin/sh\nexit 127\n')
        unavailable.chmod(0o755)
        self.target.chmod(0o2750)
        self.staged.write_text('bad')
        self.write_manifest()
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_text(), 'old')
        self.assertEqual(self.target.stat().st_mode & 0o7777, 0o2750)
        self.assertIn('rolled-back', next((self.base / '.updates').glob('*/state')).read_text())

    def test_invalid_permission_listing_aborts_before_mute(self):
        listing = self.root / 'bin/ls'
        listing.write_text('#!/bin/sh\necho "unreadable"\n')
        listing.chmod(0o755)
        self.assertNotEqual(self.run_update().returncode, 0)
        self.assertEqual(self.target.read_text(), 'old')
        self.assertFalse((self.root / 'calls').exists())

    def test_candidate_failure_restores_all_and_reports_failure(self):
        self.staged.write_text('bad')
        self.write_manifest()
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_text(), 'old')
        self.assertFalse((self.base / '.updates/pending').exists())
        self.assertIn('rolled-back', next((self.base / '.updates').glob('*/state')).read_text())

    def test_failed_start_can_recover_after_removing_runtime_directory(self):
        ctl = self.base / 'hk-ap2-prodctl'
        ctl.write_text('''#!/bin/sh
case "$1" in
 start)
    if [ "$(cat "$HK_AP2_BASE/settings")" = bad ]; then
        rm -rf "$HK_AP2_RUNDIR/run"
        exit 1
    fi
    ;;
esac
exit 0
''')
        hook = self.base / 'hk-ap2-audio-hook.sh'
        hook.write_text('#!/bin/sh\n[ -d "$HK_AP2_RUNDIR/run" ] || exit 1\n')
        self.staged.write_text('bad')
        self.write_manifest()
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_text(), 'old')
        self.assertFalse((self.base / '.updates/pending').exists())
        self.assertIn('rolled-back', next((self.base / '.updates').glob('*/state')).read_text())

    @unittest.skipUnless(Path('/proc/self/cmdline').exists(), 'Linux process identities required')
    def test_legacy_rollback_requires_exact_bindings_live_processes_and_heartbeat(self):
        runtime = self.root / 'runtime'
        run = runtime / 'run'
        safe = run / 'safe'
        safe.mkdir(parents=True)
        (safe / 'armed').touch()
        pairs = [
            ('ap2-56/shairport-sync', 'bin/shairport-sync'),
            ('ap2-56/shairport-sync.conf', 'etc/ap2/shairport-sync.conf'),
            ('hk-ap2-audio-hook.sh', 'hk-ap2-audio-hook.sh'),
            ('hk-ap2-safe-test.bt', 'bin/hk-ap2-safe-test'),
            ('hk-ap2ctl.ap256', 'bin/hk-ap2ctl'),
        ]
        tx = self.root / 'legacy-tx'
        tx.mkdir()
        restore = []
        mounts = []
        for index, (original, active) in enumerate(pairs, 1):
            target = self.base / original
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('#!/bin/sh\nwhile :; do sleep 1; done\n')
            target.chmod(0o755)
            path = runtime / 'opt' / active
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, path)
            restore.append(f'{index} {hashlib.sha256(target.read_bytes()).hexdigest()} 755 {target}\n')
            mounts.append(f'/fixture {path} none bind 0 0\n')
        (tx / 'restore.manifest').write_text(''.join(restore))
        (self.root / 'mounts').write_text(''.join(mounts))
        ctl = self.base / 'hk-ap2-prodctl'
        ctl.write_text('''#!/bin/sh
case "$1" in
 verify) exit 2;;
 status)
    printf '%s\n' 'supervisor: running pid=42' 'watchdog: running pid=43' \
        'AP2 mode: real' 'armed: yes' 'airplay port 7000: listening'
    ;;
esac
''')
        processes = []
        source = UPDATER.read_text().split('\nmain "$@"', 1)[0]
        def check(action='ap2_up restored'):
            script = source + f'\nTX="{tx}"\n{action}\n'
            return subprocess.run(['sh', '-c', script], env=self.env,
                                  capture_output=True, text=True, timeout=5)
        try:
            for relative, pidfile in [('bin/shairport-sync', run / 'shairport.pid'),
                                      ('bin/hk-ap2-safe-test', safe / 'watchdog.pid')]:
                process = subprocess.Popen(['sh', str(runtime / 'opt' / relative)], start_new_session=True)
                processes.append(process)
                pidfile.write_text(str(process.pid))
            (safe / 'watchdog.heartbeat').write_text(Path('/proc/uptime').read_text().split('.')[0])
            self.assertEqual(check().returncode, 0)
            self.assertNotEqual(check('ap2_up').returncode, 0, 'a new candidate must provide its own verify verb')
            active = runtime / 'opt/etc/ap2/shairport-sync.conf'
            active.write_text('wrong active configuration')
            self.assertNotEqual(check().returncode, 0)
            shutil.copyfile(self.base / 'ap2-56/shairport-sync.conf', active)
            (self.root / 'mounts').write_text(''.join(mounts[1:]))
            self.assertNotEqual(check().returncode, 0)
            (self.root / 'mounts').write_text(''.join(mounts))
            (safe / 'watchdog.heartbeat').write_text('0')
            self.assertNotEqual(check().returncode, 0)
        finally:
            for process in processes:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                process.wait(3)

    def test_rollback_failure_leaves_recoverable_journal_and_mute(self):
        (self.root / 'fail-restore').touch()
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        pending = self.base / '.updates/pending'
        self.assertTrue(pending.exists())
        self.assertTrue((self.root / 'hold').exists())
        (self.root / 'fail-restore').unlink()
        result = self.run_update('--recover', pending.read_text().strip())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.read_text(), 'old')
        self.assertFalse(pending.exists())

    def test_lock_and_duplicate_targets_are_rejected(self):
        (self.root / 'lock').mkdir()
        self.assertNotEqual(self.run_update().returncode, 0)
        (self.root / 'lock').rmdir()
        self.manifest.write_text(self.manifest.read_text() * 2)
        self.assertNotEqual(self.run_update().returncode, 0)
        self.assertEqual(self.target.read_text(), 'old')

    def test_symlink_target_is_rejected(self):
        outside = self.root / 'outside'
        outside.write_text('untouched')
        self.target.unlink()
        self.target.symlink_to(outside)
        self.assertNotEqual(self.run_update().returncode, 0)
        self.assertEqual(outside.read_text(), 'untouched')

    def test_insufficient_rollback_space_aborts_before_mute(self):
        df = self.root / 'bin/df'
        df.write_text('#!/bin/sh\necho "Filesystem 1024-blocks Used Available Capacity Mounted"\necho "fixture 1000 999 1 99% /"\n')
        df.chmod(0o755)
        self.assertNotEqual(self.run_update().returncode, 0)
        self.assertEqual(self.target.read_text(), 'old')
        self.assertFalse((self.root / 'calls').exists())

    def test_old_supervisor_stops_before_dependent_reload(self):
        helper=self.base/'hk-audio-dependents.sh'
        helper_new=Path(str(helper)+'.new')
        helper_new.write_text('#!/bin/sh\necho "dep-$1" >>"$FIXTURE_ROOT/calls"\n')
        helper_new.chmod(0o755)
        voice=self.base.parent/'hk-voice/hk-voice.sh'
        voice.parent.mkdir()
        voice_new=Path(str(voice)+'.new')
        voice_new.write_text('#!/bin/sh\nexit 0\n');voice_new.chmod(0o755)
        extra=''
        for target,staged in [(helper,helper_new),(voice,voice_new)]:
            extra+=f'{hashlib.sha256(staged.read_bytes()).hexdigest()} 755 {target}\n'
        self.manifest.write_text(self.manifest.read_text()+extra)
        result=self.run_update()
        self.assertEqual(result.returncode,0,result.stderr)
        calls=(self.root/'calls').read_text().splitlines()
        self.assertLess(calls.index('stop'),calls.index('dep-stop'))
        self.assertLess(calls.index('dep-stop'),calls.index('start'))
        self.assertLess(calls.index('dep-start'),calls.index('dep-verify'))

    def test_backup_failure_does_not_stop_live_services(self):
        cp = self.root / 'bin/cp'
        cp.write_text('''#!/bin/sh
case "$*" in *.bak*) exit 1;; esac
exec /bin/cp "$@"
''')
        cp.chmod(0o755)
        self.assertNotEqual(self.run_update().returncode, 0)
        self.assertEqual(self.target.read_text(), 'old')
        self.assertFalse((self.root / 'calls').exists())


if __name__ == '__main__':
    unittest.main()
