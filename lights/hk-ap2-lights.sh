#!/bin/sh
# AP2 -> stock light/display bridge.  Never touches the mixer or audio.
#
#  * While a playback lease is held (Shairport is playing), mark the stock play state as
#    playing (hk-shm-play on) so mv_ioguard shows its HK_Playing animation.
#    The stock firmware itself suppresses that animation in privacy (mic
#    off) mode.  After 3 s without a running stream the state is undone.
#  * When Shairport reports a new AirPlay volume (volume.log), send the
#    display-only GNOTIFY=VOLUMECHANGE<0..100> to mv_ioguard.
#  * While a sender holds an RTSP connection on port 7000 and the local MQTT
#    broker is listening, keep $RUN/buttons.forward so the mv_ioguard key
#    hook sends the volume/play keys to the sender instead of the stock
#    player.
#
# Run:  setsid sh hk-ap2-lights.sh run   Stop:  hk-ap2-lights.sh stop
# Touch $D/disable to make the loop idle (it releases the play state).
# boot: wait for the AP2 runtime, then run (used by S99hk-zlights; skipped
# when /vendor/hk-lights/disable exists).

D=${HK_LIGHTS_DIR:-/tmp/hk-lights}
SHM=${HK_LIGHTS_BIN:-$D}/hk-shm-play
REQUEST=/tmp/hk-ap2/opt/bin/hk-unix-request-raw
RUN=/tmp/hk-ap2/run
PCM_STATUS=/proc/asound/card0/pcm0p/sub0/status
PIDFILE=$D/lights.pid
LOG=$D/lights.log

log() {
    echo "$(date '+%Y-%m-%dT%H:%M:%S' 2>/dev/null) $*" >>"$LOG"
    size=$(wc -c <"$LOG" 2>/dev/null || echo 0)
    if [ "$size" -gt 65536 ]; then
        tail -c 16384 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
}

shairport_running_pcm() {
    # Shairport keeps the PCM running (feeding silence) for the whole active
    # session to avoid DAC pops, so "playing" is the audio hook's playback
    # lease: created when play begins, removed when it ends.
    pid=$(sed -n 1p "$RUN/shairport.pid" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    [ -f "$RUN/safe/play.started" ]
}

sender_connected() {
    # ESTABLISHED (01) with local port 7000 (0x1B58), IPv4 or IPv6.
    grep -qE '^ *[0-9]+: [0-9A-F]+:1B58 [0-9A-F]+:[0-9A-F]+ 01 ' \
        /proc/net/tcp /proc/net/tcp6 2>/dev/null
}

broker_listening() {
    # 127.0.0.1:1883 (0100007F:075B) in LISTEN (0A).
    grep -qE '^ *[0-9]+: 0100007F:075B [0-9A-F]+:[0-9A-F]+ 0A ' /proc/net/tcp 2>/dev/null
}

update_forward_flag() {
    if [ ! -f "$D/disable" ] && sender_connected && broker_listening; then
        if [ -d "$RUN" ] && [ ! -f "$RUN/buttons.forward" ]; then
            : >"$RUN/buttons.forward" && log "buttons -> AirPlay sender"
        fi
    elif [ -f "$RUN/buttons.forward" ]; then
        rm -f "$RUN/buttons.forward" && log "buttons -> stock player"
    fi
}

play_status() {
    "$SHM" get 2>/dev/null | sed -n 's/.* play_status=\([0-9-]*\) .*/\1/p'
}

claim() {
    pidof imuzop >/dev/null 2>&1 && { log "skip claim: imuzop running"; return 1; }
    status=$(play_status)
    case "$status" in
        0|3) ;;
        *) log "skip claim: play_status=$status"; return 1 ;;
    esac
    log "claim: $("$SHM" on)"
    [ "$(play_status)" = 4 ]
}

release() {
    log "release: $("$SHM" off)"
}

notify_volume() {
    # AirPlay volume: -30..0 dB, -144 = mute.  Display scale 0..100.
    level=$(echo "$1" | awk '{v=$1; if (v<-30) v=-30; if (v>0) v=0;
        printf "%d", (v+30)*100/30 + 0.5}')
    case "$level" in ''|*[!0-9]*) return ;; esac
    "$REQUEST" /tmp/RequestIOGuard "GNOTIFY=VOLUMECHANGE$level" \
        >/dev/null 2>&1 &
    req=$!
    sleep 1
    kill "$req" 2>/dev/null
    wait "$req" 2>/dev/null
    log "volume $1 -> display $level"
}

run() {
    mkdir -p "$D"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "already running"; exit 1
    fi
    echo $$ >"$PIDFILE"
    owned=0
    idle=0
    last_volume=$(tail -n 1 "$RUN/volume.log" 2>/dev/null)
    trap '[ "$owned" = 1 ] && release; rm -f "$PIDFILE" "$RUN/buttons.forward"; log stopped; exit 0' INT TERM
    log "started pid=$$"
    while :; do
        sleep 1
        update_forward_flag
        if [ -f "$D/disable" ]; then
            [ "$owned" = 1 ] && { release; owned=0; }
            continue
        fi

        if shairport_running_pcm; then
            idle=0
            if [ "$owned" = 0 ]; then
                claim && owned=1
            elif [ "$(play_status)" != 4 ]; then
                # Something else changed the stock state.  Never leave a 5
                # behind: a01controller turns 5 into setPlayerCmd:resume.
                log "state changed under us: play_status=$(play_status)"
                release
                owned=0
            fi
        elif [ "$owned" = 1 ]; then
            idle=$((idle + 1))
            if [ "$idle" -ge 3 ]; then
                release
                owned=0
            fi
        fi

        volume=$(tail -n 1 "$RUN/volume.log" 2>/dev/null)
        if [ -n "$volume" ] && [ "$volume" != "$last_volume" ]; then
            last_volume=$volume
            notify_volume "${volume#* }"
        fi
    done
}

boot() {
    [ -f /vendor/hk-lights/disable ] && exit 0
    i=0
    while [ "$i" -lt 300 ] && [ ! -x "$REQUEST" ]; do
        sleep 5
        i=$((i + 5))
    done
    run
}

case "${1:-}" in
    run) run ;;
    boot) boot ;;
    stop)
        [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null
        ;;
    status)
        if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
            echo "running pid=$(cat "$PIDFILE")"
        else
            echo "stopped"
        fi
        "$SHM" get
        tail -n 5 "$LOG" 2>/dev/null
        ;;
    *) echo "usage: $0 run|boot|stop|status"; exit 2 ;;
esac
