#!/bin/sh
# Boot: restart the stock mv_ioguard with the AirPlay 2 key hook preloaded.
# rootApp respawns mv_ioguard through PATH, where /tmp/bin comes first.
# Runs only once the AP2 safety watchdog is up (restarting mv_ioguard makes
# the stock firmware reprogram the codec volume).  On any failure the
# wrapper is removed so the stock binary runs unhooked.
# Skip: touch /vendor/hk-keys/disable
D=/vendor/hk-keys
LIB=$D/libhk-mv-ap2-keys.so
WRAP=/tmp/bin/mv_ioguard
LOG=/tmp/hk-keys/boot.log
SAFE=/tmp/hk-ap2/run/safe

mkdir -p /tmp/hk-keys /tmp/bin
log() { echo "$(date '+%Y-%m-%dT%H:%M:%S' 2>/dev/null) $*" >>"$LOG"; }

[ -f "$D/disable" ] && { log "disabled"; exit 0; }
[ -r "$LIB" ] || { log "missing $LIB"; exit 1; }

watchdog_alive() {
    pid=$(cat "$SAFE/watchdog.pid" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

i=0
while [ "$i" -lt 600 ] && ! { watchdog_alive && pidof mv_ioguard >/dev/null; }; do
    sleep 2
    i=$((i + 2))
done
watchdog_alive || { log "AP2 watchdog not running; leaving mv_ioguard stock"; exit 1; }
sleep 5

old=$(pidof mv_ioguard)
printf '#!/bin/sh\n# AirPlay 2 key hook; inert unless /tmp/hk-ap2/run/buttons.forward exists.\nexport LD_PRELOAD=%s\nexec /system/workdir/bin/mv_ioguard "$@"\n' "$LIB" >"$WRAP.tmp" &&
    chmod 755 "$WRAP.tmp" && mv "$WRAP.tmp" "$WRAP"
log "wrapper installed; restarting mv_ioguard pid=$old"
kill -TERM "$old"
i=0
while [ "$i" -lt 15 ]; do
    sleep 1
    new=$(pidof mv_ioguard)
    if [ -n "$new" ] && [ "$new" != "$old" ] && grep -q libhk-mv-ap2-keys "/proc/$new/maps" 2>/dev/null; then
        log "mv_ioguard pid=$new running with key hook"
        exit 0
    fi
    i=$((i + 1))
done
log "key hook did not come up (pid=$(pidof mv_ioguard)); removing wrapper"
rm -f "$WRAP"
p=$(pidof mv_ioguard)
if [ -n "$p" ] && grep -q libhk-mv-ap2-keys "/proc/$p/maps" 2>/dev/null; then
    kill -TERM "$p"
fi
exit 1
