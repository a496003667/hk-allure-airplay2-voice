#!/usr/bin/env python3
"""Pack an explicit, checksum-locked soft-float dependency set into cramfs.
Run on Linux with mkfs.cramfs. Never reads firmware, network, or credentials.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

REPO=Path(__file__).resolve().parents[1]

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def verify_inputs(tree,lock):
    for relative,expected in lock['files'].items():
        path=tree/relative
        if not path.is_file() or sha(path)!=expected:
            raise ValueError(f'Missing/mismatched pinned dependency: {relative}')

def build(tree,binaries,output,lock):
    verify_inputs(tree,lock)
    if output.exists(): raise ValueError('Output must not already exist')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hk-runtime-') as directory:
        root=Path(directory)/'opt';root.mkdir()
        for relative in lock['files']:
            src=tree/relative;dst=root/relative
            dst.parent.mkdir(parents=True,exist_ok=True)
            # Dereference aliases so the result never points back at a host prefix.
            shutil.copyfile(src,dst);dst.chmod(0o755 if relative.startswith(('bin/','sbin/')) else 0o644)
        for name in ('hk-ap2ctl','hk-ap2-safe-test','hk-ap2-guard.sh','hk-ap2-volume-log'):
            dst=root/'bin'/name;shutil.copyfile(REPO/'airplay2/runtime'/name,dst);dst.chmod(0o755)
        # Bind mount targets must exist on the read-only image. NQPTP itself
        # is supplied as a separately pinned 5.6-compatible artifact.
        (root/'bin/nqptp').touch();(root/'bin/nqptp').chmod(0o755)
        for name in ('hk-unix-request-raw','shairport-sync'):
            shutil.copyfile(binaries/name,root/'bin'/name);(root/'bin'/name).chmod(0o755)
        for name in ('libhk-nss-shim.so','libhk-airplay-mute.so','libhk-airplay-unmute.so'):
            shutil.copyfile(binaries/name,root/'lib'/name)
        (root/'etc/ap2').mkdir(parents=True)
        for name in ('dbus-system.conf','avahi-daemon.conf'):
            shutil.copyfile(REPO/'airplay2/runtime/config'/name,root/'etc/ap2'/name)
        shutil.copyfile(REPO/'airplay2/shairport-sync.conf',root/'etc/ap2/shairport-sync.conf')
        dry=(root/'etc/ap2/shairport-sync.conf').read_text().replace('/tmp/hk-ap2/run/airplay-pcm.fifo','/dev/null')
        (root/'etc/ap2/shairport-sync-dryrun.conf').write_text(dry)
        shutil.copyfile(REPO/'airplay2/hk-ap2-audio-hook.sh',root/'hk-ap2-audio-hook.sh')
        (root/'hk-ap2-audio-hook.sh').chmod(0o755)
        # mkfs.cramfs omits timestamps. Normalize ownership in the isolated
        # Linux builder (run as root), never chown the supplied dependency tree.
        subprocess.run(['chown','-R','0:0',str(root)],check=True)
        subprocess.run(['mkfs.cramfs','-N','little','-n','hk-ap2-runtime',str(root),str(output)],check=True)
    print(f'EXPECTED_SHA256={sha(output)}')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dependencies',type=Path,required=True)
    p.add_argument('--binaries',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--lock',type=Path,default=REPO/'docs/runtime-inputs.lock.json')
    a=p.parse_args()
    build(a.dependencies,a.binaries,a.output,json.loads(a.lock.read_text()))
