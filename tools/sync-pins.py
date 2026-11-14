#!/usr/bin/env python3
"""Refresh public checksum pins from explicit, locally built artifacts."""
import argparse
from pathlib import Path
import shutil
from release import REPO,SCRIPTS,BINARIES,PINS,digest,set_pin

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--binaries',type=Path,default=REPO/'build/bin')
args=parser.parse_args()
# Keep the small previously shipped bridge artifacts reproducible and testable.
for name,target in [('hk-ap2-dsp-bridge','airplay2/hk-ap2-dsp-bridge'),
                    ('hk-bt-open-redirect.so','airplay2/hk-bt-open-redirect.so'),
                    ('hk-unix-request-raw','airplay2/hk-unix-request-raw'),
                    ('libhk-ap2-info-shim-soft.so','airplay2/libhk-ap2-info-shim-soft.so')]:
    shutil.copyfile(args.binaries/name,REPO/target)
    (REPO/target).chmod(0o755)
source_for={target:REPO/source for source,target in SCRIPTS.items()}
for name,targets in BINARIES.items():
    for target in targets: source_for[target]=args.binaries/name
controller=REPO/'airplay2/hk-ap2-prodctl'
text=controller.read_text()
for key,target in PINS.items(): text=set_pin(text,key,digest(source_for[target],'sha1'))
controller.write_text(text)
wrapper=REPO/'airplay2/hk-bt-controller-wrapper.sh'
import re
text=wrapper.read_text()
shim=digest(args.binaries/'hk-bt-open-redirect.so')
text=re.sub(r'(SHIM_SHA256=\$\{HK_BT_REDIRECT_SHA256:-)[0-9a-f]{64}',lambda m:m[1]+shim,text)
wrapper.write_text(text)
boot=REPO/'airplay2/hk-bt-boot';text=boot.read_text()
for name,value in [('SHIM_SHA256',shim),('WRAPPER_SHA256',digest(wrapper))]:
    text=re.sub(r'(^'+name+r'=\$\{[^:]+:-)[0-9a-f]{64}',lambda m:m[1]+value,text,flags=re.M)
boot.write_text(text)
print('Updated public artifact pins; no device configuration was read or modified.')
