#!/bin/sh

# The AP2 session hook only opens the speaker after a separate watchdog has
# published a fresh lease.  A missing/stale watchdog or a prior trip leaves
# the speaker muted.

ROOT=${HK_AP2_ROOT:-/tmp/hk-ap2/opt}
RUN=${HK_AP2_RUN:-/tmp/hk-ap2/run}
SAFE="$RUN/safe"
AMIXER=${HK_AP2_AMIXER:-/system/workdir/bin/amixer}
AMIXER_LIB=/system/workdir/lib
MASTER=${HK_AP2_MASTER_SAFE:-4}
DIGITAL=${HK_AP2_DIGITAL_SAFE:-128}
PLAYBACK=${HK_AP2_PLAYBACK_SAFE:-0}
USE_STOCK_PA=${HK_AP2_USE_STOCK_PA:-0}
PA_WARMUP_MS=${HK_AP2_PA_WARMUP_MS:-200}
APPLE_REMOTE=${HK_AP2_APPLE_REMOTE:-/system/workdir/bin/apple_remote}
APPLE_REMOTE_SOCKET=${HK_AP2_APPLE_REMOTE_SOCKET:-/tmp/RequestAppleRemote}
PA_UNMUTE_LIB=${HK_AP2_PA_UNMUTE_LIB:-$ROOT/lib/libhk-airplay-unmute.so}
PA_MUTE_LIB=${HK_AP2_PA_MUTE_LIB:-$ROOT/lib/libhk-airplay-mute.so}
BT_PA_READY=$RUN/bt-pa-ready
BT_COLD_GLOBAL=${HK_BT_COLD_GLOBAL:-${RUN%/run}/bt-cold-global-mute}
BT_PCM_OWNER=${HK_BT_PCM_OWNER:-${RUN%/run}/bt-pcm-owner}
AIRPLAY_REPAIR_GAIN="$RUN/airplay-mute-repair-gain"
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
BT_OWNER="$RUN/bt-owner.pid"
BT_RELEASE="$RUN/bt-release"
RELEASE_INTENT="$RUN/release-intent"
BT_PAUSE_PENDING="$RUN/bt-pause-pending"
PCM_STATUS=${HK_AP2_PCM_STATUS:-/proc/asound/card0/pcm0p/sub0/status}
PROC_ROOT=${HK_AP2_PROC_ROOT:-/proc}
HOLD_MUTE=${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}
BT_PCM_CONTROL=${HK_BT_PCM_CONTROL:-${RUN%/run}/bt-pcm-control}
BT_PCM_OPENING=${HK_BT_PCM_OPENING:-${RUN%/run}/bt-pcm-opening}
REQUEST=${HK_AP2_REQUEST:-$ROOT/bin/hk-unix-request-raw}
STOCK_AUDIO_STATE=${HK_AP2_STOCK_AUDIO_STATE:-/vendor/airplay2/hk-stock-audio-state}
STOCK_AUDIO_LIB=${HK_AP2_STOCK_AUDIO_LIB:-/system/workdir/lib/libmvmsg.so}
STOCK_AUDIO_LIB_SHA256=${HK_AP2_STOCK_AUDIO_LIB_SHA256:-7614d522b8c42e7934aed707e3d27460df2a39765402c600aa5ce35333d373ab}
BT_PLAYER_SOCKET=${HK_BT_PLAYER_SOCKET:-/tmp/Requesta01controller}
IOGUARD_SOCKET=${HK_AP2_IOGUARD_SOCKET:-/tmp/RequestIOGuard}
COMMON=${HK_AUDIO_COMMON:-/vendor/airplay2/hk-audio-common.sh}
[ -r "$COMMON" ] || COMMON=$(dirname "$0")/hk-audio-common.sh
[ -r "$COMMON" ] || exit 1
. "$COMMON"

# The AP2 loader must not leak its glibc path into stock shell commands.
unset LD_LIBRARY_PATH

hook_log() {
    _hl_uptime=
    if [ -n "${HK_AP2_BT:-}" ]; then
        IFS=' ' read -r _hl_stamp _hl_rest <"$AUDIO_UPTIME" 2>/dev/null &&
            _hl_uptime="uptime=$_hl_stamp "
    fi
    printf '%s %s%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || date)" "$_hl_uptime" "$*" \
        >>"$HOOK_LOG" 2>/dev/null || true
    audio_trim_log "$HOOK_LOG" 65536
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
        sed -n 's/.*\[\([a-z]*\)\].*/\1/p' |
        sed -n '1p'
}

speaker_lock() { audio_lock "$SPEAKER_LOCK"; }
speaker_unlock() { audio_unlock "$SPEAKER_LOCK" 2>/dev/null || true; }
claim_owned=0
claim_value=
publish_claim() {
    audio_set_self || return 1
    _pc_start=$(audio_pid_start "$AUDIO_SELF_PID") || return 1
    _pc_now=$(audio_now) || return 1
    _pc_value="$_pc_now $AUDIO_SELF_PID $_pc_start $$-$_pc_now"
    audio_write_control "$CLAIM" "$_pc_value" || return 1
    claim_value=$_pc_value
    claim_owned=1
}
drop_claim() {
    if [ "${claim_owned:-0}" = 1 ] &&
       [ "$(audio_control_read "$CLAIM")" = "${claim_value:-}" ]; then
        rm -f "$CLAIM"
    fi
    claim_owned=0
    claim_value=
}

# The voice flag holds the uptime it was taken; ignore it after 5 minutes
# in case the voice assistant died without clearing it.
voice_owns() { audio_voice_pending; }

read_file() {
    audio_control_read "$1" || true
}

watchdog_alive() {
    audio_watchdog_live
}

watchdog_process_alive() {
    audio_watchdog_pid >/dev/null
}

stock_pa_mute() {
    level=$1
    if [ "$level" = 0 ]; then
        preload=$PA_UNMUTE_LIB
    else
        preload=$PA_MUTE_LIB
    fi
    [ -x "$APPLE_REMOTE" ] && [ -r "$preload" ] || return 1

    rm -f "$APPLE_REMOTE_SOCKET"
    : >"$PA_LOG"
    env LD_LIBRARY_PATH="$AMIXER_LIB:/lib" LD_PRELOAD="$preload" \
        "$APPLE_REMOTE" >>"$PA_LOG" 2>&1 &
    pid=$!
    # A Bluetooth source can stop during setup and terminate this hook. Do
    # not leave the temporary helper holding the runtime mount open.
    trap 'kill "$pid" 2>/dev/null || true; start_failed "interrupted PA setup"' HUP INT TERM
    # The preload constructor does the work right away and logs it; poll
    # for that line instead of a fixed 1 s wait (cap 1.5 s).
    n=0
    while [ "$n" -lt 15 ]; do
        usleep 100000 2>/dev/null || sleep 1
        grep -q "Snd_Ctrl airplay to mute $level" "$PA_LOG" 2>/dev/null && break
        n=$((n + 1))
    done
    usleep 100000 2>/dev/null || true
    rm -f "$APPLE_REMOTE_SOCKET"
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    trap 'start_failed "interrupted speaker transition"' HUP INT TERM

    # apple_remote may exit after the constructor because one stock helper
    # symbol is unavailable. The constructor runs first and is the operation
    # this hook needs.
    tail -n 8 "$PA_LOG" 2>/dev/null |
        grep -q "Snd_Ctrl airplay to mute $level"
}

active_airplay_output_ready() {
    [ -z "${HK_AP2_BT:-}" ] && [ -z "${HK_AP2_VOICE:-}" ] &&
        ! audio_marker_present "$HOLD_MUTE" &&
        audio_regular_file "$SAFE/armed" &&
        ! audio_marker_present "$SAFE/tripped" &&
        ! audio_marker_present "$BT_OWNER" &&
        audio_play_marker_live &&
        audio_read_lease && [ "$lease_source" = airplay ] &&
        [ "$lease_phase" = active ] && audio_lease_is_caller &&
        audio_voice_generation_valid && watchdog_alive &&
        pcm_clock_live &&
        [ "$(switch_state 'Ext Spk')" = on ] &&
        [ "$(switch_state 'aml audio i2s mute')" = off ] &&
        [ "$(switch_state 'Digital')" = on ]
}

repair_active_airplay_stock_mute() {
    # A phone connection can change the stock amplifier mute while AirPlay
    # still owns the running DSP clock. Recover only on an exact active AirPlay
    # path. The DAC is made silent before either original unmute interface is
    # used, so a stale mute report can never create an audible transition.
    active_airplay_output_ready || return 1
    [ -x "$STOCK_AUDIO_STATE" ] && [ -x "$REQUEST" ] &&
        [ -x "$APPLE_REMOTE" ] || return 1
    [ "$(sha256sum "$STOCK_AUDIO_LIB" 2>/dev/null | awk '{print $1}')" = \
        "$STOCK_AUDIO_LIB_SHA256" ] || return 1
    _rsp_state=$(env LD_LIBRARY_PATH="$AMIXER_LIB" "$STOCK_AUDIO_STATE") ||
        return 1
    case "$_rsp_state" in
        'PA mute=0 force_mute=0 gate=1') return 0;;
        'PA mute=0 force_mute=1 gate=1'|\
        'PA mute=1 force_mute=0 gate=1'|\
        'PA mute=1 force_mute=1 gate=1') ;;
        *) return 1;;
    esac

    audio_read_lease || return 1
    _rsp_token=$lease_token
    _rsp_time=$lease_time
    _rsp_gain=
    if audio_marker_present "$AIRPLAY_REPAIR_GAIN"; then
        _rsp_record=$(audio_control_read "$AIRPLAY_REPAIR_GAIN") || return 1
        _rsp_saved_token=
        _rsp_saved_time=
        _rsp_saved_gain=
        _rsp_saved_extra=
        read -r _rsp_saved_token _rsp_saved_time _rsp_saved_gain \
            _rsp_saved_extra <<EOF
$_rsp_record
EOF
        if [ -z "$_rsp_saved_extra" ] &&
           [ "$_rsp_saved_token" = "$_rsp_token" ] &&
           [ "$_rsp_saved_time" = "$_rsp_time" ]; then
            _rsp_gain=$_rsp_saved_gain
        else
            rm -f "$AIRPLAY_REPAIR_GAIN"
        fi
    fi
    if [ -z "$_rsp_gain" ]; then
        _rsp_gain=$(mixer_values 1)
        # A prior-generation repair must not turn its temporary zero into the
        # next generation's target. A guarded AirPlay start normally leaves a
        # non-zero mixer gain; use the configured safe gain if it did not.
        [ "$_rsp_gain" != '0,0' ] || _rsp_gain="$DIGITAL,$DIGITAL"
    fi
    _rsp_left=${_rsp_gain%%,*}
    _rsp_right=${_rsp_gain#*,}
    case "$_rsp_left:$_rsp_right" in
        *[!0-9:]*|:|*:) return 1;;
    esac
    [ "$_rsp_left" -le 255 ] && [ "$_rsp_right" -le 255 ] || return 1
    audio_write_control "$AIRPLAY_REPAIR_GAIN" \
        "$_rsp_token $_rsp_time $_rsp_gain" || return 1
    mixer_cset 1 0,0 || return 1
    usleep 100000 2>/dev/null || sleep 1
    [ "$(mixer_values 1)" = 0,0 ] || return 1

    _rsp_global=0
    _rsp_fallback=0
    _rsp_try=0
    while [ "$_rsp_try" -lt 15 ]; do
        active_airplay_output_ready || break
        _rsp_state=$(env LD_LIBRARY_PATH="$AMIXER_LIB" \
            "$STOCK_AUDIO_STATE" 2>/dev/null) || break
        case "$_rsp_state" in
            'PA mute=0 force_mute=0 gate=1')
                output_guard_ok && pcm_clock_live || break
                mixer_cset 1 "$_rsp_gain" || break
                [ "$(mixer_values 1)" = "$_rsp_gain" ] || break
                rm -f "$AIRPLAY_REPAIR_GAIN"
                hook_log "active AirPlay amplifier mute repaired"
                return 0
                ;;
            'PA mute=0 force_mute=1 gate=1'|\
            'PA mute=1 force_mute=1 gate=1')
                if [ "$_rsp_global" = 0 ]; then
                    "$REQUEST" "$IOGUARD_SOCKET" GNOTIFY=AUDIOMUTE:0 \
                        1200 >/dev/null 2>&1 || true
                    _rsp_global=1
                elif [ "$_rsp_fallback" = 0 ]; then
                    stock_pa_mute 0 || break
                    _rsp_fallback=1
                fi
                ;;
            'PA mute=1 force_mute=0 gate=1')
                if [ "$_rsp_fallback" = 0 ]; then
                    stock_pa_mute 0 || break
                    _rsp_fallback=1
                fi
                ;;
            *) break;;
        esac
        usleep 100000 2>/dev/null || sleep 1
        _rsp_try=$((_rsp_try + 1))
    done
    # The amplifier is still muted or the ownership changed. Keep the DAC at
    # zero; the watchdog and the next source's normal guarded start retain
    # authority.
    hook_log "active AirPlay amplifier mute repair failed closed"
    return 1
}

pause_bluetooth_for_airplay() {
    # Parking releases the local sound card; AVRCP pause also stops the
    # phone's timeline. Never send resume when AirPlay later releases it.
    _bp_marker="airplay-$(audio_now)"
    audio_write_control "$RUN/bt-wait-pcm" "$_bp_marker" || return 1
    # The pause is desirable, but a transient stock-player RPC failure must
    # never leave AirPlay silent. The bridge retries this marker while AirPlay
    # owns the speaker and suppresses Bluetooth reverse takeover meanwhile.
    if ! _bp_data=$("$REQUEST" "$BT_PLAYER_SOCKET" getPlayerStatus 1200 2>/dev/null); then
        audio_write_control "$BT_PAUSE_PENDING" "$_bp_marker" || return 1
        hook_log 'Bluetooth status unavailable; AirPlay proceeds with pause retry pending'
        return 0
    fi
    _bp_mode=$(printf '%s' "$_bp_data" | sed -n 's/.*"mode":"\([^"]*\)".*/\1/p')
    case "$_bp_mode" in
        41) ;;
        -1)
            rm -f "$BT_PAUSE_PENDING"
            return 0
            ;;
        ''|*[!0-9]*)
            audio_write_control "$BT_PAUSE_PENDING" "$_bp_marker" || return 1
            hook_log 'Bluetooth mode unrecognized; AirPlay proceeds with pause retry pending'
            return 0
            ;;
        *)
            audio_write_control "$BT_PAUSE_PENDING" "$_bp_marker" || return 1
            hook_log "Bluetooth mode $_bp_mode is not playable; AirPlay proceeds"
            return 0
            ;;
    esac
    case "$_bp_data" in
        *'"status":"stop"'*|*'"status":"pause"'*)
            rm -f "$BT_PAUSE_PENDING"
            return 0
            ;;
    esac
    if ! printf '%s' "$_bp_data" | grep -q '"status":"play"'; then
        audio_write_control "$BT_PAUSE_PENDING" "$_bp_marker" || return 1
        hook_log 'Bluetooth status is not playable; AirPlay proceeds with retry pending'
        return 0
    fi
    "$REQUEST" "$BT_PLAYER_SOCKET" setPlayerCmd:onepause 1200 >/dev/null 2>&1 || true
    _bp_n=0
    while [ "$_bp_n" -lt 3 ]; do
        if ! _bp_data=$("$REQUEST" "$BT_PLAYER_SOCKET" getPlayerStatus 1200 2>/dev/null); then
            audio_write_control "$BT_PAUSE_PENDING" "$_bp_marker" || return 1
            hook_log 'Bluetooth pause acknowledgement unavailable; AirPlay proceeds with retry pending'
            return 0
        fi
        case "$_bp_data" in
            *'"mode":"-1"'*|*'"status":"stop"'*|*'"status":"pause"'*)
                hook_log 'Bluetooth phone pause acknowledged before AirPlay takeover'
                rm -f "$BT_PAUSE_PENDING"
                return 0;;
        esac
        usleep 100000 2>/dev/null || sleep 1
        _bp_n=$((_bp_n + 1))
    done
    audio_write_control "$BT_PAUSE_PENDING" "$_bp_marker" || return 1
    hook_log 'Bluetooth pause not acknowledged; AirPlay proceeds with retry pending'
    return 0
}

stock_sync_global_mute() {
    # A source can be unmuted while the stock global override still holds
    # the amplifier closed. Bluetooth validates explicit source intent;
    # another source calls this only to recover our retained cold marker.
    # Both require a guarded silent clock before restoring DAC gain.
    [ "$(mixer_values 1)" = '0,0' ] &&
        [ "$(switch_state 'Ext Spk')" = off ] && pcm_clock_live && output_guard_ok || return 1
    [ -x "$STOCK_AUDIO_STATE" ] && [ -x "$REQUEST" ] || return 1
    [ "$(sha256sum "$STOCK_AUDIO_LIB" 2>/dev/null | awk '{print $1}')" = "$STOCK_AUDIO_LIB_SHA256" ] || return 1
    _bg_state=$(env LD_LIBRARY_PATH="$AMIXER_LIB" "$STOCK_AUDIO_STATE") ||
        return 1
    case "$_bg_state" in
        'PA mute=0 force_mute=0 gate=1')
            rm -f "$BT_COLD_GLOBAL"
            return 0
            ;;
        'PA mute=0 force_mute=1 gate=1'|'PA mute=1 force_mute=0 gate=1'|\
        'PA mute=1 force_mute=1 gate=1')
            ;;
        *) return 1 ;;
    esac

    # GNOTIFY=0 clears the stock global force-mute. A source-specific PA mute
    # can remain afterwards (notably after an AirPlay stop), in which case a
    # second GNOTIFY=0 is a real-firmware no-op. Keep the amplifier closed, use
    # the already verified native AirPlay unmute helper only as a fallback,
    # then require both independent flags to read back as clear.
    _bg_sent_global=0
    _bg_used_pa_fallback=0
    _bg_try=0
    while [ "$_bg_try" -lt 10 ]; do
        output_guard_ok && pcm_clock_live &&
            [ "$(mixer_values 1)" = '0,0' ] &&
            [ "$(switch_state 'Ext Spk')" = off ] || return 1
        _bg_state=$(env LD_LIBRARY_PATH="$AMIXER_LIB" "$STOCK_AUDIO_STATE") ||
            return 1
        case "$_bg_state" in
            'PA mute=0 force_mute=0 gate=1')
                rm -f "$BT_COLD_GLOBAL"
                hook_log "stock global and amplifier mute cleared and verified"
                return 0
                ;;
            'PA mute=0 force_mute=1 gate=1')
                if [ "$_bg_sent_global" = 0 ]; then
                    hook_log "Bluetooth amplifier global mute mismatch; synchronizing through stock IOGuard"
                    "$REQUEST" "$IOGUARD_SOCKET" GNOTIFY=AUDIOMUTE:0 1200 \
                        >/dev/null 2>&1 || true
                    _bg_sent_global=1
                fi
                ;;
            'PA mute=1 force_mute=0 gate=1'|\
            'PA mute=1 force_mute=1 gate=1')
                if [ "$_bg_sent_global" = 0 ]; then
                    hook_log "Bluetooth amplifier mute mismatch; synchronizing through stock IOGuard"
                    "$REQUEST" "$IOGUARD_SOCKET" GNOTIFY=AUDIOMUTE:0 1200 \
                        >/dev/null 2>&1 || true
                    _bg_sent_global=1
                elif [ "$_bg_used_pa_fallback" = 0 ]; then
                    hook_log "PA-only mute remains after stock IOGuard; using verified native unmute"
                    stock_pa_mute 0 || return 1
                    _bg_used_pa_fallback=1
                fi
                ;;
            *) return 1 ;;
        esac
        _bg_try=$((_bg_try + 1))
        usleep 100000 2>/dev/null || sleep 1
    done
    return 1
}


bt_sync_global_mute() {
    _bg_player=$("$REQUEST" "$BT_PLAYER_SOCKET" getPlayerStatus 1200 2>/dev/null) || return 1
    printf '%s' "$_bg_player" | grep -q '"mode":"41"' &&
        printf '%s' "$_bg_player" | grep -q '"status":"play"' &&
        printf '%s' "$_bg_player" | grep -q '"mute":"0"' || return 1
    stock_sync_global_mute
}

bt_cold_pre_mute() {
    # A source mute alone need not close the shared PA. Hold the official
    # global override across a fresh clock's analogue/I2S transitions.
    [ "$(mixer_values 1)" = '0,0' ] && [ "$(switch_state 'Ext Spk')" = off ] &&
        output_guard_ok || return 1
    if ! pcm_clock_live; then
        # A connected-but-paused phone can briefly publish frames and then
        # return to a stopped PCM. No output has been enabled at this point.
        # Let the bridge close/retry instead of latching a global fault.
        audio_bt_clock_stopped && return 10
        return 1
    fi
    audio_write_control "$BT_COLD_GLOBAL" "${HK_AUDIO_TOKEN:-cold-clock}" ||
        return 1
    "$REQUEST" "$IOGUARD_SOCKET" GNOTIFY=AUDIOMUTE:1 1200 >/dev/null 2>&1 || true
    _bc_try=0
    while [ "$_bc_try" -lt 10 ]; do
        output_guard_ok || return 1
        if ! pcm_clock_live; then
            audio_bt_clock_stopped && return 10
            return 1
        fi
        _bc_state=$(env LD_LIBRARY_PATH="$AMIXER_LIB" "$STOCK_AUDIO_STATE") || return 1
        # PA mute and the global force-mute are independent. Either one
        # closes the shared amplifier while Ext Spk is off and the DAC is 0.
        case "$_bc_state" in
            'PA mute=1 force_mute=0 gate=1'|\
            'PA mute=0 force_mute=1 gate=1'|\
            'PA mute=1 force_mute=1 gate=1')
                return 0
                ;;
        esac
        _bc_try=$((_bc_try + 1))
        usleep 100000 2>/dev/null || sleep 1
    done
    return 1
}

restore_cold_global_mute() {
    [ -f "$BT_COLD_GLOBAL" ] || return 0
    # Failed/cancelled starts must not leave their temporary override for
    # another source. Closing output precedes this cleanup; no clock opens.
    [ "$(mixer_values 1)" = '0,0' ] && [ "$(switch_state 'Ext Spk')" = off ] &&
        [ "$(switch_state 'aml audio i2s mute')" = on ] || return 1
    [ "$(sha256sum "$STOCK_AUDIO_LIB" 2>/dev/null | awk '{print $1}')" = "$STOCK_AUDIO_LIB_SHA256" ] || return 1
    "$REQUEST" "$IOGUARD_SOCKET" GNOTIFY=AUDIOMUTE:0 1200 >/dev/null 2>&1 || true
    if env LD_LIBRARY_PATH="$AMIXER_LIB" "$STOCK_AUDIO_STATE" --global-unmuted >/dev/null 2>&1; then
        rm -f "$BT_COLD_GLOBAL"
        return 0
    fi
    hook_log 'temporary Bluetooth global mute cleanup awaits next guarded start'
    return 1
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

publish_release_intent() {
    # Publish an exact release generation before the multi-file lease cleanup.
    # The watchdog may otherwise read play.started just before it disappears
    # and then read no lease, which must not be mistaken for a corrupted lease.
    audio_read_lease || return 0
    audio_write_control "$RELEASE_INTENT" \
        "$lease_source $lease_token $lease_time"
}

clear_play_lease() {
    publish_release_intent || true
    rm -f "$SAFE/play.started" "$SAFE/play.started.tmp" \
        "$SAFE/lease.request" "$SAFE/lease.ack" \
        "$SAFE/lease" "$SAFE/lease.tmp"
}

publish_play_lease() {
    audio_reserve || return 1
    # audio_reserve deliberately reuses a valid lease for the same owner. A
    # guarded reopen still needs to republish the start marker before asking
    # for a new nonce; otherwise the watchdog has no request to acknowledge.
    audio_write_control "$SAFE/play.started" "$lease_time"
}

publish_lease_request() {
    # The lease token can be reused when the same owner reopens after its
    # output was closed. A per-start nonce prevents an ACK from the previous
    # open from satisfying the new one.
    lease_request_nonce="$$-$(audio_now)"
    audio_write_control "$SAFE/lease.request" \
        "$lease_token $lease_request_nonce"
}

clear_airplay_wait_marker() {
    [ -z "${HK_AP2_BT:-}" ] && [ -z "${HK_AP2_VOICE:-}" ] || return 0
    case "$(audio_control_read "$RUN/bt-wait-pcm")" in
        airplay-*)
            rm -f "$RUN/bt-wait-pcm" "$BT_PAUSE_PENDING"
            ;;
    esac
}

# Called under speaker.lock, before reserving the new source's lease. The
# factory player retains its virtual Bluetooth PCM; its private backend can
# return the actual hardware only after the old output has been muted.
park_bluetooth_pcm() {
    _park_wait=0
    while [ -e "$BT_PCM_OPENING" ] && [ "$_park_wait" -lt 20 ]; do
        usleep 100000 2>/dev/null || sleep 1
        _park_wait=$((_park_wait + 1))
    done
    [ ! -e "$BT_PCM_OPENING" ] || return 1
    if [ "${HK_AP2_PARK_STOPPED_ONLY:-0}" = 1 ] &&
       ! audio_bt_clock_stopped; then
        # The orphan reaper may discover a stopped clock before taking the
        # speaker lock. New phone frames can arrive while it waits. In that
        # case the bridge must evaluate the now-running source normally rather
        # than parking a live clock underneath it.
        return 0
    fi
    _park_pcm=${HK_AP2_PCM_STATUS:-/proc/asound/card0/pcm0p/sub0/status}
    _park_owner=$(sed -n 's/^owner_pid[[:space:]]*:[[:space:]]*//p' "$_park_pcm" 2>/dev/null)
    case "$_park_owner" in ''|*[!0-9]*) return 0;; esac
    audio_bt_pcm_owned "$_park_owner" || return 0
    [ ! -e "$BT_OWNER" ] && ! audio_play_marker_live && [ ! -e "$SAFE/lease" ] || return 1
    [ -S "$BT_PCM_CONTROL" ] && [ -x "$REQUEST" ] || {
        hook_log 'Bluetooth PCM cannot be released: private backend unavailable'
        return 1
    }
    hard_mute
    [ "$(mixer_values 1)" = '0,0' ] &&
        [ "$(switch_state 'Ext Spk')" = off ] &&
        [ "$(switch_state 'aml audio i2s mute')" = on ] || return 1
    _park_token="$$-$(audio_now)"
    audio_write_control "$RUN/bt-pcm-park" "$_park_token" || return 1
    _park_reply=$("$REQUEST" "$BT_PCM_CONTROL" "park:$_park_token" 1800 2>/dev/null)
    _park_rc=$?
    rm -f "$RUN/bt-pcm-park"
    [ "$_park_rc" = 0 ] && [ "$_park_reply" = parked ] || return 1
    _park_try=0
    while [ "$_park_try" -lt 10 ]; do
        case "$(sed -n '1p' "$_park_pcm" 2>/dev/null)" in
            closed|'state: closed'|'state: CLOSED')
                hook_log 'Bluetooth hardware PCM released before new lease'
                return 0;;
        esac
        _park_try=$((_park_try + 1))
        usleep 100000 2>/dev/null || sleep 1
    done
    return 1
}

wait_for_lease_ack() {
    # A heartbeat only proves that the watchdog loop ran. That loop writes the
    # heartbeat before it reads the lease, so a new owner can receive an old
    # acknowledgement and then be hard-muted by the same no-lease iteration.
    # The watchdog acknowledges the lease token and this start's nonce only
    # after reading them and validating the source owner.
    _wfla_started=$(audio_now) || return 1
    _wfla_deadline=$((_wfla_started + 12))
    tries=0
    while [ "$tries" -lt 120 ]; do
        # During a source hand-off the heartbeat can age out while the exact
        # watchdog process is still alive and finishing the previous stop.
        # Keep output closed and require its exact token+nonce ACK; do not
        # discard the new generation merely because the old heartbeat expired.
        watchdog_process_alive || return 1
        _wfla_now=$(audio_now) || return 1
        [ "$_wfla_now" -lt "$_wfla_deadline" ] || return 1
        usleep 100000 2>/dev/null || sleep 1
        watchdog_process_alive || return 1
        audio_read_lease || return 1
        ack_token=
        ack_nonce=
        ack_extra=
        if [ -f "$SAFE/lease.ack" ] && [ ! -L "$SAFE/lease.ack" ]; then
            { read -r ack_token ack_nonce ack_extra <"$SAFE/lease.ack"; } \
                2>/dev/null || true
        fi
        [ -z "${ack_extra:-}" ] &&
            [ "$ack_token" = "$lease_token" ] &&
            [ "$ack_nonce" = "$lease_request_nonce" ] &&
            return 0
        tries=$((tries + 1))
    done
    return 1
}

pcm_clock_live() {
    grep -q '^state: RUNNING' "$PCM_STATUS" 2>/dev/null || return 1
    pcm_owner=$(sed -n 's/^owner_pid[[:space:]]*:[[:space:]]*//p' "$PCM_STATUS" 2>/dev/null)
    case "$pcm_owner" in
        ''|*[!0-9]*) return 1 ;;
    esac
    pcm_comm=$(cat "$PROC_ROOT/$pcm_owner/comm" 2>/dev/null)
    if [ -n "${HK_AP2_BT:-}" ]; then
        audio_bt_pcm_owned "$pcm_owner"
    elif [ -n "${HK_AP2_VOICE:-}" ]; then
        [ "$pcm_comm" = aplay ]
    else
        [ "$pcm_owner" = "$(read_file "$RUN/dsp-bridge.pid")" ]
    fi
}

output_guard_ok() {
    ! audio_marker_present "$HOLD_MUTE" &&
    audio_regular_file "$SAFE/armed" &&
    ! audio_marker_present "$SAFE/tripped" &&
    audio_play_marker_live &&
    audio_lease_is_caller &&
    audio_voice_generation_valid &&
    watchdog_alive
}

wait_for_pcm_clock() {
    limit=${1:-80}
    tries=0
    while [ "$tries" -lt "$limit" ]; do
        output_guard_ok || return 1
        pcm_clock_live && return 0
        usleep 100000 2>/dev/null || sleep 1
        tries=$((tries + 1))
    done
    return 1
}

start_failed() {
    reason=$1
    hook_log "start failed: $reason"
    hard_mute
    drop_claim
    if [ "$USE_STOCK_PA" = "1" ] && [ -z "${HK_AP2_BT:-}" ]; then stock_pa_mute 1 || true; fi
    restore_cold_global_mute || true
    clear_play_lease
    clear_airplay_wait_marker
    mixer_set "Auto Mute" on || true
    exit 1
}

start_deferred() {
    reason=$1
    hook_log "start deferred: $reason"
    hard_mute
    drop_claim
    restore_cold_global_mute || true
    clear_play_lease
    mixer_set "Auto Mute" on || true
    # The bridge treats 10 as a recoverable source transition. The watchdog
    # remains authoritative and any real guard failure still exits 1 above.
    exit 10
}

case "${1:-}" in
    repair-stock-pa)
        speaker_lock || exit 1
        trap 'speaker_unlock' 0
        repair_active_airplay_stock_mute
        exit $?
        ;;
    park)
        speaker_lock || exit 1
        trap 'speaker_unlock' 0
        audio_regular_file "$SAFE/armed" && watchdog_alive || exit 1
        # Reserve voice intent before waiting for any in-flight Bluetooth
        # open. Its ordinary lease is still created by the existing guard.
        if [ -n "${HK_AP2_VOICE:-}" ]; then
            if [ -n "${HK_AUDIO_TOKEN:-}" ] &&
               [ "$(audio_control_read "$RUN/current-voice-token")" != "$HK_AUDIO_TOKEN" ]; then
                exit 1
            fi
            ! audio_play_marker_live && ! audio_marker_present "$SAFE/lease" &&
                ! audio_marker_present "$BT_OWNER" || exit 1
            _park_voice_now=$(audio_now) || exit 1
            audio_write_control "$VOICE_OWNS" "$_park_voice_now" &&
                audio_write_control "$RUN/voice-token" "${HK_AUDIO_TOKEN:-}" ||
                exit 1
        fi
        if ! park_bluetooth_pcm; then
            if [ -n "${HK_AP2_VOICE:-}" ] &&
               [ "$(audio_control_read "$RUN/voice-token")" = "${HK_AUDIO_TOKEN:-}" ]; then
                rm -f "$VOICE_OWNS" "$RUN/voice-token"
            fi
            exit 1
        fi
        ;;
    reserve)
        ! audio_marker_present "$HOLD_MUTE" && audio_regular_file "$SAFE/armed" &&
            ! audio_marker_present "$SAFE/tripped" && watchdog_alive || exit 1
        speaker_lock || exit 1
        trap 'speaker_unlock' 0
        audio_reserve
        exit $?
        ;;
    guard)
        # Every speaker user calls this before touching a mixer control or
        # switching the stock amplifier.  It is the single pop-free contract:
        # supervised, with a live clock owned by the caller.  "closed" is the
        # baseline before the shared start sequence; "active" is a path that
        # is already open but must be digitally silent before gain returns.
        # This command never changes a mixer control or enables output.
        case "${2:-closed}" in
            closed)
                wait_for_pcm_clock 5 || exit 1
                [ "$(switch_state 'Ext Spk')" = "off" ] || exit 1
                [ "$(switch_state 'aml audio i2s mute')" = "on" ] || exit 1
                ;;
            active)
                wait_for_pcm_clock 5 || exit 1
                [ "$(switch_state 'Ext Spk')" = "on" ] || exit 1
                [ "$(switch_state 'aml audio i2s mute')" = "off" ] || exit 1
                [ "$(switch_state 'Digital')" = "on" ] || exit 1
                [ "$(mixer_values 1)" = "0,0" ] || exit 1
                ;;
            *) exit 2 ;;
        esac
        hook_log "output guard passed mode=${2:-closed}"
        exit 0
        ;;
    start)
        takeover_bt=0
        hook_log "start requested armed=$(audio_regular_file "$SAFE/armed" && echo yes || echo no) tripped=$(audio_marker_present "$SAFE/tripped" && echo yes || echo no)"
        if [ -n "${HK_AP2_BT:-}" ] && voice_owns; then
            hook_log "Bluetooth start refused: voice owns speaker"
            exit 1
        fi
        if [ -z "${HK_AP2_VOICE:-}" ] && [ -z "${HK_AP2_BT:-}" ]; then
            speaker_lock || exit 1
            if [ -n "${HK_AP2_REOPEN:-}" ] && audio_marker_present "$BT_OWNER"; then
                speaker_unlock
                hook_log "AirPlay reopen refused: Bluetooth owns speaker"
                exit 1
            fi
            if voice_owns; then
                audio_write_control "$DEFERRED" "1" || exit 1
                speaker_unlock
                hook_log "start deferred: the voice assistant has the speaker"
                exit 0
            fi
            if ! audio_marker_present "$HOLD_MUTE" &&
               audio_regular_file "$SAFE/armed" &&
               ! audio_marker_present "$SAFE/tripped" &&
               ! audio_marker_present "$BT_OWNER" &&
               audio_play_marker_live &&
               audio_lease_is_caller && [ "$lease_phase" = active ] &&
               watchdog_alive && pcm_clock_live &&
               [ "$(switch_state 'Ext Spk')" = on ] &&
               [ "$(switch_state 'aml audio i2s mute')" = off ] &&
               [ "$(switch_state 'Digital')" = on ]; then
                _ap_pa_ok=1
                if [ "$USE_STOCK_PA" = 1 ] && [ -x "$STOCK_AUDIO_STATE" ]; then
                    env LD_LIBRARY_PATH="$AMIXER_LIB" "$STOCK_AUDIO_STATE" --unmuted >/dev/null 2>&1 || _ap_pa_ok=0
                fi
                if [ "$_ap_pa_ok" = 1 ]; then
                    [ -f "$CLAIM" ] || publish_claim || true
                    speaker_unlock
                    hook_log "AirPlay speaker path already active"
                    exit 0
                elif repair_active_airplay_stock_mute; then
                    [ -f "$CLAIM" ] || publish_claim || true
                    speaker_unlock
                    exit 0
                fi
            fi
            publish_claim || { hook_log "could not publish AirPlay claim"; exit 1; }
            if audio_marker_present "$BT_OWNER"; then
                takeover_bt=1
                audio_write_control "$BT_RELEASE" "1" ||
                    { drop_claim; hook_log "could not request Bluetooth release"; exit 1; }
                speaker_unlock
                tries=0
                while audio_marker_present "$BT_OWNER" && [ "$tries" -lt 80 ]; do
                    usleep 100000 2>/dev/null || sleep 1
                    tries=$((tries + 1))
                done
                ! audio_marker_present "$BT_OWNER" || {
                    drop_claim
                    hook_log "Bluetooth did not release output"
                    exit 1
                }
                rm -f "$BT_RELEASE"
            else
                speaker_unlock
            fi
        fi
        ! audio_marker_present "$HOLD_MUTE" || { drop_claim; hook_log "start aborted: maintenance hold active"; exit 1; }
        audio_regular_file "$SAFE/armed" || { drop_claim; hook_log "start aborted: watchdog disarmed"; exit 1; }
        ! audio_marker_present "$SAFE/tripped" || { drop_claim; hook_log "start aborted: watchdog tripped ($(read_file "$SAFE/tripped"))"; exit 1; }
        watchdog_alive ||
            audio_watchdog_transition_live ||
            { drop_claim; hook_log "start aborted: watchdog heartbeat stale"; exit 1; }
        speaker_lock || { drop_claim; hook_log "start aborted: speaker lock unavailable"; exit 1; }
        trap 'speaker_unlock' 0
        trap 'start_failed "interrupted speaker transition"' HUP INT TERM
        if [ "$claim_owned" = 1 ]; then
            _claim_now=$(audio_control_read "$CLAIM") || _claim_now=
            if [ -z "$_claim_now" ]; then
                # A stale-claim reaper may have removed ours while no other
                # AirPlay start held the speaker lock. Recreate it atomically.
                publish_claim || start_failed "could not republish AirPlay claim"
            elif [ "$_claim_now" != "$claim_value" ]; then
                # Another AirPlay start owns the current claim. Preserve it and
                # let that generation continue; this older start must not race.
                claim_owned=0
                claim_value=
                hook_log "start superseded by a newer AirPlay claim"
                exit 1
            fi
        fi
        if [ -e "$SAFE/lease" ] && audio_read_lease && ! audio_lease_is_caller; then
            drop_claim
            hook_log "start refused: a different owner or generation holds the lease"
            exit 1
        fi
        audio_voice_generation_valid || start_failed "stale voice generation"
        if [ -n "${HK_AP2_BT:-}${HK_AP2_VOICE:-}" ] &&
           ! audio_play_marker_live && [ ! -e "$SAFE/lease" ]; then
            # A failed AirPlay start may have left a dead claim. A live claim
            # still represents an in-flight guarded start and retains source
            # priority for its bounded 30-second window.
            if audio_claim_recent "$CLAIM"; then
                hook_log "start deferred: an AirPlay start claim is active"
                exit 1
            fi
            rm -f "$CLAIM"
        fi
        if [ -z "${HK_AP2_BT:-}" ]; then
            _park_owner=$(sed -n 's/^owner_pid[[:space:]]*:[[:space:]]*//p' "$PCM_STATUS" 2>/dev/null)
            if [ -z "${HK_AP2_VOICE:-}" ] && audio_bt_pcm_owned "$_park_owner"; then takeover_bt=1; fi
            park_bluetooth_pcm || start_failed 'Bluetooth hardware PCM release failed'
            if [ -z "${HK_AP2_VOICE:-}" ] && [ -z "${HK_AP2_REOPEN:-}" ] &&
               { [ "$takeover_bt" = 1 ] || [ -S "$BT_PLAYER_SOCKET" ]; }; then
                # A parked/closed PCM can still have a connected phone.
                # Explicitly establish its paused state for this AirPlay
                # session so a later phone play edge can take it back.
                pause_bluetooth_for_airplay || start_failed 'Bluetooth phone pause failed'
            fi
        fi
        if audio_lease_is_caller && [ "$lease_phase" = active ] && pcm_clock_live &&
           [ "$(switch_state 'Ext Spk')" = on ]; then
            exit 0
        fi

        # Publish the lease before touching the mixer.  Otherwise the watchdog
        # can observe "no lease" between the baseline writes and the later
        # lease creation and hard-mute the path underneath this hook.
        rm -f "$RELEASE_INTENT" || start_failed "could not clear stale release intent"
        rm -f "$SAFE/lease.ack"
        publish_play_lease || start_failed "could not publish playback lease"
        publish_lease_request || start_failed "could not publish playback-start nonce"
        if [ -z "${HK_AP2_BT:-}" ] && [ -z "${HK_AP2_VOICE:-}" ]; then
            # AirPlay owns the connected phone from this point. Replace any
            # previous Bluetooth release token so it cannot be mistaken for
            # this AirPlay generation's release.
            audio_write_control "$RUN/bt-wait-pcm" "airplay-$(audio_now)" ||
                start_failed "could not publish AirPlay Bluetooth pause marker"
        fi
        if [ -n "${HK_AP2_BT:-}" ]; then
            # Only closing output may overlap acknowledgement: the watchdog
            # can still be finishing its preceding no-lease hard mute. Keep
            # baseline writes and every enable operation after its ack.
            mixer_cset 1 0,0 || start_failed "digital pre-mute failed"
            usleep 300000 2>/dev/null || sleep 1
        fi
        wait_for_lease_ack ||
            start_failed "watchdog did not acknowledge playback lease"

        bt_pa_key=
        if [ -n "${HK_AP2_BT:-}" ] && [ "$USE_STOCK_PA" = 1 ]; then
            [ -x "$STOCK_AUDIO_STATE" ] &&
                [ "$(sha256sum "$STOCK_AUDIO_LIB" 2>/dev/null | awk '{print $1}')" = "$STOCK_AUDIO_LIB_SHA256" ] ||
                start_failed 'Bluetooth stock layout verification failed'
            output_guard_ok && [ "$(mixer_values 1)" = '0,0' ] ||
                start_failed 'Bluetooth pre-mute guard failed'
            if ! pcm_clock_live; then
                audio_bt_clock_stopped &&
                    start_deferred 'Bluetooth clock unavailable before amplifier setup'
                start_failed 'Bluetooth pre-mute guard failed'
            fi
            # Only a verified descriptor generation can reuse the warm path.
            # Reconnect/park creates a new generation and needs a native
            # global pre-mute before analogue/I2S setup. Client3 is untouched.
            bt_pa_key=$(read_file "$BT_PCM_OWNER")
            if [ -z "$bt_pa_key" ] || [ "$(read_file "$BT_PA_READY")" != "$bt_pa_key" ]; then
                mixer_set 'Ext Spk' off || start_failed 'Bluetooth external pre-mute failed'
                bt_cold_pre_mute
                cold_rc=$?
                case "$cold_rc" in
                    0) ;;
                    10) start_deferred 'Bluetooth clock stopped before amplifier setup' ;;
                    *) start_failed 'Bluetooth cold amplifier pre-mute failed' ;;
                esac
                hook_log 'Bluetooth new PCM amplifier pre-mute verified'
            fi
        fi

        # Prepare every gain and switch while the physical path is still off.
        # The stock amplifier is enabled only once here, while the DAC remains
        # digitally silent; the clock is then allowed to run before that gain
        # returns. Repeatedly muting/unmuting the amplifier across clock and
        # I2S transitions is the click heard on AirPlay reconnects.
        if [ -z "${HK_AP2_BT:-}" ]; then
            mixer_cset 1 0,0 || start_failed "digital pre-mute failed"
            usleep 300000 2>/dev/null || sleep 1
        fi
        if [ "$USE_STOCK_PA" = "1" ] && [ -z "${HK_AP2_BT:-}" ]; then
            stock_pa_mute 1 || start_failed "stock PA pre-mute failed"
        fi
        mixer_set "Ext Spk" off || true
        mixer_set "Master" "$MASTER" || true
        baseline_digital=0
        mixer_cset 1 "$baseline_digital,$baseline_digital" || true
        mixer_cset 2 "$PLAYBACK,$PLAYBACK" || true
        mixer_set "Digital" on || true
        mixer_set "Auto Mute" off || true
        mixer_set "aml audio i2s mute" off || true

        [ "$(mixer_master)" = "$MASTER" ] || {
            start_failed "Master verification failed"
        }
        [ "$(mixer_values 1)" = "$baseline_digital,$baseline_digital" ] || {
            start_failed "Digital Playback Volume verification failed"
        }
        [ "$(mixer_values 2)" = "$PLAYBACK,$PLAYBACK" ] || {
            start_failed "Playback Volume verification failed"
        }
        [ "$(switch_state 'Digital')" = "on" ] || {
            start_failed "Digital switch verification failed"
        }
        [ "$(switch_state 'aml audio i2s mute')" = "off" ] || {
            start_failed "I2S enable verification failed"
        }
        [ "$(switch_state 'Ext Spk')" = "off" ] || {
            start_failed "external speaker mute baseline verification failed"
        }
        hook_log "mixer baseline verified master=$MASTER digital=$baseline_digital playback=$PLAYBACK"

        output_guard_ok || start_failed "output guard lost during baseline"
        wait_for_pcm_clock || start_failed "audio clock unavailable"
        if [ "$USE_STOCK_PA" = "1" ]; then
            if [ -n "${HK_AP2_BT:-}" ]; then
                bt_sync_global_mute || start_failed "Bluetooth global mute synchronization failed"
                hook_log "Bluetooth native amplifier acknowledged"
            else
                stock_pa_mute 0 || start_failed "stock PA unmute failed"
                if [ -f "$BT_COLD_GLOBAL" ]; then
                    stock_sync_global_mute || start_failed "orphaned Bluetooth global mute recovery failed"
                fi
                hook_log "stock PA unmuted"
            fi
        fi
        mixer_set "Ext Spk" on || start_failed "external speaker enable failed"
        [ "$(switch_state 'Ext Spk')" = "on" ] ||
            start_failed "external speaker enable verification failed"
        warmup=$((PA_WARMUP_MS * 1000))
        usleep "$warmup" 2>/dev/null || sleep 1
        output_guard_ok || start_failed "output guard lost during amplifier warm-up"
        wait_for_pcm_clock 5 || start_failed "audio clock lost during amplifier warm-up"
        [ "$(mixer_values 2)" = "$PLAYBACK,$PLAYBACK" ] ||
            start_failed "Playback Volume changed during amplifier warm-up"
        if [ "$USE_STOCK_PA" = "1" ] && [ -n "${HK_AP2_BT:-}" ]; then
            env LD_LIBRARY_PATH="$AMIXER_LIB" "$STOCK_AUDIO_STATE" --unmuted >/dev/null 2>&1 ||
                start_failed "Bluetooth stock amplifier is still muted"
        fi
        mixer_cset 1 "$DIGITAL,$DIGITAL" || start_failed "digital fade-in failed"
        [ "$(mixer_values 1)" = "$DIGITAL,$DIGITAL" ] ||
            start_failed "digital fade-in verification failed"
        output_guard_ok || start_failed "guard lost after fade-in"
        if [ -n "${HK_AP2_BT:-}" ] && [ -n "$bt_pa_key" ]; then
            [ "$(read_file "$BT_PCM_OWNER")" = "$bt_pa_key" ] || start_failed 'Bluetooth PCM changed during startup'
            audio_write_control "$BT_PA_READY" "$bt_pa_key" ||
                start_failed 'Bluetooth warm state publication failed'
        fi
        if [ "$lease_source" != voice ]; then
            audio_write_control "$RUN/media-source" "$lease_source" ||
                start_failed "media-source publication failed"
        fi
        lease_phase=active
        audio_write_lease || start_failed "could not commit active lease"
        hook_log "speaker enabled"
        exit 0
        ;;
    stop|mute)
        if [ "${1:-}" = stop ] && [ -z "${HK_AP2_VOICE:-}" ] && [ -z "${HK_AP2_BT:-}" ]; then
            if [ "${1:-}" = stop ] && audio_marker_present "$BT_OWNER"; then
                hook_log "AirPlay stop ignored: Bluetooth owns the speaker"
                [ "${HK_AP2_REVERSE_STOP:-0}" != 1 ] || exit 1
                exit 0
            fi
            if voice_owns; then
                hook_log "${1:-stop} ignored: the voice assistant has the speaker"
                [ "${HK_AP2_REVERSE_STOP:-0}" != 1 ] || exit 1
                exit 0
            fi
        fi
        speaker_lock || exit 1
        trap 'speaker_unlock' 0
        if [ "$1" = stop ] && [ "${HK_AP2_REVERSE_STOP:-0}" = 1 ]; then
            # Unlike an ordinary stop, a reverse-takeover request is valid
            # only for the exact active AirPlay lease it observed. The lease
            # can end or change while its caller waits for this lock.
            if ! { [ -n "${HK_AUDIO_TOKEN:-}" ] && audio_read_lease &&
                   [ "$lease_source" = airplay ] && [ "$lease_phase" = active ] &&
                   [ "$lease_token" = "$HK_AUDIO_TOKEN" ] &&
                   { [ -z "${HK_AUDIO_LEASE_TIME:-}" ] ||
                     [ "$lease_time" = "$HK_AUDIO_LEASE_TIME" ]; }; }; then
                hook_log 'reverse stop refused: AirPlay lease changed or ended'
                exit 1
            fi
        fi
        if [ "$1" = stop ] && [ -f "$SAFE/lease" ] && ! audio_read_lease; then
            start_failed "invalid lease on stop"
        fi
        if [ "$1" = stop ] && [ -f "$SAFE/lease" ] && ! audio_lease_is_caller; then
            hook_log "stop ignored: a different owner or generation holds the lease"
            exit 0
        fi
        if audio_read_lease; then lease_phase=draining; audio_write_lease || exit 1; fi
        hook_log "${1:-stop} requested"
        hard_mute
        if [ "$USE_STOCK_PA" = "1" ] && [ -z "${HK_AP2_BT:-}" ]; then stock_pa_mute 1 || true; fi
        restore_cold_global_mute || true
        clear_play_lease
        if [ "$1" = stop ]; then clear_airplay_wait_marker; fi
        if [ "$(audio_source)" = airplay ] || [ "$1" = mute ]; then rm -f "$DEFERRED" "$CLAIM"; fi
        mixer_set "Auto Mute" on || true
        exit 0
        ;;
    *)
        exit 2
        ;;
esac
