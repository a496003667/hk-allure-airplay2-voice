#!/bin/sh
# Replace AP2 files safely: stop AP2, unmount every runtime bind mount by path
# (including "(deleted)" entries), install each <file>.new while nothing is
# mounted, start AP2.  If it does not come up, restore the previous files.
#   hk-ap2-replace.sh /vendor/airplay2/ap2-56/shairport-sync.conf ...
LOG=/tmp/hk-ap2-replace.log
OPT=/tmp/hk-ap2/opt
RUN=/tmp/hk-ap2/run
TS=$(date +%Y%m%dT%H%M%S)
log() { echo "$(date '+%H:%M:%S') $*" >>"$LOG"; }

ap2_up() {
    i=0
    while [ "$i" -lt 90 ]; do
        "$OPT/bin/hk-ap2ctl" status 2>/dev/null | grep -q 'airplay port 7000: listening' &&
            grep -q real "$RUN/mode" 2>/dev/null &&
            kill -0 "$(cat "$RUN/safe/watchdog.pid" 2>/dev/null)" 2>/dev/null && return 0
        sleep 2
        i=$((i + 2))
    done
    return 1
}

release_runtime() {
    /vendor/airplay2/hk-ap2-prodctl stop >>"$LOG" 2>&1
    for round in 1 2 3; do
        for p in "$OPT/etc/ap2/shairport-sync.conf" "$OPT/bin/nqptp" "$OPT/bin/shairport-sync" \
            "$OPT/hk-ap2-audio-hook.sh" "$OPT/bin/hk-ap2ctl"; do
            umount "$p" 2>/dev/null && log "unmounted $p"
        done
    done
    grep -q " $OPT " /proc/mounts && umount "$OPT" 2>/dev/null
    ! grep -q ' /tmp/hk-ap2/opt' /proc/mounts
}

start_ap2() {
    sync; echo 1 >/proc/sys/vm/drop_caches
    /vendor/airplay2/hk-ap2-prodctl start >>"$LOG" 2>&1
    ap2_up
}

log "replace start: $*"
for f in "$@"; do [ -s "$f.new" ] || { log "missing $f.new"; exit 1; }; done
release_runtime || { log "runtime still mounted; abort"; start_ap2; exit 1; }
for f in "$@"; do
    cp -p "$f" "$f.before-$TS" && mv "$f.new" "$f" && log "installed $f ($(sha1sum "$f" | cut -c1-12))"
done
sync
if start_ap2; then
    log "AP2 up; fallback=$([ -f /tmp/hk-ap2-56.fallback ] && echo yes || echo no)"
    log done
    exit 0
fi
log "AP2 did not come up; restoring"
release_runtime
for f in "$@"; do cp -p "$f.before-$TS" "$f"; done
sync
start_ap2 && log "restored previous files" || log "restore: AP2 not up"
log done
