#!/usr/bin/env python3
"""Exercise the lights bridge's AirPlay and Bluetooth ownership state."""
import os
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "lights/hk-ap2-lights.sh"


class LightsStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.run = root / "run"
        self.bin = root / "bin"
        (self.run / "safe").mkdir(parents=True)
        self.bin.mkdir()
        self.proc = root / "proc"
        self.proc.mkdir()
        self.pcm = root / "pcm.status"
        (self.run / "shairport.pid").write_text(f"{os.getpid()}\n")
        self.env = os.environ | {
            "HK_LIGHTS_DIR": str(root / "lights"),
            "HK_LIGHTS_RUN": str(self.run),
            "HK_LIGHTS_PCM_STATUS": str(self.pcm),
            "HK_LIGHTS_PROC_ROOT": str(self.proc),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        result = subprocess.run(
            ["sh", str(SCRIPT), "state"],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def active_markers(self):
        (self.run / "safe/play.started").write_text("1\n")
        (self.run / "ap2-claim").write_text("1\n")

    def bluetooth_markers(self, comm="a01controller\n"):
        owner = 4242
        (self.run / "safe/armed").touch()
        (self.run / "bt-owner.pid").write_text("31337\n")
        self.pcm.write_text(
            "state: RUNNING\n"
            f"owner_pid   : {owner}\n"
            "trigger_time: 1.000000000\n"
        )
        (self.proc / str(owner)).mkdir()
        (self.proc / str(owner) / "comm").write_text(comm)

    def test_active_requires_both_airplay_markers(self):
        self.active_markers()
        self.assertEqual(self.state(), "active")
        (self.run / "ap2-claim").unlink()
        self.assertEqual(self.state(), "idle")

    def test_bluetooth_owner_is_not_airplay(self):
        self.active_markers()
        (self.run / "bt-owner.pid").write_text("1\n")
        self.assertEqual(self.state(), "idle")

    def test_fresh_voice_owner_is_not_airplay(self):
        self.active_markers()
        (self.run / "voice-owns").write_text("0\n")
        cut = self.bin / "cut"
        cut.write_text("#!/bin/sh\nprintf '0\\n'\n")
        cut.chmod(0o755)
        self.env["PATH"] = str(self.bin) + ":" + self.env["PATH"]
        self.assertEqual(self.state(), "idle")

    def test_running_bluetooth_lease_lights_the_ring(self):
        self.bluetooth_markers()
        self.assertEqual(self.state(), "active")
        self.pcm.write_text("state: closed\n")
        self.assertEqual(self.state(), "idle")

    def test_non_bluetooth_pcm_owner_does_not_light_the_ring(self):
        self.bluetooth_markers(comm="shairport-sync\n")
        self.assertEqual(self.state(), "idle")

    def test_forwarding_follows_media_source_and_persistent_disable(self):
        source=SCRIPT.read_text()
        function='update_forward_flag() {'+source.split('update_forward_flag() {',1)[1].split('\nplay_status()',1)[0]
        disable=self.run/'lights.disable'
        shell='''
log() { :; }
sender_connected() { return 0; }
broker_listening() { return 0; }
'''+function+'\nupdate_forward_flag\n'
        env=self.env|{'RUN':str(self.run),'D':str(self.run/'lights'),'DISABLE':str(disable)}
        (self.run/'media-source').write_text('bluetooth')
        subprocess.run(['sh','-c',shell],env=env,check=True,capture_output=True)
        self.assertFalse((self.run/'buttons.forward').exists())
        (self.run/'media-source').write_text('airplay')
        (self.run/'voice-owns').write_text('0')
        subprocess.run(['sh','-c',shell],env=env,check=True,capture_output=True)
        self.assertTrue((self.run/'buttons.forward').exists(),'volume keys still control AirPlay during its voice reply')
        disable.touch()
        subprocess.run(['sh','-c',shell],env=env,check=True,capture_output=True)
        self.assertFalse((self.run/'buttons.forward').exists())


if __name__ == "__main__":
    unittest.main()
