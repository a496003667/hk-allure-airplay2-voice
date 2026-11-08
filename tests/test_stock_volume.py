#!/usr/bin/env python3
"""Exercise the supervisor's stock volume check without an actual speaker."""
import json
import os
import pathlib
import re
import subprocess
import tempfile
import unittest

CONTROLLER = pathlib.Path(__file__).resolve().parents[1] / 'airplay2/hk-ap2-prodctl'


class StockVolumeTest(unittest.TestCase):
    def run_check(self, mode, volume):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            status = root / 'status'
            status.write_text(json.dumps({'mode': str(mode), 'vol': str(volume)}, separators=(',', ':')))
            event = root / 'event'
            request = root / 'request'
            request.write_text('''#!/bin/sh
echo clamp >"$MOCK_EVENT"
sed 's/"vol":"[0-9]*"/"vol":"30"/' "$MOCK_STATUS" >"$MOCK_STATUS.tmp"
mv "$MOCK_STATUS.tmp" "$MOCK_STATUS"
''')
            request.chmod(0o755)
            match = re.search(r'^check_stock_safety\(\) \{\n.*?^\}', CONTROLLER.read_text(), re.M | re.S)
            self.assertIsNotNone(match)
            script = '''stock_status() { cat "$MOCK_STATUS"; }
stock_value() { stock_status | sed -n 's/.*"vol":"\\([0-9][0-9]*\\)".*/\\1/p'; }
log() { :; }
REQUEST="$MOCK_REQUEST"
PLAYER_SOCKET=unused
''' + match.group() + '\ncheck_stock_safety\n'
            env = os.environ | {'MOCK_STATUS': str(status), 'MOCK_REQUEST': str(request), 'MOCK_EVENT': str(event)}
            result = subprocess.run(['sh', '-c', script], env=env, capture_output=True, text=True, timeout=5)
            return result.returncode, json.loads(status.read_text()), event.exists()

    def test_bluetooth_accepts_high_stock_volume(self):
        result, status, clamped = self.run_check(41, 52)
        self.assertEqual(result, 0)
        self.assertEqual(status['vol'], '52')
        self.assertFalse(clamped)

    def test_other_stock_modes_retain_existing_guard(self):
        result, status, clamped = self.run_check(10, 52)
        self.assertEqual(result, 0)
        self.assertEqual(status['vol'], '30')
        self.assertTrue(clamped)

    def test_out_of_range_volume_fails_closed(self):
        result, _, _ = self.run_check(41, 101)
        self.assertNotEqual(result, 0)


if __name__ == '__main__':
    unittest.main()
