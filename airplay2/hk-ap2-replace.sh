#!/bin/sh
# Transactional offline-staged update. Never releases maintenance mute.
# Usage: hk-ap2-replace.sh --manifest /path/update.manifest
# Each line: SHA256 MODE ABSOLUTE_TARGET ; candidate is TARGET.new.
# Recovery: hk-ap2-replace.sh --recover /vendor/airplay2/.updates/TRANSACTION
set -u
BASE=${HK_AP2_BASE:-/vendor/airplay2}
RUNDIR=${HK_AP2_RUNDIR:-/tmp/hk-ap2}
OPT=$RUNDIR/opt
RUN=$RUNDIR/run
CTL=${HK_AP2_UPDATE_CTL:-$BASE/hk-ap2-prodctl}
MOUNTS=${HK_AP2_MOUNTS:-/proc/mounts}
HOLD=${HK_AP2_HOLD_MUTE_FILE:-/tmp/hk-ap2-hold-mute}
LOCK=${HK_AP2_UPDATE_LOCK:-/tmp/hk-ap2-update.lock}
LOG=${HK_AP2_UPDATE_LOG:-/tmp/hk-ap2-replace.log}
TXROOT=$BASE/.updates
changed=0
stopped=0
TX=
DEPENDENTS=
log() { printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" >>"$LOG"; }
digest() { sha256sum "$1" 2>/dev/null | awk '{print $1}'; }
mode() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null; }
allowed() {
    case "$1" in *..*|*//*|*' '*|*'\t'*|*'|'*) return 1;; esac
    case "$1" in "$BASE"/*|"${BASE%/*}"/hk-voice/*|"${BASE%/*}"/hk-lights/*|"${BASE%/*}"/hk-keys/*) ;;
        *) return 1;; esac
    case "$1" in "$TXROOT"/*|*.new|*.before-*) return 1;; esac
    _allowed_path=$1
    while [ "$_allowed_path" != / ]; do
        [ ! -L "$_allowed_path" ] || return 1
        _allowed_path=$(dirname "$_allowed_path")
    done
}
validate_manifest() {
    [ -s "$1" ] && [ -f "$1" ] || return 1
    count=0
    seen='|'
    while read -r sum perm target extra; do
        [ -n "$sum" ] || continue
        case "$sum" in *[!0-9a-f]*) return 1;; esac
        [ "${#sum}" -eq 64 ] && [ -z "${extra:-}" ] || return 1
        case "$perm" in 755|644|600) ;; *) return 1;; esac
        allowed "$target" || return 1
        case "$seen" in *"|$target|"*) return 1;; esac
        seen="$seen$target|"
        [ -s "$target.new" ] && [ -f "$target.new" ] && [ ! -L "$target.new" ] || return 1
        [ "$(digest "$target.new")" = "$sum" ] && [ "$(mode "$target.new")" = "$perm" ] || return 1
        if [ "$(head -c 2 "$target.new")" = '#!' ]; then
            sh -n "$target.new" || return 1
        fi
        count=$((count + 1))
    done <"$1"
    [ "$count" -gt 0 ]
}
check_space() {
    # Candidates already occupy flash. Reserve additional room for complete
    # backups, installed copies and a full rollback while files may be mapped.
    old_bytes=0
    new_bytes=0
    while read -r sum perm target extra; do
        [ -n "$sum" ] || continue
        bytes=$(wc -c <"$target.new") || return 1
        new_bytes=$((new_bytes + bytes))
        if [ -f "$target" ]; then
            bytes=$(wc -c <"$target") || return 1
            old_bytes=$((old_bytes + bytes))
        fi
    done <"$1"
    available=$(df -Pk "$BASE" 2>/dev/null | awk 'NR==2 {print $4}')
    case "$available" in ''|*[!0-9]*) return 1;; esac
    required=$(((old_bytes * 2 + new_bytes + 1023) / 1024 + 1024))
    [ "$available" -ge "$required" ] || { log "insufficient rollback space: need ${required}KiB, have ${available}KiB"; return 1; }
}

keep_muted() {
    mkdir -p "$(dirname "$HOLD")" || return 1
    : >"$HOLD" || return 1
    if [ -x "$BASE/hk-ap2-audio-hook.sh" ]; then
        # Voice flag prevents a stale voice marker from ignoring global mute.
        env HK_AP2_VOICE=1 HK_AP2_HOLD_MUTE_FILE="$HOLD" \
            "$BASE/hk-ap2-audio-hook.sh" mute >>"$LOG" 2>&1 || return 1
    else
        return 1
    fi
}
release_runtime() {
    "$CTL" stop >>"$LOG" 2>&1 || return 1
    stopped=1
    # Enumerate actual bind destinations, including old/deleted sources.
    for round in 1 2 3; do
        paths=$(awk -v root="$OPT/" 'index($2,root)==1 {print $2}' "$MOUNTS")
        for path in $paths; do umount "$path" >>"$LOG" 2>&1 || true; done
    done
    awk -v root="$OPT/" 'index($2,root)==1 {found=1} END {exit !found}' "$MOUNTS" && return 1
    if awk -v root="$OPT" '$2==root {found=1} END {exit !found}' "$MOUNTS"; then
        umount "$OPT" >>"$LOG" 2>&1 || return 1
    fi
    return 0
}
installed_matches() {
    while read -r sum perm target extra; do
        [ -n "$sum" ] || continue
        [ "$(digest "$target")" = "$sum" ] && [ "$(mode "$target")" = "$perm" ] || return 1
    done <"$TX/candidate.manifest"
}
ap2_up() {
    i=0
    while [ "$i" -lt "${HK_AP2_UPDATE_WAIT_SECONDS:-90}" ]; do
        # verify checks the active 5.6 binding, config pins and supervisors.
        "$CTL" verify >>"$LOG" 2>&1 && return 0
        sleep 2
        i=$((i + 2))
    done
    return 1
}
start_ap2() { env HK_AP2_UPDATE_START=1 "$CTL" start >>"$LOG" 2>&1 && ap2_up; }
backup_all() {
    index=0
    : >"$TX/restore.manifest" || return 1
    while read -r sum perm target extra; do
        [ -n "$sum" ] || continue
        index=$((index + 1))
        if [ -e "$target" ]; then
            [ -f "$target" ] && [ ! -L "$target" ] || return 1
            oldsum=$(digest "$target")
            oldmode=$(mode "$target")
            cp -p "$target" "$TX/$index.bak" || return 1
            [ "$(digest "$TX/$index.bak")" = "$oldsum" ] || return 1
            printf '%s %s %s %s\n' "$index" "$oldsum" "$oldmode" "$target" >>"$TX/restore.manifest" || return 1
        else
            printf '%s - - %s\n' "$index" "$target" >>"$TX/restore.manifest" || return 1
        fi
    done <"$TX/candidate.manifest"
}
restore_all() {
    [ -s "$TX/restore.manifest" ] || return 1
    # Validate every backup before altering any target.
    while read -r index sum perm target; do
        allowed "$target" || return 1
        [ "$sum" = - ] || [ "$(digest "$TX/$index.bak")" = "$sum" ] || return 1
    done <"$TX/restore.manifest"
    while read -r index sum perm target; do
        if [ "$sum" = - ]; then rm -f "$target" || return 1
        else
            cp -p "$TX/$index.bak" "$target.restore" &&
                chmod "$perm" "$target.restore" && mv "$target.restore" "$target" || return 1
            [ "$(digest "$target")" = "$sum" ] || return 1
        fi
    done <"$TX/restore.manifest"
    sync
}
rollback() {
    log 'update failed; restoring complete previous set'
    keep_muted || return 1
    release_runtime || return 1
    [ -z "$DEPENDENTS" ] || env HK_AP2_UPDATE_TX="$TX" "$DEPENDENTS" stop "$TX/candidate.manifest" >>"$LOG" 2>&1 || return 1
    restore_all || return 1
    start_ap2 || return 1
    [ -z "$DEPENDENTS" ] || env HK_AP2_UPDATE_TX="$TX" "$DEPENDENTS" start "$TX/candidate.manifest" >>"$LOG" 2>&1 || return 1
    [ -z "$DEPENDENTS" ] || env HK_AP2_UPDATE_TX="$TX" "$DEPENDENTS" verify "$TX/candidate.manifest" >>"$LOG" 2>&1 || return 1
    printf '%s\n' rolled-back >"$TX/state"
    rm -f "$TXROOT/pending"
    log "previous version restored; update FAILED; hold retained; transaction=$TX"
}
interrupted() {
    trap '' HUP INT TERM
    if [ "$changed" = 1 ]; then
        rollback || log "RECOVERY REQUIRED: $0 --recover $TX"
    elif [ "$stopped" = 1 ]; then start_ap2 || true
    fi
    exit 130
}
main() {
    [ "$#" -eq 2 ] || return 2
    mkdir "$LOCK" 2>/dev/null || { log 'another update owns lock; abort'; return 1; }
    trap 'rmdir "$LOCK" 2>/dev/null || true' 0
    trap interrupted HUP INT TERM
    mkdir -p "$TXROOT" || return 1
    chmod 700 "$TXROOT" || return 1
    if [ "$1" = --recover ]; then
        TX=$2
        case "$TX" in "$TXROOT"/*) ;; *) return 2;; esac
        [ "$TX" = "$(cat "$TXROOT/pending" 2>/dev/null)" ] || return 1
        if [ -f "$TX/dependents.sh" ]; then
            helper_sum=$(awk -v path="$BASE/hk-audio-dependents.sh" '$3==path {print $1}' "$TX/candidate.manifest")
            [ "$(digest "$TX/dependents.sh")" = "$helper_sum" ] || return 1
            DEPENDENTS=$TX/dependents.sh
        fi
        changed=0
        rollback && return 0
        log "RECOVERY FAILED; output held; transaction=$TX"
        return 1
    fi
    [ "$1" = --manifest ] || return 2
    [ ! -f "$TXROOT/pending" ] || { log 'unfinished transaction requires --recover'; return 1; }
    validate_manifest "$2" || { log 'candidate validation failed; live files untouched'; return 1; }
    check_space "$2" || return 1
    TX=$TXROOT/$(date '+%Y%m%dT%H%M%S')-$$
    mkdir "$TX" && cp "$2" "$TX/candidate.manifest" || return 1
    backup_all || { log 'backup failed; live files untouched'; return 1; }
    needs_dependents=0
    while read -r sum perm target extra; do
        case "$target" in
            "${BASE%/*}/hk-voice/"*|"${BASE%/*}/hk-keys/"*|"${BASE%/*}/hk-lights/"*|\
            "$BASE/hk-bt-open-redirect.so"|"$BASE/hk-bt-controller-wrapper.sh"|"$BASE/hk-bt-alsa.conf"|"$BASE/hk-bt-boot") needs_dependents=1;;
        esac
    done <"$TX/candidate.manifest"
    if [ "$needs_dependents" = 1 ]; then
        helper_sum=$(awk -v path="$BASE/hk-audio-dependents.sh" '$3==path {print $1}' "$TX/candidate.manifest")
        [ -n "$helper_sum" ] && [ -x "$BASE/hk-audio-dependents.sh.new" ] || {
            log 'dependent modules require the verified reload helper in this transaction'; return 1;
        }
        DEPENDENTS=$TX/dependents.sh
        cp -p "$BASE/hk-audio-dependents.sh.new" "$DEPENDENTS" &&
            [ "$(digest "$DEPENDENTS")" = "$helper_sum" ] || return 1
    fi
    printf '%s\n' prepared >"$TX/state"
    keep_muted || return 1
    printf '%s\n' "$TX" >"$TXROOT/pending.tmp" &&
        mv "$TXROOT/pending.tmp" "$TXROOT/pending" || return 1
    sync
    changed=1
    release_runtime || return 1
    # Stop the old supervisor first: it must not respawn the old voice loop
    # between dependent shutdown and installation.
    [ -z "$DEPENDENTS" ] || env HK_AP2_UPDATE_TX="$TX" "$DEPENDENTS" stop "$TX/candidate.manifest" >>"$LOG" 2>&1 || return 1
    while read -r sum perm target extra; do
        [ -n "$sum" ] || continue
        # Keep staged inputs for diagnosis/retry; rename only verified copies.
        cp "$target.new" "$target.install" && chmod "$perm" "$target.install" &&
            [ "$(digest "$target.install")" = "$sum" ] && mv "$target.install" "$target" || return 1
    done <"$TX/candidate.manifest"
    printf '%s\n' installed >"$TX/state"
    sync
    installed_matches && start_ap2 && installed_matches || return 1
    [ -z "$DEPENDENTS" ] || env HK_AP2_UPDATE_TX="$TX" "$DEPENDENTS" start "$TX/candidate.manifest" >>"$LOG" 2>&1 || return 1
    [ -z "$DEPENDENTS" ] || env HK_AP2_UPDATE_TX="$TX" "$DEPENDENTS" verify "$TX/candidate.manifest" >>"$LOG" 2>&1 || return 1
    printf '%s\n' committed >"$TX/state"
    rm -f "$TXROOT/pending"
    changed=0
    log "update verified; maintenance hold retained; transaction=$TX"
    return 0
}
main "$@"
rc=$?
if [ "$rc" -ne 0 ] && [ "$changed" = 1 ]; then
    changed=0
    rollback || log "RECOVERY FAILED; output held; run: $0 --recover $TX"
fi
exit "$rc"
