#!/bin/sh
# On-device voice assistant loop for the Harman Kardon Allure.
#
# The stock "Alexa" wake word (asr_tts) lights MCULEDLISTENING; the key hook
# in mv_ioguard logs those GNOTIFY events to /tmp/hk-keys/voice.events.  On a
# wake we record through asr_tts (recordFileStart/Stop), let hk-voice do
# Baidu ASR -> ModelScope LLM -> Baidu TTS, and play the reply through the
# same muted-by-default speaker path that AirPlay uses (hook start/stop).
#
#   hk-voice.sh boot|start   run in the background (boot waits for AP2)
#   hk-voice.sh stop
#   hk-voice.sh status
# touch /vendor/hk-voice/disable to keep it from starting.

D=/vendor/hk-voice
RUN=/tmp/hk-voice
export HK_VOICE_BT_INJECT_ACTIVE=/tmp/hk-ap2/run/voice-inject/active
EV=/tmp/hk-keys/voice.events
LOG=$RUN/voice.log
PIDFILE=$RUN/voice.pid
REC=/dev/shm/hk-voice-rec.wav
OUT=/dev/shm/hk-voice-out.raw
REQ=$D/hk-unix-request-raw
MQTT=$D/hk-mqtt-pub
HOOK=/vendor/airplay2/hk-ap2-audio-hook.sh
SAFE=/tmp/hk-ap2/run/safe
PCM_STATUS=/proc/asound/card0/pcm0p/sub0/status
PLAYER_SOCKET=/tmp/Requesta01controller
APLAY=/system/workdir/bin/aplay
DSP_ALSA_CONF=/vendor/airplay2/hk-dsp-alsa.conf
MAX_RECORD=${HK_VOICE_MAX_RECORD:-8}
# Keep the loop and all of its short-lived network children in a positive
# LMK bucket.  The old value (15) lands in the 64 MiB bucket on this kernel,
# so ASR was killed even with about 30 MiB of reclaimable cache.  A value of
# 5 uses the 8 MiB bucket, still behind every stock process at adj 0.
VOICE_OOM_SCORE_ADJ=${HK_VOICE_OOM_SCORE_ADJ:-5}
VOICE_SUPERVISOR_OOM_SCORE_ADJ=${HK_VOICE_SUPERVISOR_OOM_SCORE_ADJ:-5}

log() {
    printf '%s %s\n' "$(date '+%m-%d %H:%M:%S')" "$*" >>"$LOG"
    size=$(wc -c <"$LOG" 2>/dev/null || echo 0)
    if [ "$size" -gt 65536 ]; then
        tail -c 32768 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
}

now_ms() {
    up=$(cut -d' ' -f1 /proc/uptime)
    echo "${up%.*}${up#*.}0"
}

ap2_playing() { [ -f "$SAFE/play.started" ]; }

pcm_closed() { [ "$(cat "$PCM_STATUS" 2>/dev/null)" = "closed" ]; }

LED_FLAG=$RUN/led.thinking
QD=$RUN/q              # reply queue read by "hk-voice feedq"
ACTION=$RUN/action

# Stock light ring states via mv_ioguard (the same GNOTIFY asr_tts sends).
led() {
    [ -n "${HK_VOICE_NOLED:-}" ] && return 0
    "$REQ" /tmp/RequestIOGuard "GNOTIFY=MCULED$1" </dev/null >/dev/null 2>&1
}

# Keep the ring in our state (THINKING, then PLAYTTS) until the reply ends:
# stock firmware drops it to IDLE on its own, so re-assert it.
led_hold() {
    printf '%s\n' "$1" >"$LED_FLAG"
    led "$1"
}

led_keeper() {
    while [ -f "$LED_FLAG" ]; do
        if [ -f "$QD/started" ] && [ "$(cat "$LED_FLAG" 2>/dev/null)" = THINKING ]; then
            led_hold PLAYTTS
        fi
        case "$(tail -n 1 "$EV" 2>/dev/null)" in
            *MCULEDIDLE|*MCULEDLISTENING)
                state=$(cat "$LED_FLAG" 2>/dev/null)
                [ -n "$state" ] && led "$state" ;;
        esac
        usleep 300000
    done
}

HOOK_ENV="HK_AP2_VOICE=1 HK_AP2_USE_STOCK_PA=1 HK_AP2_MASTER_SAFE=4 HK_AP2_DIGITAL_SAFE=192 HK_AP2_PLAYBACK_SAFE=1"
PREPARED=$RUN/prepared

# Speaker hand-off with AirPlay (see hk-ap2-audio-hook.sh): we take the
# speaker only under the shared lock, and only if AirPlay has not just
# claimed it; while we hold voice-owns, AirPlay's hook defers its start.
VOICE_OWNS=/tmp/hk-ap2/run/voice-owns
AP2_DEFERRED=/tmp/hk-ap2/run/ap2-deferred
AP2_CLAIM=/tmp/hk-ap2/run/ap2-claim
SPEAKER_LOCK=/tmp/hk-ap2/run/speaker.lock
speaker_lock() {
    n=0
    while ! mkdir "$SPEAKER_LOCK" 2>/dev/null; do
        n=$((n + 1))
        if [ "$n" -ge 30 ]; then
            rmdir "$SPEAKER_LOCK" 2>/dev/null
            mkdir "$SPEAKER_LOCK" 2>/dev/null
            return 0
        fi
        usleep 100000
    done
}
speaker_unlock() { rmdir "$SPEAKER_LOCK" 2>/dev/null; }
ap2_claimed() {   # AirPlay's hook started the path in the last 8 s
    t=$(sed -n 1p "$AP2_CLAIM" 2>/dev/null)
    case "$t" in ''|*[!0-9]*) return 1 ;; esac
    [ $(( $(cut -d. -f1 /proc/uptime) - t )) -lt 8 ]
}
# Drop voice-owns; if AirPlay asked for the speaker meanwhile, hand it over.
voice_release() {
    speaker_lock
    rm -f "$VOICE_OWNS"
    deferred=
    [ -f "$AP2_DEFERRED" ] && deferred=1
    rm -f "$AP2_DEFERRED"
    speaker_unlock
    if [ -n "$deferred" ]; then
        log "AirPlay started during the reply; handing the speaker over"
        reopen_watch </dev/null >/dev/null 2>&1 &
    fi
}

AMIXER=/system/workdir/bin/amixer
mx() { env LD_LIBRARY_PATH=/system/workdir/lib "$AMIXER" -c 0 "$@" >/dev/null 2>&1; }
mx_cset() { mx cset "numid=$1" "$2"; }
mx_get() {
    env LD_LIBRARY_PATH=/system/workdir/lib "$AMIXER" -c 0 sget "$1" 2>/dev/null |
        sed -n 's/.*\[\(on\|off\)\].*/\1/p' | sed -n 1p
}
mx_values() {
    env LD_LIBRARY_PATH=/system/workdir/lib "$AMIXER" -c 0 cget "numid=$1" \
        2>/dev/null | sed -n 's/^[[:space:]]*: values=//p' | tail -n 1
}
mx_master() {
    env LD_LIBRARY_PATH=/system/workdir/lib "$AMIXER" -c 0 sget Master 2>/dev/null |
        sed -n 's/.*Front Left: \([0-9][0-9]*\).*/\1/p' | sed -n 1p
}

stock_pa() {   # $1 = unmute|mute, via the same preload trick as the AP2 hook
    rm -f /tmp/RequestAppleRemote
    env LD_LIBRARY_PATH=/system/workdir/lib:/lib \
        LD_PRELOAD=/tmp/hk-ap2/opt/lib/libhk-airplay-$1.so \
        /system/workdir/bin/apple_remote >"$RUN/pa.log" 2>&1 &
    pa_pid=$!
    n=0   # the preload acts at once; poll its log instead of waiting 1 s
    while [ "$n" -lt 15 ]; do
        usleep 100000
        grep -q "Snd_Ctrl airplay to mute" "$RUN/pa.log" 2>/dev/null && break
        n=$((n + 1))
    done
    usleep 100000
    rm -f /tmp/RequestAppleRemote
    kill "$pa_pid" 2>/dev/null
    wait "$pa_pid" 2>/dev/null
}

# The shared audio hook owns the pop-free speaker sequence: it verifies the
# lease, DAC silence, physical mute and a running PCM clock before any stage
# is enabled.  Every voice path goes through it; there is no direct amplifier
# shortcut.
speaker_open() {
    env $HOOK_ENV "$HOOK" guard || return 1
    env $HOOK_ENV "$HOOK" start || return 1
    echo "$(date '+%Y-%m-%dT%H:%M:%S%z') voice speaker opened" >>/tmp/hk-ap2/run/hook.log
    return 0
}

speaker_guard() {
    env HK_AP2_BT=1 $HOOK_ENV "$HOOK" guard active
}

# A silence stream that keeps the DAC clocked while the amplifier switches;
# the feeder plays $QD/chime.raw and then $QD/q.N.raw as they appear.
start_stream() {
    mkdir -p "$QD"
    rm -f "$QD"/*
    "$D/hk-voice" feedq "$QD" 2>/dev/null |
        env ALSA_CONFIG_PATH="$DSP_ALSA_CONF" LD_LIBRARY_PATH=/system/workdir/lib \
        "$APLAY" -q -D hk_harman_dsp \
            -f S16_LE -r 48000 -c 2 -t raw >/dev/null 2>&1 &
}

stop_stream() {
    : >"$QD/stop"
    i=0
    while ! pcm_closed && [ "$i" -lt 30 ]; do usleep 100000; i=$((i + 1)); done
    rm -f "$QD"/*
}

# The stock Bluetooth player keeps its exclusive PCM open even while paused.
# While Bluetooth owns the speaker, the verified a01controller preload mixes
# this queue into its own snd_pcm_writei buffers. Music stays in the stream but
# is faded to silence; no second ALSA hardware open is attempted.
bt_stream_live() {
    [ -s "$BT_OWNER" ] || return 1
    [ "$(sed -n '1p' "$PCM_STATUS" 2>/dev/null)" = "state: RUNNING" ] || return 1
    owner=$(sed -n 's/^owner_pid[[:space:]]*:[[:space:]]*//p' "$PCM_STATUS" 2>/dev/null)
    case "$owner" in ''|*[!0-9]*) return 1 ;; esac
    [ "$(cat "/proc/$owner/comm" 2>/dev/null)" = a01controller ]
}

bt_inject_stop_feeder() {
    if [ -n "${BT_INJECT_PID:-}" ]; then
        [ -d "$BT_INJECT" ] && : >"$BT_INJECT/stop"
        feeder_pid=$BT_INJECT_PID
        kill "$feeder_pid" 2>/dev/null || true
        # BusyBox ash can remain in wait4 indefinitely after a FIFO-owning
        # feeder is killed. Poll instead; the shell reaps finished background
        # jobs opportunistically or the supervisor restarts this loop.
        i=0
        while kill -0 "$feeder_pid" 2>/dev/null && [ "$i" -lt 20 ]; do
            usleep 100000
            i=$((i + 1))
        done
        kill -KILL "$feeder_pid" 2>/dev/null || true
        BT_INJECT_PID=
    fi
}

bt_inject_wait_done() {
    i=0
    while [ -f "$BT_INJECT/active" ] &&
          [ ! -f "$BT_INJECT/done" ] &&
          [ ! -f "$BT_INJECT/stop" ] &&
          [ "$i" -lt 1200 ]; do
        usleep 100000
        i=$((i + 1))
    done
    [ -f "$BT_INJECT/done" ]
}

bt_inject_play_file() {
    src=$1
    [ -f "$src" ] || return 1
    rm -f "$BT_INJECT/done" "$BT_INJECT/start"
    mv "$src" "$BT_INJECT/pcm.raw.tmp" 2>/dev/null || return 1
    mv "$BT_INJECT/pcm.raw.tmp" "$BT_INJECT/pcm.raw" 2>/dev/null || return 1
    : >"$BT_INJECT/start"
    bt_inject_wait_done
    rc=$?
    rm -f "$BT_INJECT/done" "$BT_INJECT/start" "$BT_INJECT/pcm.raw"
    return "$rc"
}

bt_inject_feeder() {
    next=1
    while [ -f "$BT_INJECT/active" ] && [ ! -f "$BT_INJECT/stop" ]; do
        if [ -f "$QD/chime.raw" ]; then
            bt_inject_play_file "$QD/chime.raw" || break
            continue
        fi
        if [ -f "$QD/q.$next.raw" ]; then
            [ "$next" = 1 ] && : >"$QD/started"
            bt_inject_play_file "$QD/q.$next.raw" || break
            next=$((next + 1))
            continue
        fi
        if [ -f "$QD/abort" ]; then
            rm -f "$QD"/q.*.raw "$QD/q.end" "$QD/abort"
            : >"$QD/played"
            next=1
            continue
        fi
        if [ -f "$QD/q.end" ]; then
            : >"$QD/played"
            rm -f "$QD/q.end"
            next=1
            continue
        fi
        usleep 50000
    done
}

bt_inject_begin() {
    bt_stream_live || return 1
    rm -rf "$BT_INJECT"
    mkdir -p "$BT_INJECT" || return 1
    mkdir -p "$QD" || return 1
    rm -f "$QD"/*
    : >/tmp/hk-ap2/run/bt-voice-mix.log
    master=$(mx_master)
    digital=$(mx_values 1)
    case "$master" in ''|*[!0-9]*) master=4 ;; esac
    case "$digital" in ''|*,|*[!0-9,]*) digital=207,207 ;; esac
    printf '%s %s\n' "$master" "$digital" >"$BT_INJECT/meta"
    : >"$BT_INJECT/active"
    BT_DUCKED=1

    # The preload creates "attached" from inside a01controller on its next
    # PCM write. Refuse this path if the verified shim is not active.
    i=0
    while [ "$i" -lt 30 ] && [ ! -f "$BT_INJECT/attached" ]; do
        usleep 100000
        i=$((i + 1))
    done
    if [ ! -f "$BT_INJECT/attached" ]; then
        rm -f "$BT_INJECT/active"
        rm -rf "$BT_INJECT"
        BT_DUCKED=0
        log "Bluetooth voice mixer unavailable"
        return 1
    fi

    # Drain buffered phone audio before the voice samples are queued.
    mx_cset 1 0,0
    usleep 300000
    bt_inject_stop_feeder
    rm -f "$BT_INJECT/stop"
    bt_inject_feeder </dev/null >/dev/null 2>&1 &
    BT_INJECT_PID=$!
    log "Bluetooth voice mixer attached; music ducked"
    return 0
}

bt_inject_prepare_voice() {
    [ -f "$BT_INJECT/active" ] || return 1
    speaker_guard || return 1
    BT_DUCKED=1
    read -r saved_master saved_digital <"$BT_INJECT/meta" 2>/dev/null || true
    case "$saved_digital" in ''|*,|*[!0-9,]*) saved_digital=207,207 ;; esac
    # a01controller has written silence for several seconds by this point.
    # Reassert digital silence, wait for the DAC ramp, and recheck the shared
    # guard before restoring gain for the chime/TTS.
    mx_cset 1 0,0 || return 1
    usleep 300000
    speaker_guard || return 1
    mx_cset 1 "$saved_digital"
    usleep 200000
}

bt_inject_end() {
    [ -d "$BT_INJECT" ] || { BT_DUCKED=0; return 0; }
    read -r saved_master saved_digital <"$BT_INJECT/meta" 2>/dev/null || true
    case "$saved_master" in ''|*[!0-9]*) saved_master=4 ;; esac
    case "$saved_digital" in ''|*,|*[!0-9,]*) saved_digital=207,207 ;; esac

    bt_inject_stop_feeder
    mx_cset 1 0,0
    usleep 300000
    rm -f "$BT_INJECT/active" "$BT_INJECT/start" "$BT_INJECT/done"
    usleep 700000
    if speaker_guard; then
        mx_cset 1 "$saved_digital"
        usleep 200000
    fi
    rm -rf "$BT_INJECT"
    BT_DUCKED=0
    log "Bluetooth voice mixer released; music restored"
}

# Runs in the background (started by hk-voice once the question has ended):
# open the speaker path, queue the acknowledgement chime, and record
# "<ok|fail>" in $PREPARED.  AirPlay was already paused at the wake.
prepare_output() {
    rm -f "$PREPARED"
    led_hold THINKING
    led_keeper </dev/null >/dev/null 2>&1 &
    st=fail
    if bt_inject_prepare_voice; then
        speaker_lock
        cut -d. -f1 /proc/uptime >"$VOICE_OWNS"
        mine=1
        speaker_unlock
        st=ok
        env LD_LIBRARY_PATH=/system/workdir/lib "$D/hk-voice" tone "$QD/chime.tmp" \
            >/dev/null 2>&1 && mv "$QD/chime.tmp" "$QD/chime.raw"
        log "Bluetooth voice output prepared"
    else
        i=0
        while [ "$i" -lt 40 ] && ! pcm_closed; do
            usleep 100000
            i=$((i + 1))
        done
        speaker_lock
        if pcm_closed && ! ap2_claimed && [ ! -s "$BT_OWNER" ]; then
            cut -d. -f1 /proc/uptime >"$VOICE_OWNS"
            mine=1
        else
            mine=
        fi
        speaker_unlock
        if [ -z "$mine" ]; then
            owner=$(sed -n 's/^owner_pid[[:space:]]*:[[:space:]]*//p' "$PCM_STATUS" 2>/dev/null)
            owner_cmd=
            case "$owner" in ''|*[!0-9]*) ;; *) owner_cmd=$(cat "/proc/$owner/comm" 2>/dev/null) ;; esac
            log "output busy (owner=${owner:-none} comm=${owner_cmd:-none}); reply will not be played"
        elif [ ! -f "$SAFE/armed" ] || [ -f "$SAFE/tripped" ]; then
            log "speaker path not armed or tripped"
        else
            start_stream
            usleep 300000
            if speaker_open; then
                st=ok
                env LD_LIBRARY_PATH=/system/workdir/lib "$D/hk-voice" tone "$QD/chime.tmp" \
                    >/dev/null 2>&1 && mv "$QD/chime.tmp" "$QD/chime.raw"
            else
                log "speaker hook refused start"
                stop_stream
            fi
        fi
    fi
    [ "$st" = ok ] || [ -z "$mine" ] || voice_release
    echo "$st" >"$PREPARED.tmp" && mv "$PREPARED.tmp" "$PREPARED"
}

# Close the speaker path opened by prepare_output.
close_output() {
    i=0
    while [ ! -f "$PREPARED" ] && [ "$i" -lt 100 ]; do usleep 100000; i=$((i + 1)); done
    rm -f "$LED_FLAG"
    if [ "$BT_DUCKED" = 1 ] || [ -f "$BT_INJECT/active" ]; then
        :
    elif [ "$(cat "$PREPARED" 2>/dev/null)" = ok ]; then
        env $HOOK_ENV "$HOOK" stop      # fades out while silence still flows
        stop_stream
        voice_release
    fi
    rm -f "$PREPARED"
}

wake_count() { grep -c MCULEDLISTENING "$EV" 2>/dev/null || echo 0; }

# One question/answer round from byte position $1 of the live recording,
# waiting up to $2 ms for speech.  Returns 0 answered, 3 not recognised,
# 4 nothing said, 5 local command (in $ACTION), 6 interrupted by a new wake.
run_round() {
    rm -f "$PREPARED" "$PREPARED.tmp" "$ACTION"
    if [ "$BT_DUCKED" = 1 ]; then
        rm -f "$QD/played" "$QD/started"
    fi
    base=$(wake_count)
    HK_VOICE_PREPARE="$0 prepare" HK_VOICE_QDIR=$QD HK_VOICE_ACTION_FILE=$ACTION \
        HK_VOICE_NOSPEECH_MS=$2 nice -n 5 env ${3:+HK_VOICE_STRICT=1} LD_LIBRARY_PATH=/system/workdir/lib \
        "$D/hk-voice" listen "$CUR.swp" "$1" /dev/null </dev/null >>"$LOG" 2>&1 &
    vpid=$!
    rc=
    ticks=0
    freshed=
    PLAYED_MS=
    while :; do
        ticks=$((ticks + 1))
        [ "$ticks" -lt 300 ] || {
            log "round timed out after 60s"
            kill "$vpid" 2>/dev/null
            rc=1
            break
        }
        if [ -z "$rc" ] && ! kill -0 "$vpid" 2>/dev/null; then
            wait "$vpid"
            rc=$?
            [ "$rc" = 4 ] && return 4          # no prepare was started
            case "$rc" in 0|7) ;; *) break ;; esac
        fi
        # Once the question has been read (prepare started), stop recording.
        # LLM and TTS can take many seconds, and keeping a recording live while
        # this function waits otherwise pins 176 KiB/s of tmpfs until the hard
        # timeout. Start a fresh, rotating recording only when the reply is
        # about to play; that recording is all the follow-up window needs.
        if [ -z "$freshed" ] && [ -f "$LED_FLAG" ]; then
            rec_pause_and_drop
            freshed=paused
        fi
        if [ "$freshed" = paused ] && [ -f "$PREPARED" ]; then
            rec_fresh
            freshed=1
        fi
        [ "$freshed" = 1 ] && rec_rotate_if_needed
        [ -z "$PLAYED_MS" ] && [ -f "$QD/played" ] && PLAYED_MS=$(now_ms)
        if { [ "$rc" = 0 ] || [ "$rc" = 7 ]; } && { [ -f "$QD/played" ] || [ ! -f "$PREPARED" ] ||
                              [ "$(cat "$PREPARED")" != ok ]; }; then
            [ -f "$PREPARED" ] && break
        fi
        # "Alexa" while thinking or speaking interrupts the reply.
        n=$(wake_count)
        [ "$n" -lt "$base" ] && base=$n        # events file was truncated
        if [ "$n" -gt "$base" ]; then
            : >"$QD/q.abort" 2>/dev/null
            kill "$vpid" 2>/dev/null
            wait "$vpid" 2>/dev/null
            log "interrupted by a new wake"
            close_output
            return 6
        fi
        usleep 200000
    done
    close_output
    return "$rc"
}

do_action() {
    act=$(cat "$ACTION" 2>/dev/null)
    log "command: $act"
    case "$act" in
        pause)
            PAUSED=0
            BT_PAUSED=0
            rm -f "$BT_RESUME" "$BT_RELEASE" ;;
        play)
            PAUSED=1
            [ -f "$BT_RESUME" ] && BT_PAUSED=1 ;;
        next)
            PAUSED=1
            [ -f "$BT_RESUME" ] && BT_PAUSED=1
            "$MQTT" hk-ap2/remote nextitem >/dev/null 2>&1 ;;
        prev)
            PAUSED=1
            [ -f "$BT_RESUME" ] && BT_PAUSED=1
            "$MQTT" hk-ap2/remote previtem >/dev/null 2>&1 ;;
        volup|voldown)
            [ -f "$BT_RESUME" ] && BT_PAUSED=1
            if [ ! -f "$BT_RESUME" ] && netstat -tn 2>/dev/null | grep -q ':7000 .*ESTABLISHED'; then
                cmd=volumeup; [ "$act" = voldown ] && cmd=volumedown
                for k in 1 2 3; do "$MQTT" hk-ap2/remote "$cmd" >/dev/null 2>&1; done
            else
                v=$("$REQ" /tmp/Requesta01controller getPlayerStatus 2>/dev/null |
                    sed -n 's/.*"vol":"\([0-9]*\)".*/\1/p')
                [ -n "$v" ] || v=25
                if [ "$act" = volup ]; then v=$((v + 10)); else v=$((v - 10)); fi
                [ "$v" -gt 100 ] && v=100
                [ "$v" -lt 0 ] && v=0
                "$REQ" /tmp/Requesta01controller "setPlayerCmd:vol:$v" >/dev/null 2>&1
            fi ;;
    esac
}

# Pre-roll: asr_tts starts a fresh recording only seconds after a wake, which
# cuts off the question.  So a recording runs all the time (tmpfs, RAM), is
# restarted every PREROLL_MAX bytes while idle, and on a wake we cut out the
# part from just before the wake.  asr_tts writes <file>.swp while recording.
PRE_A=/dev/shm/hk-voice-pre-a.wav
PRE_B=/dev/shm/hk-voice-pre-b.wav
PREROLL_MAX=${HK_VOICE_PREROLL_MAX:-1764000}   # 10 s of 44.1 kHz stereo S16 (RAM)
BYTES_PER_S=176400
CUR=

rec_start() {
    rec_stop        # asr_tts keeps one recording; never leave an old one running
    REC_TRY=$(cut -d. -f1 /proc/uptime)
    if [ "$CUR" = "$PRE_A" ]; then CUR=$PRE_B; else CUR=$PRE_A; fi
    rm -f "$CUR" "$CUR.swp" "$CUR.inf"
    "$REQ" /tmp/RequestASRTTS "recordFileStart:$CUR" </dev/null >/dev/null 2>&1
}

# asr_tts crashes (and is respawned, losing its state) when it gets
# recordFileStop while nothing is recording, so only stop a live recording.
rec_stop() {
    for sf in /dev/shm/hk-voice-pre-*.swp; do
        [ -f "$sf" ] || continue
        sa=$(wc -c <"$sf" 2>/dev/null || echo 0)
        usleep 300000
        sb=$(wc -c <"$sf" 2>/dev/null || echo 0)
        if [ "$sb" != "$sa" ]; then      # still growing: a live recording
            "$REQ" /tmp/RequestASRTTS recordFileStop </dev/null >/dev/null 2>&1
            return
        fi
        rm -f "$sf"                      # left over from a dead asr_tts
    done
}

rec_size() { wc -c <"$CUR.swp" 2>/dev/null || echo 0; }

# Hard memory guard: /dev/shm is RAM.  Any pre-roll file beyond twice the
# rotation size means a recording escaped rotation; stop and delete it.
rec_guard() {
    for gf in /dev/shm/hk-voice-pre-*; do
        [ -f "$gf" ] || continue
        case "$gf" in
            "$CUR.swp") ;;
            *.swp) ;;
            *) [ "$gf" = "$CUR" ] || { rm -f "$gf"; continue; } ;;   # finished, unused
        esac
        gsz=$(wc -c <"$gf" 2>/dev/null || echo 0)
        if [ "$gsz" -gt $((PREROLL_MAX * 2)) ]; then
            log "pre-roll $gf grew to $gsz bytes; stopping it"
            rec_stop
            rm -f "$gf" "${gf%.swp}" "${gf%.swp}.inf"
            CUR=
        fi
    done
}

# Start a new pre-roll file now and drop the old one.
rec_fresh() {
    old=$CUR
    rec_start
    usleep 300000
    [ -n "$old" ] && rm -f "$old" "$old.swp" "$old.inf"
}

rec_pause_and_drop() {
    old=$CUR
    rec_stop
    CUR=
    [ -n "$old" ] && rm -f "$old" "$old.swp" "$old.inf"
}

rec_rotate_if_needed() {
    rec_guard
    size=$(rec_size)
    if [ ! -f "$CUR.swp" ]; then
        # Not recording (asr_tts starting up or respawned): retry every 5 s.
        now=$(cut -d. -f1 /proc/uptime)
        [ $((now - ${REC_TRY:-0})) -ge 5 ] || return 0
        pidof asr_tts >/dev/null 2>&1 && [ -S /tmp/RequestASRTTS ] || return 0
        rec_start
        return 0
    fi
    if [ "$size" -ge "$PREROLL_MAX" ]; then
        old=$CUR
        rec_stop
        rec_start
        usleep 200000
        rm -f "$old" "$old.swp" "$old.inf"
    fi
}

# Remove anything a killed round left behind: our silence stream holding the
# DAC (AirPlay then cannot open it), the open speaker path and the lights.
cleanup_output() {
    pids=$(ps | grep -e "[h]k-voice feedq" -e "[h]k-voice.sh prepare" -e "[h]k-voice listen" |
        awk '{print $1}')
    owned=0
    ps | grep -q "[h]k-voice feedq" && owned=1
    [ -d "$QD" ] && : >"$QD/stop"
    sleep 1
    for p in $pids; do kill "$p" 2>/dev/null; done
    for p in $(ps | grep "[a]play -q -D hk_harman_dsp -f S16_LE -r 48000" | awk '{print $1}'); do
        kill "$p" 2>/dev/null
    done
    if [ "$owned" = 1 ]; then
        env $HOOK_ENV "$HOOK" stop
        led IDLE
    fi
    if [ "$BT_DUCKED" = 1 ] || [ -f "$BT_INJECT/active" ]; then
        bt_inject_end
    fi
    rm -f "$QD"/* "$LED_FLAG" "$PREPARED" "$ACTION" /tmp/hk-ap2/run/release-dac 2>/dev/null
    rm -f "$BT_RELEASE"
    [ -f "$BT_RESUME" ] && resume_bluetooth
    rmdir "$RUN/reopen.lock" 2>/dev/null
    [ -f "$VOICE_OWNS" ] && voice_release
}

# Speak a short text (reminders), handling AirPlay like a wake does.
announce() {
    PAUSED=0
    RELEASED=0
    BT_PAUSED=0
    BT_DUCKED=0
    rm -f "$RELEASE"
    if airplay_owns_dac; then
        release_airplay
    elif bluetooth_playing; then
        if ! bt_inject_begin; then
            release_bluetooth
        fi
    fi
    prepare_output
    if [ "$(cat "$PREPARED" 2>/dev/null)" = ok ]; then
        led_hold PLAYTTS
        env LD_LIBRARY_PATH=/system/workdir/lib "$D/hk-voice" say "$1" "$QD/q.tmp" \
            >/dev/null 2>&1 ||
            env LD_LIBRARY_PATH=/system/workdir/lib "$D/hk-voice" clip remind "$QD/q.tmp" \
                >/dev/null 2>&1
        [ -f "$QD/q.tmp" ] && mv "$QD/q.tmp" "$QD/q.1.raw"
        : >"$QD/q.end"
        i=0
        while [ ! -f "$QD/played" ] && [ "$i" -lt 300 ]; do usleep 100000; i=$((i + 1)); done
    fi
    close_output
    led THINKING
    usleep 300000
    led IDLE
    rm -f "$RELEASE"
    [ "$RELEASED" = 1 ] && reopen_watch </dev/null >/dev/null 2>&1 &
    if [ "$BT_DUCKED" = 1 ]; then
        bt_inject_end
        voice_release
    fi
    [ "$BT_PAUSED" = 1 ] && resume_bluetooth
    [ "$PAUSED" = 1 ] && "$MQTT" hk-ap2/remote play >/dev/null 2>&1
}

check_reminders() {
    [ -s "$D/reminders" ] || return 0
    due=$(env LD_LIBRARY_PATH=/system/workdir/lib "$D/hk-voice" due 2>/dev/null) || return 0
    printf '%s\n' "$due" | while read -r text; do
        [ -n "$text" ] || continue
        if [ "$text" = "-" ]; then msg="提醒时间到了。"; else msg="提醒你：$text。"; fi
        log "reminder: $text"
        announce "$msg" </dev/null
        usleep 500000
        announce "$msg" </dev/null       # say it twice
    done
}

FOLLOWUPS=${HK_VOICE_FOLLOWUPS:-2}

RELEASE=/tmp/hk-ap2/run/release-dac
BT_OWNER=/tmp/hk-ap2/run/bt-owner.pid
BT_RELEASE=/tmp/hk-ap2/run/bt-release
BT_RESUME=/tmp/hk-ap2/run/bt-paused-by-voice
BT_INJECT=/tmp/hk-ap2/run/voice-inject
BT_DUCKED=0
BT_INJECT_PID=
airplay_owns_dac() {
    o=$(sed -n 's/owner_pid *: //p' "$PCM_STATUS" 2>/dev/null)
    [ -n "$o" ] || return 1
    for t in /proc/[0-9]*/task/"$o"; do
        [ -d "$t" ] || continue
        # Shairport uses the stock-ABI DSP bridge for pipe output, so either
        # process can own the DAC while an AirPlay stream is active.
        tr '\0' ' ' <"${t%/task/*}/cmdline" 2>/dev/null |
            grep -Eq 'shairport-sync|hk-ap2-dsp-bridge' && return 0
    done
    return 1
}

pcm_running() { [ "$(cat "$PCM_STATUS" 2>/dev/null | sed -n 1p)" = "state: RUNNING" ]; }

bluetooth_playing() {
    [ -S "$PLAYER_SOCKET" ] || return 1
    data=$("$REQ" "$PLAYER_SOCKET" getPlayerStatus 1200 2>/dev/null) || return 1
    printf '%s' "$data" | grep -q '"mode":"41"' || return 1
    printf '%s' "$data" | grep -q '"status":"play"' || return 1
    printf '%s' "$data" | grep -q '"mute":"0"' || return 1
}

# AirPlay holds the DAC for the whole session, even while paused.  Mute it
# (the hook fades first), then pause with the release flag set so the
# patched Shairport closes the device for us.
release_airplay() {
    pcm_running && PAUSED=1
    env $HOOK_ENV "$HOOK" stop
    rm -f "$AP2_CLAIM"
    : >"$RELEASE"
    "$MQTT" hk-ap2/remote pause >/dev/null 2>&1
    RELEASED=1
    log "AirPlay muted and paused for the voice reply (was playing: $PAUSED)"
}

# Bluetooth also holds the DAC for the whole A2DP stream. Release the bridge
# first so it can fade and close in order, then pause the stock transport.
release_bluetooth() {
    BT_PAUSED=1
    : >"$BT_RESUME"
    : >"$BT_RELEASE"
    "$REQ" "$PLAYER_SOCKET" setPlayerCmd:onepause 1200 >/dev/null 2>&1 ||
        log "Bluetooth pause command failed"
    i=0
    while [ "$i" -lt 60 ]; do
        [ ! -s "$BT_OWNER" ] && pcm_closed && break
        usleep 100000
        i=$((i + 1))
    done
    log "Bluetooth muted and paused for the voice reply"
}

resume_bluetooth() {
    try=0
    while [ "$try" -lt 3 ]; do
        rm -f "$BT_RELEASE"
        if "$REQ" "$PLAYER_SOCKET" setPlayerCmd:resume 1200 >/dev/null 2>&1; then
            rm -f "$BT_RESUME"
            BT_PAUSED=0
            log "resumed Bluetooth"
            return 0
        fi
        try=$((try + 1))
        sleep 1
    done
    log "Bluetooth resume failed; marker kept for the supervisor"
    return 1
}

# After we are done: let AirPlay have the DAC back and reopen the speaker
# path (pop-free order, via the hook) as soon as Shairport plays again.
reopen_watch() {
    mkdir "$RUN/reopen.lock" 2>/dev/null || return   # one watcher at a time
    i=0
    while [ "$i" -lt 7200 ]; do              # up to 1 h for a manual resume
        if airplay_owns_dac && pcm_running; then
            # Give Shairport's own hook a moment; open the path only if
            # nobody did, and retry (the PA helper can fail when racing).
            sleep 2
            for try in 1 2 3; do
                [ -f "$SAFE/play.started" ] &&
                    [ "$(mx_get "Ext Spk")" = on ] && break
                env $HOOK_ENV "$HOOK" start && { log "AirPlay speaker path reopened"; break; }
                log "AirPlay speaker reopen failed (try $try)"
                sleep 2
            done
            break
        fi
        netstat -tn 2>/dev/null | grep -q ':7000 .*ESTABLISHED' || break
        usleep 500000
        i=$((i + 1))
    done
    rmdir "$RUN/reopen.lock" 2>/dev/null
}

handle_wake() {
    PAUSED=0
    RELEASED=0
    BT_PAUSED=0
    BT_DUCKED=0
    rm -f "$RELEASE"
    if airplay_owns_dac; then
        release_airplay
    elif bluetooth_playing; then
        if ! bt_inject_begin; then
            release_bluetooth
        fi
    fi
    pos=$(rec_size)
    log "wake at $((pos / BYTES_PER_S))s of pre-roll"
    wait_ms=6000
    strict=
    rounds=0
    while :; do
        run_round "$pos" "$wait_ms" $strict
        rc=$?
        # A wake that came while we were finishing up (speaker closing,
        # follow-up not yet listening) is a new question: start from there.
        if [ "$rc" != 6 ] && [ "$(wake_count)" -gt "$base" ]; then
            log "new wake during the round"
            rc=6
        fi
        case "$rc" in
            0)  rounds=$((rounds + 1))
                [ "$rounds" -le "$FOLLOWUPS" ] || break
                # Follow-up: listen again without the wake word.
                led LISTENING
                # Listen from the moment the reply went quiet ("played" is
                # flagged ~0.9 s before the last sample leaves the speaker),
                # not from now: closing the speaker takes 1.5-2 s.
                pos=$(rec_size)
                if [ -n "$PLAYED_MS" ]; then
                    ago=$(( $(now_ms) - PLAYED_MS - 900 ))
                    [ "$ago" -gt 0 ] && [ "$ago" -lt 8000 ] &&
                        pos=$(( pos - ago * (BYTES_PER_S / 100) / 10 ))
                    [ "$pos" -ge 44 ] || pos=44
                fi
                wait_ms=5000
                strict=1
                log "listening for a follow-up" ;;
            5)  do_action
                break ;;
            7)  break ;;                       # e.g. "network is down" was said
            6)  # Locate the new wake in the live recording by its timestamp.
                wts=$(grep MCULEDLISTENING "$EV" | tail -n 1 | cut -d' ' -f1)
                ago=$(( $(now_ms) - ${wts:-0} ))
                [ "$ago" -ge 0 ] && [ "$ago" -le 15000 ] || ago=500
                pos=$(( $(rec_size) - ago * (BYTES_PER_S / 100) / 10 ))
                [ "$pos" -ge 44 ] || pos=44
                log "re-listening from ${ago} ms ago"
                wait_ms=6000
                strict= ;;
            3)  log "no speech recognised"; break ;;
            4)  [ "$rounds" -gt 0 ] || log "nothing said after wake"; break ;;
            *)  log "hk-voice failed rc=$rc"; break ;;
        esac
    done
    rm -f "$LED_FLAG"
    # The ring only leaves LISTENING the stock way (LISTENING -> THINKING ->
    # IDLE); going straight to IDLE after a follow-up left it lit.
    led THINKING
    usleep 300000
    led IDLE
    rm -f "$RELEASE"
    rec_fresh
    [ "$RELEASED" = 1 ] && reopen_watch </dev/null >/dev/null 2>&1 &
    if [ "$BT_DUCKED" = 1 ]; then
        bt_inject_end
        voice_release
    fi
    [ "$BT_PAUSED" = 1 ] && resume_bluetooth
    if [ "$PAUSED" = 1 ]; then
        "$MQTT" hk-ap2/remote play >/dev/null 2>&1
        log "resumed AirPlay"
    fi
    log "done"
}

loop() {
    # Under memory pressure the kernel should kill us before AirPlay (11)
    # or the stock processes (0), but not in the kernel's eagerly killed
    # adj >= 12 bucket.
    echo "$VOICE_OOM_SCORE_ADJ" >/proc/self/oom_score_adj 2>/dev/null
    [ -s "$D/clips/neterr.wav" ] ||
        env LD_LIBRARY_PATH=/system/workdir/lib "$D/hk-voice" mkclips >/dev/null 2>&1
    log "voice loop started"
    FIFO=$RUN/events.fifo
    while :; do
        [ -f "$EV" ] || : >"$EV"
        rm -f "$FIFO"
        mkfifo "$FIFO" || { sleep 5; continue; }
        tail -n0 -f "$EV" >"$FIFO" 2>/dev/null &
        tailpid=$!
        exec 3<"$FIFO"
        rec_start
        busy_until=0
        while kill -0 "$tailpid" 2>/dev/null; do
            if [ -f "$D/disable" ]; then
                rec_stop
                kill "$tailpid" 2>/dev/null
                rm -f "$PRE_A" "$PRE_A.swp" "$PRE_A.inf" "$PRE_B" "$PRE_B.swp" "$PRE_B.inf"
                log "disabled; loop exiting"
                exit 0
            fi
            if read -t 1 -r ts ev <&3; then
                case "$ev" in
                    *MCULEDLISTENING) ;;
                    *) continue ;;
                esac
                case "$ts" in ''|*[!0-9]*) continue ;; esac
                # Skip wakes queued while the previous one was being handled.
                [ "$ts" -ge "$busy_until" ] || continue
                handle_wake
                busy_until=$(now_ms)
            else
                rec_rotate_if_needed
                # Keep the default city's forecast warm (every ~30 min idle).
                idle=$(( ${idle:-0} + 1 ))
                [ $((idle % 5)) = 0 ] && check_reminders
                if [ $((idle % 1800)) = 5 ]; then
                    nice -n 10 env LD_LIBRARY_PATH=/system/workdir/lib \
                        "$D/hk-voice" weather-refresh </dev/null >/dev/null 2>&1 &
                fi
            fi
        done
        exec 3<&-
        rec_stop
        log "event reader ended; restarting"
        sleep 2
    done
}

# Memory watch (runs in the supervisor, which is not a low-memory-killer
# target): when free + page cache drops under 24 MB, log a snapshot of the
# largest processes and kernel memory, at most once a minute.
MEMLOG=$RUN/mem.log
mem_watch() {
    set -- $(awk '/^MemFree:/{f=$2} /^Cached:/{c=$2} /^Shmem:/{s=$2}
                  END{print f, c - s}' /proc/meminfo)
    [ $(( $1 + $2 )) -lt 24576 ] || return 0
    now=$(cut -d. -f1 /proc/uptime)
    [ $((now - ${MEM_LAST:-0})) -ge 60 ] || return 0
    MEM_LAST=$now
    {
        echo "== $(date '+%m-%d %H:%M:%S') free ${1} kB, file cache ${2} kB"
        grep -E '^(AnonPages|Shmem|Slab|SUnreclaim|KernelStack|PageTables|VmallocUsed):' /proc/meminfo |
            tr -s ' ' | tr '\n' ' '
        echo
        df -k /dev/shm /tmp 2>/dev/null | awk 'NR>1{print "tmpfs", $6, $3 " kB"}'
        for p in /proc/[0-9]*; do
            r=$(awk '/^VmRSS:/{print $2}' "$p/status" 2>/dev/null)
            [ -n "$r" ] && [ "$r" -gt 400 ] &&
                echo "$r ${p#/proc/} $(tr '\0' ' ' <"$p/cmdline" 2>/dev/null | cut -c1-60)"
        done | sort -rn | head -12
    } >>"$MEMLOG" 2>/dev/null
    if [ "$(wc -c <"$MEMLOG")" -gt 32768 ]; then
        tail -c 16384 "$MEMLOG" >"$MEMLOG.tmp" && mv "$MEMLOG.tmp" "$MEMLOG"
    fi
}

supervisor_health() {
    pid=$(cat "$PIDFILE" 2>/dev/null)
    case "$pid" in ''|*[!0-9]*) return 0 ;; esac
    kill -0 "$pid" 2>/dev/null || return 0

    # A healthy loop rotates the 10 s RAM pre-roll long before this limit.  A
    # stuck Alexa round otherwise grows it at 176.4 KiB/s and eventually makes
    # LMK reclaim Shairport or the stock player.
    for f in /dev/shm/hk-voice-pre-*.swp; do
        [ -f "$f" ] || continue
        size=$(wc -c <"$f" 2>/dev/null || echo 0)
        if [ "$size" -gt $((PREROLL_MAX * 2)) ]; then
            log "voice pre-roll stuck at $size bytes; restarting loop $pid"
            kill -KILL "$pid" 2>/dev/null || true
            rm -f "$PIDFILE" "$RUN/prepare-watch" /dev/shm/hk-voice-pre-*
            for p in $(ps | grep "[t]ail -n0 -f $EV" | awk '{print $1}'); do
                kill "$p" 2>/dev/null || true
            done
            return 0
        fi
    done

    # The prepare hook opens the speaker path while ASR runs and normally exits
    # in a few seconds. If an orphan remains for roughly one minute, terminate
    # that round and let the supervisor create a clean loop.
    prepare_pids=$(ps | awk '$0 ~ /[h]k-voice\.sh prepare$/ {print $1}')
    if [ -z "$prepare_pids" ]; then
        rm -f "$RUN/prepare-watch"
        return 0
    fi
    prepare_key=$(printf '%s' "$prepare_pids" | tr -d ' \n')
    old_key=
    old_count=0
    read -r old_key old_count <"$RUN/prepare-watch" 2>/dev/null || true
    if [ "$old_key" = "$prepare_key" ]; then
        count=$((old_count + 1))
    else
        count=1
    fi
    printf '%s %s\n' "$prepare_key" "$count" >"$RUN/prepare-watch"
    prepared=$(cat "$PREPARED" 2>/dev/null || true)
    limit=6
    [ "$prepared" = ok ] && limit=3
    if [ "$count" -ge "$limit" ]; then
        log "voice prepare hook stuck for ${count} health checks; terminating it"
        for p in $prepare_pids; do kill -KILL "$p" 2>/dev/null || true; done
        rm -f "$RUN/prepare-watch"
        # Once PREPARED=ok the main round already has its speaker path and
        # reply queue. Kill only the stale helper; restarting the loop here
        # would discard a valid TTS response and create another silent gap.
        if [ "$prepared" != ok ]; then
            kill -KILL "$pid" 2>/dev/null || true
            rm -f "$PIDFILE" /dev/shm/hk-voice-pre-*
            for p in $(ps | grep "[t]ail -n0 -f $EV" | awk '{print $1}'); do
                kill "$p" 2>/dev/null || true
            done
        fi
    fi
}

case "${1:-start}" in
    boot)
        [ -f "$D/disable" ] && exit 0
        # Wait for the AP2 speaker path (watchdog) and the key hook.
        i=0
        while [ "$i" -lt 180 ] && [ ! -f "$SAFE/watchdog.pid" ]; do sleep 2; i=$((i + 1)); done
        exec "$0" supervise
        ;;
    supervise)
        # Keep the voice loop running; restart it if it dies (e.g. killed
        # under memory pressure), with a growing pause between restarts.
        mkdir -p "$RUN"
        echo "$VOICE_SUPERVISOR_OOM_SCORE_ADJ" >/proc/self/oom_score_adj 2>/dev/null
        echo $$ >"$RUN/supervisor.pid"
        "$0" start
        back=10
        tick=0
        while :; do
            sleep 5
            mem_watch
            tick=$((tick + 1))
            [ $((tick % 2)) = 0 ] || continue
            supervisor_health
            [ -f "$D/disable" ] && continue
            pid=$(cat "$PIDFILE" 2>/dev/null)
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
                back=10
                continue
            fi
            log "voice loop not running; restarting in ${back}s"
            sleep "$back"
            "$0" start
            [ "$back" -lt 300 ] && back=$((back * 2))
        done
        ;;
    start)
        [ -f "$D/disable" ] && exit 0
        mkdir -p "$RUN"
        pid=$(cat "$PIDFILE" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo "already running: $pid"
            exit 0
        fi
        # A killed shell can leave its FIFO reader orphaned while the
        # supervisor replaces the authoritative loop.  Two loops both consume
        # wake events and can race through ASR, so reap exact stale instances
        # before creating a new FIFO.
        for p in $(ps | awk '$0 ~ /[h]k-voice\.sh start$/ {print $1}'); do
            [ "$p" = "$$" ] && continue
            kill "$p" 2>/dev/null || true
        done
        cleanup_output
        loop </dev/null >/dev/null 2>&1 &
        echo $! >"$PIDFILE"
        ;;
    stop)
        sp=$(cat "$RUN/supervisor.pid" 2>/dev/null)
        [ -n "$sp" ] && kill "$sp" 2>/dev/null
        rm -f "$RUN/supervisor.pid"
        pid=$(cat "$PIDFILE" 2>/dev/null)
        [ -n "$pid" ] && kill "$pid" 2>/dev/null
        cleanup_output
        for p in $(ps | grep "[t]ail -n0 -f $EV" | awk '{print $1}'); do kill "$p"; done
        rec_stop
        rm -f "$PIDFILE" "$REC" "$OUT" /dev/shm/hk-voice-pre-* "$RUN/events.fifo"
        ;;
    prepare)
        mkdir -p "$RUN"
        prepare_output
        ;;
    say)
        # Speak a test sentence through the same path: hk-voice.sh say <text>
        mkdir -p "$RUN"
        prepare_output
        if [ "$(cat "$PREPARED" 2>/dev/null)" = ok ]; then
            env LD_LIBRARY_PATH=/system/workdir/lib "$D/hk-voice" say "$2" "$QD/q.tmp" &&
                mv "$QD/q.tmp" "$QD/q.1.raw" && : >"$QD/q.end"
            i=0
            while [ ! -f "$QD/played" ] && [ "$i" -lt 300 ]; do usleep 100000; i=$((i + 1)); done
        fi
        close_output
        led IDLE
        ;;
    status)
        pid=$(cat "$PIDFILE" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "running: $pid"; else echo "stopped"; fi
        tail -n 12 "$LOG" 2>/dev/null
        ;;
esac
exit 0
