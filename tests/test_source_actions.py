"""Run source-aware command dispatch and queue interruption without audio."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
VOICE=(ROOT/'voice/hk-voice.sh').read_text()


class SourceActionsTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='hk-actions-')
        self.root=Path(self.temp.name)
        self.log=self.root/'calls';self.log.touch()
        self.env=os.environ|{'ACTION':str(self.root/'action'),'BT_RESUME':str(self.root/'resume'),
            'BT_RELEASE':str(self.root/'release'),'BT_DUCKED':'1','PAUSED':'0','BT_PAUSED':'0',
            'PLAYER_SOCKET':'fake','HK_VOICE_SOURCE':'bluetooth','EVENTS':str(self.log),
            'STATUSFILE':str(self.root/'status'),'REQ':str(self.root/'request'),
            'MQTT':str(self.root/'mqtt')}
        (self.root/'status').write_text('play')
        self.script('request','''#!/bin/sh
case "$2" in
 getPlayerStatus) printf '{"mode":"41","status":"%s","vol":"20"}' "$(cat "$STATUSFILE")";;
 setPlayerCmd:onepause) echo pause >>"$EVENTS"; echo stop >"$STATUSFILE";;
 *) printf '%s\n' "$2" >>"$EVENTS";;
esac
''')
        self.script('mqtt','#!/bin/sh\necho "mqtt $*" >>"$EVENTS"\n')

    def tearDown(self): self.temp.cleanup()

    def script(self,name,text):
        p=self.root/name;p.write_text(text);p.chmod(0o755)

    def action(self,name):
        (self.root/'action').write_text(name)
        function='player_value() {'+VOICE.split('player_value() {',1)[1].split('\n# Pre-roll:',1)[0]
        shell='log() { :; }; audio_delay() { :; };\n'+function+'\ndo_action\nprintf "%s %s\\n" "$BT_PAUSED" "$PAUSED"\n'
        result=subprocess.run(['sh','-c',shell],env=self.env,capture_output=True,text=True,timeout=4)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout.strip()

    def test_bluetooth_next_prev_do_not_target_airplay(self):
        self.action('next');self.action('prev')
        self.assertEqual(self.log.read_text().splitlines(),['setPlayerCmd:next','setPlayerCmd:prev'])

    def test_bluetooth_pause_is_real_and_cancels_automatic_resume(self):
        (self.root/'resume').touch()
        self.assertEqual(self.action('pause'),'0 0')
        self.assertEqual(self.log.read_text().strip(),'pause')
        self.assertFalse((self.root/'resume').exists())
        self.assertEqual((self.root/'status').read_text().strip(),'stop')

    def test_paused_bluetooth_is_not_toggled_back_to_play(self):
        (self.root/'status').write_text('stop')
        self.action('pause')
        self.assertEqual(self.log.read_text(),'')

    def test_airplay_action_still_uses_existing_remote(self):
        self.env.update(HK_VOICE_SOURCE='airplay',BT_DUCKED='0')
        self.action('next')
        self.assertEqual(self.log.read_text().strip(),'mqtt hk-ap2/remote nextitem')

    def test_completed_round_queue_is_removed_before_next_generation(self):
        queue=self.root/'queues';queue.mkdir()
        marker=self.root/'current'
        functions='voice_round_current() {'+VOICE.split('voice_round_current() {',1)[1].split('\nrun_round()',1)[0]
        shell='. "$HK_AUDIO_COMMON"; audio_now() { echo 100; };\n'+functions+'''
new_round_queue || exit 1
old=$QD
printf old >"$QD/q.1.raw"
new_round_queue || exit 1
test ! -d "$old" || exit 2
test -d "$QD" || exit 3
test "$(cat "$VOICE_CURRENT")" = "$HK_AUDIO_TOKEN" || exit 4
'''
        env=self.env|{'QUEUE_ROOT':str(queue),'QD':str(queue),'RUN':str(self.root),
            'VOICE_CURRENT':str(marker),'PIDFILE':str(self.root/'absent-pid'),
            'HK_AUDIO_COMMON':str(ROOT/'airplay2/hk-audio-common.sh'),
            'HK_AUDIO_UPTIME_FILE':str(self.root/'clock')}
        (self.root/'clock').write_text('100.00 0.00\n')
        result=subprocess.run(['sh','-c',shell],env=env,capture_output=True,text=True,timeout=4)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_voice_respects_a_live_structured_airplay_claim(self):
        claim=self.root/'ap2-claim'
        function='ap2_claimed() {'+VOICE.split('ap2_claimed() {',1)[1].split('\n# Drop voice-owns',1)[0]
        env=self.env|{'HK_AUDIO_COMMON':str(ROOT/'airplay2/hk-audio-common.sh'),
                      'HK_AUDIO_UPTIME_FILE':str(self.root/'clock'),
                      'AP2_CLAIM':str(claim)}
        (self.root/'clock').write_text('100.00 0.00\n')
        start=subprocess.check_output(
            ['sh','-c','. "$HK_AUDIO_COMMON"; audio_pid_start "$1"','sh',str(os.getpid())],
            env=env,text=True).strip()
        claim.write_text(f'100 {os.getpid()} {start} claim-1\n')
        result=subprocess.run(['sh','-c','. "$HK_AUDIO_COMMON"; '+function+'\nap2_claimed'],
                              env=env,capture_output=True,text=True,timeout=3)
        self.assertEqual(result.returncode,0,result.stderr)

        claim.write_text('100 999999999 1 dead-claim\n')
        result=subprocess.run(['sh','-c','. "$HK_AUDIO_COMMON"; '+function+'\nap2_claimed'],
                              env=env,capture_output=True,text=True,timeout=3)
        self.assertNotEqual(result.returncode,0)

    def test_abort_has_priority_over_queued_bluetooth_reply(self):
        queue=self.root/'queue';queue.mkdir()
        inject=self.root/'inject';inject.mkdir();(inject/'active').touch()
        (queue/'q.abort').touch();(queue/'q.1.raw').write_bytes(b'old reply')
        feeder='bt_inject_feeder() {'+VOICE.split('bt_inject_feeder() {',1)[1].split('\nbt_inject_begin()',1)[0]
        shell='bt_inject_play_file() { echo played >>"$EVENTS"; };\n'+feeder+'\nbt_inject_feeder\n'
        result=subprocess.run(['sh','-c',shell],env=self.env|{'QD':str(queue),'BT_INJECT':str(inject)},
                              capture_output=True,text=True,timeout=3)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.log.read_text(),'')
        self.assertTrue((inject/'abort').exists())
        self.assertTrue((queue/'aborted').exists())
        self.assertFalse((queue/'q.1.raw').exists())

    def run_release_airplay(self, replacement=False):
        source=(ROOT/'voice/hk-voice.sh').read_text()
        function='release_airplay() {'+source.split('release_airplay() {',1)[1].split(
            '\n# Bluetooth also holds',1)[0]
        lease=self.root/'lease'
        lease.write_text('airplay 42 1 token123 100 active\n')
        calls=self.root/'hook-calls'
        self.script('release-hook',f'''#!/bin/sh
printf '%s %s\\n' "$HK_AUDIO_TOKEN" "$HK_AUDIO_LEASE_TIME" >>"{calls}"
if [ "${{REPLACE_LEASE:-0}}" = 1 ]; then
  printf 'airplay 42 1 token123 101 active\\n' >"{lease}"
else
  rm -f "{lease}"
fi
''')
        self.script('release-mqtt',f'#!/bin/sh\necho "$*" >>"{self.log}"\n')
        shell=function+'''
log() { :; }
airplay_owns_dac() { return 0; }
release_airplay
rc=$?
printf '%s\\n' "$PAUSED"
exit "$rc"
'''
        env=self.env|{
            'HK_AUDIO_COMMON':str(ROOT/'airplay2/hk-audio-common.sh'),
            'HK_AP2_RUN':str(self.root/'run'),
            'LEASE_FILE':str(lease),
            'HOOK':str(self.root/'release-hook'),
            'HOOK_ENV':'',
            'MQTT':str(self.root/'release-mqtt'),
            'RELEASE':str(self.root/'release-dac'),
            'PAUSED':'0',
            'RELEASED':'0',
            'BT_OWNER':str(self.root/'bt-owner.pid'),
            'AP2_CLAIM':str(self.root/'ap2-claim'),
            'REPLACE_LEASE':'1' if replacement else '0',
        }
        prefix='''. "$HK_AUDIO_COMMON"
audio_read_lease() {
[ -r "$LEASE_FILE" ] || return 1
IFS=' ' read -r lease_source lease_pid lease_start lease_token lease_time lease_phase <"$LEASE_FILE"
[ -n "${lease_token:-}" ]
}
'''
        result=subprocess.run(['sh','-c',prefix+shell],env=env,
                              capture_output=True,text=True,timeout=4)
        return result,calls,lease

    def test_voice_airplay_release_uses_the_exact_observed_generation(self):
        result,calls,_=self.run_release_airplay()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(calls.read_text().strip(),'token123 100')
        self.assertEqual(result.stdout.strip(),'1')
        self.assertTrue((self.root/'release-dac').exists())
        self.assertEqual(self.log.read_text().strip(),'hk-ap2/remote pause')

    def test_replacement_airplay_generation_survives_an_old_voice_release(self):
        result,calls,lease=self.run_release_airplay(replacement=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(calls.read_text().strip(),'token123 100')
        self.assertEqual(lease.read_text().split()[4],'101')
        self.assertEqual(result.stdout.strip(),'0')
        self.assertFalse((self.root/'release-dac').exists())
        self.assertEqual(self.log.read_text(),'')


if __name__=='__main__': unittest.main()
