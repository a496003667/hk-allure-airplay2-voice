#!/bin/sh
# Host-only cross build. Never connects to or runs commands on a speaker.
set -eu
REPO=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
OUT=${HK_BUILD_OUT:-$REPO/build/bin}
HARD=${HK_HARD_PREFIX:-armv7-linux-gnueabihf-}
SOFT=${HK_SOFT_PREFIX:-arm-unknown-linux-gnueabi-}
: "${HK_STOCK_ASOUND:?Set HK_STOCK_ASOUND to libasound.so.2 extracted from your own stock firmware}"
: "${HK_SOFT_DEPS:?Set HK_SOFT_DEPS to the isolated soft-float dependency prefix}"
: "${HK_SPS_SOURCE:?Set HK_SPS_SOURCE to a clean Shairport checkout at the locked commit}"
command -v "${HARD}gcc" >/dev/null
command -v "${SOFT}gcc" >/dev/null
PATH=$(dirname "$(command -v "${SOFT}gcc")"):$PATH
export PATH
[ -f "$HK_STOCK_ASOUND" ] && [ -d "$HK_SOFT_DEPS/lib" ]
mkdir -p "$OUT"
cd "$REPO"
"${HARD}gcc" -Os -o "$OUT/hk-voice" voice/hk-voice.c -ldl -static-libgcc
"${HARD}gcc" -Os -fomit-frame-pointer -fno-builtin -fno-stack-protector -nostdlib -static -Wl,-e,_start \
    tools/hk-unix-request-raw.c -o "$OUT/hk-unix-request-raw"
for pair in 'hk-mqtt-pub tools/hk-mqtt-pub.c' 'hk-shm-play lights/hk-shm-play.c'; do
    set -- $pair
    "${HARD}gcc" -Os -fomit-frame-pointer -fno-builtin -fno-stack-protector -nostdlib -static \
        -Wl,-e,_start tools/hk-arm-start.S "$2" -lgcc -o "$OUT/$1"
done
"${HARD}gcc" -march=armv7-a -Os -ffreestanding -fno-builtin -fno-stack-protector -nostdlib \
    -Wl,-e,_start -Wl,--gc-sections airplay2/hk-ap2-dsp-bridge.c "$HK_STOCK_ASOUND" \
    -o "$OUT/hk-ap2-dsp-bridge"
"${HARD}gcc" -Os -shared -fPIC airplay2/hk-bt-open-redirect.c -ldl -static-libgcc -o "$OUT/hk-bt-open-redirect.so"
"${HARD}gcc" -Os -shared -fPIC keys/hk-mv-ap2-keys.c -ldl -static-libgcc -o "$OUT/libhk-mv-ap2-keys.so"
for setting in mute:1 unmute:0; do
    "${HARD}gcc" -Os -shared -fPIC -nostdlib -DHK_AIRPLAY_MUTE=${setting#*:} \
        airplay2/hk-stock-pa.c -o "$OUT/libhk-airplay-${setting%:*}.so"
done
"${SOFT}gcc" -Os -shared -fPIC airplay2/hk-ap2-info-shim.c -ldl -o "$OUT/libhk-ap2-info-shim-soft.so"
"${SOFT}gcc" -Os -shared -fPIC -nostdlib airplay2/hk-nss-shim.c -o "$OUT/libhk-nss-shim.so"
# Apply only to the locked checkout. A previously patched build must already
# contain this exact patch; never reset or overwrite another developer's work.
[ "$(git -C "$HK_SPS_SOURCE" rev-parse HEAD)" = 9118bbce71f287df2dfdfb5bed3911d92a9fdb0f ]
if git -C "$HK_SPS_SOURCE" apply --check "$REPO/airplay2/shairport-sync-5.6-hk.patch" 2>/dev/null; then
    git -C "$HK_SPS_SOURCE" apply "$REPO/airplay2/shairport-sync-5.6-hk.patch"
else
    git -C "$HK_SPS_SOURCE" apply --reverse --check "$REPO/airplay2/shairport-sync-5.6-hk.patch"
fi
cd "$HK_SPS_SOURCE"
if [ ! -f Makefile ]; then
    autoreconf -fi
    PKG_CONFIG_LIBDIR="$HK_SOFT_DEPS/lib/pkgconfig" PKG_CONFIG_PATH="$HK_SOFT_DEPS/lib/pkgconfig" \
    CC="${SOFT}gcc" CXX="${SOFT}g++" ./configure --host=arm-unknown-linux-gnueabi \
        --sysconfdir=/opt/etc --with-alsa --with-avahi --with-ssl=openssl --with-soxr --with-metadata \
        --with-mqtt-client --with-airplay-2 --with-libdaemon --with-pipe \
        "CFLAGS=-O2 -march=armv7-a -I$HK_SOFT_DEPS/include" \
        "CXXFLAGS=-O2 -march=armv7-a -I$HK_SOFT_DEPS/include" \
        "LDFLAGS=-L$HK_SOFT_DEPS/lib -Wl,-rpath-link,$HK_SOFT_DEPS/lib -Wl,--as-needed"
fi
make -j "${HK_BUILD_JOBS:-4}"
cp shairport-sync "$OUT/shairport-sync"
printf '%s\n' "Built in $OUT; not installed or tested on a speaker."
