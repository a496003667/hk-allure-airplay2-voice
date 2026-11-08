#!/usr/bin/env python3
"""Verify that AirPlay and voice both terminate at the stock Harman DSP."""
import hashlib
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
AIRPLAY = ROOT / "airplay2"


def sha1(path):
    return hashlib.sha1(path.read_bytes()).hexdigest()


class DspRoutingTest(unittest.TestCase):
    def test_dsp_config_enables_harmandsp_without_extra_softvol(self):
        config = (AIRPLAY / "hk-dsp-alsa.conf").read_text()
        self.assertIn("/system/workdir/lib/libasound_module_pcm_harmandsp.so", config)
        self.assertRegex(config, r"type\s+harmandsp")
        self.assertRegex(config, r"bypassmode\s+0")
        self.assertNotRegex(config, r"type\s+softvol")

    def test_airplay_pipe_is_fixed_format_and_keeps_minus_nine_db(self):
        config = (AIRPLAY / "shairport-sync.conf").read_text()
        self.assertRegex(config, r'output_backend\s*=\s*"pipe"')
        self.assertRegex(config, r'name\s*=\s*"/tmp/hk-ap2/run/airplay-pcm\.fifo"')
        self.assertRegex(config, r"output_rate\s*=\s*48000")
        self.assertRegex(config, r'output_format\s*=\s*"S32_LE"')
        self.assertRegex(config, r"output_channels\s*=\s*2")
        self.assertRegex(config, r"volume_max_db\s*=\s*-9\.0")
        self.assertNotIn('output_device = "hw:0,0"', config)

    def test_voice_stream_uses_shared_dsp_device(self):
        script = (ROOT / "voice/hk-voice.sh").read_text()
        self.assertIn("DSP_ALSA_CONF=/vendor/airplay2/hk-dsp-alsa.conf", script)
        self.assertIn('ALSA_CONFIG_PATH="$DSP_ALSA_CONF"', script)
        self.assertIn('"$APLAY" -q -D hk_harman_dsp', script)
        self.assertNotIn("-D hw:0,0", script)
        self.assertIn("shairport-sync|hk-ap2-dsp-bridge", script)

    def test_prodctl_pins_current_airplay_and_dsp_artifacts(self):
        controller = (AIRPLAY / "hk-ap2-prodctl").read_text()
        expected = {
            "CONF56_SHA1": sha1(AIRPLAY / "shairport-sync.conf"),
            "DSP_ALSA_CONF_SHA1": sha1(AIRPLAY / "hk-dsp-alsa.conf"),
            "DSP_BRIDGE_SHA1": sha1(AIRPLAY / "hk-ap2-dsp-bridge"),
        }
        for name, digest in expected.items():
            match = re.search(rf"^{name}=([0-9a-f]{{40}})$", controller, re.M)
            self.assertIsNotNone(match, name)
            self.assertEqual(match.group(1), digest, name)

    def test_dsp_bridge_uses_stock_hard_float_abi(self):
        image = (AIRPLAY / "hk-ap2-dsp-bridge").read_bytes()
        self.assertEqual(image[:4], b"\x7fELF")
        flags = int.from_bytes(image[36:40], "little")
        self.assertTrue(flags & 0x400, "bridge must use the stock hard-float ABI")
        self.assertFalse(flags & 0x200, "soft-float ABI cannot load the stock plugin")


if __name__ == "__main__":
    unittest.main()
