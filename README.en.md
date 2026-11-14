[简体中文](README.md) | **English**

# Harman Kardon Allure: AirPlay 2 + on-device voice assistant

Adds **AirPlay 2** to the Harman Kardon Allure (the Alexa smart speaker) and replaces the retired Alexa with a Chinese voice assistant that **runs on the speaker itself**. The stock light ring, physical buttons and volume ring are wired back up to the new features.

AirPlay, the stock Bluetooth player and voice replies use the Harman DSP. The project retains the stock wake word and physical controls, and provides a persistent mTLS maintenance channel, audio handoff safeguards and rollback procedures.

The 2026-11-14 audit repairs are being validated offline on `fix/audit-20261114`; **nothing from this branch has been deployed to the speaker**. Earlier AirPlay/Bluetooth playback does not establish acceptance of these changes. Audible pops, source switching, voice handoff, bass hardware and sustained operation still need device tests. See the bilingual [repair status](docs/AUDIT-REPAIRS-2026-11-14.md) and [repair plan](docs/REPAIR-PLAN-2026-11-14.md).

## ⚠️ Read this first

- You need a **root shell** on the speaker. See [initial root access](docs/ROOTING.md) and the [persistent mTLS maintenance channel](docs/MAINTENANCE.md) (Chinese). The latter starts at boot and is used for routine maintenance. Certificates and keys are not included.
- The speaker has **no offline recovery path**. This project only touches the `/vendor` user-data partition and `/etc/init.d` start scripts; it never writes the bootloader, kernel or firmware partitions, with module-specific disable semantics described in the [build/update guide](docs/BUILD-AND-UPDATE.md). Disabling AirPlay retains shared protection needed by Bluetooth and voice. Please keep to the same boundary.
- Tested on one Allure only (Amlogic quad-core Cortex‑A5, ~110 MB RAM, Linux 3.10.33, firmware from 2019-09).
- Not affiliated with Harman Kardon, Amazon, Apple, Baidu or ModelScope; all names are trademarks of their respective owners.
- The voice assistant speaks and understands **Mandarin Chinese** (Baidu ASR/TTS); the prompts are in Chinese.

## Features

| Module | What it does |
|---|---|
| **AirPlay 2** | Shairport Sync 5.6 + NQPTP, routed through the stock ABI bridge into the Harman DSP. This branch adds the tested 24 dB flat range: software attenuation -30 to -6 dB; default protocol input -30 maps to -30 dB, subject to sender control |
| **Bluetooth / bass** | Keeps the stock A2DP player, routes it through Harman DSP and the stock 33-step software volume; actual low-frequency driver operation still needs listening tests |
| **Pop prevention** | Starts I2S data, unmutes the stock amplifier, switches the speaker on and fades in; the latest sequence still needs listening tests across source changes |
| **Lights / volume ring** | Shows the stock "playing" animation during playback; the volume ring follows the sender's volume |
| **Buttons** | While an AirPlay sender is connected, play/pause and volume keys control the sender, with volume kept in sync both ways |
| **Voice assistant** | Uses the stock "Alexa" wake word; Baidu short-speech ASR → ModelScope Qwen LLM (streamed) → Baidu TTS (voice 4189), with spoken replies routed through the same Harman DSP |
| **Conversation** | A chime when you finish speaking; follow-up questions without the wake word for ~5 s; say "Alexa" to interrupt; speaks while the answer is still being generated |
| **Bluetooth + voice** | By default the same PCM ducks music to silence and mixes the reply; phone track position may continue. An explicit pause command really pauses and cancels automatic resume |
| **Local commands** | Pause / resume / next / previous / louder / quieter, handled without the LLM |
| **Weather** | open-meteo (free, no key); default city by IP geolocation; any city, today to the day after tomorrow |
| **Reminders / alarms / timers** | e.g. "十分钟后提醒我关火" (remind me in ten minutes), "明早七点叫我" (wake me at 7), "倒计时五分钟" (5-minute timer), list and cancel; many phrasings are recognised |
| **AirPlay + voice** | Waking it during playback fades out and pauses the music, frees the DAC, answers, then resumes |
| **Reliability** | Stored spoken prompts for network and service errors; optional processes keep positive OOM scores; AP2 reclamation preserves a valid Bluetooth lease; pre-roll and logs are bounded; kernel reclaim order is not guaranteed |
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

Use `tools/build-local.sh`, `tools/sync-pins.py`, and `tools/release.py` as described in the [bilingual build/update guide](docs/BUILD-AND-UPDATE.md). Sources now include the DSP bridge, Bluetooth redirect, IPC tool, keys, NSS/capability shims and stock-amplifier helpers. `docs/runtime-inputs.lock.json` pins the third-party runtime inputs; obtain proprietary stock ALSA libraries from your own firmware.

AirPlay uses the isolated soft-float/glibc 2.27 runtime; DSP/Bluetooth/voice use the stock hard-float ABI. Validate ELF ABI and GLIBC symbol versions after building. An unverified 4.3.6 fallback is no longer accepted as equivalent to the 5.6 path.

## Installing (outline)

1. Put the modules under `/vendor/airplay2`, `/vendor/hk-lights`, `/vendor/hk-keys`, `/vendor/hk-voice` and `/vendor/hk-debloat`, and the start scripts in `/etc/init.d/`. `hk-dsp-alsa.conf` and `hk-ap2-dsp-bridge` must be under `/vendor/airplay2`. The Bluetooth DSP route, installation and rollback are documented in [docs/BLUETOOTH.md](docs/BLUETOOTH.md).
2. Use `tools/release.py package --device-config <private-config>` to preserve the existing speaker ID and generate its `ap2-device.env` pin; `--device-mac <MAC>` can generate a new device template. Keep both outside the public repository.
3. Voice assistant: copy `voice/voice.conf.example` to `/vendor/hk-voice/voice.conf` (mode 600) and fill in your Baidu speech keys (short-speech ASR + TTS) and ModelScope API-Inference key. Run `hk-voice check` to test them and `hk-voice mkclips` to create the offline prompts. `voice/update-voice-keys.sh` rotates keys later (set `HK_PUT` / `HK_SHELL` to your own way of reaching the speaker).
4. The speaker's CA bundle is old: open-meteo needs the ISRG Root X1/X2 roots added, combined into `/vendor/hk-voice/ca.pem`.
5. Update from a complete manifest: validate the release, upload it, stage `.new` siblings with `tools/stage-update.sh`, then run `hk-ap2-replace.sh --manifest <release>/update.manifest`. It validates candidates, space and complete backups, holds audio muted, stops/unmounts/replaces the set, verifies the exact target and reloads affected dependent modules. Update failure returns nonzero even after successful rollback. Recovery failure stays muted; success also retains the maintenance hold for user-present listening.
6. `/vendor/airplay2/disable` disables AirPlay reception while retaining shared Bluetooth/voice protection. Keys, lights and voice use their own persistent disable files. First installation and recovery differ from updating an existing speaker; follow the build/update guide.

## Credits

[Shairport Sync](https://github.com/mikebrady/shairport-sync) and [NQPTP](https://github.com/mikebrady/nqptp) by Mike Brady, open-meteo, Baidu AI Cloud speech services, and ModelScope API-Inference.

## License

The author's own code and documentation are released under **GPL-3.0** (see `LICENSE`). `airplay2/shairport-sync-5.6-hk.patch` modifies Shairport Sync and remains under Shairport Sync's own license (see its `LICENSES` file).
