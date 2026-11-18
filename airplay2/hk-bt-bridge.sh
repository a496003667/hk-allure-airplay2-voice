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
MQTT=${HK_BT_MQTT:-/vendor/hk-voice/hk-mqtt-pub}
OWNER=$RUN/bt-owner.pid
HEARTBEAT=$RUN/bt-heartbeat
RELEASE=$RUN/bt-release
WAIT_PCM=$RUN/bt-wait-pcm
PAUSE_PENDING=$RUN/bt-pause-pending
VOICE_INJECT=$RUN/voice-inject/active
LOCK=$RUN/speaker.lock
BRIDGE_LOCK=$RUN/bt-bridge.lock
LOG=$RUN/bt-bridge.log
PCM=${HK_BT_PCM_STATUS:-/proc/asound/card0/pcm0p/sub0/status}
PLAYER_SOCKET=${HK_BT_PLAYER_SOCKET:-/tmp/Requesta01controller}
BT_PCM_CONTROL=${HK_BT_PCM_CONTROL:-${RUN%/run}/bt-pcm-control}
STOCK_AUDIO_STATE=${HK_AP2_STOCK_AUDIO_STATE:-/vendor/airplay2/hk-stock-audio-state}
COMMON=${HK_AUDIO_COMMON:-/vendor/airplay2/hk-audio-common.sh}
[ -r "$COMMON" ] || COMMON=$(dirname "$0")/hk-audio-common.sh
[ -r "$COMMON" ] || exit 1
. "$COMMON"
active=0
child=
HOOK_POLL_LIMIT=12
command -v usleep >/dev/null 2>&1 && HOOK_POLL_LIMIT=60
BT_START_ACTIVITY_GRACE=${HK_BT_START_ACTIVITY_GRACE_SECONDS:-3}
case "$BT_START_ACTIVITY_GRACE" in
    ''|*[!0-9]*|0) BT_START_ACTIVITY_GRACE=3;;
esac
[ "$BT_START_ACTIVITY_GRACE" -le 8 ] || BT_START_ACTIVITY_GRACE=8
BT_START_CHECK_FAILURES=${HK_BT_START_CHECK_FAILURES:-5}
case "$BT_START_CHECK_FAILURES" in
    ''|*[!0-9]*|0) BT_START_CHECK_FAILURES=5;;
esac
[ "$BT_START_CHECK_FAILURES" -le 10 ] || BT_START_CHECK_FAILURES=10
RELEASE_WAIT_TRIES=${HK_BT_RELEASE_WAIT_TRIES:-50}
case "$RELEASE_WAIT_TRIES" in
    ''|*[!0-9]*|0) RELEASE_WAIT_TRIES=50;;
esac

now() { audio_now; }
log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*" >>"$LOG"; audio_trim_log "$LOG"; }
trace() {
    [ "${HK_BT_DEBUG_HANDOFF:-0}" = 1 ] || return 0
    log "trace $*"
}
heartbeat() {
    _hb_now=$(now) || return 1
    audio_write_control "$HEARTBEAT" "$_hb_now"
}

watchdog_live() {
    audio_watchdog_live
}

bt_playing() {
    bt_pcm_live || return 1
    bt_source_playing
}

bt_start_source_live() {
    # AirPlay handoff briefly closes the hardware clock. The factory player
    # can retain that stopped PCM while the private virtual PCM continues to
    # receive fresh frames. Requiring bt_playing here aborts a valid cold start
    # before the hook can publish its lease and reopen the DSP.
    bt_playing && return 0
    bt_source_playing || return 1
    read_reverse_activity || return 1
    _bssl_now=$(now) || return 1
    [ "$_ra_last" != 0 ] && [ "$_ra_last" -le "$_bssl_now" ] &&
        [ $((_bssl_now - _ra_last)) -le 2 ] || return 1
    _bssl_identity="$_ra_pid:$_ra_start:$_ra_instance"
    if [ -z "${bt_start_activity_identity:-}" ]; then
        # Anchor the start to one live PCM instance. Every later observation
        # must add frames, so a cached tail cannot sustain a paused start.
        bt_start_activity_identity=$_bssl_identity
        bt_start_activity_frames=$_ra_frames
        bt_start_activity_progress_time=$_bssl_now
        return 0
    fi
    [ "$_bssl_identity" = "$bt_start_activity_identity" ] || return 1
    if [ -z "${bt_start_activity_frames:-}" ]; then
        # The acquisition sample is the authorization to begin this bounded
        # start. Anchor the per-poll progress check at the first observation
        # made after the hook has actually launched.
        bt_start_activity_frames=$_ra_frames
        bt_start_activity_progress_time=$_bssl_now
        return 0
    fi
    case "${bt_start_activity_frames}:$_ra_frames:${bt_start_activity_progress_time:-}" in
        *[!0-9:]*|:*|*:) return 1;;
    esac
    if [ "$_ra_frames" -gt "$bt_start_activity_frames" ]; then
        bt_start_activity_frames=$_ra_frames
        bt_start_activity_progress_time=$_bssl_now
        return 0
    fi
    [ "$_ra_frames" -eq "$bt_start_activity_frames" ] || return 1
    # The stock status RPC and the virtual PCM can each pause briefly while the
    # DSP slave is being rebuilt. A flat frame count for the whole bounded
    # window still means the source stalled; only this short window is exempt.
    [ $((_bssl_now - bt_start_activity_progress_time)) -le \
        "$BT_START_ACTIVITY_GRACE" ]
}

bt_source_playing() {
    bt_last_disconnected=0
    watchdog_live || return 1
    [ -x "$REQ" ] && [ -S "$PLAYER_SOCKET" ] || return 1
    data=$("$REQ" "$PLAYER_SOCKET" getPlayerStatus 1200 2>/dev/null) || return 1
    if printf '%s' "$data" | grep -q '"mode":"-1"'; then
        # A confirmed disconnect ends the paused stream's generation. A
        # subsequent connection may use the initial unowned-PCM repair.
        # The barrier is removed later under speaker.lock. This function is
        # also called while that lock is already held, and an unconditional
        # rm here could delete a concurrently published AirPlay barrier.
        bt_last_disconnected=1
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
last_pause_retry_check=-1
last_claim_reap=-1
last_orphan_park_check=-1
reverse_token=
reverse_paused=0
reverse_require_quiet=0
reverse_armed=0
bridge_lock_held=0
last_airplay_stock_check=0
next_airplay_stock_repair=0
stock_repair_cooldown=${HK_AP2_STOCK_REPAIR_COOLDOWN:-5}
case "$stock_repair_cooldown" in
    ''|*[!0-9]*|0) stock_repair_cooldown=5;;
esac
reverse_instance=
reverse_pid=
reverse_start=
reverse_frames=0
reverse_pending_identity=
reverse_pending_since=0
reverse_pending_last_sample=0
reverse_pending_frames=0
reverse_pending_samples=0
last_reverse_check=-1
wait_source_playing=0
wait_activity_pid=
wait_activity_start=
wait_activity_instance=
wait_activity_frames=
wait_activity_last_sample=0
wait_activity_samples=0
wait_activity_since=0
bt_last_disconnected=0

read_reverse_activity() {
    # The private backend reports only a process identity, PCM instance,
    # frame count and time. Never inspect or store the audio itself.
    [ -S "$BT_PCM_CONTROL" ] || return 1
    _ra_reply=$("$REQ" "$BT_PCM_CONTROL" activity 1200 2>/dev/null) || return 1
    IFS=' ' read -r _ra_kind _ra_pid _ra_instance _ra_frames _ra_last _ra_extra <<EOF
$_ra_reply
EOF
    [ "$_ra_kind" = activity ] && [ -z "$_ra_extra" ] || return 1
    case "$_ra_pid:$_ra_instance:$_ra_frames:$_ra_last" in
        *[!0-9:]*|:*|*:|*::* ) return 1;;
    esac
    _ra_start=$(audio_pid_start "$_ra_pid")
    audio_pid_live "$_ra_pid" "$_ra_start" || return 1
    [ "$(cat "$AUDIO_PROC/$_ra_pid/comm" 2>/dev/null)" = a01controller ]
}

observe_airplay_takeover() {
    if audio_marker_present "$PAUSE_PENDING"; then
        # The AirPlay start is still trying to pause a phone that was already
        # playing. Its continuing frames are the old playback generation, not
        # a new takeover request. Let the background AVRCP retry establish the
        # quiet boundary first.
        [ -z "$reverse_token" ] || reverse_require_quiet=1
        reverse_token=
        reverse_paused=0
        reverse_armed=0
        reverse_instance=
        reverse_pending_identity=
        trace 'AirPlay takeover witness paused for pending phone pause'
        return 0
    fi
    # A stale "play" alone cannot steal AirPlay. During this exact lease,
    # first witness a stable pause, then require fresh phone PCM as well as
    # the player's new play state. Old buffered data is never played.
    if ! { ! audio_marker_present "$SAFE/tripped" &&
           ! audio_marker_present "${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}" &&
           ! audio_voice_pending && watchdog_live && audio_read_lease &&
           [ "$lease_source" = airplay ]; }; then
        trace 'AirPlay takeover guard lost'
        [ -z "$reverse_token" ] || reverse_require_quiet=1
        reverse_token=; reverse_paused=0; reverse_armed=0; reverse_instance=; reverse_pending_identity=
        return 0
    fi
    if [ "$reverse_token" != "$lease_token" ]; then
        trace "AirPlay lease changed old=${reverse_token:-none} new=$lease_token"
        [ -z "$reverse_token" ] || reverse_require_quiet=1
        reverse_token=$lease_token; reverse_paused=0; reverse_armed=0; reverse_instance=; reverse_pending_identity=
    fi
    _rt_now=$(now)
    [ "$_rt_now" -gt "$last_reverse_check" ] || return 0
    last_reverse_check=$_rt_now
    _rt_data=$("$REQ" "$PLAYER_SOCKET" getPlayerStatus 1200 2>/dev/null) || {
        reverse_pending_identity=; return 0;
    }
    _rt_idle=0
    if printf '%s' "$_rt_data" | grep -q '"mode":"41"'; then
        case "$_rt_data" in *'"status":"stop"'*|*'"status":"pause"'*) _rt_idle=1;; esac
    elif printf '%s' "$_rt_data" | grep -q '"mode":"-1"'; then
        # A phone can connect and immediately start music while AirPlay is
        # playing. Two confirmed disconnected observations establish the
        # same quiet baseline; newly received PCM is still required.
        _rt_idle=1
    else
        reverse_paused=0; reverse_armed=0; reverse_instance=; reverse_pending_identity=
        return 0
    fi
    if [ "$_rt_idle" = 1 ]; then
            reverse_paused=1
            reverse_require_quiet=0
            reverse_pending_identity=
            if read_reverse_activity; then
                if [ "$reverse_instance:$reverse_pid:$reverse_start:$reverse_frames" = \
                     "$_ra_instance:$_ra_pid:$_ra_start:$_ra_frames" ]; then
                    reverse_armed=1
                    trace "idle PCM witness armed frames=$_ra_frames"
                else
                    reverse_instance=$_ra_instance; reverse_pid=$_ra_pid
                    reverse_start=$_ra_start; reverse_frames=$_ra_frames; reverse_armed=0
                fi
            elif [ ! -S "$BT_PCM_CONTROL" ]; then
                # A connected but paused phone need not have opened a PCM
                # yet. Require two idle observations before arming.
                if [ "$reverse_instance" = absent ]; then reverse_armed=1
                else reverse_instance=absent; reverse_armed=0; fi
            else
                # A busy/refused socket is not evidence of a quiet source.
                reverse_paused=0; reverse_armed=0; reverse_instance=
            fi
            return 0
    fi
    [ "$lease_phase" = active ] || {
        reverse_pending_identity=; return 0;
    }
    if [ "$reverse_require_quiet" = 1 ] && [ "$reverse_paused" != 1 ]; then
        # After an explicit interruption or a lease replacement, cached play
        # state cannot reuse the previous lease's evidence. A new quiet sample
        # is required before a fresh phone-start confirmation can begin.
        reverse_pending_identity=
        trace 'fresh phone start refused until a new quiet sample'
        return 0
    fi
    printf '%s' "$_rt_data" | grep -q '"status":"play"' &&
        printf '%s' "$_rt_data" | grep -q '"mute":"0"' || { reverse_pending_identity=; return 0; }
    read_reverse_activity || { reverse_pending_identity=; return 0; }
    # If an existing process restarted, discard the old pause witness.
    if [ -n "$reverse_instance" ] && [ "$reverse_instance" != absent ] &&
       [ "$reverse_pid:$reverse_start" != "$_ra_pid:$_ra_start" ]; then
        reverse_paused=0; reverse_armed=0; reverse_instance=; reverse_pending_identity=
        return 0
    fi
    [ "$_ra_frames" != 0 ] && [ "$_ra_last" != 0 ] && [ "$_ra_last" -le "$_rt_now" ] &&
        [ $((_rt_now - _ra_last)) -le 2 ] || { reverse_pending_identity=; return 0; }
    if [ "$reverse_instance" = "$_ra_instance" ] &&
       [ "$reverse_frames" = "$_ra_frames" ]; then reverse_pending_identity=; return 0; fi
    if [ "$reverse_armed" != 1 ]; then
        # A phone can connect and start before this bridge observes any quiet
        # sample. A cached "play" string alone is insufficient, so require
        # continuing fresh frames from one PCM instance over two seconds.
        # This admits a real quick start without letting a short tail steal
        # AirPlay from the current sender.
        _rt_identity="$_ra_pid:$_ra_start:$_ra_instance"
        if [ "$reverse_pending_identity" != "$_rt_identity" ]; then
            reverse_pending_identity=$_rt_identity
            reverse_pending_since=$_rt_now
            reverse_pending_last_sample=$_rt_now
            reverse_pending_frames=$_ra_frames
            reverse_pending_samples=1
            trace "fresh phone confirmation started frames=$_ra_frames"
            return 0
        fi
        if [ "$reverse_pending_frames" = "$_ra_frames" ] ||
           [ $((_rt_now - reverse_pending_last_sample)) -gt 2 ]; then
            # A stalled stream or a missed observation cannot demonstrate
            # continuing input. Begin a new interval at the next sample.
            reverse_pending_identity=
            return 0
        fi
        reverse_pending_last_sample=$_rt_now
        reverse_pending_frames=$_ra_frames
        reverse_pending_samples=$((reverse_pending_samples + 1))
        trace "fresh phone confirmation samples=$reverse_pending_samples frames=$_ra_frames"
        [ "$reverse_pending_samples" -ge 3 ] &&
            [ $((_rt_now - reverse_pending_since)) -ge 2 ] || return 0
    fi
    _yb_source_pid=$_ra_pid
    _yb_source_start=$_ra_start
    _yb_source_instance=$_ra_instance
    _yb_observed_frames=$_ra_frames
    reverse_paused=0
    yield_airplay_to_bluetooth "$reverse_token"
}

retry_pending_phone_pause() {
    audio_marker_present "$PAUSE_PENDING" || return 0
    _rpp_now=$(now) || return 0
    [ "$_rpp_now" -gt "$last_pause_retry_check" ] || return 0
    last_pause_retry_check=$_rpp_now
    _rpp_marker=$(audio_control_read "$PAUSE_PENDING") || return 0

    # The pause marker belongs only to an active AirPlay generation. Once that
    # generation ends, deliberately leave the phone timeline alone.
    if ! { audio_read_lease &&
           [ "$lease_source" = airplay ] &&
           [ "$lease_phase" = active ]; }; then
        # AirPlay publishes a claim before it parks Bluetooth and pauses the
        # phone, then publishes its lease after those handoff steps. Do not
        # delete the retry marker during that bounded start window.
        audio_claim_recent "${HK_AP2_RUN:-$RUN}/ap2-claim" && return 0
        [ -z "$(audio_control_read "$OWNER")" ] || return 0
        if [ "$(audio_control_read "$PAUSE_PENDING")" = "$_rpp_marker" ]; then
            rm -f "$PAUSE_PENDING"
            trace 'removed pause-retry marker after AirPlay generation ended'
        fi
        return 0
    fi
    ! audio_marker_present "$SAFE/tripped" &&
        ! audio_marker_present "${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}" &&
        ! audio_voice_pending && watchdog_live || return 0

    lock_speaker || return 0
    _rpp_remove=0
    _rpp_pause=0
    _rpp_removed_ok=0
    if [ "$(audio_control_read "$PAUSE_PENDING")" = "$_rpp_marker" ] &&
       ! audio_marker_present "$OWNER" &&
       ! audio_marker_present "$SAFE/tripped" &&
       ! audio_marker_present "${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}" &&
       ! audio_voice_pending && watchdog_live &&
       audio_read_lease && [ "$lease_source" = airplay ] &&
       [ "$lease_phase" = active ]; then
        _rpp_data=$("$REQ" "$PLAYER_SOCKET" getPlayerStatus 1200 2>/dev/null) ||
            _rpp_data=
        case "$_rpp_data" in
            *'"mode":"-1"'*|*'"status":"stop"'*|*'"status":"pause"'*)
                _rpp_remove=1
                ;;
            *'"mode":"41"'*)
                # A connected phone can retain a play timeline while its
                # factory player is muted. The AirPlay handoff still needs to
                # stop that timeline before a later phone play can take over.
                if printf '%s' "$_rpp_data" | grep -q '"status":"play"'; then
                    _rpp_pause=1
                fi
                ;;
        esac
        if [ "$_rpp_remove" = 1 ]; then
            if rm -f "$PAUSE_PENDING"; then
                _rpp_removed_ok=1
            fi
        elif [ "$_rpp_pause" = 1 ]; then
            if "$REQ" "$PLAYER_SOCKET" setPlayerCmd:onepause 1200 \
                >/dev/null 2>&1; then
                trace 'retried pending Bluetooth phone pause'
            fi
        fi
    fi
    audio_unlock "$LOCK" 2>/dev/null || true

    if [ "$_rpp_removed_ok" = 1 ]; then
        log 'pending Bluetooth phone pause confirmed'
    fi
}

repair_active_airplay_stock_mute() {
    # The stock player can mute the shared amplifier when a phone connects,
    # even if that phone never starts a Bluetooth stream. AirPlay remains the
    # active owner, so a PA-only or global mute leaves its DSP clock running
    # but audible output closed. Repair only while the phone is idle; a phone
    # play request must go through the normal reverse-handover witness.
    audio_read_lease && [ "$lease_source" = airplay ] &&
        [ "$lease_phase" = active ] || return 0
    _ras_lease_token=$lease_token
    _ras_lease_time=$lease_time
    ! audio_marker_present "$OWNER" &&
        ! audio_marker_present "$SAFE/tripped" &&
        ! audio_marker_present "${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}" &&
        ! audio_voice_pending && watchdog_live || return 0
    _ras_now=$(now) || return 0
    [ "$_ras_now" -gt "$last_airplay_stock_check" ] || return 0
    last_airplay_stock_check=$_ras_now
    [ -x "$STOCK_AUDIO_STATE" ] || return 0
    _ras_state=$(env LD_LIBRARY_PATH=/system/workdir/lib \
        "$STOCK_AUDIO_STATE" 2>/dev/null) || return 0
    case "$_ras_state" in
        'PA mute=0 force_mute=0 gate=1') return 0;;
        'PA mute=0 force_mute=1 gate=1'|\
        'PA mute=1 force_mute=0 gate=1'|\
        'PA mute=1 force_mute=1 gate=1') ;;
        *) return 0;;
    esac
    # The stock status string can retain a stale play timeline after the
    # phone has paused, and its RPC can fail independently of audio. The
    # private backend's fresh-frame counter is the decisive activity signal:
    # defer while the phone is producing audio, otherwise restore the current
    # AirPlay owner's amplifier state.
    if read_reverse_activity &&
       [ "$_ra_frames" != 0 ] && [ "$_ra_last" != 0 ] &&
       [ "$_ra_last" -le "$_ras_now" ] &&
       [ $((_ras_now - _ra_last)) -le 2 ]; then
        trace 'active AirPlay mute repair deferred for fresh phone frames'
        return 0
    fi
    [ "$_ras_now" -ge "$next_airplay_stock_repair" ] || return 0
    audio_read_lease && [ "$lease_source" = airplay ] &&
        [ "$lease_phase" = active ] &&
        [ "$lease_token" = "$_ras_lease_token" ] &&
        [ "$lease_time" = "$_ras_lease_time" ] || return 0
    trace 'requesting active AirPlay stock mute repair for an idle phone'
    if env HK_AP2_BT= HK_AP2_VOICE= HK_AP2_USE_STOCK_PA=1 \
        HK_AUDIO_TOKEN="$_ras_lease_token" \
        HK_AUDIO_LEASE_TIME="$_ras_lease_time" \
        "$HOOK" repair-stock-pa >/dev/null 2>&1; then
        log 'repaired stock amplifier mute during active AirPlay'
        next_airplay_stock_repair=$((_ras_now + stock_repair_cooldown))
    else
        log 'active AirPlay stock amplifier repair failed; retrying later'
        next_airplay_stock_repair=$((_ras_now + stock_repair_cooldown))
    fi
}

yield_airplay_to_bluetooth() {
    _yb_token=$1
    trace "yield requested token=$_yb_token source_frames=$_yb_observed_frames"
    [ -x "$MQTT" ] || { log 'AirPlay pause publisher unavailable; takeover refused'; return 1; }
    lock_speaker || { trace 'yield speaker lock unavailable'; return 1; }
    if ! { audio_read_lease && [ "$lease_source" = airplay ] &&
           [ "$lease_token" = "$_yb_token" ] && [ "$lease_phase" = active ] &&
           ! audio_marker_present "$SAFE/tripped" &&
           ! audio_marker_present "${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}" &&
           ! audio_voice_pending; }; then
        trace 'yield lease recheck failed before fresh-frame wait'
        audio_unlock "$LOCK"; return 1
    fi
    # The evidence sample and lock acquisition are not one atomic operation.
    # Always wait for one more frame from the same live PCM instance while
    # holding the speaker lock. A single tail or cached "play" string therefore
    # cannot pause AirPlay; a real continuation normally produces this frame in
    # well under one second.
    _yb_fresh=0
    _yb_wait_try=0
    while [ "$_yb_wait_try" -lt 10 ]; do
        if read_reverse_activity &&
           [ "$_ra_pid:$_ra_start:$_ra_instance" = \
             "$_yb_source_pid:$_yb_source_start:$_yb_source_instance" ] &&
           [ "$_ra_frames" -gt "$_yb_observed_frames" ]; then
            _yb_fresh=1
            break
        fi
        if ! { audio_read_lease && [ "$lease_source" = airplay ] &&
               [ "$lease_token" = "$_yb_token" ] && [ "$lease_phase" = active ] &&
               ! audio_marker_present "$SAFE/tripped" &&
               ! audio_marker_present "${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}" &&
               watchdog_live && ! audio_voice_pending; }; then
            trace 'yield lease recheck failed during fresh-frame wait'
            audio_unlock "$LOCK"; return 1
        fi
        audio_delay 100000
        _yb_wait_try=$((_yb_wait_try + 1))
    done
    [ "$_yb_fresh" = 1 ] || {
        trace 'yield timed out waiting for a fresh frame'
        audio_unlock "$LOCK"; return 1
    }
    # The phone can pause or reconnect while we wait for the speaker lock.
    # Recheck its current intent, process/PCM generation and fresh input.
    _yb_data=$("$REQ" "$PLAYER_SOCKET" getPlayerStatus 1200 2>/dev/null) || {
        trace 'yield player status query failed'
        audio_unlock "$LOCK"; return 1;
    }
    _yb_now=$(now)
    if ! { printf '%s' "$_yb_data" | grep -q '"mode":"41"' &&
           printf '%s' "$_yb_data" | grep -q '"status":"play"' &&
           printf '%s' "$_yb_data" | grep -q '"mute":"0"' &&
           read_reverse_activity &&
           [ "$_ra_pid:$_ra_start:$_ra_instance" = "$_yb_source_pid:$_yb_source_start:$_yb_source_instance" ] &&
           [ "$_ra_frames" -gt "$_yb_observed_frames" ] &&
           [ "$_ra_last" != 0 ] && [ "$_ra_last" -le "$_yb_now" ] && [ $((_yb_now - _ra_last)) -le 2 ] &&
           audio_read_lease && [ "$lease_source" = airplay ] &&
           [ "$lease_token" = "$_yb_token" ] && [ "$lease_phase" = active ] &&
           ! audio_marker_present "$SAFE/tripped" &&
           ! audio_marker_present "${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}" &&
           watchdog_live && ! audio_voice_pending; }; then
        trace 'yield status/generation recheck failed'
        audio_unlock "$LOCK"; return 1
    fi
    audio_unlock "$LOCK"
    trace 'yield confirmed; closing AirPlay output'
    # The existing source hook fades and closes output before its clock can
    # stop. Its generation check prevents an old request muting a new owner.
    env HK_AP2_BT= HK_AUDIO_TOKEN="$_yb_token" \
        HK_AUDIO_LEASE_TIME="$lease_time" \
        HK_AP2_REVERSE_STOP=1 HK_AP2_USE_STOCK_PA=1 "$HOOK" stop || return 1
    trace 'AirPlay stop hook completed'
    ! audio_marker_present "$SAFE/lease" && ! audio_marker_present "$OWNER" &&
        ! audio_marker_present "$SAFE/tripped" &&
        ! audio_marker_present "${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}" &&
        watchdog_live && ! audio_voice_pending || return 1
    audio_write_control "$RUN/release-dac" "1" || return 1
    if ! "$MQTT" hk-ap2/remote pause >/dev/null 2>&1; then
        "$TEST" mute >/dev/null 2>&1 || true
        log 'AirPlay pause request failed; output remains muted'
        return 1
    fi
    _yb_try=0
    while [ "$_yb_try" -lt "$RELEASE_WAIT_TRIES" ]; do
        if ! { ! audio_marker_present "$RUN/ap2-claim" &&
               ! audio_marker_present "$SAFE/lease" &&
               ! audio_marker_present "$OWNER" &&
               ! audio_marker_present "$SAFE/tripped" &&
               ! audio_marker_present "${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}" &&
               ! audio_voice_pending; }; then
            # Voice may have taken over and now owns this shared release flag.
            audio_voice_pending || rm -f "$RUN/release-dac"
            return 1
        fi
        case "$(sed -n '1p' "$PCM" 2>/dev/null)" in
            closed|'state: closed'|'state: CLOSED') break;;
            'state: RUNNING') bt_pcm_live && break;;
        esac
        audio_delay 100000
        _yb_try=$((_yb_try + 1))
    done
    if [ "$_yb_try" -ge "$RELEASE_WAIT_TRIES" ]; then
        "$TEST" mute >/dev/null 2>&1 || true
        log 'AirPlay hardware release timed out; output remains muted'
        return 1
    fi
    audio_voice_pending && return 1
    rm -f "$RUN/release-dac"
    log 'AirPlay pause requested and clock released for new Bluetooth playback'
}

refresh_paused_source() {
    _rp_now=$(now)
    [ "$_rp_now" -gt "$last_pause_check" ] || return 0
    last_pause_check=$_rp_now
    # Keep the existing source-generation/disconnect observation even when
    # the fast playback probe rejects a stopped PCM before querying stock.
    if bt_source_playing; then
        wait_source_playing=1
        trace "source sample tick=$_rp_now playing=1"
    else
        wait_source_playing=0
        trace "source sample tick=$_rp_now playing=0 disconnected=$bt_last_disconnected"
        reset_wait_evidence
    fi
}

reset_wait_evidence() {
    wait_activity_pid=
    wait_activity_start=
    wait_activity_instance=
    wait_activity_frames=
    wait_activity_last_sample=0
    wait_activity_samples=0
    wait_activity_since=0
}

reap_disconnected_bluetooth_marker() {
    # A disconnect normally ends the paused Bluetooth generation. Revalidate
    # the stock state while holding the same lock used to publish AirPlay
    # barriers. This prevents a stale cached "-1" observation, or an AirPlay
    # session that starts after our first query, from losing its barrier.
    [ "$bt_last_disconnected" = 1 ] || return 0
    _rdbm_marker=$(audio_control_read "$WAIT_PCM") || return 0
    case "$_rdbm_marker" in
        bt-*|backend-*) ;;
        *) return 0;;
    esac
    lock_speaker || return 1
    _rdbm_remove=0
    if [ "$(audio_control_read "$WAIT_PCM")" = "$_rdbm_marker" ] &&
       [ -x "$REQ" ] && [ -S "$PLAYER_SOCKET" ] &&
       ! audio_read_lease && ! audio_play_marker_live &&
       ! audio_claim_recent "$RUN/ap2-claim" &&
       ! audio_marker_present "$OWNER" &&
       ! audio_marker_present "$SAFE/tripped" &&
       ! audio_marker_present "${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}" &&
       ! audio_voice_pending && watchdog_live; then
        _rdbm_data=$("$REQ" "$PLAYER_SOCKET" getPlayerStatus 1200 2>/dev/null) ||
            _rdbm_data=
        if printf '%s' "$_rdbm_data" | grep -q '"mode":"-1"' &&
           [ "$(audio_control_read "$WAIT_PCM")" = "$_rdbm_marker" ]; then
            _rdbm_remove=1
        fi
    fi
    if [ "$_rdbm_remove" = 1 ]; then
        rm -f "$WAIT_PCM" || _rdbm_remove=0
        [ "$_rdbm_remove" != 1 ] ||
            log 'removed disconnected Bluetooth pause barrier'
    fi
    audio_unlock "$LOCK" 2>/dev/null || true
}

reap_completed_airplay_release_marker() {
    # A pause request can fail or time out after the AirPlay stop hook has
    # already closed the device. Keep release-dac while any AirPlay generation
    # or claim is live, but remove it once the device is genuinely closed so it
    # cannot block every later AirPlay session.
    audio_marker_present "$RUN/release-dac" || return 0
    lock_speaker || return 1
    _rcar_remove=0
    if audio_marker_present "$RUN/release-dac" &&
       ! audio_read_lease && ! audio_play_marker_live &&
       ! audio_claim_recent "$RUN/ap2-claim" &&
       ! audio_marker_present "$OWNER" &&
       ! audio_marker_present "$SAFE/tripped" &&
       ! audio_marker_present "${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}" &&
       ! audio_voice_pending && watchdog_live; then
        case "$(sed -n '1p' "$PCM" 2>/dev/null)" in
            closed|'state: closed'|'state: CLOSED') _rcar_remove=1;;
        esac
    fi
    if [ "$_rcar_remove" = 1 ]; then
        rm -f "$RUN/release-dac" || _rcar_remove=0
        [ "$_rcar_remove" != 1 ] ||
            log 'removed completed AirPlay release marker'
    fi
    audio_unlock "$LOCK" 2>/dev/null || true
}

reap_orphaned_bluetooth_slave() {
    # A controller/backend can survive a bridge restart while retaining its
    # virtual PCM in PREPARED or XRUN with no lease, owner or pause token.
    # That physical slave would otherwise block the next AirPlay session.
    # Park it only while the clock is in a known stopped state and no source
    # owns audio; the hook rechecks again under speaker.lock.
    _robs_now=$(now) || return 1
    [ "$_robs_now" -gt "$last_orphan_park_check" ] || return 0
    last_orphan_park_check=$_robs_now
    case "$(sed -n '1p' "$PCM" 2>/dev/null)" in
        'state: XRUN'|'state: SETUP'|'state: PREPARED'|'state: PAUSED'|\
        'state: SUSPENDED'|'state: DRAINING') ;;
        *) return 0;;
    esac
    _robs_owner=$(sed -n 's/^owner_pid[[:space:]]*:[[:space:]]*//p' "$PCM" 2>/dev/null)
    audio_bt_pcm_owned "$_robs_owner" || return 0
    ! audio_read_lease && ! audio_play_marker_live &&
        ! audio_claim_recent "$RUN/ap2-claim" &&
        ! audio_marker_present "$OWNER" &&
        ! audio_marker_present "$WAIT_PCM" &&
        ! audio_marker_present "$SAFE/tripped" &&
        ! audio_marker_present "${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}" &&
        ! audio_voice_pending && watchdog_live || return 0
    trace 'reaping an orphaned stopped Bluetooth DSP clock'
    if HK_AP2_PARK_STOPPED_ONLY=1 run_hook park; then
        case "$(sed -n '1p' "$PCM" 2>/dev/null)" in
            closed|'state: closed'|'state: CLOSED')
                [ -n "$child" ] || rm -f "$HEARTBEAT"
                log 'released orphaned Bluetooth DSP clock'
                ;;
        esac
    fi
}

wait_has_fresh_playback() {
    # A retained virtual PCM can resume after its hardware slave was parked or
    # entered XRUN. The backend blocks late frames while bt-wait-pcm exists, so
    # use its aggregate frame counter to prove that the phone is playing now.
    # Requiring several advances prevents a cached tail from starting output.
    # A normal AirPlay stop clears the barrier, while a stock Bluetooth pause
    # leaves a token. Evidence is still required in both cases: playback state
    # alone is not enough because the factory player can retain "play" while
    # its PCM is stopped.
    if audio_marker_present "$WAIT_PCM"; then
        case "$(audio_control_read "$WAIT_PCM" 2>/dev/null)" in
            airplay-*) return 1;;
        esac
    fi
    [ "$wait_source_playing" = 1 ] || return 1
    read_reverse_activity || return 1
    _wf_now=$(now) || return 1
    [ "$_ra_last" != 0 ] && [ "$_ra_last" -le "$_wf_now" ] &&
        [ $((_wf_now - _ra_last)) -le 2 ] || return 1
    if [ -z "$wait_activity_instance" ] ||
       [ "$wait_activity_pid:$_ra_start:$wait_activity_instance" != \
         "$_ra_pid:$_ra_start:$_ra_instance" ]; then
        wait_activity_pid=$_ra_pid
        wait_activity_start=$_ra_start
        wait_activity_instance=$_ra_instance
        wait_activity_frames=$_ra_frames
        wait_activity_last_sample=$_wf_now
        wait_activity_samples=0
        wait_activity_since=$_wf_now
        trace "paused-PCM evidence started frames=$_ra_frames"
        return 1
    fi
    [ "$_ra_frames" -gt "$wait_activity_frames" ] || return 1
    wait_activity_frames=$_ra_frames
    wait_activity_last_sample=$_wf_now
    wait_activity_samples=$((wait_activity_samples + 1))
    trace "paused-PCM evidence samples=$wait_activity_samples elapsed=$((_wf_now - wait_activity_since))"
    # Continuous hardware-period advances within a fraction of one uptime
    # second can still be a cached tail. Require the same PCM instance to keep
    # advancing for two seconds before removing the late-frame barrier.
    [ "$wait_activity_samples" -ge 3 ] &&
        [ $((_wf_now - wait_activity_since)) -ge 2 ]
}

reap_stale_airplay_claim() {
    [ -e "$RUN/ap2-claim" ] || return 0
    _rc_now=$(now) || return 1
    [ "$_rc_now" -gt "$last_claim_reap" ] || return 0
    last_claim_reap=$_rc_now
    audio_claim_recent "$RUN/ap2-claim" && return 0
    lock_speaker || return 1
    _rac_owner=$(audio_control_read "$OWNER") || _rac_owner=
    _rac_owned_lease=0
    if audio_read_lease; then
        _rac_has_lease=1
        [ "$lease_source" = bluetooth ] && [ "$lease_pid" = "$$" ] &&
            _rac_owned_lease=1
    else
        _rac_has_lease=0
    fi
    if { [ "$_rac_has_lease" = 0 ] ||
         { [ "${active:-0}" = 1 ] && [ "$_rac_owned_lease" = 1 ]; }; } &&
       { [ -z "$_rac_owner" ] ||
         { [ "${active:-0}" = 1 ] && [ "$_rac_owner" = "$$" ]; }; } &&
       ! audio_claim_recent "$RUN/ap2-claim"; then
        rm -f "$RUN/ap2-claim"
        log 'removed stale AirPlay start claim'
    fi
    audio_unlock "$LOCK" 2>/dev/null || true
}

reap_stale_control_markers() {
    # An ownerless release request can survive an interrupted takeover. It
    # must not poison every later Bluetooth acquisition.
    if audio_marker_present "$RELEASE"; then
        if lock_speaker; then
            if ! audio_marker_present "$OWNER" && ! audio_read_lease &&
               ! audio_play_marker_live; then
                rm -f "$RELEASE"
                log 'removed stale Bluetooth release request'
            fi
            audio_unlock "$LOCK" 2>/dev/null || true
        fi
    fi
    # AirPlay normally removes this barrier in its stop hook. If that optional
    # process is killed mid-release, the no-lease state plus a new, continuously
    # advancing Bluetooth stream is safe to admit. Keep all maintenance and
    # voice gates intact.
    _rscm_wait=$(audio_control_read "$WAIT_PCM") || _rscm_wait=
    case "$_rscm_wait" in airplay-*) ;; *) return 0;; esac
    ! audio_marker_present "$SAFE/tripped" &&
        ! audio_marker_present "${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}" &&
        watchdog_live && ! audio_voice_pending || return 0
    lock_speaker || return 0
    if ! audio_read_lease && ! audio_play_marker_live &&
       ! audio_claim_recent "$RUN/ap2-claim" &&
       ! audio_marker_present "$OWNER" &&
       ! audio_marker_present "$SAFE/tripped" &&
       ! audio_marker_present "${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}" &&
       watchdog_live && ! audio_voice_pending &&
       [ "$(audio_control_read "$WAIT_PCM")" = "$_rscm_wait" ]; then
        rm -f "$WAIT_PCM"
        log 'removed stale AirPlay Bluetooth barrier'
    fi
    audio_unlock "$LOCK" 2>/dev/null || true
}

bluetooth_acquisition_ready() {
    if bt_playing; then
        trace 'Bluetooth acquisition ready from a running PCM'
        return 0
    fi
    if wait_has_fresh_playback; then
        trace 'Bluetooth acquisition ready from sustained new frames'
        return 0
    fi
    return 1
}

bluetooth_acquisition_locked() {
    # The sustained-evidence check runs before taking speaker.lock. Recheck the
    # non-stateful safety gates in the caller, then require one additional
    # concrete frame while holding the lock. Unlike calling the stateful
    # evidence collector a second time, this waits briefly for the continuous
    # stream rather than requiring the clock to advance again.
    bt_playing && return 0
    if audio_marker_present "$WAIT_PCM"; then
        case "$(audio_control_read "$WAIT_PCM" 2>/dev/null)" in
            airplay-*) return 1;;
        esac
    fi
    [ "$wait_activity_samples" -ge 3 ] &&
        [ -n "$wait_activity_instance" ] || return 1
    _bl_pid=$wait_activity_pid
    _bl_start=$wait_activity_start
    _bl_instance=$wait_activity_instance
    _bl_frames=$wait_activity_frames
    _bl_try=0
    while [ "$_bl_try" -lt 10 ]; do
        if bt_source_playing && read_reverse_activity &&
           [ "$_ra_pid:$_ra_start:$_ra_instance" = \
             "$_bl_pid:$_bl_start:$_bl_instance" ] &&
           [ "$_ra_frames" -gt "$_bl_frames" ]; then
            return 0
        fi
        ! audio_marker_present "$SAFE/tripped" &&
            ! audio_marker_present "$RELEASE" &&
            ! audio_voice_pending && ! audio_play_marker_live &&
            ! ap2_recent_claim && watchdog_live || return 1
        audio_delay 100000
        _bl_try=$((_bl_try + 1))
    done
    return 1
}

repair_stalled_bluetooth() {
    # a01controller can keep reporting "play" after the A2DP stream stalls:
    # metadata and totlen are present, but it never opens the PCM and no audio
    # reaches /tmp/bt-avk-fifo. Rebuild that transport with its own pause and
    # resume commands. Keep the speaker path closed throughout.
    [ "${HK_BT_STALL_REPAIR:-1}" = 1 ] || return 1
    ! audio_marker_present "$WAIT_PCM" || return 1
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
    [ "$wait_source_playing" = 1 ] || { stall_since=; return 1; }
    # Do not consume a new uptime second with a stale "not playing" sample.
    # refresh_paused_source and this function read uptime separately, so the
    # clock can cross a boundary between them. Keep the new second available
    # for the immediately following fresh playing observation.
    last_stall_check=$current
    trace "transport sample tick=$current source_playing=1"
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
    audio_claim_recent "$RUN/ap2-claim"
}

lock_speaker() { audio_lock "$LOCK"; }

terminate_hook_child() {
    _thc_pid=${child:-}
    [ -n "$_thc_pid" ] || return 0
    kill -TERM "$_thc_pid" 2>/dev/null || true
    _thc_try=20
    command -v usleep >/dev/null 2>&1 || _thc_try=3
    _thc_count=0
    while kill -0 "$_thc_pid" 2>/dev/null && [ "$_thc_count" -lt "$_thc_try" ]; do
        _thc_count=$((_thc_count + 1))
        audio_delay 100000
    done
    if kill -0 "$_thc_pid" 2>/dev/null; then
        kill -KILL "$_thc_pid" 2>/dev/null || true
    fi
    wait "$_thc_pid" 2>/dev/null || true
    child=
}

run_hook() {
    action=$1
    bt_master=4
    bt_start_activity_identity=
    bt_start_activity_frames=
    bt_start_activity_progress_time=
    start_source_failures=0
    case "$action" in
        start)
            # Follow the stock player volume on the 0..32 Master scale instead
            # of forcing Master to 4 each time the Bluetooth path opens.
            case "${vol:-}" in ''|*[!0-9]*) return 1;; esac
            [ "$vol" -le 100 ] || return 1
            bt_master=$(((vol * 32 + 50) / 100))
            # Match the stock 33-level curve: anything above 0% uses at least
            # its lowest non-silent step instead of rounding 1% down to mute.
            if [ "$vol" -gt 0 ] && [ "$bt_master" -lt 1 ]; then
                bt_master=1
            fi
            if [ -n "${wait_activity_instance:-}" ]; then
                bt_start_activity_identity="$wait_activity_pid:$wait_activity_start:$wait_activity_instance"
            fi
            ;;
        stop|park) ;;
        *) return 1;;
    esac
    # PCM5121 uses an inverted digital-volume scale: 0 is mute and 207 is
    # 0 dB. Keep the analogue step at the stock -6 dB setting and apply the
    # stock 33-step Master control before the Harman limiter.
    env HK_AP2_BT=1 HK_AUDIO_TOKEN="${BT_TOKEN:-}" HK_AP2_USE_STOCK_PA=1 HK_AP2_MASTER_SAFE="$bt_master" \
        HK_AP2_DIGITAL_SAFE=207 HK_AP2_PLAYBACK_SAFE=0 "$HOOK" "$action" &
    child=$!
    count=0
    hook_deadline=$(($(now) + 12))
    while kill -0 "$child" 2>/dev/null; do
        heartbeat || {
            trace 'start heartbeat write failed'
            terminate_hook_child
            return 1
        }
        if [ "$action" = start ]; then
            if ! { audio_regular_file "$SAFE/armed" &&
                ! audio_marker_present "$SAFE/tripped" &&
                [ "$(audio_control_read "$OWNER")" = "$$" ] &&
                ! audio_marker_present "$RELEASE" && ! audio_voice_pending; }; then
                trace "start safety gate lost armed=$(audio_regular_file "$SAFE/armed" && echo 1 || echo 0) owner=$(audio_control_read "$OWNER" 2>/dev/null || true)"
                terminate_hook_child
                # A pause or track change can stop the source mid-start.
                # It is safe to close and retry only while the watchdog is
                # healthy and there is no competing speaker owner or trip.
                if start_retry_safe; then
                    return 10
                fi
                return 1
            fi
            if bt_start_source_live; then
                start_source_failures=0
            else
                start_source_failures=$((start_source_failures + 1))
                trace "Bluetooth start source check missed count=$start_source_failures"
                if [ "$start_source_failures" -ge "$BT_START_CHECK_FAILURES" ]; then
                    terminate_hook_child
                    log 'Bluetooth source was not sustained during start; output closed'
                    # A short RPC dropout must not become a global fault. The
                    # physical path was closed by the terminated hook. Retry
                    # without a global latch while the independent watchdog
                    # process remains alive; release failures still latch.
                    if start_retry_safe; then
                        return 10
                    fi
                    return 1
                fi
            fi
        fi
        count=$((count + 1))
        [ "$count" -lt "$HOOK_POLL_LIMIT" ] && [ "$(now)" -lt "$hook_deadline" ] ||
            {
                trace "start monitor timed out count=$count"
                terminate_hook_child
                return 1
            }
        audio_delay 200000
    done
    wait "$child"; rc=$?
    child=
    return "$rc"
}

start_retry_safe() {
    # A source pause or a short RPC/frame-sampling miss is recoverable only
    # while this bridge still owns the control file and the independent
    # watchdog is either current or inside the already approved, bounded
    # source-transition grace. A live PID with stale heartbeat is still a fault.
    audio_regular_file "$SAFE/armed" &&
        ! audio_marker_present "$SAFE/tripped" &&
        [ "$(audio_control_read "$OWNER")" = "$$" ] &&
        ! audio_marker_present "$RELEASE" && ! audio_voice_pending &&
        { watchdog_live || audio_watchdog_transition_live; }
}

release_output() {
    [ "$active" = 1 ] || return 0
    release_ok=1
    # After any established stream stops, wait for a new valid PCM rather
    # than resuming a phone that may have deliberately paused playback.
    if ! audio_write_control "$WAIT_PCM" "${BT_TOKEN:-}"; then
        "$TEST" mute >/dev/null 2>&1 || true
        log 'could not record Bluetooth pause; manual mute latched'
        release_ok=0
    fi
    if ! run_hook stop; then
        "$TEST" mute >/dev/null 2>&1 || true
        log 'stop hook failed; manual mute latched'
        release_ok=0
    fi
    rm -f "$OWNER" "$RELEASE"
    # The stock player deliberately retains its virtual PCM handle. Return the
    # actual DSP device too, otherwise PREPARED/XRUN can strand the next source
    # and a long pause can force the resume path through an unrecoverable slave.
    if ! run_hook park; then
        "$TEST" mute >/dev/null 2>&1 || true
        log 'Bluetooth DSP release failed; manual mute latched'
        release_ok=0
    fi
    rm -f "$HEARTBEAT"
    active=0
    if [ "$release_ok" = 1 ]; then
        log 'Bluetooth released output and DSP slave'
    fi
}

cleanup() {
    trap - EXIT HUP INT TERM
    if [ -n "$child" ]; then
        "$TEST" mute >/dev/null 2>&1 || true
        terminate_hook_child
    fi
    release_output
    if [ "$(audio_control_read "$RUN/bt-bridge.pid")" = "$$" ]; then
        rm -f "$RUN/bt-bridge.pid"
    fi
    if [ "$bridge_lock_held" = 1 ]; then
        audio_unlock "$BRIDGE_LOCK" 2>/dev/null || true
        bridge_lock_held=0
    fi
}

trap cleanup EXIT
trap 'exit 0' HUP INT TERM

case "${1:-}" in __run) ;; *) echo "usage: $0 __run" >&2; exit 2;; esac
echo 11 >"/proc/$$/oom_score_adj" 2>/dev/null || true
existing_bridge=$(audio_control_read "$RUN/bt-bridge.pid") || existing_bridge=
case "$existing_bridge" in
    ''|*[!0-9]*) ;;
    *)
        if kill -0 "$existing_bridge" 2>/dev/null &&
           tr '\000' ' ' <"/proc/$existing_bridge/cmdline" 2>/dev/null |
               grep -q 'hk-bt-bridge\.sh'; then
            echo "$$ duplicate Bluetooth bridge refused" >>"$LOG"
            exit 1
        fi
        ;;
esac
if ! audio_lock "$BRIDGE_LOCK"; then
    echo "$$ duplicate Bluetooth bridge refused" >>"$LOG"
    exit 1
fi
bridge_lock_held=1
    ! audio_marker_present "$OWNER" ||
        { "$TEST" mute >/dev/null 2>&1 || true; exit 1; }
audio_write_control "$RUN/bt-bridge.pid" "$$" || exit 1
log "bridge started pid=$$"
while audio_regular_file "$SAFE/armed"; do
    if [ "$active" = 0 ]; then reap_stale_control_markers || true; fi
    if [ "$active" = 0 ]; then retry_pending_phone_pause || true; fi
    if [ "$active" = 0 ]; then repair_active_airplay_stock_mute || true; fi
    if [ "$active" = 0 ]; then observe_airplay_takeover || true; fi
    reap_stale_airplay_claim || true
    if [ "$active" = 0 ] && ! audio_marker_present "$SAFE/tripped" &&
       ! audio_marker_present "$RELEASE" && ! audio_voice_pending &&
       ! audio_play_marker_live && ! ap2_recent_claim &&
       watchdog_live; then
        refresh_paused_source
        reap_disconnected_bluetooth_marker || true
        reap_completed_airplay_release_marker || true
        reap_orphaned_bluetooth_slave || true
        repair_stalled_bluetooth || true
    fi
    if [ "$active" = 1 ]; then
        heartbeat || { "$TEST" mute >/dev/null 2>&1 || true; break; }
        if audio_marker_present "$SAFE/tripped" ||
           audio_marker_present "$RELEASE" ||
           audio_marker_present "$WAIT_PCM" ||
           ! audio_play_marker_live ||
           ! bt_playing; then
            release_output
        fi
    elif ! audio_marker_present "$SAFE/tripped" &&
         ! audio_marker_present "$RELEASE" &&
         ! audio_voice_pending && ! audio_play_marker_live &&
         ! ap2_recent_claim && bluetooth_acquisition_ready; then
        trace 'Bluetooth acquisition passed the unlocked gate'
        if lock_speaker; then
            trace 'Bluetooth acquisition holds the speaker lock'
            if ! audio_marker_present "$SAFE/tripped" &&
               ! audio_marker_present "$RELEASE" &&
               ! audio_voice_pending && ! audio_play_marker_live &&
               ! ap2_recent_claim && bluetooth_acquisition_locked; then
                trace 'Bluetooth acquisition passed the locked gate'
                rm -f "$WAIT_PCM" || { audio_unlock "$LOCK"; "$TEST" mute >/dev/null 2>&1 || true; break; }
                reset_wait_evidence
                wait_source_playing=0
                BT_SEQ=$((${BT_SEQ:-0} + 1))
                BT_TOKEN="bt-$$-$(now)-$BT_SEQ"
                heartbeat && audio_write_control "$OWNER" "$$" && active=1
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
