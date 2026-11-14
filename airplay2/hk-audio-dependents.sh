#!/bin/sh
# Update-only lifecycle of affected modules. Always used under maintenance hold.
set -u
BASE=${HK_AP2_BASE:-/vendor/airplay2}
VENDOR=${BASE%/*}
HOLD=${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}
TX=${HK_AP2_UPDATE_TX:-}
[ "$#" -eq 2 ] && [ -f "$HOLD" ] && [ -n "$TX" ] || exit 2
ACTION=$1
MANIFEST=$2
voice=0; lights=0; keys=0; bluetooth=0
while read -r sum perm target extra; do
    case "$target" in
        "$VENDOR/hk-voice/"*) voice=1;;
        "$VENDOR/hk-lights/"*) lights=1;;
        "$VENDOR/hk-keys/"*) keys=1;;
        "$BASE/hk-bt-open-redirect.so"|"$BASE/hk-bt-controller-wrapper.sh"|"$BASE/hk-bt-alsa.conf"|"$BASE/hk-bt-boot") bluetooth=1;;
    esac
done <"$MANIFEST"
stock_pid() {
    _sp_pid=$(pidof a01controller 2>/dev/null || true)
    case "$_sp_pid" in ''|*[!0-9]*|0|1) return 1;; esac
    tr '\000' ' ' <"/proc/$_sp_pid/cmdline" 2>/dev/null | grep -Fq '/system/workdir/bin/a01controller' || return 1
    printf '%s\n' "$_sp_pid"
}
voice_up() {
    _vu_pid=$(cat /tmp/hk-voice/voice.pid 2>/dev/null)
    case "$_vu_pid" in ''|*[!0-9]*|0|1) return 1;; esac
    kill -0 "$_vu_pid" 2>/dev/null && tr '\000' ' ' <"/proc/$_vu_pid/cmdline" | grep -Fq 'hk-voice.sh'
}
case "$ACTION" in
 stop)
    if [ "$voice" = 1 ] && [ -x "$VENDOR/hk-voice/hk-voice.sh" ]; then
        _old_voice=$(cat /tmp/hk-voice/voice.pid 2>/dev/null || true)
        "$VENDOR/hk-voice/hk-voice.sh" stop || exit 1
        case "$_old_voice" in ''|*[!0-9]*|0|1) _old_voice=;; esac
        _tries=0
        while [ -n "$_old_voice" ] && kill -0 "$_old_voice" 2>/dev/null &&
              ! grep -q '^State:.*Z' "/proc/$_old_voice/status" 2>/dev/null; do
            [ "$_tries" -lt 20 ] || exit 1
            sleep 1
            _tries=$((_tries + 1))
        done
    fi
    if [ "$lights" = 1 ] && [ -x "$VENDOR/hk-lights/hk-ap2-lights.sh" ]; then
        "$VENDOR/hk-lights/hk-ap2-lights.sh" stop || true
    fi
    # rootApp remains alive. The stock player is reloaded only after the new
    # wrapper has been installed and the independent watchdog is armed.
    if [ "$bluetooth" = 1 ]; then stock_pid >"$TX/stock-player.before" || exit 1; fi
    ;;
 start)
    if [ "$bluetooth" = 1 ]; then
        "$BASE/hk-bt-boot" || exit 1
        _old=$(stock_pid) || exit 1
        kill -TERM "$_old" || exit 1
        _tries=0
        while [ "$_tries" -lt 25 ]; do
            sleep 1
            _new=$(stock_pid) || _new=
            if [ -n "$_new" ] && [ "$_new" != "$_old" ] &&
               grep -F "$BASE/hk-bt-open-redirect.so" "/proc/$_new/maps" | grep -vq '(deleted)'; then break; fi
            _tries=$((_tries + 1))
        done
        [ "$_tries" -lt 25 ] || exit 1
    fi
    if [ "$keys" = 1 ] && [ ! -f "$VENDOR/hk-keys/disable" ]; then
        "$VENDOR/hk-keys/hk-mv-keys-boot.sh" || exit 1
    fi
    if [ "$lights" = 1 ] && [ ! -f "$VENDOR/hk-lights/disable" ]; then
        HK_LIGHTS_BIN="$VENDOR/hk-lights" "$VENDOR/hk-lights/hk-ap2-lights.sh" boot \
            </dev/null >/dev/null 2>&1 &
    fi
    if [ "$voice" = 1 ] && [ ! -f "$VENDOR/hk-voice/disable" ]; then
        "$VENDOR/hk-voice/hk-voice.sh" boot </dev/null >/dev/null 2>&1 &
    fi
    ;;
 verify)
    _tries=0
    if [ "$voice" = 1 ] && [ ! -f "$VENDOR/hk-voice/disable" ]; then
        while ! voice_up && [ "$_tries" -lt 20 ]; do sleep 1; _tries=$((_tries + 1)); done
        voice_up || exit 1
    fi
    if [ "$bluetooth" = 1 ]; then
        _pid=$(stock_pid) || exit 1
        grep -F "$BASE/hk-bt-open-redirect.so" "/proc/$_pid/maps" | grep -vq '(deleted)' || exit 1
    fi
    ;;
 *) exit 2;;
esac
