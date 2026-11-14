#!/bin/sh
# Launch the verified stock Bluetooth player with a private ALSA route.
#
# rootApp searches /tmp/bin before /system/workdir/bin.  hk-bt-boot installs
# this wrapper there after /vendor has been mounted and before rootApp starts.
# Any checksum or capability failure executes the unmodified stock binary.
set -u

STOCK=${HK_BT_STOCK_EXE:-/system/workdir/bin/a01controller}
STOCK_SHA256=${HK_BT_STOCK_SHA256:-150bdfbe5757fd81010d2b99ed700fff591664ba729e901617e31b5d32f748f1}
SHIM=${HK_BT_REDIRECT_LIB:-/vendor/airplay2/hk-bt-open-redirect.so}
SHIM_SHA256=${HK_BT_REDIRECT_SHA256:-30d670bf574d12e4db352b5c0128954d5c550dd97a149a37f3487deed80bbd60}
CONF=${HK_BT_PRIVATE_CONF:-/vendor/airplay2/hk-bt-alsa.conf}
CONF_SHA256=${HK_BT_PRIVATE_CONF_SHA256:-2d128861ebd8285815d2247230fe9d32b43936b36a7bf916a90955e30c21c44a}

hash_of() {
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
}

if [ -z "${LD_PRELOAD:-}" ] &&
   [ -x "$STOCK" ] && [ -r "$SHIM" ] && [ -r "$CONF" ] &&
   [ "$(hash_of "$STOCK")" = "$STOCK_SHA256" ] &&
   [ "$(hash_of "$SHIM")" = "$SHIM_SHA256" ] &&
   [ "$(hash_of "$CONF")" = "$CONF_SHA256" ]; then
    export ALSA_CONFIG_PATH="$CONF"
    export LD_PRELOAD="$SHIM"
    export HK_BT_INJECT_DIR=/tmp/hk-ap2/run/voice-inject
fi

exec "$STOCK" "$@"
