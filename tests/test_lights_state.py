#!/usr/bin/env python3
"""Exercise the lights bridge's AirPlay-only playback ownership state."""
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
        (self.run / "shairport.pid").write_text(f"{os.getpid()}\n")
        self.env = os.environ | {
            "HK_LIGHTS_DIR": str(root / "lights"),
            "HK_LIGHTS_RUN": str(self.run),
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


if __name__ == "__main__":
    unittest.main()
