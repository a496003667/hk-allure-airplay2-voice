#!/bin/sh
# On-device preparation: .new siblings only, never replace a live target.
set -eu
[ "$#" -eq 1 ] || exit 2
PACKAGE=$1
PREFIX=${HK_STAGE_ROOT:-}
case "$PREFIX" in ''|/*) ;; *) exit 2;; esac
[ ! -d "${HK_AP2_UPDATE_LOCK:-/tmp/hk-ap2-update.lock}" ] || exit 1
[ -s "$PACKAGE/update.manifest" ] || exit 1
TEMPFILE=
trap '[ -z "$TEMPFILE" ] || rm -f "$TEMPFILE"' 0
while read -r sha perm target extra; do
    [ -z "${extra:-}" ] || exit 1
    case "$target" in /vendor/airplay2/*|/vendor/hk-voice/*|/vendor/hk-keys/*|/vendor/hk-lights/*) ;; *) exit 1;; esac
    case "$target" in *..*|*.new) exit 1;; esac
    case "$perm" in 755|644|600) ;; *) exit 1;; esac
    source=$PACKAGE$target
    live=$PREFIX$target
    [ -f "$source" ] && [ ! -L "$source" ] && [ ! -L "$live" ] && [ ! -L "$live.new" ] || exit 1
    parent=$(dirname "$live")
    while [ "$parent" != / ]; do
        [ ! -L "$parent" ] || exit 1
        parent=$(dirname "$parent")
    done
    [ "$(sha256sum "$source" | awk '{print $1}')" = "$sha" ] || exit 1
    mkdir -p "$(dirname "$live")"
    TEMPFILE=$live.new.stage.$$
    # noclobber opens exclusively; an old symlink cannot redirect the copy.
    (set -C; umask 077; cat "$source" >"$TEMPFILE")
    [ "$(sha256sum "$TEMPFILE" | awk '{print $1}')" = "$sha" ] || exit 1
    chmod "$perm" "$TEMPFILE"
    mv "$TEMPFILE" "$live.new"
    TEMPFILE=
done <"$PACKAGE/update.manifest"
printf '%s\n' 'Staged only. Use the repaired updater --manifest; keep output held for device verification.'
