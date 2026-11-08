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
        self.assertRegex(config, r"volume_range_db\s*=\s*38")
        self.assertRegex(config, r'volume_control_profile\s*=\s*"flat"')
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
            "AUDIO_HOOK_OVERRIDE_SHA1": sha1(AIRPLAY / "hk-ap2-audio-hook.sh"),
            "VOICE_SCRIPT_SHA1": sha1(ROOT / "voice/hk-voice.sh"),
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

    def test_bluetooth_voice_mixer_uses_stock_hard_float_abi(self):
        image = (AIRPLAY / "hk-bt-open-redirect.so").read_bytes()
        self.assertEqual(image[:4], b"\x7fELF")
        flags = int.from_bytes(image[36:40], "little")
        self.assertTrue(flags & 0x400, "mixer must use the stock hard-float ABI")
        self.assertFalse(flags & 0x200, "soft-float ABI cannot load in a01controller")
        source = (AIRPLAY / "hk-bt-open-redirect.c").read_text()
        self.assertIn("snd_pcm_writei", source)
        self.assertIn('marker_exists("active")', source)

    def test_prodctl_pins_bluetooth_bridge(self):
        controller = (AIRPLAY / "hk-ap2-prodctl").read_text()
        match = re.search(r"^BT_BRIDGE_SHA1=([0-9a-f]{40})$", controller, re.M)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), sha1(AIRPLAY / "hk-bt-bridge.sh"))

    def test_prodctl_keeps_bluetooth_alive_when_airplay_process_is_recycled(self):
        controller = (AIRPLAY / "hk-ap2-prodctl").read_text()
        self.assertIn("AP2 unavailable while Bluetooth is playing", controller)
        self.assertIn('[ -f "$RUN/bt-owner.pid" ]', controller)

    def test_prodctl_restarts_the_voice_supervisor(self):
        controller = (AIRPLAY / "hk-ap2-prodctl").read_text()
        self.assertIn("ensure_voice_supervisor", controller)
        self.assertIn('nohup "$VOICE_SCRIPT" supervise', controller)
        self.assertIn("hk-voice.sh supervise", controller)

    def test_prodctl_accepts_a_device_only_airplay_hash_pin(self):
        controller = (AIRPLAY / "hk-ap2-prodctl").read_text()
        self.assertIn('DEVICE_PINS=${HK_AP2_DEVICE_PINS:-$BASE/ap2-device.env}', controller)
        self.assertIn("CONF56_SHA1=$device_conf_sha1", controller)
        self.assertIn('[ "${#device_conf_sha1}" -eq 40 ]', controller)


if __name__ == "__main__":
    unittest.main()
