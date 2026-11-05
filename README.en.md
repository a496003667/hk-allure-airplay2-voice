[简体中文](README.md) | **English**

# Harman Kardon Allure: AirPlay 2 + on-device voice assistant

Adds **AirPlay 2** to the Harman Kardon Allure (the Alexa smart speaker) and replaces the retired Alexa with a Chinese voice assistant that **runs on the speaker itself**. The stock light ring, physical buttons and volume ring are wired back up to the new features.

## ⚠️ Read this first

- You need a **root shell** on the speaker. See [initial root access](docs/ROOTING.md) and the [persistent mTLS maintenance channel](docs/MAINTENANCE.md) (Chinese). The latter starts at boot and is used for routine maintenance. Certificates and keys are not included.
- The speaker has **no offline recovery path**. This project only touches the `/vendor` user-data partition and `/etc/init.d` start scripts; it never writes the bootloader, kernel or firmware partitions, and every feature can be switched off with `touch …/disable`. Please keep to the same boundary.
- Tested on one Allure only (Amlogic quad-core Cortex‑A5, ~110 MB RAM, Linux 3.10.33, firmware from 2019-09).
- Not affiliated with Harman Kardon, Amazon, Apple, Baidu or ModelScope; all names are trademarks of their respective owners.
- The voice assistant speaks and understands **Mandarin Chinese** (Baidu ASR/TTS); the prompts are in Chinese.

## Features

| Module | What it does |
|---|---|
| **AirPlay 2** | Shairport Sync 5.6 + NQPTP. Works from iPhone and Mac (Shairport Sync supports being added to the Home app; not tested by the author); 44.1/48 kHz, 32-bit output; software volume capped at -6 dB |
| **No pops** | Starts the I2S data first, then unmutes the stock amplifier, then switches the speaker on and fades in; no pops on pause, seek, or when two senders take over from each other |
| **Lights / volume ring** | Shows the stock "playing" animation during playback; the volume ring follows the sender's volume |
| **Buttons** | While an AirPlay sender is connected, play/pause and volume keys control the sender, with volume kept in sync both ways |
| **Voice assistant** | Uses the stock "Alexa" wake word; Baidu short-speech ASR → ModelScope Qwen LLM (streamed) → Baidu TTS (voice 4189) |
| **Conversation** | A chime when you finish speaking; follow-up questions without the wake word for ~5 s; say "Alexa" to interrupt; speaks while the answer is still being generated |
| **Local commands** | Pause / resume / next / previous / louder / quieter, handled without the LLM |
| **Weather** | open-meteo (free, no key); default city by IP geolocation; any city, today to the day after tomorrow |
| **Reminders / alarms / timers** | e.g. "十分钟后提醒我关火" (remind me in ten minutes), "明早七点叫我" (wake me at 7), "倒计时五分钟" (5-minute timer), list and cancel; many phrasings are recognised |
| **AirPlay + voice** | Waking it during playback fades out and pauses the music, frees the DAC, answers, then resumes |
| **Reliability** | Stored spoken prompts for network and service errors; the assistant is the first to go under memory pressure; a supervisor restarts it; the pre-roll recording is size-capped |
| **Debloat** | Stops the OTA updater, Alexa alarms and Alexa multi-room at boot (reversible) |

## Layout

```
airplay2/   Shairport Sync patch, config, AP2 controller (prodctl), speaker hook, safe-replace script
  runtime/  safety watchdog, resource guard and control scripts used by the AP2 runtime
lights/     light ring / volume ring bridge (hk-ap2-lights.sh + hk-shm-play.c)
keys/       key hook preloaded into the stock mv_ioguard
voice/      voice assistant: hk-voice.c (ASR / LLM / TTS / weather / reminders / playback queue),
            hk-voice.sh (main loop), key update script, config example
debloat/    stop OTA, Alexa alarms, Alexa multi-room
tools/      two tiny tools: MQTT publish and unix-socket request (no libc)
init/       /etc/init.d start scripts
docs/       technical notes and pitfalls (Chinese)
```

## Building

- **Shairport Sync**: take upstream 5.6 (the author used commit `9118bbc`), apply `airplay2/shairport-sync-5.6-hk.patch`, and cross-compile with a crosstool-ng `arm-unknown-linux-gnueabi` toolchain (soft-float, glibc 2.27). Dependencies: alsa-lib, libsoxr, ffmpeg (libavcodec/swresample), libsodium, libgcrypt, libplist, libmosquitto, avahi, libconfig, libpopt, OpenSSL. The runtime is packed as a cramfs image under `/vendor/airplay2/`, fully isolated from the stock hard-float glibc 2.19. NQPTP is upstream 1.2.x, unmodified.
- **hk-voice / key hook**: `armv7-linux-gnueabihf-gcc -Os -o hk-voice hk-voice.c -ldl -static-libgcc`. It reaches the stock libcurl + OpenSSL 1.1 through `dlopen`, avoids libm, and needs no symbol newer than GLIBC_2.17, so it runs on the stock glibc 2.19.
- **tools/**: static programs written against raw system calls; any ARM cross compiler will do.

## Installing (outline)

1. Put the modules under `/vendor/airplay2`, `/vendor/hk-lights`, `/vendor/hk-keys`, `/vendor/hk-voice` and `/vendor/hk-debloat`, and the start scripts in `/etc/init.d/`.
2. Set `airplay_device_id` in `airplay2/shairport-sync.conf` to your speaker's wlan0 MAC address.
3. Voice assistant: copy `voice/voice.conf.example` to `/vendor/hk-voice/voice.conf` (mode 600) and fill in your Baidu speech keys (short-speech ASR + TTS) and ModelScope API-Inference key. Run `hk-voice check` to test them and `hk-voice mkclips` to create the offline prompts. `voice/update-voice-keys.sh` rotates keys later (set `HK_PUT` / `HK_SHELL` to your own way of reaching the speaker).
4. The speaker's CA bundle is old: open-meteo needs the ISRG Root X1/X2 roots added, combined into `/vendor/hk-voice/ca.pem`.
5. **Never overwrite AirPlay files while they are bind-mounted.** Upload them as `<file>.new` and run `airplay2/hk-ap2-replace.sh <file>…` (stop, unmount, install, start, roll back on failure). `hk-ap2-prodctl` checks each file's SHA1, so update the hashes in it when you change a file.
6. To switch a feature off: `touch /vendor/<module>/disable`.

## Credits

[Shairport Sync](https://github.com/mikebrady/shairport-sync) and [NQPTP](https://github.com/mikebrady/nqptp) by Mike Brady, open-meteo, Baidu AI Cloud speech services, and ModelScope API-Inference.

## License

The author's own code and documentation are released under **GPL-3.0** (see `LICENSE`). `airplay2/shairport-sync-5.6-hk.patch` modifies Shairport Sync and remains under Shairport Sync's own license (see its `LICENSES` file).
