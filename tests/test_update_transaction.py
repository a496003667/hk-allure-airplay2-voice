"""Run the real updater against a private fake runtime and staged files."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

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

    def test_wrong_mode_aborts_before_mutation(self):
        self.staged.chmod(0o755)
        self.assertNotEqual(self.run_update().returncode, 0)
        self.assertEqual(self.target.read_text(), 'old')

    def test_candidate_failure_restores_all_and_reports_failure(self):
        self.staged.write_text('bad')
        self.write_manifest()
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.target.read_text(), 'old')
        self.assertFalse((self.base / '.updates/pending').exists())
        self.assertIn('rolled-back', next((self.base / '.updates').glob('*/state')).read_text())

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
