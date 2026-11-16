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
SHIM_SHA256=${HK_BT_REDIRECT_SHA256:-f26226b3b03a4ec08d257ff4cede090f1f62be2aedd0c9973077e4e4d46a9689}
CONF=${HK_BT_PRIVATE_CONF:-/vendor/airplay2/hk-bt-alsa.conf}
CONF_SHA256=${HK_BT_PRIVATE_CONF_SHA256:-2693ecef47f57f4f9df526114018d5c2ad2dfe0b6b36aba9cb7192235590ebf2}
PARK=${HK_BT_PARK_LIB:-/vendor/airplay2/libasound_module_pcm_hkbtpark.so}
PARK_SHA256=${HK_BT_PARK_SHA256:-d0c0b52d05dcf4209f088c7aafb7d9d5368cd9a9a66da028e5da26491bc5abdd}

hash_of() {
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
}

if [ -z "${LD_PRELOAD:-}" ] &&
   [ -x "$STOCK" ] && [ -r "$SHIM" ] && [ -r "$CONF" ] && [ -r "$PARK" ] &&
   [ "$(hash_of "$STOCK")" = "$STOCK_SHA256" ] &&
   [ "$(hash_of "$SHIM")" = "$SHIM_SHA256" ] &&
   [ "$(hash_of "$CONF")" = "$CONF_SHA256" ] &&
   [ "$(hash_of "$PARK")" = "$PARK_SHA256" ]; then
    export ALSA_CONFIG_PATH="$CONF"
    export LD_PRELOAD="$SHIM"
    export HK_BT_INJECT_DIR=/tmp/hk-ap2/run/voice-inject
fi

exec "$STOCK" "$@"
