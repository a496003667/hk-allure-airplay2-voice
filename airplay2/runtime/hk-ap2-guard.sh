#!/bin/sh

# Fail safe AP2 resource guard.  It only manages the AP2 runtime and never
# signals the stock audio services or the maintenance shell.

set -u

ROOT=${HK_AP2_ROOT:-/tmp/hk-ap2/opt}
RUN=${HK_AP2_RUN:-/tmp/hk-ap2/run}
MIN_RUN_FREE_KIB=${HK_AP2_MIN_RUN_FREE_KIB:-8192}
MIN_AVAILABLE_KIB=${HK_AP2_MIN_AVAILABLE_RUN_KIB:-32768}
MAX_LOG_BYTES=${HK_AP2_MAX_LOG_BYTES:-131072}
GUARD_OOM_SCORE_ADJ=${HK_AP2_GUARD_OOM_SCORE_ADJ:-600}

mkdir -p "$RUN"
printf '%s\n' "$$" >"$RUN/guard.pid"
echo "$GUARD_OOM_SCORE_ADJ" >"/proc/$$/oom_score_adj" 2>/dev/null || true

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || date)" "$*" \
        >>"$RUN/guard.log"
}

memory_kib() {
    # This kernel predates MemAvailable.  Approximate it from reclaimable
    # page cache and slab, excluding tmpfs-backed Shmem pages.
    awk '
        /^MemFree:/ {free = $2}
        /^Buffers:/ {buffers = $2}
        /^Cached:/ {cached = $2}
        /^SReclaimable:/ {sreclaim = $2}
        /^Shmem:/ {shmem = $2}
        END {
            available = free + buffers + cached + sreclaim - shmem
            printf "%d %d\n", free, available
        }
    ' /proc/meminfo 2>/dev/null
}

read_pid() {
    cat "$RUN/$1.pid" 2>/dev/null || true
}

expected_comm() {
    case "$1" in
        dbus) echo dbus-daemon ;;
        avahi) echo avahi-daemon ;;
        nqptp) echo nqptp ;;
        shairport) echo shairport-sync ;;
        *) return 1 ;;
    esac
}

pid_matches() {
    kind=$1
    pid=$2
    comm=$(cat "/proc/$pid/comm" 2>/dev/null || true)
    expected=$(expected_comm "$kind") || return 1
    [ "$comm" = "$expected" ] && return 0
    [ "$comm" = "ld-linux.so.3" ] || return 1
    case "$(strings "/proc/$pid/cmdline" 2>/dev/null || true)" in
        *"/opt/bin/$expected"*|*"/opt/sbin/$expected"*) return 0 ;;
    esac
    return 1
}

mute_audio() {
    if [ -x "$ROOT/hk-ap2-audio-hook.sh" ]; then
        "$ROOT/hk-ap2-audio-hook.sh" mute >/dev/null 2>&1 || true
    fi
}

stop_kind() {
    kind=$1
    pid=$(read_pid "$kind")
    case "$pid" in
        ''|*[!0-9]*) rm -f "$RUN/$kind.pid"; return 0 ;;
    esac
    if kill -0 "$pid" 2>/dev/null && pid_matches "$kind" "$pid"; then
        kill -TERM "$pid" 2>/dev/null || true
    fi
    rm -f "$RUN/$kind.pid"
}

stop_ap2() {
    mute_audio
    stop_kind shairport
    stop_kind nqptp
    stop_kind avahi
    stop_kind dbus
    rm -f "$RUN/mode"
}

trim_logs() {
    for log_file in \
        "$RUN/dbus.log" \
        "$RUN/avahi.log" \
        "$RUN/nqptp.log" \
        "$RUN/shairport.log" \
        "$RUN/guard.log"; do
        [ -f "$log_file" ] || continue
        size=$(wc -c <"$log_file" 2>/dev/null || echo 0)
        case "$size" in
            ''|*[!0-9]*) size=0 ;;
        esac
        if [ "$size" -gt "$MAX_LOG_BYTES" ]; then
            tail -c 65536 "$log_file" >"$log_file.tmp" 2>/dev/null &&
                mv "$log_file.tmp" "$log_file" 2>/dev/null || true
        fi
    done
}

trip() {
    reason=$1
    printf '%s\n' "$reason" >"$RUN/low-memory-trip"
    log "tripped: $reason"
    stop_ap2
    rm -f "$RUN/guard.pid"
    log "AP2 stopped and audio muted"
    exit 0
}

log "guard started pid=$$ min_free=${MIN_RUN_FREE_KIB}KiB min_available=${MIN_AVAILABLE_KIB}KiB"
low_count=0
loop=0

while :; do
    shairport_pid=$(read_pid shairport)
    case "$shairport_pid" in
        ''|*[!0-9]*)
            trip "shairport pid missing"
            ;;
    esac
    if ! kill -0 "$shairport_pid" 2>/dev/null ||
        ! pid_matches shairport "$shairport_pid"; then
        trip "shairport exited"
    fi

    memory=$(memory_kib)
    free=${memory%% *}
    available=${memory#* }
    case "$free:$available" in
        *[!0-9:]*|:|*:)
            free=0
            available=0
            ;;
    esac
    if [ "$free" -lt "$MIN_RUN_FREE_KIB" ] ||
        [ "$available" -lt "$MIN_AVAILABLE_KIB" ]; then
        low_count=$((low_count + 1))
        if [ "$low_count" -ge 3 ]; then
            trip "memory low: free=${free}KiB available~${available}KiB"
        fi
    else
        low_count=0
    fi

    loop=$((loop + 1))
    if [ $((loop % 15)) -eq 0 ]; then
        trim_logs
    fi
    sleep 2
done
