#!/bin/sh
# Bridge stock Bluetooth playback to the shared, supervised speaker path.
# The AP2 watchdog independently revokes this lease if this process dies.
set -u
PATH=/system/workdir/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH
ROOT=${HK_AP2_ROOT:-/tmp/hk-ap2/opt}
RUN=${HK_AP2_RUN:-/tmp/hk-ap2/run}
SAFE=$RUN/safe
REQ=$ROOT/bin/hk-unix-request-raw
HOOK=$ROOT/hk-ap2-audio-hook.sh
TEST=$ROOT/bin/hk-ap2-safe-test
OWNER=$RUN/bt-owner.pid
HEARTBEAT=$RUN/bt-heartbeat
RELEASE=$RUN/bt-release
WAIT_PCM=$RUN/bt-wait-pcm
VOICE_INJECT=$RUN/voice-inject/active
LOCK=$RUN/speaker.lock
LOG=$RUN/bt-bridge.log
PCM=${HK_BT_PCM_STATUS:-/proc/asound/card0/pcm0p/sub0/status}
PLAYER_SOCKET=${HK_BT_PLAYER_SOCKET:-/tmp/Requesta01controller}
COMMON=${HK_AUDIO_COMMON:-/vendor/airplay2/hk-audio-common.sh}
[ -r "$COMMON" ] || COMMON=$(dirname "$0")/hk-audio-common.sh
[ -r "$COMMON" ] || exit 1
. "$COMMON"
active=0
child=
HOOK_POLL_LIMIT=12
command -v usleep >/dev/null 2>&1 && HOOK_POLL_LIMIT=60

now() { audio_now; }
log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >>"$LOG"; audio_trim_log "$LOG"; }
heartbeat() { now >"$HEARTBEAT.tmp" && mv "$HEARTBEAT.tmp" "$HEARTBEAT"; }

watchdog_live() {
    wd=$(sed -n '1p' "$SAFE/watchdog.pid" 2>/dev/null)
    beat=$(sed -n '1p' "$SAFE/watchdog.heartbeat" 2>/dev/null)
    case "$wd:$beat" in ''|*[!0-9:]*|:*|*:) return 1;; esac
    kill -0 "$wd" 2>/dev/null || return 1
    current=$(now)
    [ "$beat" -le "$current" ] && [ $((current - beat)) -le 4 ]
}

bt_playing() {
    bt_pcm_live || return 1
    bt_source_playing
}

bt_source_playing() {
    watchdog_live || return 1
    [ -x "$REQ" ] && [ -S "$PLAYER_SOCKET" ] || return 1
    data=$("$REQ" "$PLAYER_SOCKET" getPlayerStatus 1200 2>/dev/null) || return 1
    if printf '%s' "$data" | grep -q '"mode":"-1"'; then
        # A confirmed disconnect ends the paused stream's generation. A
        # subsequent connection may use the initial unowned-PCM repair.
        rm -f "$WAIT_PCM"
        return 1
    fi
    printf '%s' "$data" | grep -q '"mode":"41"' || return 1
    printf '%s' "$data" | grep -q '"status":"play"' || return 1
    printf '%s' "$data" | grep -q '"mute":"0"' || return 1
    vol=$(printf '%s' "$data" | sed -n 's/.*"vol":"\([0-9][0-9]*\)".*/\1/p')
    case "$vol" in ''|*[!0-9]*) return 1;; esac
    [ "$vol" -le 100 ] || return 1
}

bt_pcm_live() {
    grep -q '^state: RUNNING' "$PCM" || return 1
    owner=$(sed -n 's/^owner_pid[[:space:]]*:[[:space:]]*//p' "$PCM")
    case "$owner" in ''|*[!0-9]*) return 1;; esac
    audio_bt_pcm_owned "$owner"
}

stall_since=
last_stall_repair=0
last_stall_check=-1
last_pause_check=-1

refresh_paused_source() {
    [ -f "$WAIT_PCM" ] || return 0
    _rp_now=$(now)
    [ "$_rp_now" -gt "$last_pause_check" ] || return 0
    last_pause_check=$_rp_now
    # Keep the existing source-generation/disconnect observation even when
    # the fast playback probe rejects a stopped PCM before querying stock.
    bt_source_playing || true
}

repair_stalled_bluetooth() {
    # a01controller can keep reporting "play" after the A2DP stream stalls:
    # metadata and totlen are present, but it never opens the PCM and no audio
    # reaches /tmp/bt-avk-fifo. Rebuild that transport with its own pause and
    # resume commands. Keep the speaker path closed throughout.
    [ "${HK_BT_STALL_REPAIR:-1}" = 1 ] || return 1
    [ ! -f "$WAIT_PCM" ] || return 1
    # A retained XRUN PCM is also normal after a phone pause. Never send
    # pause/resume based on stale "play" metadata in that state.
    case "$(sed -n '1p' "$PCM" 2>/dev/null)" in
        closed|'state: closed'|'state: CLOSED') ;;
        *) return 1;;
    esac
    current=$(now)
    # Fast idle wakeups must not multiply stock-player RPC traffic while
    # disconnected. Transport repair still samples metadata at most once/s.
    [ "$current" -gt "$last_stall_check" ] || return 1
    last_stall_check=$current
    bt_source_playing || { stall_since=; return 1; }
    bt_pcm_live && { stall_since=; return 1; }
    [ -n "$stall_since" ] || stall_since=$current
    [ $((current - stall_since)) -ge "${HK_BT_STALL_SECONDS:-4}" ] || return 1
    # Never turn a recurring broken transport into a pause/resume loop.
    [ $((current - last_stall_repair)) -ge "${HK_BT_STALL_COOLDOWN:-30}" ] || return 1
    log "Bluetooth reports play without PCM; rebuilding A2DP transport"
    if "$REQ" "$PLAYER_SOCKET" setPlayerCmd:onepause 1200 >/dev/null 2>&1; then
        sleep 1
        "$REQ" "$PLAYER_SOCKET" setPlayerCmd:resume 1200 >/dev/null 2>&1 ||
            log "Bluetooth A2DP resume command failed"
    else
        log "Bluetooth A2DP pause command failed"
    fi
    last_stall_repair=$current
    stall_since=
    return 0
}

ap2_recent_claim() {
    stamp=$(sed -n '1p' "$RUN/ap2-claim" 2>/dev/null)
    case "$stamp" in ''|*[!0-9]*) return 1;; esac
    current=$(now)
    [ "$stamp" -le "$current" ] && [ $((current - stamp)) -lt 8 ]
}

lock_speaker() { audio_lock "$LOCK"; }

run_hook() {
    action=$1
    bt_master=4
    if [ "$action" = start ]; then
        # Follow the stock player volume on the 0..32 Master scale instead
        # of forcing Master to 4 each time the Bluetooth path opens.
        case "${vol:-}" in ''|*[!0-9]*) return 1;; esac
        [ "$vol" -le 100 ] || return 1
        bt_master=$(((vol * 32 + 50) / 100))
        # Match the stock 33-level curve: anything above 0% uses at least its
        # lowest non-silent step instead of rounding 1% down to mute.
        if [ "$vol" -gt 0 ] && [ "$bt_master" -lt 1 ]; then
            bt_master=1
        fi
    fi
    # PCM5121 uses an inverted digital-volume scale: 0 is mute and 207 is
    # 0 dB. Keep the analogue step at the stock -6 dB setting and apply the
    # stock 33-step Master control before the Harman limiter.
    env HK_AP2_BT=1 HK_AUDIO_TOKEN="${BT_TOKEN:-}" HK_AP2_USE_STOCK_PA=1 HK_AP2_MASTER_SAFE="$bt_master" \
        HK_AP2_DIGITAL_SAFE=207 HK_AP2_PLAYBACK_SAFE=0 "$HOOK" "$action" &
    child=$!
    count=0
    hook_deadline=$(($(now) + 12))
    while kill -0 "$child" 2>/dev/null; do
        heartbeat || return 1
        if [ "$action" = start ]; then
            if ! { [ -f "$SAFE/armed" ] && [ ! -f "$SAFE/tripped" ] &&
                [ ! -f "$RELEASE" ] && ! audio_voice_pending &&
                bt_playing; }; then
                kill "$child" 2>/dev/null || true
                wait "$child" 2>/dev/null || true
                child=
                # A pause or track change can stop the source mid-start.
                # It is safe to close and retry only while the watchdog is
                # healthy and there is no competing speaker owner or trip.
                if [ -f "$SAFE/armed" ] && [ ! -f "$SAFE/tripped" ] &&
                   [ ! -f "$RELEASE" ] && ! audio_voice_pending &&
                   watchdog_live; then
                    return 10
                fi
                return 1
            fi
        fi
        count=$((count + 1))
        [ "$count" -lt "$HOOK_POLL_LIMIT" ] && [ "$(now)" -lt "$hook_deadline" ] || { kill -KILL "$child" 2>/dev/null || true; wait "$child" 2>/dev/null || true; child=; return 1; }
        audio_delay 200000
    done
    wait "$child"; rc=$?
    child=
    return "$rc"
}

release_output() {
    [ "$active" = 1 ] || return 0
    # After any established stream stops, wait for a new valid PCM rather
    # than resuming a phone that may have deliberately paused playback.
    if ! { printf '%s\n' "${BT_TOKEN:-}" >"$WAIT_PCM.tmp" && mv "$WAIT_PCM.tmp" "$WAIT_PCM"; }; then
        "$TEST" mute >/dev/null 2>&1 || true
        log 'could not record Bluetooth pause; manual mute latched'
    fi
    if ! run_hook stop; then
        "$TEST" mute >/dev/null 2>&1 || true
        log 'stop hook failed; manual mute latched'
    fi
    rm -f "$OWNER" "$HEARTBEAT" "$RELEASE"
    active=0
    log 'Bluetooth released output'
}

cleanup() {
    trap - EXIT HUP INT TERM
    if [ -n "$child" ]; then
        "$TEST" mute >/dev/null 2>&1 || true
        kill -TERM "$child" 2>/dev/null || true
        child=
    fi
    release_output
    rm -f "$RUN/bt-bridge.pid"
}

trap cleanup EXIT
trap 'exit 0' HUP INT TERM

case "${1:-}" in __run) ;; *) echo "usage: $0 __run" >&2; exit 2;; esac
echo 11 >"/proc/$$/oom_score_adj" 2>/dev/null || true
[ ! -f "$OWNER" ] || { "$TEST" mute >/dev/null 2>&1 || true; exit 1; }
echo "$$" >"$RUN/bt-bridge.pid"
log "bridge started pid=$$"
while [ -f "$SAFE/armed" ]; do
    if [ "$active" = 0 ] && [ ! -f "$SAFE/tripped" ] &&
       [ ! -f "$RELEASE" ] && ! audio_voice_pending &&
       [ ! -s "$SAFE/play.started" ] && ! ap2_recent_claim &&
       watchdog_live; then
        refresh_paused_source
        repair_stalled_bluetooth || true
    fi
    if [ "$active" = 1 ]; then
        heartbeat || { "$TEST" mute >/dev/null 2>&1 || true; break; }
        if [ -f "$SAFE/tripped" ] || [ -f "$RELEASE" ] || [ -f "$WAIT_PCM" ] ||
           [ ! -s "$SAFE/play.started" ] ||
           ! bt_playing; then
            release_output
        fi
    elif [ ! -f "$SAFE/tripped" ] && [ ! -f "$RELEASE" ] &&
         ! audio_voice_pending && [ ! -s "$SAFE/play.started" ] &&
         ! ap2_recent_claim && bt_playing; then
        if lock_speaker; then
            if [ ! -f "$SAFE/tripped" ] && [ ! -f "$RELEASE" ] &&
               ! audio_voice_pending && [ ! -s "$SAFE/play.started" ] &&
               ! ap2_recent_claim; then
                rm -f "$WAIT_PCM" || { audio_unlock "$LOCK"; "$TEST" mute >/dev/null 2>&1 || true; break; }
                BT_SEQ=$((${BT_SEQ:-0} + 1))
                BT_TOKEN="bt-$$-$(now)-$BT_SEQ"
                heartbeat && printf '%s\n' "$$" >"$OWNER.tmp" && mv "$OWNER.tmp" "$OWNER" && active=1
            fi
            audio_unlock "$LOCK" 2>/dev/null || true
        fi
        if [ "$active" = 1 ]; then
            if run_hook start; then
                log 'Bluetooth acquired output at conservative volume'
            else
                rc=$?
                if [ "$rc" = 10 ]; then
                    release_output
                    log 'Bluetooth source stopped during start; output closed'
                else
                    "$TEST" mute >/dev/null 2>&1 || true
                    release_output
                    log 'Bluetooth start failed; manual mute latched'
                fi
            fi
        fi
    fi
    if [ "$active" = 0 ]; then audio_delay 200000; else sleep 1; fi
done
