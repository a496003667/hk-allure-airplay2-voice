#!/usr/bin/env python3
"""Regression checks for the on-device Alexa Bluetooth hand-off service."""
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
VOICE = ROOT / "voice/hk-voice.sh"


class VoiceServiceTest(unittest.TestCase):
    def test_voice_loop_stays_in_positive_lmk_bucket(self):
        script = VOICE.read_text()
        default = re.search(
            r"^VOICE_OOM_SCORE_ADJ=\$\{HK_VOICE_OOM_SCORE_ADJ:-([0-9]+)\}$",
            script,
            re.M,
        )
        self.assertIsNotNone(default)
        self.assertEqual(default.group(1), "5")
        self.assertIn('echo "$VOICE_OOM_SCORE_ADJ" >/proc/self/oom_score_adj', script)
        self.assertNotIn("echo 15 >/proc/self/oom_score_adj", script)

    def test_all_voice_speaker_paths_use_the_shared_pop_free_guard(self):
        script = VOICE.read_text()
        self.assertIn('"$HOOK" guard', script)
        self.assertIn('env HK_AP2_BT=1 $HOOK_ENV "$HOOK" guard active', script)
        self.assertNotRegex(script, r'sset "Ext Spk" on|sset Ext Spk on')
        opens = [line for line in script.splitlines()
                 if '"$HOOK" start' in line and 'guard' not in line]
        self.assertEqual(len(opens), 2)
        self.assertTrue(all('env $HOOK_ENV' in line for line in opens))
        self.assertIn('wait_for_pcm_clock',
                      (VOICE.parents[1] / 'airplay2/hk-ap2-audio-hook.sh').read_text())

        prepare = script.split("bt_inject_prepare_voice() {", 1)[1].split(
            "\nbt_inject_end() {", 1
        )[0]
        self.assertLess(prepare.index('speaker_guard'), prepare.index('mx_cset 1 0,0'))
        self.assertLess(prepare.rindex('speaker_guard'),
                        prepare.index('mx_cset 1 "$saved_digital"'))
        end = script.split("bt_inject_end() {", 1)[1].split(
            "\n# Runs in the background", 1
        )[0]
        self.assertLess(end.index('mx_cset 1 0,0'), end.index('speaker_guard'))
        self.assertLess(end.index('speaker_guard'),
                        end.index('mx_cset 1 "$saved_digital"'))

    def test_supervisor_stays_in_positive_lmk_bucket(self):
        script = VOICE.read_text()
        self.assertIn(
            "VOICE_SUPERVISOR_OOM_SCORE_ADJ=${HK_VOICE_SUPERVISOR_OOM_SCORE_ADJ:-5}",
            script,
        )
        supervise = script.split("    supervise)\n", 1)[1].split("    start)\n", 1)[0]
        self.assertIn(
            'echo "$VOICE_SUPERVISOR_OOM_SCORE_ADJ" >/proc/self/oom_score_adj',
            supervise,
        )

    def test_start_reaps_a_stale_fifo_reader(self):
        script = VOICE.read_text()
        start = script.split("    start)\n", 1)[1].split("    stop)\n", 1)[0]
        self.assertIn("/[h]k-voice\\.sh start$/", start)
        self.assertIn('kill "$p" 2>/dev/null || true', start)
        self.assertLess(start.index("[h]k-voice\\.sh start$"), start.index("loop </dev/null"))

    def test_bluetooth_is_released_before_listening_and_resumed_at_the_end(self):
        script = VOICE.read_text()
        wake = script.split("handle_wake() {", 1)[1].split("\nloop() {", 1)[0]
        self.assertLess(wake.index("release_bluetooth"), wake.index("run_round"))
        self.assertLess(wake.index("run_round"), wake.rindex("resume_bluetooth"))

    def test_bluetooth_feeder_cleanup_never_waits_forever(self):
        script = VOICE.read_text()
        cleanup = script.split("bt_inject_stop_feeder() {", 1)[1].split(
            "\nbt_inject_wait_done() {", 1
        )[0]
        self.assertIn('kill -KILL "$feeder_pid"', cleanup)
        self.assertNotIn('wait "$BT_INJECT_PID"', cleanup)

    def test_new_bluetooth_feeder_consumes_the_reply(self):
        script = VOICE.read_text()
        functions = script.split("bt_inject_stop_feeder() {", 1)[1].split(
            "\nbt_inject_begin() {", 1
        )[0]
        functions = "bt_inject_stop_feeder() {" + functions
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inject, queue = root / "inject", root / "queue"
            inject.mkdir()
            queue.mkdir()
            (inject / "active").touch()
            (queue / "q.1.raw").write_bytes(b"reply")
            (queue / "q.end").touch()
            shell = functions + '''
usleep() { sleep 0.05; }
bt_inject_play_file() { mv "$1" "$BT_INJECT/consumed"; }
BT_INJECT_PID=
bt_inject_stop_feeder
test ! -f "$BT_INJECT/stop" || exit 1
rm -f "$BT_INJECT/stop"
bt_inject_feeder &
feeder=$!
i=0
while [ ! -f "$QD/played" ] && [ "$i" -lt 100 ]; do
    sleep 0.05
    i=$((i + 1))
done
: >"$BT_INJECT/stop"
wait "$feeder"
test -f "$BT_INJECT/consumed" && test -f "$QD/played"
'''
            result = subprocess.run(
                ["sh", "-c", shell],
                env={"BT_INJECT": str(inject), "QD": str(queue), "PATH": "/usr/bin:/bin"},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((inject / "consumed").read_bytes(), b"reply")

    def test_bluetooth_voice_teardown_never_raises_master_or_unmutes_a_trip(self):
        script = VOICE.read_text()
        body = script.split("bt_inject_end() {", 1)[1].split("\n# Runs in the background", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inject, safe = root / "inject", root / "safe"
            inject.mkdir()
            safe.mkdir()
            (inject / "meta").write_text("5 207,207\n")
            (safe / "armed").touch()
            (safe / "play.started").write_text("1\n")
            (safe / "tripped").touch()
            shell = '''
bt_inject_stop_feeder() { :; }
mx_cset() { printf '%s %s\\n' "$1" "$2" >>"$TEST_LOG"; }
mx() { printf 'master %s\\n' "$*" >>"$TEST_LOG"; }
usleep() { :; }
bt_stream_live() { return 0; }
log() { :; }
bt_inject_end() {
''' + body + '''
bt_inject_end
'''
            result = subprocess.run(
                ["sh", "-c", shell],
                env={"BT_INJECT": str(inject), "SAFE": str(safe),
                     "TEST_LOG": str(root / "calls"), "PATH": "/usr/bin:/bin"},
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "calls").read_text(), "1 0,0\n")

    def test_supervisor_recovers_stuck_round_and_pre_roll_growth(self):
        script = VOICE.read_text()
        self.assertIn("supervisor_health()", script)
        self.assertIn('voice prepare hook stuck', script)
        self.assertIn('voice pre-roll stuck', script)
        supervise = script.split("    supervise)\n", 1)[1].split("    start)\n", 1)[0]
        self.assertIn("supervisor_health", supervise)

    def test_recording_pauses_while_waiting_for_the_reply(self):
        script = VOICE.read_text()
        round_body = script.split("run_round() {", 1)[1].split("\ndo_action() {", 1)[0]
        self.assertIn("rec_pause_and_drop", round_body)
        self.assertIn("freshed=paused", round_body)
        self.assertLess(round_body.index("rec_pause_and_drop"), round_body.index("rec_fresh"))
        self.assertIn("round timed out after 60s", round_body)

    def test_stale_prepare_after_a_valid_output_does_not_restart_the_round(self):
        script = VOICE.read_text()
        health = script.split("supervisor_health() {", 1)[1].split("\ncase ", 1)[0]
        self.assertIn('[ "$prepared" = ok ] && limit=3', health)
        self.assertIn('if [ "$prepared" != ok ]; then', health)


if __name__ == "__main__":
    unittest.main()
