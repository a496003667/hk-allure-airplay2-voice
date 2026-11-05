#!/bin/sh
# Runtime-only removal of stock OTA and Alexa helpers.  Nothing on flash is
# modified; a reboot (or "revert") restores the stock behaviour.
#
#  a01remoteupdate  OTA client incl. the nightly silent-upgrade window.
#                   Started once by rootApp via system(); not respawned.
#  alexa_alert      Alexa alarms/timers/reminders (plays its own tones).
#                   Started once; not respawned.
#  asr_mrm          Alexa multi-room music.  rootApp respawns it 1 s after it
#                   exits while /system/workdir/bin/asr_mrm exists, so a
#                   sleeping stub is bind-mounted over the binary first.
#
# asr_tts (Alexa client, but also the microphone capture path that a local
# Xiaoai replacement needs) is deliberately left alone.

D=${HK_DEBLOAT_DIR:-/tmp/hk-debloat}
STUB=$D/asr_mrm-stub
MRM=/system/workdir/bin/asr_mrm
LOG=$D/debloat.log

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S' 2>/dev/null) $*" | tee -a "$LOG"; }

mrm_stubbed() { grep -q " $MRM " /proc/mounts; }

stop_named() {
    name=$1
    pids=$(pidof "$name")
    [ -n "$pids" ] || { log "$name: not running"; return 0; }
    kill $pids 2>/dev/null
    i=0
    while [ "$i" -lt 5 ] && pidof "$name" >/dev/null; do sleep 1; i=$((i + 1)); done
    pidof "$name" >/dev/null && kill -9 $(pidof "$name") 2>/dev/null
    sleep 1
    if pidof "$name" >/dev/null; then
        log "$name: still running pid=$(pidof "$name")"
        return 1
    fi
    log "$name: stopped (was $pids)"
}

stop_real_mrm() {
    # The stub is also called asr_mrm; only stop processes running the
    # real binary (their cmdline is "asr_mrm", the stub's is "/bin/sh ...").
    for pid in $(pidof asr_mrm); do
        case "$(tr '\0' ' ' </proc/$pid/cmdline 2>/dev/null)" in
            *asr_mrm-stub*|/bin/sh*|sh\ *) ;;
            *) kill "$pid" 2>/dev/null && log "asr_mrm: sent TERM to $pid" ;;
        esac
    done
}

apply() {
    mkdir -p "$D"
    stop_named a01remoteupdate
    stop_named alexa_alert

    if ! mrm_stubbed; then
        printf '#!/bin/sh\n# hk-debloat placeholder for asr_mrm\nwhile :; do sleep 3600; done\n' >"$STUB"
        chmod 755 "$STUB"
        mount -o bind "$STUB" "$MRM" || { log "asr_mrm: bind mount failed"; return 1; }
        log "asr_mrm: stub bind-mounted"
    fi
    stop_real_mrm
    sleep 3
    status
}

revert() {
    if mrm_stubbed; then
        umount "$MRM" && log "asr_mrm: stub unmounted"
        # Stopping the stub lets rootApp's guard thread start the real one.
        for pid in $(pidof asr_mrm); do
            case "$(tr '\0' ' ' </proc/$pid/cmdline 2>/dev/null)" in
                /bin/sh*|sh\ *) kill "$pid" 2>/dev/null ;;
            esac
        done
    fi
    if ! pidof a01remoteupdate >/dev/null; then
        (cd /system/workdir && a01remoteupdate >/dev/null 2>&1 &)
        log "a01remoteupdate: restarted"
    fi
    if ! pidof alexa_alert >/dev/null; then
        (cd /system/workdir && alexa_alert >/dev/null 2>&1 &)
        log "alexa_alert: restarted"
    fi
}

status() {
    for name in a01remoteupdate alexa_alert asr_mrm asr_tts rootApp a01controller mv_ioguard; do
        pids=$(pidof "$name")
        cmd=""
        for pid in $pids; do cmd="$cmd [$(tr '\0' ' ' </proc/$pid/cmdline 2>/dev/null)]"; done
        echo "$name: ${pids:-not running}$cmd"
    done
    mrm_stubbed && echo "asr_mrm stub: mounted" || echo "asr_mrm stub: not mounted"
    grep -E 'MemFree|MemAvailable' /proc/meminfo
}

boot() {
    # Boot-time: the two helpers rootApp starts once with system(), plus the
    # asr_mrm stub (see below).
    mkdir -p "$D"
    [ -f /vendor/hk-debloat/disable ] && { log "boot: disabled by flag"; return 0; }
    i=0
    while [ "$i" -lt 180 ]; do
        pidof a01remoteupdate >/dev/null && pidof alexa_alert >/dev/null && break
        sleep 2
        i=$((i + 2))
    done
    sleep 10
    stop_named a01remoteupdate
    stop_named alexa_alert
    # asr_mrm (Alexa multi-room) is unused; the runtime stub ran without
    # problems on 2026-11-04, so apply it at boot too.  To keep the stock
    # asr_mrm: touch /vendor/hk-debloat/keep-mrm
    if [ ! -f /vendor/hk-debloat/keep-mrm ] && ! mrm_stubbed; then
        printf '#!/bin/sh\n# hk-debloat placeholder for asr_mrm\nwhile :; do sleep 3600; done\n' >"$STUB"
        chmod 755 "$STUB"
        if mount -o bind "$STUB" "$MRM"; then
            log "asr_mrm: stub bind-mounted"
            stop_real_mrm
        else
            log "asr_mrm: bind mount failed"
        fi
    fi
}

case "${1:-}" in
    apply) apply ;;
    boot) boot ;;
    revert) revert ;;
    status) status ;;
    *) echo "usage: $0 apply|boot|revert|status"; exit 2 ;;
esac
