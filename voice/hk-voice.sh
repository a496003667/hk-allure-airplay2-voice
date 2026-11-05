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
APLAY=/system/workdir/bin/aplay
MAX_RECORD=${HK_VOICE_MAX_RECORD:-8}

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

HOOK_ENV="HK_AP2_USE_STOCK_PA=1 HK_AP2_MASTER_SAFE=4 HK_AP2_DIGITAL_SAFE=192 HK_AP2_PLAYBACK_SAFE=1"
PREPARED=$RUN/prepared

AMIXER=/system/workdir/bin/amixer
mx() { env LD_LIBRARY_PATH=/system/workdir/lib "$AMIXER" -c 0 "$@" >/dev/null 2>&1; }
mx_get() {
    env LD_LIBRARY_PATH=/system/workdir/lib "$AMIXER" -c 0 sget "$1" 2>/dev/null |
        sed -n 's/.*\[\(on\|off\)\].*/\1/p' | sed -n 1p
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

# Open the speaker path without a pop (verified step by step with the user):
# start I2S data while the stock amplifier is still muted, then unmute the
# amplifier, then switch Ext Spk on and fade the digital volume in.  The AP2
# hook's order (amplifier first, then I2S) pops audibly.
speaker_open() {
    [ -f "$SAFE/armed" ] && [ ! -f "$SAFE/tripped" ] || return 1
    hb=$(cat "$SAFE/watchdog.heartbeat" 2>/dev/null)
    printf '%s\n' "$(cut -d. -f1 /proc/uptime)" >"$SAFE/play.started.tmp" &&
        mv "$SAFE/play.started.tmp" "$SAFE/play.started" || return 1
    i=0   # wait for the watchdog to see the lease, as the hook does
    while [ "$i" -lt 40 ]; do
        usleep 100000
        [ "$(cat "$SAFE/watchdog.heartbeat" 2>/dev/null)" != "$hb" ] && break
        i=$((i + 1))
    done
    mx sset "Ext Spk" off
    mx sset Master 4
    mx cset numid=1 0,0
    mx cset numid=2 1,1
    mx sset Digital on
    mx sset "Auto Mute" off
    mx sset "aml audio i2s mute" off
    usleep 200000
    stock_pa unmute
    mx sset "Ext Spk" on
    usleep 200000
    mx cset numid=1 192,192
    echo "$(date '+%Y-%m-%dT%H:%M:%S%z') voice speaker opened" >>/tmp/hk-ap2/run/hook.log
    return 0
}

# A silence stream that keeps the DAC clocked while the amplifier switches;
# the feeder plays $QD/chime.raw and then $QD/q.N.raw as they appear.
start_stream() {
    mkdir -p "$QD"
    rm -f "$QD"/*
    "$D/hk-voice" feedq "$QD" 2>/dev/null |
        env LD_LIBRARY_PATH=/system/workdir/lib "$APLAY" -q -D hw:0,0 \
            -f S16_LE -r 48000 -c 2 -t raw >/dev/null 2>&1 &
}

stop_stream() {
    : >"$QD/stop"
    i=0
    while ! pcm_closed && [ "$i" -lt 30 ]; do usleep 100000; i=$((i + 1)); done
    rm -f "$QD"/*
}

# Runs in the background (started by hk-voice once the question has ended):
# open the speaker path, queue the acknowledgement chime, and record
# "<ok|fail>" in $PREPARED.  AirPlay was already paused at the wake.
prepare_output() {
    rm -f "$PREPARED"
    led_hold THINKING
    led_keeper </dev/null >/dev/null 2>&1 &
    st=fail
    i=0
    while [ "$i" -lt 40 ] && ! pcm_closed; do
        usleep 100000
        i=$((i + 1))
    done
    if ! pcm_closed; then
        log "output busy; reply will not be played"
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
    echo "$st" >"$PREPARED.tmp" && mv "$PREPARED.tmp" "$PREPARED"
}

# Close the speaker path opened by prepare_output.
close_output() {
    i=0
    while [ ! -f "$PREPARED" ] && [ "$i" -lt 100 ]; do usleep 100000; i=$((i + 1)); done
    rm -f "$LED_FLAG"
    if [ "$(cat "$PREPARED" 2>/dev/null)" = ok ]; then
        env $HOOK_ENV "$HOOK" stop      # fades out while silence still flows
        stop_stream
    fi
    rm -f "$PREPARED"
}

wake_count() { grep -c MCULEDLISTENING "$EV" 2>/dev/null || echo 0; }

# One question/answer round from byte position $1 of the live recording,
# waiting up to $2 ms for speech.  Returns 0 answered, 3 not recognised,
# 4 nothing said, 5 local command (in $ACTION), 6 interrupted by a new wake.
run_round() {
    rm -f "$PREPARED" "$PREPARED.tmp" "$ACTION"
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
        [ "$ticks" -lt 900 ] || { log "round timed out"; kill "$vpid" 2>/dev/null; rc=1; break; }
        if [ -z "$rc" ] && ! kill -0 "$vpid" 2>/dev/null; then
            wait "$vpid"
            rc=$?
            [ "$rc" = 4 ] && return 4          # no prepare was started
            case "$rc" in 0|7) ;; *) break ;; esac
        fi
        # Once the question has been read (prepare started), the old pre-roll
        # can go: start a fresh recording that also covers the reply and the
        # moments right after it.
        if [ -z "$freshed" ] && [ -f "$LED_FLAG" ]; then
            rec_fresh
            freshed=1
        fi
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
        pause) PAUSED=0 ;;   # already paused at the wake; just don't resume
        play) PAUSED=1 ;;    # resumed (and the path reopened) at the end
        next) PAUSED=1; "$MQTT" hk-ap2/remote nextitem >/dev/null 2>&1 ;;
        prev) PAUSED=1; "$MQTT" hk-ap2/remote previtem >/dev/null 2>&1 ;;
        volup|voldown)
            if netstat -tn 2>/dev/null | grep -q ':7000 .*ESTABLISHED'; then
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
PREROLL_MAX=${HK_VOICE_PREROLL_MAX:-3528000}   # 20 s of 44.1 kHz stereo S16
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
    for p in $(ps | grep "[a]play -q -D hw:0,0 -f S16_LE -r 48000" | awk '{print $1}'); do
        kill "$p" 2>/dev/null
    done
    if [ "$owned" = 1 ]; then
        env $HOOK_ENV "$HOOK" stop
        led IDLE
    fi
    rm -f "$QD"/* "$LED_FLAG" "$PREPARED" "$ACTION" /tmp/hk-ap2/run/release-dac 2>/dev/null
    rmdir "$RUN/reopen.lock" 2>/dev/null
}

# Speak a short text (reminders), handling AirPlay like a wake does.
announce() {
    PAUSED=0
    RELEASED=0
    rm -f "$RELEASE"
    airplay_owns_dac && release_airplay
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
airplay_owns_dac() {
    o=$(sed -n 's/owner_pid *: //p' "$PCM_STATUS" 2>/dev/null)
    [ -n "$o" ] || return 1
    for t in /proc/[0-9]*/task/"$o"; do
        [ -d "$t" ] || continue
        tr '\0' ' ' <"${t%/task/*}/cmdline" 2>/dev/null | grep -q shairport-sync && return 0
    done
    return 1
}

pcm_running() { [ "$(cat "$PCM_STATUS" 2>/dev/null | sed -n 1p)" = "state: RUNNING" ]; }

# AirPlay holds the DAC for the whole session, even while paused.  Mute it
# (the hook fades first), then pause with the release flag set so the
# patched Shairport closes the device for us.
release_airplay() {
    pcm_running && PAUSED=1
    env $HOOK_ENV "$HOOK" stop
    : >"$RELEASE"
    "$MQTT" hk-ap2/remote pause >/dev/null 2>&1
    RELEASED=1
    log "AirPlay muted and paused for the voice reply (was playing: $PAUSED)"
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
    rm -f "$RELEASE"
    airplay_owns_dac && release_airplay
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
    if [ "$PAUSED" = 1 ]; then
        "$MQTT" hk-ap2/remote play >/dev/null 2>&1
        log "resumed AirPlay"
    fi
    log "done"
}

loop() {
    # Under memory pressure the kernel should kill us before AirPlay (11)
    # or the stock processes (0).
    echo 15 >/proc/self/oom_score_adj 2>/dev/null
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
        echo $$ >"$RUN/supervisor.pid"
        "$0" start
        back=10
        while :; do
            sleep 10
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
