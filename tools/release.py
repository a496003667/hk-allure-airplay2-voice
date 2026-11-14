#!/usr/bin/env python3
"""Host-side release builder. No SSH, network, speaker or credential access."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess

REPO=Path(__file__).resolve().parents[1]
# Explicit allowlist: never recursively package the working directory.
SCRIPTS={
 'airplay2/hk-audio-dependents.sh':'airplay2/hk-audio-dependents.sh',
 'airplay2/hk-ap2-prodctl':'airplay2/hk-ap2-prodctl',
 'airplay2/hk-ap2-replace.sh':'airplay2/hk-ap2-replace.sh',
 'airplay2/hk-ap2-audio-hook.sh':'airplay2/hk-ap2-audio-hook.sh',
 'airplay2/hk-audio-common.sh':'airplay2/hk-audio-common.sh',
 'airplay2/runtime/hk-ap2-safe-test':'airplay2/hk-ap2-safe-test.bt',
 'airplay2/runtime/hk-ap2ctl':'airplay2/hk-ap2ctl.ap256',
 'airplay2/runtime/hk-ap2-guard.sh':'airplay2/hk-ap2-guard.sh',
 'airplay2/runtime/hk-ap2-volume-log':'airplay2/hk-ap2-volume-log',
 'airplay2/hk-bt-bridge.sh':'airplay2/hk-bt-bridge.sh',
 'airplay2/hk-bt-controller-wrapper.sh':'airplay2/hk-bt-controller-wrapper.sh',
 'airplay2/hk-bt-boot':'airplay2/hk-bt-boot',
 'airplay2/hk-dsp-alsa.conf':'airplay2/hk-dsp-alsa.conf',
 'airplay2/hk-bt-alsa.conf':'airplay2/hk-bt-alsa.conf',
 'airplay2/mosquitto.conf':'airplay2/ap2-56/mosquitto.conf',
 'airplay2/shairport-sync.conf':'airplay2/ap2-56/shairport-sync.conf',
 'voice/hk-voice.sh':'hk-voice/hk-voice.sh',
 'lights/hk-ap2-lights.sh':'hk-lights/hk-ap2-lights.sh',
 'keys/hk-mv-keys-boot.sh':'hk-keys/hk-mv-keys-boot.sh',
 'init/S99hk-airplay2':'airplay2/S99hk-airplay2',
}
BINARIES={
 'hk-unix-request-raw':['airplay2/hk-unix-request-raw','hk-voice/hk-unix-request-raw'],
 'hk-mqtt-pub':['hk-voice/hk-mqtt-pub'], 'hk-voice':['hk-voice/hk-voice'],
 'hk-ap2-dsp-bridge':['airplay2/hk-ap2-dsp-bridge'],
 'hk-bt-open-redirect.so':['airplay2/hk-bt-open-redirect.so'],
 'libhk-mv-ap2-keys.so':['hk-keys/libhk-mv-ap2-keys.so'],
 'hk-shm-play':['hk-lights/hk-shm-play'],
 'libhk-ap2-info-shim-soft.so':['airplay2/libhk-ap2-info-shim-soft.so'],
 'shairport-sync':['airplay2/ap2-56/shairport-sync'],
}
PINS={
 'AP2CTL_OVERRIDE_256_SHA1':'airplay2/hk-ap2ctl.ap256',
 'AUDIO_HOOK_OVERRIDE_SHA1':'airplay2/hk-ap2-audio-hook.sh',
 'SAFE_TEST_OVERRIDE_SHA1':'airplay2/hk-ap2-safe-test.bt',
 'BT_BRIDGE_SHA1':'airplay2/hk-bt-bridge.sh', 'DSP_ALSA_CONF_SHA1':'airplay2/hk-dsp-alsa.conf',
 'DSP_BRIDGE_SHA1':'airplay2/hk-ap2-dsp-bridge', 'VOICE_SCRIPT_SHA1':'hk-voice/hk-voice.sh',
 'COMMON_SHA1':'airplay2/hk-audio-common.sh', 'REQUEST_OVERRIDE_SHA1':'airplay2/hk-unix-request-raw',
 'GUARD_OVERRIDE_SHA1':'airplay2/hk-ap2-guard.sh', 'VOLUME_OVERRIDE_SHA1':'airplay2/hk-ap2-volume-log',
 'INFO_SHIM_SHA1':'airplay2/libhk-ap2-info-shim-soft.so', 'SPS56_SHA1':'airplay2/ap2-56/shairport-sync',
 'CONF56_SHA1':'airplay2/ap2-56/shairport-sync.conf',
}


def digest(path,algo='sha256'): return hashlib.new(algo,Path(path).read_bytes()).hexdigest()

def set_pin(text,key,value):
    text,n=re.subn(r'^'+re.escape(key)+r'=[^\n]*$',key+'='+value,text,flags=re.M)
    if n!=1: raise ValueError(f'Expected one {key} pin, got {n}')
    return text

def check_elf(path,soft=False,raw=False):
    data=Path(path).read_bytes()
    if data[:6]!=b'\x7fELF\x01\x01' or int.from_bytes(data[18:20],'little')!=40:
        raise ValueError(f'Not a 32-bit little-endian ARM artifact: {path}')
    flags=int.from_bytes(data[36:40],'little')
    if soft and flags&0x400: raise ValueError(f'Expected soft-float ABI: {path}')
    if not soft and not raw and not flags&0x400: raise ValueError(f'Expected hard-float ABI: {path}')

def copy_file(source,target,mode):
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(source,target);target.chmod(mode)

def pin_tree(vendor):
    controller=vendor/'airplay2/hk-ap2-prodctl'
    text=controller.read_text()
    for key,relative in PINS.items(): text=set_pin(text,key,digest(vendor/relative,'sha1'))
    controller.write_text(text)
    wrapper=vendor/'airplay2/hk-bt-controller-wrapper.sh'
    shim=digest(vendor/'airplay2/hk-bt-open-redirect.so')
    private_conf=digest(vendor/'airplay2/hk-bt-alsa.conf')
    text=wrapper.read_text()
    text=re.sub(r'(SHIM_SHA256=\$\{HK_BT_REDIRECT_SHA256:-)[0-9a-f]{64}',lambda m:m[1]+shim,text)
    text=re.sub(r'(CONF_SHA256=\$\{HK_BT_PRIVATE_CONF_SHA256:-)[0-9a-f]{64}',lambda m:m[1]+private_conf,text)
    wrapper.write_text(text)
    boot=vendor/'airplay2/hk-bt-boot';text=boot.read_text()
    for key,value in [('SHIM_SHA256',shim),('WRAPPER_SHA256',digest(wrapper)),('CONF_SHA256',private_conf)]:
        text=re.sub(r'(^'+key+r'=\$\{[^:]+:-)[0-9a-f]{64}',lambda m:m[1]+value,text,flags=re.M)
    boot.write_text(text)

def write_manifest(root):
    lines=[];meta=[]
    for p in sorted((root/'vendor').rglob('*')):
        if not p.is_file(): continue
        relative=p.relative_to(root)
        perm=stat.S_IMODE(p.stat().st_mode)
        if perm not in (0o600,0o644,0o755): raise ValueError(f'Unexpected mode: {relative}')
        if any(c.isspace() for c in str(relative)): raise ValueError('Whitespace in target path')
        lines.append(f'{digest(p)} {perm:o} /{relative}\n')
        meta.append({'path':str(relative),'mode':f'{perm:o}','sha256':digest(p),'bytes':p.stat().st_size})
    (root/'update.manifest').write_text(''.join(lines))
    (root/'release.json').write_text(json.dumps({'schema':1,'target':'Shairport 5.6 + guarded DSP',
        'device_tested':False,'files':meta},indent=2)+'\n')

def package(output,binaries,device_mac=None,device_config=None,runtime_image=None,nqptp=None,mosquitto=None):
    if output.exists(): raise ValueError('Output must not exist; avoid mixing releases')
    for name in BINARIES:
        check_elf(binaries/name,soft=name in ('shairport-sync','libhk-ap2-info-shim-soft.so'),
                  raw=name in ('hk-mqtt-pub','hk-shm-play','hk-unix-request-raw'))
    vendor=output/'vendor'
    for source,target in SCRIPTS.items():
        p=REPO/source
        executable=p.read_bytes().startswith(b'#!')
        if executable: subprocess.run(['sh','-n',str(p)],check=True)
        copy_file(p,vendor/target,0o755 if executable else 0o644)
    for source,targets in BINARIES.items():
        for target in targets: copy_file(binaries/source,vendor/target,0o755)
    pin_tree(vendor)
    if runtime_image:
        target=vendor/'airplay2/hk-ap2-runtime.cramfs'
        copy_file(runtime_image,target,0o644)
        controller=vendor/'airplay2/hk-ap2-prodctl'
        controller.write_text(set_pin(controller.read_text(),'EXPECTED_SHA256',digest(target)))
    for binary,name,pin in [(nqptp,'nqptp','NQPTP56_SHA1'),(mosquitto,'mosquitto',None)]:
        if binary:
            check_elf(binary,soft=True)
            target=vendor/'airplay2/ap2-56'/name;copy_file(binary,target,0o755)
            if pin:
                controller=vendor/'airplay2/hk-ap2-prodctl'
                controller.write_text(set_pin(controller.read_text(),pin,digest(target,'sha1')))
    if device_mac and device_config: raise ValueError('Choose a MAC template or an existing device config')
    if device_config:
        conf=vendor/'airplay2/ap2-56/shairport-sync.conf'
        text=device_config.read_text()
        for key,value in [('volume_range_db','24'),('volume_max_db','-6.0'),('default_airplay_volume','-30.0')]:
            text,n=re.subn(r'(^\s*'+key+r'\s*=\s*)[^;]+;',lambda m:m[1]+value+';',text,flags=re.M)
            if n!=1: raise ValueError(f'Expected one device setting: {key}')
        if not re.search(r'output_backend\s*=\s*"pipe"',text): raise ValueError('Device config must retain the DSP pipe')
        if not re.search(r'volume_control_profile\s*=\s*"flat"',text): raise ValueError('Device config must use flat volume profile')
        conf.write_text(text)
        pins=vendor/'airplay2/ap2-device.env'
        pins.write_text('CONF56_SHA1='+digest(conf,'sha1')+'\n');pins.chmod(0o600)
    if device_mac:
        mac=device_mac.replace(':','').replace('-','')
        if not re.fullmatch(r'[0-9a-fA-F]{12}',mac): raise ValueError('Invalid device MAC')
        conf=vendor/'airplay2/ap2-56/shairport-sync.conf'
        text,n=re.subn(r'airplay_device_id\s*=\s*0x[0-9a-fA-F]+L;',f'airplay_device_id = 0x{mac.upper()}L;',conf.read_text())
        if n!=1: raise ValueError('Missing unique device ID placeholder')
        conf.write_text(text)
        pins=vendor/'airplay2/ap2-device.env'
        pins.write_text('CONF56_SHA1='+digest(conf,'sha1')+'\n');pins.chmod(0o600)
    write_manifest(output)

def verify(root):
    manifest=(root/'update.manifest').read_text().splitlines()
    seen=set()
    for line in manifest:
        sha,mode,target=line.split()
        if not target.startswith('/vendor/') or '..' in target or target in seen: raise ValueError('Invalid target')
        seen.add(target)
        p=root/target.lstrip('/')
        if p.is_symlink() or digest(p)!=sha or f'{stat.S_IMODE(p.stat().st_mode):o}'!=mode:
            raise ValueError(f'Integrity/mode failure: {target}')
    if not seen: raise ValueError('Empty release')
    print(f'Verified {len(seen)} artifacts; device deployment and listening remain pending.')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('package');p.add_argument('--output',type=Path,required=True)
    p.add_argument('--binaries',type=Path,default=REPO/'build/bin');p.add_argument('--device-mac')
    p.add_argument('--device-config',type=Path);p.add_argument('--runtime-image',type=Path)
    p.add_argument('--nqptp',type=Path);p.add_argument('--mosquitto',type=Path)
    p=sub.add_parser('verify');p.add_argument('directory',type=Path)
    args=parser.parse_args()
    if args.command=='package': package(args.output,args.binaries,args.device_mac,args.device_config,args.runtime_image,args.nqptp,args.mosquitto);verify(args.output)
    else: verify(args.directory)
