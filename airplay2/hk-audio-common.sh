#!/bin/sh
# Shared lightweight primitives. Sourcing this file never changes audio.
AUDIO_PROC=${HK_AP2_PROC_ROOT:-/proc}
AUDIO_UPTIME=${HK_AUDIO_UPTIME_FILE:-/proc/uptime}
AUDIO_ID_PROC=${HK_AUDIO_ID_PROC_ROOT:-/proc}
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
    _al_count=0
    while ! mkdir "$_al_path" 2>/dev/null; do
        if [ -s "$_al_path/owner" ]; then
            read -r _al_pid _al_start <"$_al_path/owner" || return 1
            if ! audio_pid_live "$_al_pid" "$_al_start"; then
                rm -f "$_al_path/owner"
                rmdir "$_al_path" 2>/dev/null || true
            fi
        fi
        _al_count=$((_al_count + 1))
        [ "$_al_count" -lt 30 ] || return 1
        audio_delay 100000
    done
    audio_set_self || return 1
    _al_start=$(audio_pid_start "$AUDIO_SELF_PID")
    [ -n "$_al_start" ] || { rmdir "$_al_path"; return 1; }
    printf '%s %s\n' "$AUDIO_SELF_PID" "$_al_start" >"$_al_path/owner" || return 1
}
audio_unlock() {
    audio_set_self || return 1
    [ "$(cat "$1/owner" 2>/dev/null)" = "$AUDIO_SELF_PID $(audio_pid_start "$AUDIO_SELF_PID")" ] || return 1
    rm -f "$1/owner" && rmdir "$1"
}
audio_trim_log() {
    _at_file=$1
    _at_max=${2:-65536}
    [ -f "$_at_file" ] || return 0
    _at_size=$(wc -c <"$_at_file" 2>/dev/null) || return 1
    [ "$_at_size" -gt "$_at_max" ] || return 0
    audio_set_self || return 1
    _at_identity="$AUDIO_SELF_PID $(audio_pid_start "$AUDIO_SELF_PID")"
    _at_owner="$_at_file.trim-owner.$AUDIO_SELF_PID"
    printf '%s\n' "$_at_identity" >"$_at_owner" || return 1
    # A hard link publishes an initialized owner atomically. A killed
    # rotator cannot leave an ownerless directory that disables rotation.
    if ! ln "$_at_owner" "$_at_file.trim-lock" 2>/dev/null; then
        read -r _at_pid _at_start <"$_at_file.trim-lock" 2>/dev/null || true
        if ! audio_pid_live "${_at_pid:-}" "${_at_start:-}"; then
            rm -f "$_at_file.trim-lock"
            ln "$_at_owner" "$_at_file.trim-lock" 2>/dev/null || { rm -f "$_at_owner"; return 0; }
        else rm -f "$_at_owner"; return 0
        fi
    fi
    rm -f "$_at_owner"
    # Writers use O_APPEND. Keep the inode; rename leaves a hidden unbounded
    # file behind when a daemon still owns its stdout descriptor.
    tail -c "$((_at_max / 2))" "$_at_file" >"$_at_file.trim" &&
        cat "$_at_file.trim" >"$_at_file"
    rm -f "$_at_file.trim"
    [ "$(cat "$_at_file.trim-lock" 2>/dev/null)" != "$_at_identity" ] || rm -f "$_at_file.trim-lock"
}
audio_voice_pending() {
    _av_stamp=$(sed -n '1p' "${HK_AP2_RUN:-$RUN}/voice-owns" 2>/dev/null)
    _av_now=$(audio_now) || return 1
    case "$_av_stamp:$_av_now" in *[!0-9:]*|:*|*:) return 1;; esac
    [ "$_av_stamp" -le "$_av_now" ] && [ $((_av_now - _av_stamp)) -lt 300 ]
}

audio_source() {
    if [ -n "${HK_AP2_BT:-}" ]; then printf 'bluetooth\n'
    elif [ -n "${HK_AP2_VOICE:-}" ]; then printf 'voice\n'
    else printf 'airplay\n'; fi
}
audio_read_lease() {
    [ -s "$SAFE/lease" ] || return 1
    read -r lease_source lease_pid lease_start lease_token lease_time lease_phase <"$SAFE/lease" || return 1
    case "$lease_source" in airplay|bluetooth|voice) ;; *) return 1;; esac
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
    [ "$(cat "$_vg_run/current-voice-token" 2>/dev/null)" = "$HK_AUDIO_TOKEN" ] &&
        [ "$(cat "$_vg_run/voice-token" 2>/dev/null)" = "$HK_AUDIO_TOKEN" ]
}

audio_reserve() {
    audio_voice_generation_valid || return 1
    _ar_source=$(audio_source)
    case "$_ar_source" in
        bluetooth) _ar_pid=$(cat "$RUN/bt-owner.pid" 2>/dev/null);;
        voice) _ar_pid=${HK_AP2_VOICE_OWNER:-$(cat "${HK_VOICE_RUN:-/tmp/hk-voice}/voice.pid" 2>/dev/null)};;
        airplay) _ar_pid=$(cat "$RUN/shairport.pid" 2>/dev/null);;
    esac
    _ar_start=$(audio_pid_start "$_ar_pid")
    audio_pid_live "$_ar_pid" "$_ar_start" || return 1
    _ar_now=$(audio_now) || return 1
    [ -n "$_ar_now" ] || return 1
    if [ -e "$SAFE/lease" ]; then
        audio_read_lease || return 1
        [ "$lease_source:$lease_pid:$lease_start" = "$_ar_source:$_ar_pid:$_ar_start" ] || return 1
        [ -z "${HK_AUDIO_TOKEN:-}" ] || [ "$lease_token" = "$HK_AUDIO_TOKEN" ] || return 1
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
    printf '%s\n' "$_ar_now" >"$SAFE/play.started.tmp" &&
        mv "$SAFE/play.started.tmp" "$SAFE/play.started"
}
audio_write_lease() {
    printf '%s %s %s %s %s %s\n' "$lease_source" "$lease_pid" "$lease_start" \
        "$lease_token" "$lease_time" "$lease_phase" >"$SAFE/lease.tmp" &&
        mv "$SAFE/lease.tmp" "$SAFE/lease"
}
audio_lease_is_caller() {
    audio_read_lease || return 1
    [ "$lease_source" = "$(audio_source)" ] || return 1
    [ -z "${HK_AUDIO_TOKEN:-}" ] || [ "$lease_token" = "$HK_AUDIO_TOKEN" ]
}
audio_bt_valid() {
    [ -s "$SAFE/play.started" ] && audio_read_lease &&
        [ "$lease_source" = bluetooth ] || return 1
    [ "$(cat "$RUN/bt-owner.pid" 2>/dev/null)" = "$lease_pid" ] || return 1
    _ab_beat=$(cat "$RUN/bt-heartbeat" 2>/dev/null)
    _ab_now=$(audio_now) || return 1
    case "$_ab_beat:$_ab_now" in *[!0-9:]*|:*|*:) return 1;; esac
    [ "$_ab_beat" -le "$_ab_now" ] && [ $((_ab_now - _ab_beat)) -le 4 ] || return 1
    _ab_pcm=${HK_AP2_PCM_STATUS:-${HK_BT_PCM_STATUS:-/proc/asound/card0/pcm0p/sub0/status}}
    grep -q '^state: RUNNING' "$_ab_pcm" 2>/dev/null || return 1
    _ab_owner=$(sed -n 's/^owner_pid[[:space:]]*:[[:space:]]*//p' "$_ab_pcm")
    case "$_ab_owner" in ''|*[!0-9]*) return 1;; esac
    [ "$(cat "$AUDIO_PROC/$_ab_owner/comm" 2>/dev/null)" = a01controller ]
}
