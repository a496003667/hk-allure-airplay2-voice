**简体中文** | [English](README.en.md)

# Harman Kardon Allure：AirPlay 2 + 本机语音助手改造

给 Harman Kardon Allure（Alexa 版智能音箱）加上 **AirPlay 2**，并用一套**跑在音箱本机**的中文语音助手替换已停用的 Alexa。原厂灯效、实体按键、音量环都接回到了新功能上。

## ⚠️ 风险提示

- 需要音箱的 **root shell**；[首次获取 root](docs/ROOTING.md) 使用原厂 UPnP 入口，后续日常使用[开机自启的 mTLS 维护通道](docs/MAINTENANCE.md)。仓库不包含维护通道的证书和密钥。
- 这台音箱**没有离线救砖手段**。本项目只改 `/vendor` 用户数据区和 `/etc/init.d` 启动脚本，从不写 bootloader、内核或固件分区；所有功能都可以用 `touch …/disable` 关掉。请你也遵守这个边界。
- 只在作者自己的一台 Allure 上验证过（Amlogic 4 核 Cortex‑A5，约 110 MB 内存，Linux 3.10.33，2019-09 固件）。
- 与哈曼卡顿、Amazon、Apple、百度、魔搭无任何关联；相关名称为各自所有者的商标。

## 功能

| 模块 | 说明 |
|---|---|
| **AirPlay 2** | Shairport Sync 5.6 + NQPTP。iPhone、Mac 都能投（Shairport Sync 支持加入"家庭" App，作者未实测）；发送端控制软件音量（最高 -6 dB、默认 -30 dB），经原厂 ABI 桥接入 Harman 两路 DSP |
| **蓝牙 / 低音** | 保留原厂 A2DP 蓝牙，把播放器接回 Harman DSP 的左右两路：左声道驱动 90 mm 低音单元，右声道驱动中高音；手机音量驱动 DSP 前的 33 级软件音量 |
| **防爆音** | AirPlay、蓝牙和语音统一经过 `hk-ap2-audio-hook.sh` 的 `guard`：先验证租约、时钟和静音基线，再开 I2S 数据流、解除原厂功放静音、开喇叭并渐入 |
| **灯效 / 音量环** | 播放时显示原厂播放灯效，音量环跟随发送端音量 |
| **实体按键** | 连着 AirPlay 时，播放/暂停、音量键控制发送端，并与原厂音量双向同步 |
| **语音助手** | 用原厂的 "Alexa" 唤醒词；百度短语音识别 → 魔搭 Qwen 大模型（流式）→ 百度语音合成（音色 4189），播报同样经过 Harman DSP |
| **语音交互** | 说完有提示音；回答后 5 秒内可直接追问；喊 "Alexa" 可打断；边生成边播报 |
| **本地指令** | 暂停 / 继续 / 下一首 / 上一首 / 大声 / 小声，不经大模型 |
| **天气** | open-meteo（免费、无需 key），默认城市按 IP 定位，可问任意城市和明后天 |
| **提醒 / 闹钟 / 定时器** | "十分钟后提醒我关火""明早七点叫我""倒计时五分钟""我有哪些提醒""取消提醒" |
| **AirPlay 与语音共存** | 放歌时唤醒：音乐淡出并暂停、让出声卡，回答完自动续播 |
| **可靠性** | 断网 / 服务出错有离线语音提示；内存紧张时语音助手先退让；守护进程自动拉起；预录有内存上限 |
| **精简** | 开机停用 OTA、Alexa 闹钟、Alexa 多房间（可恢复） |

## 目录

```
airplay2/   Shairport Sync 补丁、配置、AP2 主控 prodctl、喇叭开关 hook、安全替换脚本
  runtime/  AP2 运行时里的安全看门狗、资源守卫、控制脚本
lights/     灯效 / 音量环桥接（hk-ap2-lights.sh + hk-shm-play.c）
keys/       预加载进原厂 mv_ioguard 的按键钩子
voice/      语音助手：hk-voice.c（识别/大模型/合成/天气/提醒/播放队列）、hk-voice.sh（主循环）
debloat/    停用 OTA、Alexa 闹钟、Alexa 多房间
tools/      MQTT 发布、unix socket 请求两个小工具（无 libc 依赖）
init/       /etc/init.d 启动脚本
docs/       技术笔记与踩坑记录
```

## 构建

- **Shairport Sync**：取官方 5.6（作者基于提交 `9118bbc`），打上 `airplay2/shairport-sync-5.6-hk.patch`，用 crosstool-ng 构建的 `arm-unknown-linux-gnueabi`（soft-float，glibc 2.27）交叉编译，依赖 alsa-lib、libsoxr、ffmpeg（libavcodec/swresample）、libsodium、libgcrypt、libplist、libmosquitto、avahi、libconfig、libpopt、OpenSSL。运行时打包成 cramfs 放在 `/vendor/airplay2/`，与原厂的 hard-float glibc 2.19 完全隔离。NQPTP 用上游 1.2.x，未改动。
- **AirPlay DSP 桥**：Shairport 输出固定 S32_LE/48 kHz 原始 PCM；`airplay2/hk-ap2-dsp-bridge.c` 用原厂 hard-float ABI 读取 FIFO，仅在收到音频时打开 `harmandsp`。构建命令见文件头。
- **hk-voice / 按键钩子**：`armv7-linux-gnueabihf-gcc -Os -o hk-voice hk-voice.c -ldl -static-libgcc`。程序用 `dlopen` 调用原厂的 libcurl + OpenSSL 1.1，不链接 libm，符号版本不超过 GLIBC_2.17，可以在原厂 glibc 2.19 上运行。
- **tools/**：用原始系统调用写的静态小程序，任意 ARM 交叉编译器都能编。

## 安装要点

1. 把各模块放到 `/vendor/airplay2`、`/vendor/hk-lights`、`/vendor/hk-keys`、`/vendor/hk-voice`、`/vendor/hk-debloat`，启动脚本放到 `/etc/init.d/`。`hk-dsp-alsa.conf` 和 `hk-ap2-dsp-bridge` 必须位于 `/vendor/airplay2`；蓝牙 DSP 路径、安装和回滚见 [docs/BLUETOOTH.md](docs/BLUETOOTH.md)。
2. 改 `airplay2/shairport-sync.conf` 里的 `airplay_device_id`，填你音箱 wlan0 的 MAC。
3. 语音助手：把 `voice/voice.conf.example` 复制成 `/vendor/hk-voice/voice.conf`（权限 600），填入百度语音（短语音识别 + 语音合成）和魔搭 API-Inference 的 key；运行 `hk-voice check` 检查；`hk-voice mkclips` 生成离线提示音。以后换 key 用 `voice/update-voice-keys.sh`（把 `HK_PUT` / `HK_SHELL` 设成你自己连接音箱的方式）。
4. 设备上的 CA 证书很旧，访问 open-meteo 需要额外的 ISRG Root X1/X2 根证书，合并成 `/vendor/hk-voice/ca.pem`。
5. 已经被 bind mount 的 AirPlay 文件**不要直接覆盖**，先上传为 `<文件>.new`，再用 `airplay2/hk-ap2-replace.sh <文件>…` 替换（先停、卸载、安装、启动，失败自动回滚）。`hk-ap2-prodctl` 会校验各文件的 SHA1，改动后要同步更新里面的哈希。
6. 关掉某个功能：`touch /vendor/<模块>/disable`。

## 致谢

[Shairport Sync](https://github.com/mikebrady/shairport-sync) 与 [NQPTP](https://github.com/mikebrady/nqptp)（Mike Brady），open-meteo，百度智能云语音技术，魔搭社区 API-Inference。

## 许可

本仓库中作者自己的代码与文档以 **GPL-3.0** 发布（见 `LICENSE`）。`airplay2/shairport-sync-5.6-hk.patch` 是对 Shairport Sync 的修改，遵循 Shairport Sync 原有的许可（见其 `LICENSES` 文件）。
