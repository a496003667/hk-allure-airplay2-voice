#!/usr/bin/env python3
"""Exercise the verified Bluetooth launcher without loading ALSA."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "airplay2/hk-bt-controller-wrapper.sh"
BOOT = REPO / "airplay2/hk-bt-boot"
BOOT_ORDER = REPO / "init/S05linkplay.sh"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BluetoothLauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stock = self.root / "stock"
        self.stock.write_text(
            '#!/bin/sh\nprintf "%s|%s|%s\\n" '
            '"${ALSA_CONFIG_PATH:-stock}" "${LD_PRELOAD:-none}" "$1"\n'
        )
        self.stock.chmod(0o755)
        self.shim = self.root / "redirect.so"
        self.shim.write_text("isolated test shim\n")
        self.conf = self.root / "alsa.conf"
        self.conf.write_text("isolated test configuration\n")
        self.env = os.environ.copy()
        self.env.pop("LD_PRELOAD", None)
        self.env.pop("ALSA_CONFIG_PATH", None)
        self.env.update(
            {
                "HK_BT_STOCK_EXE": str(self.stock),
                "HK_BT_STOCK_SHA256": sha256(self.stock),
                "HK_BT_REDIRECT_LIB": str(self.shim),
                "HK_BT_REDIRECT_SHA256": sha256(self.shim),
                "HK_BT_PRIVATE_CONF": str(self.conf),
                "HK_BT_PRIVATE_CONF_SHA256": sha256(self.conf),
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_wrapper(self, env=None):
        return subprocess.run(
            ["sh", str(WRAPPER), "argument"],
            env=env or self.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()

    def test_verified_launch_sets_private_path(self):
        self.assertEqual(
            self.run_wrapper(),
            f"{self.conf}|{self.shim}|argument",
        )

    def test_any_integrity_failure_falls_back_to_stock(self):
        for key in (
            "HK_BT_STOCK_SHA256",
            "HK_BT_REDIRECT_SHA256",
            "HK_BT_PRIVATE_CONF_SHA256",
        ):
            self.assertEqual(
                self.run_wrapper(self.env | {key: "bad"}),
                "stock|none|argument",
            )
        self.assertEqual(
            self.run_wrapper(self.env | {"LD_PRELOAD": "existing"}),
            "stock|existing|argument",
        )

    def test_boot_installs_only_a_fully_verified_wrapper(self):
        base = self.root / "base"
        base.mkdir()
        wrapper = base / "hk-bt-controller-wrapper.sh"
        wrapper.write_text("verified wrapper\n")
        wrapper.chmod(0o755)
        shim = base / "hk-bt-open-redirect.so"
        shim.write_text("verified shim\n")
        conf = base / "hk-bt-alsa.conf"
        conf.write_text("verified config\n")
        target = self.root / "tmp/bin/a01controller"
        env = self.env | {
            "HK_BT_BASE": str(base),
            "HK_BT_TARGET": str(target),
            "HK_BT_BOOT_LOG": str(self.root / "boot.log"),
            "HK_BT_STOCK_SHA256": sha256(self.stock),
            "HK_BT_WRAPPER_SHA256": sha256(wrapper),
            "HK_BT_REDIRECT_SHA256": sha256(shim),
            "HK_BT_PRIVATE_CONF_SHA256": sha256(conf),
        }
        result = subprocess.run(
            ["sh", str(BOOT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(), wrapper.read_text())

        target.unlink()
        subprocess.run(
            ["sh", str(BOOT)],
            env=env | {"HK_BT_PRIVATE_CONF_SHA256": "bad"},
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        self.assertFalse(target.exists())

    def test_boot_installs_launcher_after_vendor_and_before_rootapp(self):
        script = BOOT_ORDER.read_text()
        vendor_mount = script.index("mount -t ubifs /dev/ubi2_0 /vendor")
        hook = script.index("/vendor/airplay2/hk-bt-boot")
        rootapp = script.index("rootApp &")
        self.assertLess(vendor_mount, hook)
        self.assertLess(hook, rootapp)


if __name__ == "__main__":
    unittest.main()
