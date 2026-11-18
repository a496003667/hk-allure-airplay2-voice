"""Exercise startup admission using Linux 3.10 memory snapshots, without ALSA."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'airplay2/hk-ap2-prodctl').read_text()
FUNCTIONS = 'startup_memory_kib() {' + SOURCE.split('startup_memory_kib() {', 1)[1].split('\nsupervisor_pid() {', 1)[0]


class StartupMemoryTest(unittest.TestCase):
    def check(self, *, free, cached, shmem=0, dirty=0, writeback=0, slab=0, buffers=0):
        with tempfile.TemporaryDirectory(prefix='hk-memory-') as directory:
            meminfo = Path(directory) / 'meminfo'
            values = {'MemFree': free, 'Buffers': buffers, 'Cached': cached, 'Shmem': shmem,
                      'Dirty': dirty, 'Writeback': writeback, 'SReclaimable': slab}
            meminfo.write_text(''.join(f'{key}: {value} kB\n' for key, value in values.items()))
            shell = FUNCTIONS + '\nlog() { echo "$*"; }\nstartup_memory_ok\n'
            return subprocess.run(['sh', '-c', shell], capture_output=True, text=True, timeout=3,
                                  env=os.environ | {'MEMINFO': str(meminfo), 'MIN_START_FREE_KIB': '8192',
                                                    'MIN_START_AVAILABLE_KIB': '49152'})

    def test_clean_cache_allows_start_without_a_raw_free_false_rejection(self):
        result = self.check(free=34696, cached=28000, shmem=4000, dirty=128, slab=1000)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('available~59068KiB', result.stdout)

    def test_low_raw_free_still_refuses_despite_large_cache(self):
        self.assertNotEqual(self.check(free=4096, cached=90000).returncode, 0)

    def test_tmpfs_does_not_count_as_reclaimable_cache(self):
        self.assertNotEqual(self.check(free=20000, cached=40000, shmem=30000).returncode, 0)

    def test_dirty_and_writeback_pages_are_excluded(self):
        self.assertNotEqual(self.check(free=20000, cached=40000, dirty=25000, writeback=10000).returncode, 0)

    def test_only_half_reclaimable_slab_is_credited(self):
        self.assertNotEqual(self.check(free=30000, cached=0, slab=30000).returncode, 0)

    def test_sufficient_free_memory_passes_without_cache(self):
        self.assertEqual(self.check(free=60000, cached=0).returncode, 0)

    def test_admitted_budget_propagates_the_same_floor_to_nested_runtime(self):
        runtime = (ROOT / 'airplay2/runtime/hk-ap2ctl').read_text()
        resources = 'check_resources() {' + runtime.split('check_resources() {', 1)[1].split('\nrotate_log()', 1)[0]
        with tempfile.TemporaryDirectory(prefix='hk-memory-nested-') as directory:
            root = Path(directory)
            meminfo = root / 'meminfo'
            meminfo.write_text('MemFree: 29908 kB\nBuffers: 0 kB\nCached: 30000 kB\nShmem: 5320 kB\n')
            nested = root / 'runtime-check.sh'
            nested.write_text('''MIN_FREE_KIB=${HK_AP2_MIN_FREE_KIB:-30720}
free_kib() { awk '/^MemFree:/ {print $2; exit}' "$MEMINFO"; }
''' + resources + '\ncheck_resources\n')
            shell = FUNCTIONS + '\nlog() { :; }\nstartup_memory_ok && sh "$NESTED_CHECK"\n'
            result = subprocess.run(['sh', '-c', shell], capture_output=True, text=True, timeout=3,
                                    env=os.environ | {'MEMINFO': str(meminfo), 'MIN_START_FREE_KIB': '8192',
                                                      'MIN_START_AVAILABLE_KIB': '49152',
                                                      'HK_AP2_MIN_FREE_KIB': '30720',
                                                      'NESTED_CHECK': str(nested)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_or_missing_memory_counters_fail_closed(self):
        self.assertNotEqual(self.check(free='invalid', cached=90000).returncode, 0)
        with tempfile.TemporaryDirectory(prefix='hk-memory-empty-') as directory:
            meminfo = Path(directory) / 'meminfo'
            meminfo.write_text('MemFree: 60000 kB\n')
            shell = FUNCTIONS + '\nlog() { :; }\nstartup_memory_ok\n'
            result = subprocess.run(['sh', '-c', shell], capture_output=True, text=True, timeout=3,
                                    env=os.environ | {'MEMINFO': str(meminfo), 'MIN_START_FREE_KIB': '8192',
                                                      'MIN_START_AVAILABLE_KIB': '49152'})
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
