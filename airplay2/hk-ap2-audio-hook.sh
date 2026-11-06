#!/bin/sh

# The AP2 session hook only opens the speaker after a separate watchdog has
# published a fresh lease.  A missing/stale watchdog or a prior trip leaves
# the speaker muted.

ROOT=${HK_AP2_ROOT:-/tmp/hk-ap2/opt}
RUN=${HK_AP2_RUN:-/tmp/hk-ap2/run}
SAFE="$RUN/safe"
AMIXER=/system/workdir/bin/amixer
AMIXER_LIB=/system/workdir/lib
MASTER=${HK_AP2_MASTER_SAFE:-4}
DIGITAL=${HK_AP2_DIGITAL_SAFE:-128}
PLAYBACK=${HK_AP2_PLAYBACK_SAFE:-0}
USE_STOCK_PA=${HK_AP2_USE_STOCK_PA:-0}
APPLE_REMOTE=/system/workdir/bin/apple_remote
PA_UNMUTE_LIB=${HK_AP2_PA_UNMUTE_LIB:-$ROOT/lib/libhk-airplay-unmute.so}
PA_MUTE_LIB=${HK_AP2_PA_MUTE_LIB:-$ROOT/lib/libhk-airplay-mute.so}
PA_LOG="$RUN/pa-hook.log"
HOOK_LOG="$RUN/hook.log"
# Speaker hand-off with the on-device voice assistant, which calls this hook
# with HK_AP2_VOICE=1.  While it has the speaker (voice-owns), AirPlay's own
# start/stop calls must not touch the path: the start is deferred and the
# voice assistant hands the speaker over when its reply ends.
VOICE_OWNS="$RUN/voice-owns"
DEFERRED="$RUN/ap2-deferred"
CLAIM="$RUN/ap2-claim"
SPEAKER_LOCK="$RUN/speaker.lock"

# The AP2 loader must not leak its glibc path into stock shell commands.
unset LD_LIBRARY_PATH

hook_log() {
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || date)" "$*" \
        >>"$HOOK_LOG" 2>/dev/null || true
    size=$(wc -c <"$HOOK_LOG" 2>/dev/null || echo 0)
    case "$size" in
        ''|*[!0-9]*) return 0 ;;
    esac
    if [ "$size" -gt 65536 ]; then
        tail -c 32768 "$HOOK_LOG" >"$HOOK_LOG.tmp" 2>/dev/null &&
            mv "$HOOK_LOG.tmp" "$HOOK_LOG" 2>/dev/null || true
    fi
}

mixer_set() {
    env LD_LIBRARY_PATH="$AMIXER_LIB" "$AMIXER" -c 0 sset "$1" "$2" \
        >/dev/null 2>&1
}

mixer_cset() {
    env LD_LIBRARY_PATH="$AMIXER_LIB" "$AMIXER" -c 0 cset "numid=$1" "$2" \
        >/dev/null 2>&1
}

mixer_master() {
    env LD_LIBRARY_PATH="$AMIXER_LIB" "$AMIXER" -c 0 sget "Master" 2>/dev/null |
        sed -n 's/.*Front Left: \([0-9][0-9]*\).*/\1/p' |
        sed -n '1p'
}

mixer_values() {
    env LD_LIBRARY_PATH="$AMIXER_LIB" "$AMIXER" -c 0 cget "numid=$1" \
        2>/dev/null |
        sed -n 's/^[[:space:]]*: values=//p' |
        tail -n 1
}

switch_state() {
    env LD_LIBRARY_PATH="$AMIXER_LIB" "$AMIXER" -c 0 sget "$1" 2>/dev/null |
        sed -n 's/.*\[\(on\|off\)\].*/\1/p' |
        sed -n '1p'
}

speaker_lock() {
    n=0
    while ! mkdir "$SPEAKER_LOCK" 2>/dev/null; do
        n=$((n + 1))
        if [ "$n" -ge 30 ]; then             # stale lock (holder killed)
            rmdir "$SPEAKER_LOCK" 2>/dev/null
            mkdir "$SPEAKER_LOCK" 2>/dev/null
            return 0
        fi
        usleep 100000 2>/dev/null || sleep 1
    done
}

speaker_unlock() { rmdir "$SPEAKER_LOCK" 2>/dev/null || true; }

# The voice flag holds the uptime it was taken; ignore it after 5 minutes
# in case the voice assistant died without clearing it.
voice_owns() {
    t=$(sed -n 1p "$VOICE_OWNS" 2>/dev/null)
    case "$t" in ''|*[!0-9]*) return 1 ;; esac
    now=$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0)
    [ $((now - t)) -lt 300 ]
}

read_file() {
    [ -f "$1" ] && sed -n '1p' "$1" 2>/dev/null || true
}

watchdog_alive() {
    pid=$(read_file "$SAFE/watchdog.pid")
    heartbeat=$(read_file "$SAFE/watchdog.heartbeat")
    [ -n "$pid" ] && [ -n "$heartbeat" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    current=$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0)
    [ $((current - heartbeat)) -le 4 ]
}

stock_pa_mute() {
    level=$1
    if [ "$level" = 0 ]; then
        preload=$PA_UNMUTE_LIB
    else
        preload=$PA_MUTE_LIB
    fi
    [ -x "$APPLE_REMOTE" ] && [ -r "$preload" ] || return 1

    rm -f /tmp/RequestAppleRemote
    : >"$PA_LOG"
    env LD_LIBRARY_PATH="$AMIXER_LIB:/lib" LD_PRELOAD="$preload" \
        "$APPLE_REMOTE" >>"$PA_LOG" 2>&1 &
    pid=$!
    # The preload constructor does the work right away and logs it; poll
    # for that line instead of a fixed 1 s wait (cap 1.5 s).
    n=0
    while [ "$n" -lt 15 ]; do
        usleep 100000 2>/dev/null || sleep 1
        grep -q "Snd_Ctrl airplay to mute $level" "$PA_LOG" 2>/dev/null && break
        n=$((n + 1))
    done
    usleep 100000 2>/dev/null || true
    rm -f /tmp/RequestAppleRemote
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true

    # apple_remote may exit after the constructor because one stock helper
    # symbol is unavailable. The constructor runs first and is the operation
    # this hook needs.
    tail -n 8 "$PA_LOG" 2>/dev/null |
        grep -q "Snd_Ctrl airplay to mute $level"
}

hard_mute() {
    # Fade out with the DAC's ramped digital volume first, so the amplifier is
    # switched off on silence; only then stop the I2S data.  Cutting the data
    # or switching the amplifier while audio is flowing is audible as a pop.
    mixer_cset 1 0,0 || true
    usleep 100000 2>/dev/null || true
    mixer_set "Ext Spk" off || true
    mixer_set "aml audio i2s mute" on || true
    # Leave "Playback Volume" (numid=2, the DAC's 0/-6 dB analog gain step)
    # alone: switching it on every pause/stop was audible as a pop, and the
    # watchdog switched it straight back during the playback lease anyway.
    mixer_set "Digital" off || true
    mixer_set "Master" 0 || true
}

clear_play_lease() {
    rm -f "$SAFE/play.started" "$SAFE/play.started.tmp"
}

publish_play_lease() {
    printf '%s\n' "$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0)" \
        >"$SAFE/play.started.tmp" 2>/dev/null &&
        mv "$SAFE/play.started.tmp" "$SAFE/play.started" 2>/dev/null
}

watchdog_heartbeat() {
    read_file "$SAFE/watchdog.heartbeat"
}

wait_for_lease_ack() {
    previous_heartbeat=$1
    tries=0
    while [ "$tries" -lt 40 ]; do           # poll every 0.1 s, up to 4 s
        usleep 100000 2>/dev/null || sleep 1
        watchdog_alive || return 1
        current_heartbeat=$(watchdog_heartbeat)
        case "$current_heartbeat" in
            ''|*[!0-9]*) return 1 ;;
        esac
        [ "$current_heartbeat" -gt "$previous_heartbeat" ] && return 0
        tries=$((tries + 1))
    done
    return 1
}

start_failed() {
    reason=$1
    hook_log "start failed: $reason"
    hard_mute
    [ "$USE_STOCK_PA" != "1" ] || stock_pa_mute 1 || true
    clear_play_lease
    mixer_set "Auto Mute" on || true
    exit 1
}

case "${1:-}" in
    start)
        hook_log "start requested armed=$([ -f "$SAFE/armed" ] && echo yes || echo no) tripped=$([ -f "$SAFE/tripped" ] && echo yes || echo no)"
        if [ -z "${HK_AP2_VOICE:-}" ]; then
            speaker_lock
            if voice_owns; then
                : >"$DEFERRED"
                speaker_unlock
                hook_log "start deferred: the voice assistant has the speaker"
                exit 0
            fi
            cut -d. -f1 /proc/uptime >"$CLAIM" 2>/dev/null
            speaker_unlock
        fi
        [ -f "$SAFE/armed" ] || exit 1
        [ ! -f "$SAFE/tripped" ] || exit 1
        watchdog_alive || exit 1

        # Publish the lease before touching the mixer.  Otherwise the watchdog
        # can observe "no lease" between the baseline writes and the later
        # lease creation and hard-mute the path underneath this hook.
        previous_heartbeat=$(watchdog_heartbeat)
        case "$previous_heartbeat" in
            ''|*[!0-9]*) start_failed "invalid watchdog heartbeat" ;;
        esac
        publish_play_lease || start_failed "could not publish playback lease"
        wait_for_lease_ack "$previous_heartbeat" ||
            start_failed "watchdog did not acknowledge playback lease"

        # Configure the low-volume path while the speaker is still off.
        mixer_set "Ext Spk" off || true
        mixer_set "Master" "$MASTER" || true
        mixer_cset 1 "$DIGITAL,$DIGITAL" || true
        mixer_cset 2 "$PLAYBACK,$PLAYBACK" || true
        mixer_set "Digital" on || true
        mixer_set "Auto Mute" off || true
        mixer_set "aml audio i2s mute" on || true

        [ "$(mixer_master)" = "$MASTER" ] || {
            start_failed "Master verification failed"
        }
        [ "$(mixer_values 1)" = "$DIGITAL,$DIGITAL" ] || {
            start_failed "Digital Playback Volume verification failed"
        }
        [ "$(mixer_values 2)" = "$PLAYBACK,$PLAYBACK" ] || {
            start_failed "Playback Volume verification failed"
        }
        [ "$(switch_state 'Digital')" = "on" ] || {
            start_failed "Digital switch verification failed"
        }
        [ "$(switch_state 'aml audio i2s mute')" = "on" ] || {
            start_failed "I2S mute baseline verification failed"
        }
        [ "$(switch_state 'Ext Spk')" = "off" ] || {
            start_failed "external speaker mute baseline verification failed"
        }
        hook_log "mixer baseline verified master=$MASTER digital=$DIGITAL playback=$PLAYBACK"

        [ "$(read_file "$SAFE/watchdog.pid")" ] ||
            start_failed "watchdog pid disappeared"
        watchdog_alive || start_failed "watchdog stopped during baseline"
        watchdog_alive || start_failed "watchdog stopped before output enable"
        [ ! -f "$SAFE/tripped" ] || start_failed "watchdog tripped before output enable"
        # Open the path while the DAC is digitally silent: start the I2S data
        # while the stock PA is still muted, then unmute the PA, then switch
        # the amplifier on and fade in by restoring the digital volume (the
        # DAC ramps it).  Unmuting the PA before the I2S data starts pops
        # audibly (verified step by step with the user, 2026-11-04).
        mixer_cset 1 0,0 || start_failed "digital pre-mute failed"
        mixer_set "aml audio i2s mute" off || start_failed "I2S unmute failed"
        [ "$(switch_state 'aml audio i2s mute')" = "off" ] ||
            start_failed "I2S unmute verification failed"
        usleep 200000 2>/dev/null || true
        if [ "$USE_STOCK_PA" = "1" ]; then
            stock_pa_mute 0 || start_failed "stock PA unmute failed"
            hook_log "stock PA unmuted"
        fi
        mixer_set "Ext Spk" on || start_failed "external speaker enable failed"
        [ "$(switch_state 'Ext Spk')" = "on" ] ||
            start_failed "external speaker enable verification failed"
        usleep 200000 2>/dev/null || sleep 1
        mixer_cset 1 "$DIGITAL,$DIGITAL" || start_failed "digital fade-in failed"
        [ "$(mixer_values 1)" = "$DIGITAL,$DIGITAL" ] ||
            start_failed "digital fade-in verification failed"
        hook_log "speaker enabled"
        exit 0
        ;;
    stop|mute)
        if [ -z "${HK_AP2_VOICE:-}" ]; then
            rm -f "$DEFERRED" "$CLAIM"
            if voice_owns; then
                hook_log "${1:-stop} ignored: the voice assistant has the speaker"
                exit 0
            fi
        fi
        hook_log "${1:-stop} requested"
        hard_mute
        [ "$USE_STOCK_PA" != "1" ] || stock_pa_mute 1 || true
        clear_play_lease
        mixer_set "Auto Mute" on || true
        exit 0
        ;;
    *)
        exit 2
        ;;
esac
