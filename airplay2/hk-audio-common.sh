#!/bin/sh
# Shared lightweight primitives. Sourcing this file never changes audio.
AUDIO_PROC=${HK_AP2_PROC_ROOT:-${HK_BT_PROC_ROOT:-/proc}}
AUDIO_UPTIME=${HK_AUDIO_UPTIME_FILE:-/proc/uptime}
AUDIO_ID_PROC=${HK_AUDIO_ID_PROC_ROOT:-/proc}
audio_regular_file() {
    [ -f "$1" ] && [ ! -L "$1" ]
}
audio_marker_present() {
    # Safety markers fail closed for any filesystem object, including a FIFO,
    # directory or dangling symlink. Authorizing markers must additionally use
    # audio_regular_file and a strict parser.
    [ -e "$1" ] || [ -L "$1" ]
}
audio_control_read() {
    audio_regular_file "$1" || return 1
    sed -n '1p' "$1" 2>/dev/null
}
audio_write_control() {
    _awc_path=$1
    _awc_value=$2
    audio_set_self || return 1
    _awc_start=$(audio_pid_start "$AUDIO_SELF_PID") || return 1
    # Include the process start time in the temporary name. A dead writer's
    # lock marker can then be distinguished from an unrelated process that has
    # inherited the same numeric PID.
    _awc_tmp="$_awc_path.tmp.$AUDIO_SELF_PID.$_awc_start"
    rm -f "$_awc_tmp" || return 1
    if ! (set -C; printf '%s\n' "$_awc_value" >"$_awc_tmp"); then
        rm -f "$_awc_tmp"
        return 1
    fi
    if ! mv "$_awc_tmp" "$_awc_path"; then
        rm -f "$_awc_tmp"
        return 1
    fi
}
audio_play_marker_live() {
    audio_regular_file "$SAFE/play.started" &&
        [ -s "$SAFE/play.started" ]
}
audio_claim_recent() {
    # A live hook keeps priority for the full guarded-start window. Legacy
    # claims contained only an uptime second and expire after eight seconds.
    _acr_value=$(audio_control_read "$1") || return 1
    _acr_time=
    _acr_pid=
    _acr_start=
    _acr_nonce=
    _acr_extra=
    read -r _acr_time _acr_pid _acr_start _acr_nonce _acr_extra <<EOF
$_acr_value
EOF
    [ -z "$_acr_extra" ] || return 1
    _acr_now=$(audio_now) || return 1
    case "$_acr_time:$_acr_now" in *[!0-9:]*|:*|*:) return 1;; esac
    [ "$_acr_time" -le "$_acr_now" ] || return 1
    if [ -z "$_acr_pid" ] && [ -z "$_acr_start" ] && [ -z "$_acr_nonce" ]; then
        [ $((_acr_now - _acr_time)) -lt 8 ]
        return
    fi
    case "$_acr_pid:$_acr_start" in
        *[!0-9:]*|:*|*:|*::*) return 1;;
    esac
    case "$_acr_nonce" in
        ''|*[!a-zA-Z0-9:_-]*) return 1;;
    esac
    [ $((_acr_now - _acr_time)) -le 30 ] || return 1
    audio_pid_live "$_acr_pid" "$_acr_start"
}
audio_now() {
    _an_time=$(cut -d. -f1 "$AUDIO_UPTIME" 2>/dev/null) || return 1
    case "$_an_time" in ''|*[!0-9]*) return 1;; esac
    printf '%s\n' "$_an_time"
}
audio_pid_start() {
    case "$1" in ''|*[!0-9]*|0|1) return 1;; esac
    if [ -r "$AUDIO_ID_PROC/$1/stat" ]; then
        sed 's/.*) //' "$AUDIO_ID_PROC/$1/stat" | awk 'NF>=20 {print $20}'
    elif [ ! -d /proc/self ] && [ "$AUDIO_ID_PROC" = /proc ]; then
        # Native macOS test host only. Linux never falls back from /proc.
        _aps_start=$(ps -p "$1" -o lstart= 2>/dev/null)
        [ -n "$_aps_start" ] || return 1
        printf '%s\n' "$_aps_start" | cksum | awk '{print $1}'
    else return 1
    fi
}

audio_pid_live() {
    case "$1:$2" in *[!0-9:]*|:*|*:|0:*|1:*) return 1;; esac
    kill -0 "$1" 2>/dev/null && [ "$(audio_pid_start "$1")" = "$2" ]
}
audio_delay() { usleep "$1" 2>/dev/null || sleep 1; }
audio_set_self() {
    # $$ stays at the parent PID inside an ash background function. Read
    # /proc/self with a shell builtin, not a command-substitution child.
    if [ -r /proc/self/stat ]; then
        IFS= read -r _as_stat </proc/self/stat || return 1
        AUDIO_SELF_PID=${_as_stat%% *}
    else AUDIO_SELF_PID=$$
    fi
}
audio_lock() {
    _al_path=$1
    _al_limit=${HK_AUDIO_LOCK_TRIES:-55}
    case "$_al_limit" in ''|*[!0-9]*|0) _al_limit=55;; esac
    _al_count=0
    _al_ownerless=0
    AUDIO_LOCK_WAITED=0
    while ! mkdir "$_al_path" 2>/dev/null; do
        AUDIO_LOCK_WAITED=1
        if [ -L "$_al_path/owner" ]; then
            return 1
        elif [ -s "$_al_path/owner" ]; then
            _al_ownerless=0
            if _al_owner=$(audio_control_read "$_al_path/owner"); then
                read -r _al_pid _al_start <<EOF
$_al_owner
EOF
                if ! audio_pid_live "$_al_pid" "$_al_start"; then
                    rm -f "$_al_path/owner"
                    rmdir "$_al_path" 2>/dev/null || true
                fi
            else
                [ ! -L "$_al_path/owner" ] || return 1
                continue
            fi
        elif [ ! -e "$_al_path/owner" ] && [ ! -L "$_al_path/owner" ] ||
             { [ -f "$_al_path/owner" ] && [ ! -L "$_al_path/owner" ] &&
               [ ! -s "$_al_path/owner" ]; }; then
            _al_ownerless=$((_al_ownerless + 1))
            if [ "$_al_ownerless" -ge 5 ]; then
                # A process killed between mkdir and publishing owner used to
                # leave a permanent ownerless lock. Give each new creator five
                # consecutive ownerless intervals, then remove only temporary
                # owner files whose writer is gone. A live creator is never
                # disturbed.
                _al_live_writer=0
                for _al_tmp in "$_al_path"/owner.tmp.*; do
                    [ -e "$_al_tmp" ] || [ -L "$_al_tmp" ] || continue
                    _al_tmp_name=${_al_tmp##*/}
                    _al_tmp_suffix=${_al_tmp_name#owner.tmp.}
                    _al_tmp_pid=${_al_tmp_suffix%%.*}
                    _al_tmp_start=
                    case "$_al_tmp_suffix" in
                        *.*) _al_tmp_start=${_al_tmp_suffix#*.};;
                    esac
                    case "$_al_tmp_pid" in
                        ''|*[!0-9]*) continue;;
                    esac
                    if [ -n "$_al_tmp_start" ]; then
                        if audio_pid_live "$_al_tmp_pid" "$_al_tmp_start"; then
                            _al_live_writer=1
                        else
                            rm -f "$_al_tmp"
                        fi
                    else
                        # Backward compatibility for a temp left by an older build.
                        if kill -0 "$_al_tmp_pid" 2>/dev/null; then
                            _al_live_writer=1
                        else
                            rm -f "$_al_tmp"
                        fi
                    fi
                done
                if [ "$_al_live_writer" = 0 ] && [ ! -s "$_al_path/owner" ]; then
                    if [ -f "$_al_path/owner" ] && [ ! -L "$_al_path/owner" ] &&
                       [ ! -s "$_al_path/owner" ]; then
                        rm -f "$_al_path/owner"
                    fi
                    rmdir "$_al_path" 2>/dev/null || true
                fi
                _al_ownerless=0
            fi
        fi
        _al_count=$((_al_count + 1))
        [ "$_al_count" -lt "$_al_limit" ] || return 1
        audio_delay 100000
    done
    audio_set_self || return 1
    _al_start=$(audio_pid_start "$AUDIO_SELF_PID")
    [ -n "$_al_start" ] || { rmdir "$_al_path"; return 1; }
    # Publish the complete owner record atomically. A contender used to be
    # able to read a half-written "pid" line, treat it as stale, and steal a
    # lock whose creator was still alive.
    audio_write_control "$_al_path/owner" "$AUDIO_SELF_PID $_al_start" ||
        { rmdir "$_al_path" 2>/dev/null || true; return 1; }
}
audio_try_lock() {
    HK_AUDIO_LOCK_TRIES=${2:-3} audio_lock "$1"
}
audio_unlock() {
    audio_set_self || return 1
    [ "$(audio_control_read "$1/owner")" = "$AUDIO_SELF_PID $(audio_pid_start "$AUDIO_SELF_PID")" ] || return 1
    rm -f "$1/owner" && rmdir "$1"
}
audio_trim_log() {
    _at_file=$1
    _at_max=${2:-65536}
    audio_regular_file "$_at_file" || return 0
    _at_size=$(wc -c <"$_at_file" 2>/dev/null) || return 1
    [ "$_at_size" -gt "$_at_max" ] || return 0
    audio_set_self || return 1
    _at_identity="$AUDIO_SELF_PID $(audio_pid_start "$AUDIO_SELF_PID")"
    _at_owner="$_at_file.trim-owner.$AUDIO_SELF_PID"
    rm -f "$_at_owner" || return 1
    (set -C; printf '%s\n' "$_at_identity" >"$_at_owner") || return 1
    # A hard link publishes an initialized owner atomically. A killed
    # rotator cannot leave an ownerless directory that disables rotation.
    if ! ln "$_at_owner" "$_at_file.trim-lock" 2>/dev/null; then
        _at_lock_owner=$(audio_control_read "$_at_file.trim-lock") || _at_lock_owner=
        _at_pid=
        _at_start=
        read -r _at_pid _at_start <<EOF
$_at_lock_owner
EOF
        if ! audio_pid_live "${_at_pid:-}" "${_at_start:-}"; then
            rm -f "$_at_file.trim-lock"
            ln "$_at_owner" "$_at_file.trim-lock" 2>/dev/null || { rm -f "$_at_owner"; return 0; }
        else rm -f "$_at_owner"; return 0
        fi
    fi
    rm -f "$_at_owner"
    # Writers use O_APPEND. Keep the inode; rename leaves a hidden unbounded
    # file behind when a daemon still owns its stdout descriptor.
    rm -f "$_at_file.trim"
    tail -c "$((_at_max / 2))" "$_at_file" >"$_at_file.trim" &&
        cat "$_at_file.trim" >"$_at_file"
    rm -f "$_at_file.trim"
    [ "$(audio_control_read "$_at_file.trim-lock")" != "$_at_identity" ] || rm -f "$_at_file.trim-lock"
}
audio_voice_pending() {
    _av_path="${HK_AP2_RUN:-$RUN}/voice-owns"
    if [ ! -e "$_av_path" ] && [ ! -L "$_av_path" ]; then return 1; fi
    # A present but malformed ownership marker fails closed. It could be a
    # torn/corrupted hand-off and must not silently grant the speaker to a
    # lower-priority source.
    _av_stamp=$(audio_control_read "$_av_path") || return 0
    _av_now=$(audio_now) || return 0
    case "$_av_stamp:$_av_now" in *[!0-9:]*|:*|*:) return 0;; esac
    [ "$_av_stamp" -le "$_av_now" ] && [ $((_av_now - _av_stamp)) -lt 300 ]
}

audio_source() {
    if [ -n "${HK_AP2_BT:-}" ]; then printf 'bluetooth\n'
    elif [ -n "${HK_AP2_VOICE:-}" ]; then printf 'voice\n'
    else printf 'airplay\n'; fi
}
audio_read_lease() {
    audio_regular_file "$SAFE/lease" && [ -s "$SAFE/lease" ] || return 1
    read -r lease_source lease_pid lease_start lease_token lease_time \
        lease_phase lease_extra <"$SAFE/lease" || return 1
    [ -z "${lease_extra:-}" ] || return 1
    case "$lease_source" in airplay|bluetooth|voice) ;; *) return 1;; esac
    case "$lease_pid:$lease_start" in *[!0-9:]*|:*|*:) return 1;; esac
    case "$lease_time:$lease_token" in *[!a-zA-Z0-9:_-]*|:*|*:) return 1;; esac
    case "$lease_time" in ''|*[!0-9]*) return 1;; esac
    case "$lease_phase" in priming|active|draining) ;; *) return 1;; esac
    audio_pid_live "$lease_pid" "$lease_start" || return 1
    _arl_now=$(audio_now) || return 1
    [ -n "$_arl_now" ] && [ "$lease_time" -le "$_arl_now" ] || return 1
    [ "$lease_phase" != priming ] || [ $((_arl_now - lease_time)) -le 12 ]
}
audio_voice_generation_valid() {
    [ "$(audio_source)" != voice ] && return 0
    [ -z "${HK_AUDIO_TOKEN:-}" ] && return 0
    _vg_run=${SAFE%/safe}
    [ "$(audio_control_read "$_vg_run/current-voice-token")" = "$HK_AUDIO_TOKEN" ] &&
        [ "$(audio_control_read "$_vg_run/voice-token")" = "$HK_AUDIO_TOKEN" ]
}

audio_reserve() {
    audio_voice_generation_valid || return 1
    _ar_source=$(audio_source)
    case "$_ar_source" in
        bluetooth) _ar_pid=$(audio_control_read "$RUN/bt-owner.pid") || return 1;;
        voice)
            if [ -n "${HK_AP2_VOICE_OWNER:-}" ]; then
                _ar_pid=$HK_AP2_VOICE_OWNER
            else
                _ar_pid=$(audio_control_read "${HK_VOICE_RUN:-/tmp/hk-voice}/voice.pid") ||
                    return 1
            fi
            ;;
        airplay) _ar_pid=$(audio_control_read "$RUN/shairport.pid") || return 1;;
    esac
    _ar_start=$(audio_pid_start "$_ar_pid")
    audio_pid_live "$_ar_pid" "$_ar_start" || return 1
    _ar_now=$(audio_now) || return 1
    [ -n "$_ar_now" ] || return 1
    if [ -e "$SAFE/lease" ]; then
        audio_read_lease || return 1
        [ "$lease_source:$lease_pid:$lease_start" = "$_ar_source:$_ar_pid:$_ar_start" ] || return 1
        [ -z "${HK_AUDIO_TOKEN:-}" ] || [ "$lease_token" = "$HK_AUDIO_TOKEN" ] || return 1
        [ -z "${HK_AUDIO_LEASE_TIME:-}" ] ||
            [ "$lease_time" = "$HK_AUDIO_LEASE_TIME" ] || return 1
        return 0
    fi
    lease_source=$_ar_source
    lease_pid=$_ar_pid
    lease_start=$_ar_start
    lease_token=${HK_AUDIO_TOKEN:-$_ar_pid-$_ar_start-$_ar_now-$$}
    case "$lease_token" in ''|*[!a-zA-Z0-9_-]*) return 1;; esac
    lease_time=$_ar_now
    lease_phase=priming
    audio_write_lease || return 1
    audio_write_control "$SAFE/play.started" "$_ar_now"
}
audio_write_lease() {
    audio_write_control "$SAFE/lease" \
        "$lease_source $lease_pid $lease_start $lease_token $lease_time $lease_phase"
}
audio_lease_is_caller() {
    audio_read_lease || return 1
    [ "$lease_source" = "$(audio_source)" ] || return 1
    [ -z "${HK_AUDIO_TOKEN:-}" ] ||
        [ "$lease_token" = "$HK_AUDIO_TOKEN" ] || return 1
    [ -z "${HK_AUDIO_LEASE_TIME:-}" ] ||
        [ "$lease_time" = "$HK_AUDIO_LEASE_TIME" ] || return 1
}
audio_bt_bridge_live() {
    audio_play_marker_live && audio_read_lease &&
        [ "$lease_source" = bluetooth ] || return 1
    [ "$(audio_control_read "$RUN/bt-owner.pid")" = "$lease_pid" ] || return 1
    _ab_beat=$(audio_control_read "$RUN/bt-heartbeat") || return 1
    _ab_now=$(audio_now) || return 1
    case "$_ab_beat:$_ab_now" in *[!0-9:]*|:*|*:) return 1;; esac
    [ "$_ab_beat" -le "$_ab_now" ] && [ $((_ab_now - _ab_beat)) -le 4 ]
}
audio_bt_pcm_owned() {
    case "${1:-}" in ''|*[!0-9]*) return 1;; esac
    _abo_run=${HK_AP2_RUN:-${SAFE%/safe}}
    _abo_file=${HK_BT_PCM_OWNER:-${_abo_run%/run}/bt-pcm-owner}
    if [ -e "$_abo_file" ] || [ -L "$_abo_file" ]; then
        [ -f "$_abo_file" ] && [ ! -L "$_abo_file" ] || return 1
        read -r _abo_pid _abo_start _abo_fd _abo_generation _abo_extra <"$_abo_file" || return 1
        [ -z "$_abo_extra" ] || return 1
        case "$_abo_pid:$_abo_start:$_abo_fd:$_abo_generation" in
            *[!0-9:]*|:*|*:|*::* ) return 1;;
        esac
        audio_pid_live "$_abo_pid" "$_abo_start" || return 1
        [ "$(cat "$AUDIO_PROC/$_abo_pid/comm" 2>/dev/null)" = a01controller ] || return 1
        [ "$(readlink "$AUDIO_PROC/$_abo_pid/fd/$_abo_fd" 2>/dev/null)" = "${HK_BT_PCM_DEVICE:-/dev/snd/pcmC0D0p}" ] || return 1
        [ "$(cat "$_abo_file" 2>/dev/null)" = "$_abo_pid $_abo_start $_abo_fd $_abo_generation" ] &&
            audio_pid_live "$_abo_pid" "$_abo_start"
        return $?
    fi
    # Compatibility with the previous backend or stock route. A dead
    # opener alone is never accepted without the descriptor proof above.
    [ "$(cat "$AUDIO_PROC/$1/comm" 2>/dev/null)" = a01controller ]
}
audio_bt_valid() {
    audio_bt_bridge_live || return 1
    _ab_pcm=${HK_AP2_PCM_STATUS:-${HK_BT_PCM_STATUS:-/proc/asound/card0/pcm0p/sub0/status}}
    grep -q '^state: RUNNING' "$_ab_pcm" 2>/dev/null || return 1
    _ab_owner=$(sed -n 's/^owner_pid[[:space:]]*:[[:space:]]*//p' "$_ab_pcm")
    case "$_ab_owner" in ''|*[!0-9]*) return 1;; esac
    audio_bt_pcm_owned "$_ab_owner"
}

# A stock Bluetooth pause can retain its PCM in XRUN instead of closing it.
# Only known stopped states owned by that player (or an actually closed
# device) qualify for a guarded restart. A different running owner does not.
audio_bt_clock_stopped() {
    _abc_pcm=${HK_AP2_PCM_STATUS:-${HK_BT_PCM_STATUS:-/proc/asound/card0/pcm0p/sub0/status}}
    _abc_state=$(sed -n '1p' "$_abc_pcm" 2>/dev/null)
    case "$_abc_state" in
        closed|'state: closed'|'state: CLOSED') return 0;;
        'state: XRUN'|'state: SETUP'|'state: PREPARED'|'state: PAUSED'|'state: SUSPENDED'|'state: DRAINING') ;;
        *) return 1;;
    esac
    _abc_owner=$(sed -n 's/^owner_pid[[:space:]]*:[[:space:]]*//p' "$_abc_pcm" 2>/dev/null)
    case "$_abc_owner" in ''|*[!0-9]*) return 1;; esac
    audio_bt_pcm_owned "$_abc_owner"
}

# The watchdog may carry a separate start identity so PID reuse cannot make an
# unrelated process look like the live safety loop. Older installations only
# have watchdog.pid; retain that form as a compatibility fallback.
audio_watchdog_pid() {
    _awp_identity_file="$SAFE/watchdog.identity"
    if [ -e "$_awp_identity_file" ] || [ -L "$_awp_identity_file" ]; then
        _awp_identity=$(audio_control_read "$_awp_identity_file") || return 1
        read -r _awp_pid _awp_start _awp_extra <<EOF
$_awp_identity
EOF
        [ -z "${_awp_extra:-}" ] || return 1
        audio_pid_live "$_awp_pid" "$_awp_start" || return 1
        printf '%s\n' "$_awp_pid"
        return 0
    fi
    _awp_pid=$(audio_control_read "$SAFE/watchdog.pid") || return 1
    case "$_awp_pid" in ''|*[!0-9]*) return 1;; esac
    kill -0 "$_awp_pid" 2>/dev/null || return 1
    printf '%s\n' "$_awp_pid"
}

audio_watchdog_live() {
    _awl_pid=$(audio_watchdog_pid) || return 1
    _awl_heartbeat=$(audio_control_read "$SAFE/watchdog.heartbeat") || return 1
    _awl_now=$(audio_now) || return 1
    case "$_awl_heartbeat:$_awl_now" in
        *[!0-9:]*|:*|*:) return 1;;
    esac
    [ "$_awl_heartbeat" -le "$_awl_now" ] &&
        [ $((_awl_now - _awl_heartbeat)) -le 4 ]
}

# A source hand-off can hold the speaker lock long enough for the watchdog's
# four-second heartbeat window to expire even though its exact process is still
# alive. Permit a bounded delay only while a real source transition exists;
# active playback never receives this grace and must keep the strict heartbeat.
audio_watchdog_transition_live() {
    audio_watchdog_pid >/dev/null || return 1
    _awtl_heartbeat=$(audio_control_read "$SAFE/watchdog.heartbeat") || return 1
    _awtl_now=$(audio_now) || return 1
    _awtl_grace=${HK_AP2_WATCHDOG_TRANSITION_GRACE_SECONDS:-15}
    case "$_awtl_heartbeat:$_awtl_now:$_awtl_grace" in
        *[!0-9:]*|:*|*:|*::*) return 1;;
    esac
    [ "$_awtl_grace" -ge 1 ] && [ "$_awtl_grace" -le 60 ] || return 1
    [ "$_awtl_heartbeat" -le "$_awtl_now" ] &&
        [ $((_awtl_now - _awtl_heartbeat)) -le "$_awtl_grace" ] ||
        return 1

    if audio_read_lease; then
        case "$lease_phase" in
            priming|draining) return 0;;
            *) return 1;;
        esac
    fi
    # AirPlay publishes a live claim before releasing Bluetooth and before its
    # guard creates the priming lease. Voice similarly publishes voice-owns
    # during source hand-off before its priming lease is established.
    audio_claim_recent "${HK_AP2_RUN:-$RUN}/ap2-claim" ||
        audio_voice_pending
}
