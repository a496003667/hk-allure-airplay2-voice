# 技术笔记与踩坑记录

在一台 Harman Kardon Allure 上实测得到的结论。别的固件版本可能不同，动手前先只读核查。

## 硬件与系统

- Amlogic M8 系列 SoC：4 核 Cortex‑A5，最高 1.536 GHz（原厂固定在 768 MHz，`performance` 调度），约 110 MB 可用内存，128 MB NAND（A/B 两份固件分区，`/vendor` 是约 19 MB 的 UBIFS 用户区）。
- 声卡 `AML-M8AUDIO`，DAC 为 TI PCM512x（i2c 2-004c）。`hw:0,0` 可用 S16/S24/S32、44.1/48/96 kHz，S24_3LE 不可用。
- Wi‑Fi/蓝牙：Broadcom BCM4345 系列（SDIO），驱动只开放 802.11n HT20，2.4/5 GHz 都能用。
- 原厂用户态：hard-float glibc 2.19、BusyBox、libcurl 7.63 + OpenSSL 1.1.0i（在 `/system/workdir/lib`）、`aplay`/`arecord`/`amixer`。
- 核心原厂进程：`rootApp`（总控，会拉起其他进程）、`mv_ioguard`（灯环 / 按键 / MCU 通信）、`a01controller`（播放器状态与音量）、`asr_tts`（麦克风 + Alexa 本地唤醒词）。

## 出声路径（最关键）

- 蓝牙、AirPlay 和语音播报最终都进入原厂 `harmandsp`：它先做左右相加、约 300 Hz 分频和低音/EQ 处理，再写 PCM512x。ALSA `Master` 是原厂播放器的 softvol，对 AirPlay 和语音响度没有作用。
- 只开 `Ext Spk`、解除 I2S 静音还不够，还要解除**原厂功放静音**：用 `LD_PRELOAD` 预加载一个小库再启动原厂 `apple_remote`，它的构造函数会调用原厂的 `Snd_Ctrl airplay to mute 0/1`。
- **无爆音的打开顺序**（和用户逐步对听确认）：
  1. 数据流先跑起来（静音也行）；
  2. 功放仍静音时，数字音量设 0，解除 I2S 静音；
  3. 解除原厂功放静音；
  4. 打开 `Ext Spk`；
  5. 数字音量渐入（DAC 自带斜坡）。
  如果先解除功放静音、再开 I2S，会有一声"啪"。关闭时顺序相反：先数字淡出，再关功放和喇叭，最后停数据流。
- 麦克风录不到这种爆音，只能靠人耳逐步核对。
- 一个独立的看门狗（`hk-ap2-safe-test __watchdog`）只在有"播放租约"时才允许打开喇叭，并限制 Master / 数字音量的上限；没有租约就强制静音。

## AirPlay 2（Shairport Sync 5.6 本地补丁）

补丁见 `airplay2/shairport-sync-5.6-hk.patch`，主要改动：

- 加特性位 22 和 `vv=2`：没有它们 Mac 调不了音量。
- 修正 get_info plist 里多出的 `</dict>`（严格的 plist 解析会得到空 plist）。
- 没有会话时的远程控制回退；事件 socket 接收 2 秒超时。
- Shairport 5.6 是 soft-float 构建，原厂 `harmandsp` 是 hard-float 插件，动态加载器不能把两者直接放进一个进程。因此 Shairport 先输出固定 S32_LE/48 kHz PCM 到 `/tmp/hk-ap2/run/airplay-pcm.fifo`，再由 hard-float `hk-ap2-dsp-bridge` 使用原厂 ALSA 打开 DSP。
- `hk-ap2-dsp-bridge` 只在 FIFO 有数据时打开声卡，约 1 秒没有新数据就关闭；它退出后由 AP2 supervisor 拉起，启动和关闭阶段仍有独立 watchdog 与硬静音保护。
- 44.1 kHz 源会在 Shairport 内重采样到 48 kHz，因为原始 pipe 没有格式变更通知；这样桥进程始终只处理一种确定的格式。
- 另一台设备 pair-setup 时立即让出旧会话，解决 iPhone 和 Mac 互相接管时的爆音。最初 pair-verify 也会触发，但后台做 pair-verify 的设备会把正在播放的发送端踢掉，已去掉。抢占、TEARDOWN、播放中断线都会以 `hk:` 开头写进 shairport.log。
- 告诉发送端的缓冲区（audioBufferSize）从 8 MB 改成 2 MB：发送端会把它填满，原来播放几分钟后 Shairport 会多占约 8 MB 内存。
- `/tmp/hk-ap2/run/release-dac` 存在时，现有 Shairport 补丁会阻止 ALSA 自动重开。改用 pipe 后，桥进程在数据停止约 1 秒后自行关闭 DSP，语音助手主要依据这个关闭状态接管声卡。
- 已知现象：AirPlay 重启后，Mac 不会主动重发音量，手动断开再重连一次就好。

## 按键与灯效

- 按键：把 `libhk-mv-ap2-keys.so` 预加载进原厂 `mv_ioguard`（PATH 里 `/tmp/bin` 排在前面，rootApp 重新拉起时就会走包装脚本）。钩住 `UnixSocketServer_read`：`setPlayerCmd:onepause` 转成 `playpause`；`ext_slave_vol:N` 是绝对值，和跟踪的 MCU 值比较后换算成两步 `volumeup`/`volumedown`，经本地 MQTT 发给 Shairport。只在 7000 端口有 ESTABLISHED 连接时启用。
- 重启 `mv_ioguard` 会让原厂重设编解码器音量，所以只在 AirPlay 看门狗运行时才做。
- 同一个钩子把 `GNOTIFY=MCULED*` 写到 `/tmp/hk-keys/voice.events`，语音助手据此得知唤醒。
- 灯环状态可以向 `/tmp/RequestIOGuard` 发 `GNOTIFY=MCULEDTHINKING/PLAYTTS/LISTENING/IDLE` 来切换。原厂过几秒会自己切回 IDLE，需要定时补发。从 LISTENING 直接切到 IDLE 时灯不灭，要先切 THINKING 再切 IDLE。

## 语音助手

- 唤醒：原厂 `asr_tts` 的 Amazon 本地唤醒词 "Alexa"；没登录 Alexa 时，它也会照常点亮 LISTENING 灯效。
- 录音：给 `/tmp/RequestASRTTS` 发 `recordFileStart:<路径>` / `recordFileStop`，录成 44.1 kHz 双声道 WAV，较响的那个声道是麦克风。录音时写的是 `<路径>.swp`，停止后才改名。
- **坑 1**：唤醒后 `asr_tts` 要过好几秒才响应新的录音请求，问题的开头会被截掉。解决办法是一直预录（放在 tmpfs，每 20 秒轮换），唤醒后从预录里截取。
- **坑 2**：`asr_tts` 在**没有录音时收到 `recordFileStop` 会崩溃**，随后被重新拉起。循环触发会把内存耗尽，低内存杀手会连 AirPlay 和原厂进程一起杀掉。所以只对仍在增长的 `.swp` 发送 stop，并给预录文件设了 2 倍轮换大小的上限。
- 判断开口：预加重后计算 100 ms 帧能量，阈值随底噪调整（约 `floor*1.8+40`）；截取音频时从开口处往回找到不短于 0.2 秒的停顿，避免截掉紧跟在 "Alexa" 后面的话。
- 识别：百度短语音标准版（dev_pid 1537）。试过百度极速版、远场模型 1936（账号无权限）、谱减法降噪、硅基流动 SenseVoice，在这台机器上都没有稳定的提升。
- 大模型：魔搭 API-Inference，OpenAI 兼容接口，`Qwen/Qwen3.8-Flash-Next`，`enable_thinking=false`，流式输出、按句切分后逐句合成。天气和提醒用 tool call；提醒的绝对时间由程序按北京时间计算，不让模型自己换算。
- 合成：百度 TTS，音色 4189 返回 24 kHz（基础音色是 16 kHz），要按 WAV 头里的采样率处理。
- 播放：`hk-voice feedq | aplay -D hk_harman_dsp` 一直输出静音流，回复按句插入，流本身先经过原厂 Harman DSP，喇叭仍只在静音时开关（见"出声路径"）。
- 原厂 CA 证书太旧，没有 ISRG Root X1/X2，访问 open-meteo 要用自带的 CA 包。
- 音箱没有可路由的 IPv6，curl 强制只走 IPv4。
- 语音助手和 AirPlay 交接喇叭：两边都在 `/tmp/hk-ap2/run/speaker.lock` 下判断归属。语音占着喇叭时（`voice-owns`），AirPlay hook 的 start 推迟、stop 忽略；回答结束后由语音助手按无爆音顺序把喇叭交还给 AirPlay。没有这个机制时，两边会在同一秒开关功放，导致爆音，AirPlay 还会没声音。
- 直接 kill 语音循环可能留下静音流占着声卡，导致 AirPlay 报 "hw:0,0 is busy"；要用 `hk-voice.sh stop/start`，它们会清理残留。

## 内存

- 正常空闲约 18–30 MB（按 MemFree 算，页缓存多时会显得偏低）。
- 语音循环 `oom_score_adj=15`，AirPlay 是 11，原厂进程是 0：内存紧张时先杀语音助手。
- 低内存杀手（lowmemorykiller）只在空闲内存和文件缓存**同时**低于 6 MB 时才会杀 `oom_score_adj` 为 15 的语音循环。语音监护进程每 5 秒检查一次，两者之和低于 24 MB 时，把内存快照写进 `/tmp/hk-voice/mem.log`。
- 预录每 10 秒轮换一次，内存盘里最多约 1.7 MB。
