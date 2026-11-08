#!/bin/sh
mknod  /dev/nvram c 211 0

#for ubi mount
ubiattach /dev/ubi_ctrl -m 5
mount -t ubifs /dev/ubi1_0 /bkupgrade

ubiattach /dev/ubi_ctrl -m 6
mount -t ubifs /dev/ubi2_0 /vendor


cd /system/workdir
source /system/workdir/evn_amlogic.sh

ifconfig lo up

#for Alexa debug
mkdir -p /tmp/web

#for cxdish i2c
ln -s /dev/i2c-4 /dev/i2cM0

#for Http server
#boa &

#for BT
mkdir -p /vendor/bsa

# Install the verified Bluetooth DSP launcher before rootApp starts.
if [ -x /vendor/airplay2/hk-bt-boot ]; then
    /vendor/airplay2/hk-bt-boot
fi

#for launch rootApp
rootApp &
