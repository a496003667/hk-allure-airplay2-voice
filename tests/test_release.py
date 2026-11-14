"""Build and validate an allowlisted release in an empty directory."""
import importlib.util
from pathlib import Path
import tempfile
import os
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('release',ROOT/'tools/release.py')
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='hk-package-')
        self.root=Path(self.temp.name)
        self.bin=self.root/'bin';self.bin.mkdir()
        for name in release.BINARIES:
            data=bytearray(64);data[:6]=b'\x7fELF\x01\x01';data[18:20]=(40).to_bytes(2,'little')
            data[36:40]=(0x200 if name in ('shairport-sync','libhk-ap2-info-shim-soft.so') else 0x400).to_bytes(4,'little')
            (self.bin/name).write_bytes(data)
        self.output=self.root/'candidate'

    def tearDown(self): self.temp.cleanup()

    def test_empty_directory_package_has_correct_modes_pins_and_no_credentials(self):
        release.package(self.output,self.bin)
        release.verify(self.output)
        vendor=self.output/'vendor'
        text=(vendor/'airplay2/hk-ap2-prodctl').read_text()
        for pin,relative in release.PINS.items():
            self.assertIn(pin+'='+release.digest(vendor/relative,'sha1'),text)
        for p in vendor.rglob('*'):
            self.assertNotIn('secure-shell',str(p))
            self.assertNotEqual(p.name,'voice.conf')
            if p.is_file() and p.read_bytes().startswith(b'#!'):
                self.assertEqual(p.stat().st_mode&0o777,0o755)

    def test_device_config_preserves_identity_and_creates_private_pin(self):
        config=self.root/'private.conf'
        text=(ROOT/'airplay2/shairport-sync.conf').read_text().replace('0x001122334455L','0x020000000001L')
        config.write_text(text.replace('volume_range_db = 24','volume_range_db = 30'))
        release.package(self.output,self.bin,device_config=config)
        vendor=self.output/'vendor'
        conf=vendor/'airplay2/ap2-56/shairport-sync.conf'
        self.assertIn('0x020000000001L',conf.read_text())
        self.assertIn('volume_range_db = 24',conf.read_text())
        pin=vendor/'airplay2/ap2-device.env'
        self.assertEqual(pin.stat().st_mode&0o777,0o600)
        self.assertEqual(pin.read_text(),'CONF56_SHA1='+release.digest(conf,'sha1')+'\n')
        release.verify(self.output)

    def test_tamper_and_host_binary_are_rejected(self):
        (self.bin/'shairport-sync').write_bytes(b'not ARM')
        with self.assertRaises(ValueError): release.package(self.output,self.bin)
        self.assertFalse(self.output.exists())

    def test_staging_preserves_live_files_and_rejects_symlink_siblings(self):
        release.package(self.output,self.bin)
        device=self.root/'device';device.mkdir()
        env=os.environ|{'HK_STAGE_ROOT':str(device.resolve()),'HK_AP2_UPDATE_LOCK':str(self.root/'absent-lock')}
        live=device/'vendor/airplay2/hk-ap2-prodctl';live.parent.mkdir(parents=True)
        live.write_text('running original')
        command=['sh',str(ROOT/'tools/stage-update.sh'),str(self.output)]
        result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(live.read_text(),'running original')
        self.assertEqual(Path(str(live)+'.new').read_bytes(),(self.output/'vendor/airplay2/hk-ap2-prodctl').read_bytes())
        staged=Path(str(live)+'.new');staged.unlink()
        outside=self.root/'must-not-change';outside.write_text('safe')
        staged.symlink_to(outside)
        result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(outside.read_text(),'safe')

    def test_mixed_releases_are_rejected(self):
        release.package(self.output,self.bin)
        with self.assertRaises(ValueError): release.package(self.output,self.bin)
        target=self.output/'vendor/airplay2/hk-ap2-audio-hook.sh'
        target.write_text('tampered')
        with self.assertRaises(ValueError): release.verify(self.output)


if __name__=='__main__': unittest.main()
