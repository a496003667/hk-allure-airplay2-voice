#!/usr/bin/env python3
"""End-to-end multi-process cross-alternation tests for AirPlay, Bluetooth, and Alexa."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
COMMON = REPO / 'airplay2/hk-audio-common.sh'
HOOK = REPO / 'airplay2/hk-ap2-audio-hook.sh'
BRIDGE = REPO / 'airplay2/hk-bt-bridge.sh'
WATCHDOG = REPO / 'airplay2/runtime/hk-ap2-safe-test'
GUARD = REPO / 'airplay2/runtime/hk-ap2-guard.sh'
VOICE = REPO / 'voice/hk-voice.sh'
LIGHTS = REPO / 'lights/hk-ap2-lights.sh'


def eventually(predicate, timeout=12.0, step=0.05, msg='condition timed out'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(step)
    raise AssertionError(msg)


def _alive(pid):
    try:
        os.kill(pid, 0)
        stat = Path(f'/proc/{pid}/stat')
        if stat.exists() and stat.read_text().split()[2] == 'Z':
            return False
        return True
    except ProcessLookupError:
        return False


class CrossSourceAlternationTest(unittest.TestCase):
    """Real shell daemon harness exercising AirPlay <-> Bluetooth <-> Alexa."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hk-cross-')
        self.base = Path(self.tmp.name)
        self.opt = self.base / 'opt'
        self.run = self.base / 'run'
        self.safe = self.run / 'safe'
        self.proc = self.base / 'proc'
        self.voice_dir = self.base / 'vendor-voice'
        self.voice_run = self.base / 'voice-run'
        for d in (
            self.opt / 'bin',
            self.opt / 'lib',
            self.safe,
            self.proc,
            self.voice_dir / 'clips',
            self.voice_run,
        ):
            d.mkdir(parents=True, exist_ok=True)

        for name in ('libhk-airplay-mute.so', 'libhk-airplay-unmute.so'):
            (self.opt / 'lib' / name).write_bytes(b'preload')
        (self.voice_dir / 'clips/neterr.wav').write_bytes(b'clip')

        self.clock = self.base / 'clock'
        self.tick = 100
        self.clock.write_text(f'{self.tick}.00 0.00\n')
        self._clock_stop = threading.Event()
        self._clock_lock = threading.Lock()

        self.mixer_file = self.base / 'mixer.json'
        self.mixer_file.write_text(json.dumps({
            'Master': '0',
            '1': '0,0',
            '2': '1,1',
            'Digital': 'off',
            'Auto Mute': 'on',
            'Ext Spk': 'off',
            'aml audio i2s mute': 'on',
        }))
        self.pa_file = self.base / 'pa.json'
        self.pa_file.write_text(json.dumps({'pa': 1, 'force': 0, 'gate': 1}))
        self.phone_file = self.base / 'phone.json'
        self.phone_file.write_text(json.dumps({
            'mode': '41', 'status': 'stop', 'vol': '25', 'mute': '0',
        }))
        self.pcm_file = self.base / 'pcm-status'
        self.pcm_file.write_text('closed\n')
        self.activity_file = self.base / 'activity'
        self.events_file = self.base / 'events.log'
        self.events_file.touch()

        self.libmvmsg = self.base / 'libmvmsg.so'
        self.libmvmsg.write_bytes(b'verified-stock-libmvmsg')
        import hashlib
        lib_sha = hashlib.sha256(self.libmvmsg.read_bytes()).hexdigest()

        self.children = []
        self.sockets = []
        for sock_name in ('player.sock', 'ioguard.sock', 'bt-pcm-control'):
            s = socket.socket(socket.AF_UNIX)
            p = self.base / sock_name
            s.bind(str(p))
            self.sockets.append(s)

        # Dummy long-lived processes representing shairport, dsp-bridge, and a01controller
        fake_sps = self.base / 'shairport-sync'
        fake_sps.symlink_to('/bin/sleep')
        self.shairport_proc = self._spawn_bg([str(fake_sps), '120'])
        self.dsp_proc = self._spawn_bg(['sleep', '120'])
        self.bt_ctrl_proc = self._spawn_bg(['sleep', '120'])

        (self.run / 'shairport.pid').write_text(f'{self.shairport_proc.pid}\n')
        (self.run / 'dsp-bridge.pid').write_text(f'{self.dsp_proc.pid}\n')
        self._register_proc(self.shairport_proc.pid, 'shairport-sync', '/tmp/hk-ap2/opt/bin/shairport-sync')
        self._register_proc(self.dsp_proc.pid, 'hk-ap2-dsp-bri', '/vendor/airplay2/hk-ap2-dsp-bridge')
        self._register_proc(self.bt_ctrl_proc.pid, 'a01controller', '/system/workdir/bin/a01controller', with_pcm_fd=True)

        self.set_bt_activity(frames=100, last_sec=0, instance=5001)
        self._build_mocks()

        self.env = os.environ | {
            'PATH': f"{self.base / 'mockbin'}:{os.environ['PATH']}",
            'HK_AUDIO_COMMON': str(COMMON),
            'HK_AUDIO_UPTIME_FILE': str(self.clock),
            'HK_AP2_ROOT': str(self.opt),
            'HK_AP2_RUN': str(self.run),
            'HK_AP2_PROC_ROOT': str(self.proc),
            'HK_BT_PROC_ROOT': str(self.proc),
            'HK_AP2_PCM_STATUS': str(self.pcm_file),
            'HK_BT_PCM_STATUS': str(self.pcm_file),
            'HK_AP2_HOLD_MUTE_FILE': str(self.base / 'hold-mute'),
            'HK_AP2_AMIXER': str(self.base / 'mockbin/amixer'),
            'HK_AP2_APPLE_REMOTE': str(self.base / 'mockbin/apple_remote'),
            'HK_AP2_APPLE_REMOTE_SOCKET': str(self.base / 'apple_remote.sock'),
            'HK_AP2_STOCK_AUDIO_STATE': str(self.base / 'mockbin/stock-audio-state'),
            'HK_AP2_STOCK_AUDIO_LIB': str(self.libmvmsg),
            'HK_AP2_STOCK_AUDIO_LIB_SHA256': lib_sha,
            'HK_AP2_REQUEST': str(self.opt / 'bin/hk-unix-request-raw'),
            'HK_BT_PLAYER_SOCKET': str(self.base / 'player.sock'),
            'HK_AP2_IOGUARD_SOCKET': str(self.base / 'ioguard.sock'),
            'HK_BT_PCM_CONTROL': str(self.base / 'bt-pcm-control'),
            'HK_BT_PCM_OWNER': str(self.base / 'bt-pcm-owner'),
            'HK_BT_PCM_OPENING': str(self.base / 'bt-pcm-opening'),
            'HK_BT_COLD_GLOBAL': str(self.base / 'bt-cold-global-mute'),
            'HK_BT_PCM_DEVICE': '/dev/snd/pcmC0D0p',
            'HK_BT_MQTT': str(self.voice_dir / 'hk-mqtt-pub'),
            'HK_AP2_USE_STOCK_PA': '1',
            'HK_AP2_MASTER_SAFE': '4',
            'HK_AP2_DIGITAL_SAFE': '192',
            'HK_AP2_PLAYBACK_SAFE': '1',
            'HK_AP2_BT_DIGITAL_SAFE': '207',
            'HK_AP2_BT_PLAYBACK_SAFE': '0',
            'HK_AP2_MAX_PLAY_SECONDS': '0',
            'HK_AP2_PA_WARMUP_MS': '20',
            'HK_VOICE_RUN': str(self.voice_run),
            'MOCK_MIXER': str(self.mixer_file),
            'MOCK_PA': str(self.pa_file),
            'MOCK_PHONE': str(self.phone_file),
            'MOCK_PCM': str(self.pcm_file),
            'MOCK_ACTIVITY': str(self.activity_file),
            'MOCK_EVENTS': str(self.events_file),
            'MOCK_DSP_PID': str(self.dsp_proc.pid),
            'MOCK_BT_PID': str(self.bt_ctrl_proc.pid),
            'MOCK_PROC': str(self.proc),
        }

        (self.safe / 'armed').touch()
        self._clock_thread = threading.Thread(target=self._run_clock, daemon=True)
        self._clock_thread.start()

    def tearDown(self):
        self._clock_stop.set()
        self._clock_thread.join(timeout=2)
        (self.safe / 'armed').unlink(missing_ok=True)
        for p in reversed(self.children):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        for s in self.sockets:
            s.close()
        self.tmp.cleanup()

    def _run_clock(self):
        while not self._clock_stop.wait(0.95):
            with self._clock_lock:
                self.tick += 1
                tmp = self.clock.with_suffix('.tmp')
                tmp.write_text(f'{self.tick}.00 0.00\n')
                os.replace(tmp, self.clock)

    def now_tick(self):
        with self._clock_lock:
            return self.tick

    def _spawn_bg(self, cmd, env=None):
        p = subprocess.Popen(
            cmd,
            env=env or os.environ,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.children.append(p)
        return p

    def _register_proc(self, pid, comm, cmdline=None, with_pcm_fd=False):
        pdir = self.proc / str(pid)
        (pdir / 'fd').mkdir(parents=True, exist_ok=True)
        (pdir / 'task' / str(pid)).mkdir(parents=True, exist_ok=True)
        (pdir / 'comm').write_text(f'{comm}\n')
        (pdir / 'cmdline').write_bytes((cmdline or comm).encode() + b'\x00')
        if with_pcm_fd:
            fd_link = pdir / 'fd/7'
            if not fd_link.exists():
                fd_link.symlink_to('/dev/snd/pcmC0D0p')

    def publish_bt_owner_proof(self, generation=1):
        start = subprocess.check_output(
            ['sh', '-c', '. "$1"; audio_pid_start "$2"', 'sh', str(COMMON), str(self.bt_ctrl_proc.pid)],
            text=True,
        ).strip()
        content = f'{self.bt_ctrl_proc.pid} {start} 7 {generation}\n'
        tmp = self.base / 'bt-pcm-owner.tmp'
        tmp.write_text(content)
        os.replace(tmp, self.base / 'bt-pcm-owner')

    def set_bt_activity(self, frames, last_sec, instance=5001):
        tmp = self.activity_file.with_suffix('.tmp')
        tmp.write_text(f'activity {self.bt_ctrl_proc.pid} {instance} {frames} {last_sec}\n')
        os.replace(tmp, self.activity_file)

    def set_phone(self, status='stop', mode='41', vol='25', mute='0'):
        tmp = self.phone_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(
            {'mode': mode, 'status': status, 'vol': vol, 'mute': mute},
            separators=(',', ':'),
        ))
        os.replace(tmp, self.phone_file)

    def mixer_state(self):
        return json.loads(self.mixer_file.read_text())

    def pa_state(self):
        return json.loads(self.pa_file.read_text())

    def events(self):
        return self.events_file.read_text().splitlines()

    def _exec_script(self, path: Path, body: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o755)

    def _build_mocks(self):
        mockbin = self.base / 'mockbin'
        mockbin.mkdir(parents=True, exist_ok=True)

        self._exec_script(mockbin / 'usleep', '''#!/bin/sh
# Scale usleep down 4x to keep multi-stage tests fast while preserving ordering
us=${1:-100000}
awk -v u="$us" 'BEGIN { printf "%.4f\\n", (u / 4000000.0 < 0.005 ? 0.005 : u / 4000000.0) }' | xargs sleep
''')

        self._exec_script(mockbin / 'amixer', '''#!/usr/bin/env python3
import fcntl, json, os, sys
p = os.environ['MOCK_MIXER']
lock_p = p + '.lock'
with open(lock_p, 'w') as lf:
    fcntl.flock(lf, fcntl.LOCK_EX)
    s = json.load(open(p))
    a = sys.argv[3:]
    act, raw_key = a[0], a[1]
    key = raw_key.removeprefix('numid=')
    if act in ('sset', 'cset'):
        val = a[2]
        s[key] = val
        tmp = f"{p}.{os.getpid()}"
        with open(tmp, 'w') as wf:
            json.dump(s, wf)
        os.replace(tmp, p)
        with open(os.environ['MOCK_EVENTS'], 'a') as ef:
            ef.write(f"mixer:{key}={val}\\n")
    elif act == 'cget':
        print('  : values=' + s[key])
    elif key == 'Master':
        print('Front Left: ' + s[key])
    else:
        print('Front Left: Playback [' + s[key] + ']')
''')

        self._exec_script(mockbin / 'apple_remote', '''#!/usr/bin/env python3
import fcntl, json, os
preload = os.environ.get('LD_PRELOAD', '')
level = 0 if preload.endswith('unmute.so') else 1
p = os.environ['MOCK_PA']
with open(p + '.lock', 'w') as lf:
    fcntl.flock(lf, fcntl.LOCK_EX)
    s = json.load(open(p))
    # Verify DAC digital silence whenever unmuting PA!
    mixer = json.load(open(os.environ['MOCK_MIXER']))
    if level == 0 and mixer['1'] != '0,0':
        with open(os.environ['MOCK_EVENTS'], 'a') as ef:
            ef.write(f"POP_HAZARD:pa_unmute_at_gain_{mixer['1']}\\n")
    s['pa'] = 1 if (level == 1 or s['force'] == 1) else 0
    tmp = f"{p}.{os.getpid()}"
    json.dump(s, open(tmp, 'w'))
    os.replace(tmp, p)
    with open(os.environ['MOCK_EVENTS'], 'a') as ef:
        ef.write(f"pa:{level}\\n")
print(f"Snd_Ctrl airplay to mute {level}", flush=True)
''')

        self._exec_script(mockbin / 'stock-audio-state', '''#!/usr/bin/env python3
import json, os, sys
s = json.load(open(os.environ['MOCK_PA']))
print(f"PA mute={s['pa']} force_mute={s['force']} gate={s['gate']}")
if len(sys.argv) == 2:
    if sys.argv[1] == '--global-unmuted' and s['force']:
        sys.exit(3)
    if sys.argv[1] == '--unmuted' and (s['force'] or s['pa'] or not s['gate']):
        sys.exit(3)
sys.exit(0)
''')

        req_script = '''#!/usr/bin/env python3
import fcntl, json, os, sys
sock, cmd = sys.argv[1], sys.argv[2]
ev = os.environ['MOCK_EVENTS']
if cmd == 'getPlayerStatus':
    if os.environ.get('MOCK_BT_RPC_FAIL') == '1':
        sys.exit(1)
    print(open(os.environ['MOCK_PHONE']).read().strip())
elif cmd.startswith('setPlayerCmd:'):
    sub = cmd.split(':', 1)[1]
    with open(ev, 'a') as ef:
        ef.write(f"bt_cmd:{sub}\\n")
    p = os.environ['MOCK_PHONE']
    with open(p + '.lock', 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        s = json.load(open(p))
        if sub == 'onepause' and os.environ.get('MOCK_STALE_PLAY') != '1':
            s['status'] = 'pause'
        elif sub == 'resume':
            s['status'] = 'play'
        elif sub.startswith('vol:'):
            s['vol'] = sub.split(':', 1)[1]
        tmp = f"{p}.{os.getpid()}"
        json.dump(s, open(tmp, 'w'), separators=(',', ':'))
        os.replace(tmp, p)
elif cmd == 'activity':
    if not os.path.exists(os.environ['MOCK_ACTIVITY']):
        sys.exit(1)
    print(open(os.environ['MOCK_ACTIVITY']).read().strip())
elif cmd.startswith('park:'):
    open(os.environ['MOCK_PCM'], 'w').write('closed\\n')
    owner_f = os.environ['HK_BT_PCM_OWNER']
    if os.path.exists(owner_f):
        os.unlink(owner_f)
    with open(ev, 'a') as ef:
        ef.write('bt_pcm:parked\\n')
    print('parked')
elif cmd.startswith('GNOTIFY=AUDIOMUTE:'):
    val = int(cmd.rsplit(':', 1)[1])
    mixer = json.load(open(os.environ['MOCK_MIXER']))
    if val == 0 and mixer['1'] != '0,0':
        with open(ev, 'a') as ef:
            ef.write(f"POP_HAZARD:global_unmute_at_gain_{mixer['1']}\\n")
    p = os.environ['MOCK_PA']
    with open(p + '.lock', 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        s = json.load(open(p))
        old_force = s['force']
        s['force'] = val
        if val == 1:
            s['pa'] = 1
        elif old_force == 1 and os.environ.get('MOCK_PA_ONLY_STICKY') != '1':
            s['pa'] = 0
        tmp = f"{p}.{os.getpid()}"
        json.dump(s, open(tmp, 'w'))
        os.replace(tmp, p)
    with open(ev, 'a') as ef:
        ef.write(f"gnotify:audiomute:{val}\\n")
elif cmd.startswith('GNOTIFY='):
    with open(ev, 'a') as ef:
        ef.write(f"gnotify:{cmd.split('=', 1)[1]}\\n")
elif cmd.startswith('recordFile'):
    pass
else:
    sys.exit(2)
'''
        self._exec_script(self.opt / 'bin/hk-unix-request-raw', req_script)
        self._exec_script(self.voice_dir / 'hk-unix-request-raw', req_script)

        mqtt_script = '''#!/usr/bin/env python3
import os, sys
topic, payload = sys.argv[1], sys.argv[2]
with open(os.environ['MOCK_EVENTS'], 'a') as ef:
    ef.write(f"mqtt:{topic}:{payload}\\n")
if payload == 'pause':
    # Simulated Shairport pipe bridge closes hardware PCM when paused with release-dac
    run = os.environ['HK_AP2_RUN']
    if os.path.exists(run + '/release-dac'):
        open(os.environ['MOCK_PCM'], 'w').write('closed\\n')
'''
        self._exec_script(self.voice_dir / 'hk-mqtt-pub', mqtt_script)

        # Link real hook and real safe-test into opt/ so bridge & voice invoke actual production scripts
        self._exec_script(
            self.opt / 'hk-ap2-audio-hook.sh',
            f'#!/bin/sh\nexec sh "{HOOK}" "$@"\n',
        )
        self._exec_script(
            self.opt / 'bin/hk-ap2-safe-test',
            f'#!/bin/sh\nexec sh "{WATCHDOG}" "$@"\n',
        )

        # Mock hk-voice C binary for deterministic 2-round Q&A + feedq + tone + connected
        self._exec_script(self.voice_dir / 'hk-voice', '''#!/usr/bin/env python3
import os, subprocess, sys, time
cmd = sys.argv[1]
if cmd == 'connected':
    sys.exit(0 if os.environ.get('MOCK_AP2_CONNECTED', '1') == '1' else 1)
elif cmd == 'tone':
    open(sys.argv[2], 'wb').write(b'\\x00' * 64)
    sys.exit(0)
elif cmd == 'say' or cmd == 'clip':
    out = sys.argv[3]
    open(out, 'wb').write(b'\\x01' * 64)
    sys.exit(0)
elif cmd == 'feedq':
    qdir = sys.argv[2]
    # Emit raw PCM silence while waiting for q.1.raw + q.end or stop
    next_idx = 1
    while not os.path.exists(qdir + '/stop'):
        chime = qdir + '/chime.raw'
        if os.path.exists(chime):
            os.unlink(chime)
        qf = f'{qdir}/q.{next_idx}.raw'
        if os.path.exists(qf):
            if next_idx == 1:
                open(qdir + '/started', 'w').close()
            os.unlink(qf)
            next_idx += 1
            continue
        if os.path.exists(qdir + '/q.end'):
            os.unlink(qdir + '/q.end')
            open(qdir + '/played', 'w').close()
            next_idx = 1
        sys.stdout.buffer.write(b'\\x00' * 192)
        sys.stdout.buffer.flush()
        time.sleep(0.02)
    sys.exit(0)
elif cmd == 'listen':
    qdir = os.environ['HK_VOICE_QDIR']
    strict = os.environ.get('HK_VOICE_STRICT', '')
    rounds_file = os.environ.get('MOCK_VOICE_ROUNDS_FILE', '')
    max_rounds = int(os.environ.get('MOCK_VOICE_MAX_ROUNDS', '1'))
    cur = int(open(rounds_file).read().strip()) if (rounds_file and os.path.exists(rounds_file)) else 0
    if cur >= max_rounds:
        sys.exit(4)  # no follow-up speech -> clean exit without prepare
    if rounds_file:
        open(rounds_file, 'w').write(str(cur + 1))
    # Optional hook executed mid-question (while user is still speaking, BEFORE prepare!)
    mid_hook = os.environ.get('MOCK_MID_QUESTION_CMD', '')
    if mid_hook and cur == 0:
        subprocess.run(['sh', '-c', mid_hook], check=False)
    time.sleep(0.12)
    prep = os.environ.get('HK_VOICE_PREPARE', '')
    if prep:
        subprocess.Popen(['sh', '-c', prep])
    deadline = time.monotonic() + 8.0
    while not os.path.exists(qdir + '/prepared') and time.monotonic() < deadline:
        time.sleep(0.03)
    with open(os.environ['MOCK_EVENTS'], 'a') as ef:
        ef.write(f"voice_round:{cur + 1}:prepared={open(qdir + '/prepared').read().strip() if os.path.exists(qdir + '/prepared') else 'missing'}\\n")
    tmp = qdir + '/q.tmp'
    open(tmp, 'wb').write(b'\\x02' * 64)
    os.replace(tmp, qdir + '/q.1.raw')
    open(qdir + '/q.end', 'w').close()
    sys.exit(0)
sys.exit(0)
''')

        self._exec_script(mockbin / 'aplay', '''#!/usr/bin/env python3
import os, pathlib, sys, time
pid = os.getpid()
proc = pathlib.Path(os.environ['MOCK_PROC']) / str(pid)
proc.mkdir(parents=True, exist_ok=True)
(proc / 'comm').write_text('aplay\\n')
pcm = pathlib.Path(os.environ['MOCK_PCM'])
pcm.write_text(f'state: RUNNING\\nowner_pid   : {pid}\\n')
with open(os.environ['MOCK_EVENTS'], 'a') as ef:
    ef.write(f'aplay:open:{pid}\\n')
try:
    while sys.stdin.buffer.read(192):
        pass
finally:
    if f'owner_pid   : {pid}' in pcm.read_text():
        pcm.write_text('closed\\n')
    with open(os.environ['MOCK_EVENTS'], 'a') as ef:
        ef.write(f'aplay:closed:{pid}\\n')
''')

    def start_daemons(self):
        wd = self._spawn_bg(['sh', str(WATCHDOG), '__watchdog'], env=self.env)
        eventually(lambda: (self.safe / 'watchdog.heartbeat').exists(), msg='watchdog did not start')
        br = self._spawn_bg(['sh', str(BRIDGE), '__run'], env=self.env)
        eventually(lambda: (self.run / 'bt-bridge.pid').exists(), msg='bt-bridge did not start')
        return wd, br

    def start_airplay_stream(self):
        """Simulate Shairport opening DSP pipe and invoking audio hook start."""
        def open_dsp_when_claimed():
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if ((self.run / 'ap2-claim').exists()
                        and (self.safe / 'play.started').exists()
                        and not (self.run / 'bt-owner.pid').exists()
                        and (self.safe / 'lease').exists()
                        and (self.safe / 'lease').read_text().startswith('airplay ')):
                    self.pcm_file.write_text(f'state: RUNNING\nowner_pid   : {self.dsp_proc.pid}\n')
                    return
                time.sleep(0.02)

        t = threading.Thread(target=open_dsp_when_claimed, daemon=True)
        t.start()
        res = subprocess.run(
            ['sh', str(HOOK), 'start'],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=12,
        )
        t.join(timeout=2)
        self.assertEqual(
            res.returncode, 0,
            f"AirPlay start failed: stderr={res.stderr}\nhook.log={(self.run / 'hook.log').read_text() if (self.run / 'hook.log').exists() else ''}",
        )

    def stop_airplay_stream(self):
        res = subprocess.run(
            ['sh', str(HOOK), 'stop'],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=8,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.pcm_file.write_text('closed\n')

    def start_phone_bt_stream(self, stop_event: threading.Event, instance=5001, start_frames=500):
        """Simulate phone A2DP streaming frames into hkbtpark and opening DSP once allowed."""
        self.set_phone(status='play', mode='41', vol='35', mute='0')

        def pump():
            frames = start_frames
            opened = False
            while not stop_event.wait(0.06):
                t = self.now_tick()
                frames += 256
                self.set_bt_activity(frames=frames, last_sec=t, instance=instance)
                # Simulate hkbtpark opening slave once AirPlay releases hardware & barriers
                if not opened:
                    try:
                        pcm_now = self.pcm_file.read_text().strip()
                    except OSError:
                        pcm_now = ''
                    try:
                        lease_txt = (self.safe / 'lease').read_text()
                    except OSError:
                        lease_txt = ''
                    lease_ok = (not lease_txt) or lease_txt.startswith('bluetooth ')
                    if (pcm_now == 'closed' and lease_ok
                            and not (self.run / 'ap2-claim').exists()
                            and not (self.run / 'bt-release').exists()
                            and not (self.run / 'bt-wait-pcm').exists()):
                        self.publish_bt_owner_proof(generation=instance + frames)
                        self.pcm_file.write_text(f'state: RUNNING\nowner_pid   : {self.bt_ctrl_proc.pid}\n')
                        opened = True
                # Simulate a01controller voice-inject preload handshake when active
                inject_dir = self.run / 'voice-inject'
                if (inject_dir / 'active').exists():
                    (inject_dir / 'attached').touch()
                if (inject_dir / 'abort').exists():
                    (inject_dir / 'abort').unlink(missing_ok=True)
                    (inject_dir / 'aborted').touch()
                if (inject_dir / 'start').exists():
                    (inject_dir / 'start').unlink(missing_ok=True)
                    (inject_dir / 'pcm.raw').unlink(missing_ok=True)
                    (inject_dir / 'done').touch()

        th = threading.Thread(target=pump, daemon=True)
        th.start()
        return th

    def run_voice_wake(self, rounds=1, extra_env=None):
        """Execute real hk-voice.sh handle_wake against the live runtime."""
        rounds_file = self.base / 'voice-rounds-counter'
        rounds_file.write_text('0')
        # Prepare a customized wrapper around voice/hk-voice.sh overriding hardcoded paths to sandbox
        script = VOICE.read_text()
        patched = (
            script
            .replace('D=/vendor/hk-voice', f'D="{self.voice_dir}"')
            .replace('RUN=/tmp/hk-voice', f'RUN="{self.voice_run}"')
            .replace('/tmp/hk-ap2/run', str(self.run))
            .replace('/vendor/airplay2/hk-ap2-audio-hook.sh', str(HOOK))
            .replace('/proc/asound/card0/pcm0p/sub0/status', str(self.pcm_file))
            .replace('/tmp/Requesta01controller', str(self.base / 'player.sock'))
            .replace('/tmp/RequestIOGuard', str(self.base / 'ioguard.sock'))
            .replace('/tmp/RequestASRTTS', str(self.base / 'ioguard.sock'))
            .replace('/system/workdir/bin/aplay', str(self.base / 'mockbin/aplay'))
            .replace('/system/workdir/bin/amixer', str(self.base / 'mockbin/amixer'))
            .replace('/system/workdir/bin/apple_remote', str(self.base / 'mockbin/apple_remote'))
            .replace('/tmp/hk-ap2/opt/lib', str(self.opt / 'lib'))
            .replace('/dev/shm/hk-voice-', str(self.base / 'shm-voice-'))
            .replace('/tmp/hk-keys/voice.events', str(self.base / 'voice.events'))
        )
        # Allow airplay_owns_dac to inspect MOCK_PROC
        patched = patched.replace('for t in /proc/[0-9]*/task/"$o"; do', 'for t in "$MOCK_PROC"/[0-9]*/task/"$o"; do')
        runner = self.base / 'hk-voice-sandbox.sh'
        self._exec_script(runner, patched)
        (self.base / 'shm-voice-pre-a.wav.swp').write_bytes(b'\x00' * 352800)
        (self.base / 'voice.events').write_text('1000 GNOTIFY=MCULEDLISTENING\n')

        wake_driver = self.base / 'run-wake.sh'
        # Extract everything before `case "${1:-start}" in` and invoke handle_wake directly
        defs = patched.split('\ncase "${1:-start}" in', 1)[0]
        self._exec_script(
            wake_driver,
            defs + f'\nCUR="{self.base / "shm-voice-pre-a.wav"}"\n'
            f'set -- "{runner}"\n'
            f'HK_VOICE_PREPARE="{runner} prepare"\n'
            'handle_wake\n',
        )
        # Override $0 inside run_round by running via sh <runner_wake> where HK_VOICE_PREPARE points to runner
        wake_text = wake_driver.read_text().replace('HK_VOICE_PREPARE="$0 prepare"', f'HK_VOICE_PREPARE="{runner} prepare"')
        wake_driver.write_text(wake_text)

        env = self.env | {
            'MOCK_VOICE_ROUNDS_FILE': str(rounds_file),
            'MOCK_VOICE_MAX_ROUNDS': str(rounds),
            'HK_VOICE_NOLED': '1',
        } | (extra_env or {})
        res = subprocess.run(
            ['sh', str(wake_driver)],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(res.returncode, 0, f"handle_wake failed: {res.stderr}\nvoice.log={(self.voice_run / 'voice.log').read_text() if (self.voice_run / 'voice.log').exists() else ''}")
        return res

    def assert_no_pop_hazards_or_trips(self):
        ev = self.events()
        hazards = [x for x in ev if x.startswith('POP_HAZARD:')]
        self.assertEqual(hazards, [], f'Audible pop hazard detected: {hazards}')
        self.assertFalse(
            (self.safe / 'tripped').exists(),
            f"Unexpected watchdog trip: {(self.safe / 'tripped').read_text() if (self.safe / 'tripped').exists() else ''}",
        )

    def test_gate1_airplay_phone_connect_only_with_double_pa_mute_and_stale_play(self):
        """Gate 1: Phone connects (stale play, no frames) & mutes stock PA twice 1s apart."""
        self.start_daemons()
        self.start_airplay_stream()
        self.assertEqual(self.mixer_state()['1'], '192,192')
        self.assertEqual(self.pa_state()['pa'], 0)

        # Phone connects with stale "play" status, zero fresh frames, and mutes PA #1 (A2DP)
        self.set_phone(status='play', mode='41', vol='30', mute='0')
        self.pa_file.write_text(json.dumps({'pa': 1, 'force': 0, 'gate': 1}))
        eventually(lambda: self.pa_state()['pa'] == 0 and self.mixer_state()['1'] == '192,192', timeout=6.0,
                   msg='First stock PA mute during phone connect was not repaired')

        # 1s later AVRCP connects and triggers global force_mute + PA mute #2!
        self.pa_file.write_text(json.dumps({'pa': 1, 'force': 1, 'gate': 1}))
        eventually(
            lambda: self.pa_state()['pa'] == 0 and self.pa_state()['force'] == 0 and self.mixer_state()['1'] == '192,192',
            timeout=6.0,
            msg='Second stock PA/global mute within 1s was not repaired rapidly',
        )

        # AirPlay lease must remain uninterrupted throughout
        lease = (self.safe / 'lease').read_text().split()
        self.assertEqual(lease[0], 'airplay')
        self.assertEqual(lease[5], 'active')
        self.assertNotIn('mqtt:hk-ap2/remote:pause', self.events())
        self.assert_no_pop_hazards_or_trips()

    def test_stale_bluetooth_play_clears_pause_pending_and_allows_subsequent_phone_takeover(self):
        """Stale status=play must not trap bt-pause-pending forever or block real phone play."""
        self.env['MOCK_STALE_PLAY'] = '1'
        self.set_phone(status='play', mode='41', vol='30', mute='0')
        self.start_daemons()

        # AirPlay starts while phone reports stale "play" and ignores AVRCP pause
        self.start_airplay_stream()
        self.assertTrue((self.run / 'bt-pause-pending').exists())

        # Bridge witnesses silent PCM (>2s old, 0 new frames) after retry and clears bt-pause-pending
        eventually(
            lambda: not (self.run / 'bt-pause-pending').exists(),
            timeout=8.0,
            msg='bt-pause-pending deadlocked on stale status=play with silent PCM',
        )
        self.env.pop('MOCK_STALE_PLAY', None)

        # Now user actually taps Play on phone -> fresh PCM frames flow -> Bluetooth takes over!
        bt_stop = threading.Event()
        bt_thread = self.start_phone_bt_stream(bt_stop, instance=6001, start_frames=1000)
        try:
            eventually(
                lambda: (self.safe / 'lease').exists()
                and (self.safe / 'lease').read_text().startswith('bluetooth ')
                and (self.safe / 'lease').read_text().split()[5] == 'active',
                timeout=16.0,
                msg='Bluetooth failed to take over AirPlay after stale-play recovery',
            )
            self.assertIn('mqtt:hk-ap2/remote:pause', self.events())
            self.assertEqual(self.mixer_state()['1'], '207,207')
            self.assertEqual(self.mixer_state()['Ext Spk'], 'on')
            self.assert_no_pop_hazards_or_trips()
        finally:
            bt_stop.set()
            bt_thread.join(timeout=2)

    def test_full_three_way_alternation_airplay_bluetooth_alexa_matrix(self):
        """8-stage continuous marathon: AP2 -> Alexa(2r) -> AP2 -> BT -> Alexa(2r) -> AP2 -> BT -> Alexa+DeferredAP2 -> Idle."""
        self.start_daemons()

        # Stage 1: AirPlay starts
        self.start_airplay_stream()
        self.assertEqual((self.run / 'media-source').read_text().strip(), 'airplay')
        self.assertEqual(self.mixer_state()['1'], '192,192')

        # Stage 2: Alexa wakes during AirPlay with 2-round follow-up Q&A!
        # Also verify that during user speech (mid-question), release-dac & voice-owns stay intact!
        mid_check = self.base / 'mid-question-state'
        ap2_reopen_stop = threading.Event()

        def ap2_dsp_reopen_watcher():
            while not ap2_reopen_stop.wait(0.03):
                if ((self.run / 'ap2-claim').exists()
                        and (self.safe / 'play.started').exists()
                        and not (self.run / 'voice-owns').exists()
                        and not (self.run / 'bt-owner.pid').exists()):
                    if self.pcm_file.read_text().strip() == 'closed':
                        self.pcm_file.write_text(f'state: RUNNING\nowner_pid   : {self.dsp_proc.pid}\n')

        reopen_th = threading.Thread(target=ap2_dsp_reopen_watcher, daemon=True)
        reopen_th.start()
        try:
            self.run_voice_wake(
                rounds=2,
                extra_env={
                    'MOCK_MID_QUESTION_CMD': (
                        f'test -f "{self.run}/voice-owns" && test -f "{self.run}/release-dac" '
                        f'&& echo ok >"{mid_check}" || echo missing >"{mid_check}"'
                    ),
                    'HK_AP2_PCM_STATUS': str(self.pcm_file),
                },
            )
        finally:
            ap2_reopen_stop.set()
            reopen_th.join(timeout=2)
        self.assertEqual(mid_check.read_text().strip(), 'ok', 'voice-owns and release-dac must hold during user speech')
        self.assertIn('voice_round:1:prepared=ok', self.events())
        self.assertIn('voice_round:2:prepared=ok', self.events())
        self.assertIn('mqtt:hk-ap2/remote:play', self.events())
        self.assertEqual((self.safe / 'lease').read_text().split()[0], 'airplay',
                         'reopen_watch must synchronously restore active AirPlay lease before MQTT play!')
        self.assert_no_pop_hazards_or_trips()

        # Stage 3: Phone starts Bluetooth -> takes over AirPlay
        bt_stop = threading.Event()
        bt_thread = self.start_phone_bt_stream(bt_stop, instance=7001, start_frames=2000)
        try:
            eventually(
                lambda: (self.safe / 'lease').exists()
                and (self.safe / 'lease').read_text().startswith('bluetooth ')
                and (self.safe / 'lease').read_text().split()[5] == 'active',
                timeout=16.0,
                msg='Stage 3 Bluetooth takeover failed',
            )
            self.assertEqual((self.run / 'media-source').read_text().strip(), 'bluetooth')
            self.assertEqual(self.mixer_state()['1'], '207,207')

            # Stage 4: Alexa wakes during Bluetooth with 2-round follow-up Q&A (mix mode)!
            self.events_file.write_text('')
            self.run_voice_wake(rounds=2)
            ev4 = self.events()
            self.assertIn('voice_round:1:prepared=ok', ev4, f'BT Round 1 failed: {ev4}')
            self.assertIn('voice_round:2:prepared=ok', ev4, f'BT Round 2 follow-up failed: {ev4}')
            self.assertEqual(self.mixer_state()['1'], '207,207', 'Bluetooth digital unity not restored after 2-round Alexa')
            self.assertTrue((self.safe / 'lease').read_text().startswith('bluetooth '))
            self.assert_no_pop_hazards_or_trips()

            # Stage 5: Reverse handoff — Mac starts AirPlay while Bluetooth is playing
            bt_stop.set()
            bt_thread.join(timeout=2)
            self.start_airplay_stream()
            self.assertEqual((self.safe / 'lease').read_text().split()[0], 'airplay')
            self.assertFalse((self.run / 'bt-owner.pid').exists())
            self.assert_no_pop_hazards_or_trips()
        finally:
            bt_stop.set()

        # Stage 6: Phone starts Bluetooth AGAIN (2nd round-trip!)
        bt_stop2 = threading.Event()
        bt_thread2 = self.start_phone_bt_stream(bt_stop2, instance=7002, start_frames=5000)
        try:
            eventually(
                lambda: (self.safe / 'lease').exists()
                and (self.safe / 'lease').read_text().startswith('bluetooth ')
                and (self.safe / 'lease').read_text().split()[5] == 'active',
                timeout=16.0,
                msg='Stage 6 second Bluetooth takeover failed',
            )

            # Stage 7: Alexa wakes during Bluetooth, AND Mac AirPlay requests start MID-QUESTION (deferred)!
            def dsp_bridge_helper():
                deadline = time.monotonic() + 15.0
                while time.monotonic() < deadline:
                    if (self.run / 'ap2-claim').exists() and (self.safe / 'play.started').exists():
                        if not (self.run / 'bt-owner.pid').exists():
                            bt_stop2.set()
                            self.pcm_file.write_text(f'state: RUNNING\nowner_pid   : {self.dsp_proc.pid}\n')
                            return
                    time.sleep(0.02)

            dsp_th = threading.Thread(target=dsp_bridge_helper, daemon=True)
            dsp_th.start()
            self.run_voice_wake(
                rounds=1,
                extra_env={
                    'MOCK_MID_QUESTION_CMD': f'sh "{HOOK}" start',
                },
            )
            eventually(
                lambda: (self.safe / 'lease').exists()
                and (self.safe / 'lease').read_text().startswith('airplay ')
                and (self.safe / 'lease').read_text().split()[5] == 'active',
                timeout=10.0,
                msg='Stage 7 deferred AirPlay start during Bluetooth Alexa did not hand over to AirPlay',
            )
            dsp_th.join(timeout=2)
            self.assert_no_pop_hazards_or_trips()
        finally:
            bt_stop2.set()
            bt_thread2.join(timeout=2)

        # Stage 8: Clean AirPlay stop -> verified idle
        self.stop_airplay_stream()
        self.assertFalse((self.safe / 'lease').exists())
        self.assertFalse((self.run / 'voice-owns').exists())
        self.assertFalse((self.run / 'speaker.lock').exists())
        self.assertEqual(self.mixer_state()['Ext Spk'], 'off')
        self.assertEqual(self.mixer_state()['1'], '0,0')
        self.assert_no_pop_hazards_or_trips()

    def test_alexa_during_bluetooth_falls_back_cleanly_when_phone_disconnects_mid_question(self):
        """If phone stops/disconnects during user speech, Alexa falls back to standalone aplay and cleans up."""
        self.start_daemons()
        bt_stop = threading.Event()
        bt_thread = self.start_phone_bt_stream(bt_stop, instance=8001, start_frames=300)
        try:
            eventually(
                lambda: (self.safe / 'lease').exists()
                and (self.safe / 'lease').read_text().startswith('bluetooth ')
                and (self.safe / 'lease').read_text().split()[5] == 'active',
                timeout=10.0,
            )

            # Mid-question: phone disconnects!
            disconnect_script = self.base / 'disconnect-phone.sh'
            def stop_bt_mid_question():
                bt_stop.set()
                self.set_phone(status='stop', mode='-1', vol='25', mute='0')
                self.pcm_file.write_text('state: XRUN\nowner_pid   : ' + str(self.bt_ctrl_proc.pid) + '\n')

            # Trigger stop_bt_mid_question via a tiny unix signal file watched by a helper thread
            trigger = self.base / 'trigger-disconnect'
            def watcher():
                while not trigger.exists() and not self._clock_stop.is_set():
                    time.sleep(0.02)
                if trigger.exists():
                    stop_bt_mid_question()
            w_th = threading.Thread(target=watcher, daemon=True)
            w_th.start()

            self.events_file.write_text('')
            self.run_voice_wake(
                rounds=1,
                extra_env={'MOCK_MID_QUESTION_CMD': f': >"{trigger}"; sleep 0.6'},
            )
            w_th.join(timeout=2)
            ev = self.events()
            self.assertIn('voice_round:1:prepared=ok', ev, f'Fallback standalone voice failed: {ev}')
            self.assertTrue(any(x.startswith('aplay:open:') for x in ev), 'Standalone aplay did not open')
            self.assertTrue(any(x.startswith('aplay:closed:') for x in ev), 'Standalone aplay leaked open!')
            self.assertFalse((self.safe / 'lease').exists(), 'Voice lease leaked after fallback!')
            self.assertFalse((self.run / 'voice-owns').exists())
            self.assert_no_pop_hazards_or_trips()
        finally:
            bt_stop.set()
            bt_thread.join(timeout=2)

    def test_alexa_idle_wake_does_not_resume_paused_phone_with_stale_play_status(self):
        """Paused phone with stale status=play must never be sent resume after idle Alexa."""
        self.start_daemons()
        self.set_phone(status='play', mode='41', vol='25', mute='0')
        self.events_file.write_text('')
        self.run_voice_wake(rounds=1, extra_env={'MOCK_AP2_CONNECTED': '0'})
        ev = self.events()
        self.assertIn('voice_round:1:prepared=ok', ev)
        self.assertNotIn('bt_cmd:resume', ev, f'Stale play phone was falsely resumed: {ev}')
        self.assert_no_pop_hazards_or_trips()

    def test_bluetooth_tolerates_transient_status_rpc_miss_during_active_playback(self):
        """A single transient getPlayerStatus timeout during active BT must not drop audio."""
        self.start_daemons()
        bt_stop = threading.Event()
        bt_thread = self.start_phone_bt_stream(bt_stop, instance=9001, start_frames=400)
        try:
            eventually(
                lambda: (self.safe / 'lease').exists()
                and (self.safe / 'lease').read_text().startswith('bluetooth ')
                and (self.safe / 'lease').read_text().split()[5] == 'active',
                timeout=10.0,
            )
            # Inject transient RPC failure via wrapper toggle
            orig_req = (self.opt / 'bin/hk-unix-request-raw').read_text()
            fail_flag = self.base / 'fail-once'
            fail_flag.touch()
            patched_req = orig_req.replace(
                "if os.environ.get('MOCK_BT_RPC_FAIL') == '1':",
                f"if os.path.exists('{fail_flag}'):\n        os.unlink('{fail_flag}')\n        sys.exit(1)\n    if False:",
            )
            self._exec_script(self.opt / 'bin/hk-unix-request-raw', patched_req)
            eventually(lambda: not fail_flag.exists(), timeout=4.0)
            time.sleep(0.5)
            self.assertTrue((self.run / 'bt-owner.pid').exists(), 'Single RPC timeout dropped active Bluetooth!')
            self.assertTrue((self.safe / 'lease').read_text().startswith('bluetooth '))
            self.assert_no_pop_hazards_or_trips()
        finally:
            bt_stop.set()
            bt_thread.join(timeout=2)

    def test_resource_guard_reclaims_clean_page_cache_and_still_trips_on_real_exhaustion(self):
        """hk-ap2-guard.sh reclaims clean page cache when MemFree < 4096 but available >= 32768."""
        meminfo = self.base / 'meminfo'
        drop_caches = self.base / 'drop_caches'
        meminfo.write_text(
            'MemFree: 3100 kB\nBuffers: 4000 kB\nCached: 45000 kB\nSReclaimable: 2000 kB\nShmem: 4000 kB\n'
        )
        # Reclaim simulation watcher: writing 1 to drop_caches converts clean cache into MemFree
        stop_watch = threading.Event()
        reclaimed = threading.Event()

        def simulate_kernel_drop_caches():
            while not stop_watch.wait(0.05):
                if drop_caches.exists() and drop_caches.read_text().strip() == '1':
                    drop_caches.unlink()
                    if not reclaimed.is_set():
                        reclaimed.set()
                        meminfo.write_text(
                            'MemFree: 36000 kB\nBuffers: 4000 kB\nCached: 12000 kB\nSReclaimable: 2000 kB\nShmem: 4000 kB\n'
                        )

        w = threading.Thread(target=simulate_kernel_drop_caches, daemon=True)
        w.start()
        guard_env = self.env | {
            'HK_AP2_MIN_RUN_FREE_KIB': '4096',
            'HK_AP2_MIN_AVAILABLE_RUN_KIB': '32768',
            'HK_AP2_MEMINFO_FILE': str(meminfo),
            'HK_AP2_DROP_CACHES_FILE': str(drop_caches),
        }
        guard = self._spawn_bg(['sh', str(GUARD)], env=guard_env)
        try:
            eventually(lambda: reclaimed.is_set(), timeout=5.0, msg='Guard did not reclaim page cache')
            time.sleep(0.5)
            self.assertIsNone(guard.poll(), 'Guard tripped falsely despite reclaimable page cache!')
            self.assertFalse((self.run / 'low-memory-trip').exists())

            # Now simulate real un-reclaimable exhaustion (available < 32768)
            meminfo.write_text(
                'MemFree: 2500 kB\nBuffers: 500 kB\nCached: 42000 kB\nSReclaimable: 500 kB\nShmem: 40000 kB\n'
            )
            eventually(lambda: (self.run / 'low-memory-trip').exists(), timeout=10.0,
                       msg='Guard failed to trip on genuine un-reclaimable memory exhaustion')
            self.assertIn('memory low:', (self.run / 'low-memory-trip').read_text())
        finally:
            stop_watch.set()
            w.join(timeout=2)

    def test_watchdog_startup_contention_never_deadlocks_bluetooth_or_voice_ack(self):
        """Rapid Voice & Bluetooth startups overlapping watchdog's idle pass must ACK in <2s."""
        self._spawn_bg(['sh', str(WATCHDOG), '__watchdog'], env=self.env)
        eventually(lambda: (self.safe / 'watchdog.heartbeat').exists())

        for cycle in range(3):
            # Voice standalone start -> stop
            tok = f'voice-fast-{cycle}'
            (self.run / 'voice-owns').write_text(f'{self.now_tick()}\n')
            (self.run / 'voice-token').write_text(f'{tok}\n')
            (self.run / 'current-voice-token').write_text(f'{tok}\n')
            (self.voice_run / 'voice.pid').write_text(f'{self.shairport_proc.pid}\n')
            self._register_proc(900 + cycle, 'aplay')
            self.pcm_file.write_text(f'state: RUNNING\nowner_pid   : {900 + cycle}\n')
            venv = self.env | {'HK_AP2_VOICE': '1', 'HK_AUDIO_TOKEN': tok}
            t0 = time.monotonic()
            r = subprocess.run(['sh', str(HOOK), 'start'], env=venv, capture_output=True, text=True, timeout=6)
            elapsed = time.monotonic() - t0
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertLess(elapsed, 2.5, f'Voice start stalled {elapsed:.2f}s waiting for watchdog ACK')
            subprocess.run(['sh', str(HOOK), 'stop'], env=venv, check=True, timeout=5)
            for m in ('voice-owns', 'voice-token', 'current-voice-token'):
                (self.run / m).unlink(missing_ok=True)
            self.pcm_file.write_text('closed\n')
            self.assert_no_pop_hazards_or_trips()

    def test_lights_bridge_suppresses_airplay_volume_display_while_bluetooth_owns_speaker(self):
        """Background AirPlay volume.state updates must not clobber MCU volume during Bluetooth."""
        lights_dir = self.base / 'lights-dir'
        lights_dir.mkdir()
        shm_tool = lights_dir / 'hk-shm-play'
        self._exec_script(shm_tool, '#!/bin/sh\necho "play_status=0"\n')
        patched_lights = (
            LIGHTS.read_text()
            .replace('REQUEST=/tmp/hk-ap2/opt/bin/hk-unix-request-raw', f'REQUEST="{self.opt / "bin/hk-unix-request-raw"}"')
            .replace('/tmp/RequestIOGuard', str(self.base / 'ioguard.sock'))
        )
        lights_script = self.base / 'hk-ap2-lights-test.sh'
        self._exec_script(lights_script, patched_lights)
        (self.run / 'volume.state').write_text('1 -24.0 -24.0 0\n')
        (self.run / 'bt-owner.pid').write_text(f'{self.bt_ctrl_proc.pid}\n')

        lights_env = self.env | {
            'HK_LIGHTS_DIR': str(lights_dir),
            'HK_LIGHTS_BIN': str(lights_dir),
            'HK_LIGHTS_RUN': str(self.run),
            'HK_LIGHTS_PCM_STATUS': str(self.pcm_file),
            'HK_LIGHTS_PROC_ROOT': str(self.proc),
            'HK_LIGHTS_DISABLE_FILE': str(self.base / 'lights-disable'),
        }
        self._spawn_bg(['sh', str(lights_script), 'run'], env=lights_env)
        eventually(lambda: (lights_dir / 'lights.pid').exists())
        self.events_file.write_text('')
        # AirPlay updates volume.state while Bluetooth owns speaker -> no VOLUMECHANGE sent!
        (self.run / 'volume.state').write_text('2 -18.0 -18.0 0\n')
        time.sleep(1.5)
        self.assertFalse(
            any('VOLUMECHANGE' in x for x in self.events()),
            f'AirPlay volume overwrote MCU during Bluetooth: {self.events()}',
        )
        # Once Bluetooth releases speaker, subsequent AirPlay volume update DOES notify MCU!
        (self.run / 'bt-owner.pid').unlink()
        (self.run / 'volume.state').write_text('3 -15.0 -15.0 0\n')
        eventually(lambda: any('VOLUMECHANGE' in x for x in self.events()), timeout=4.0)

    def test_audio_lock_succeeds_across_rapid_concurrent_unlock_edges(self):
        """Concurrent contenders must not abort when owner disappears mid-read during unlock."""
        lock_path = self.run / 'contend.lock'
        worker_script = self.base / 'lock-worker.sh'
        self._exec_script(worker_script, f'''#!/bin/sh
set -eu
. "{COMMON}"
i=0
while [ "$i" -lt 15 ]; do
    audio_lock "{lock_path}"
    usleep 10000 2>/dev/null || true
    audio_unlock "{lock_path}"
    i=$((i + 1))
done
''')
        procs = [
            subprocess.Popen(['sh', str(worker_script)], env=self.env, stderr=subprocess.PIPE, text=True)
            for _ in range(4)
        ]
        for p in procs:
            _, err = p.communicate(timeout=15)
            self.assertEqual(p.returncode, 0, f'audio_lock contender failed on unlock edge: {err}')
        self.assertFalse(lock_path.exists())

        # Symlink owner inside lock directory must still fail closed immediately
        lock_path.mkdir()
        (lock_path / 'owner').symlink_to('/etc/passwd')
        res = subprocess.run(
            ['sh', '-c', f'. "{COMMON}"; audio_lock "{lock_path}"'],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=4,
        )
        self.assertNotEqual(res.returncode, 0)


if __name__ == '__main__':
    unittest.main()
