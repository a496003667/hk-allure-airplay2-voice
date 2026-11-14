**简体中文** | [English](README.en.md)

# Harman Kardon Allure：AirPlay 2 + 本机语音助手改造

给 Harman Kardon Allure（Alexa 版智能音箱）加上 **AirPlay 2**，并用一套**跑在音箱本机**的中文语音助手替换已停用的 Alexa。原厂灯效、实体按键、音量环都接回到了新功能上。

AirPlay、原厂蓝牙和语音播报接入 Harman DSP；音箱保留原厂唤醒词和实体控制，并通过开机自启的 mTLS 通道维护。项目提供音频交接保护与回滚方案，适用于愿意自行维护设备的用户。

2026-11-14 审计修复已在 `fix/audit-20261114` 分支完成 **111 项 Linux/BusyBox 回归，并部署到作者设备通过静音核验**：实际运行 Shairport 5.6、24 dB flat 配置、DSP、蓝牙桥和独立保护；语音、灯效、按键已重载。设备保留维护静音等待本轮试听；爆音、来源切换、完整语音交接、低频单元和长测仍待验收。详见[实机记录](docs/DEVICE-VALIDATION-2026-11-14.md)、[修复状态](docs/AUDIT-REPAIRS-2026-11-14.md)与[修复方案](docs/REPAIR-PLAN-2026-11-14.md)。

## ⚠️ 风险提示

- 需要音箱的 **root shell**；[首次获取 root](docs/ROOTING.md) 使用原厂 UPnP 入口，后续日常使用[开机自启的 mTLS 维护通道](docs/MAINTENANCE.md)。仓库不包含维护通道的证书和密钥。
- 这台音箱**没有离线救砖手段**。本项目只改 `/vendor` 用户数据区和 `/etc/init.d` 启动脚本，从不写 bootloader、内核或固件分区；各模块的禁用语义见[构建与交付说明](docs/BUILD-AND-UPDATE.md)；关闭 AirPlay 会保留蓝牙/语音需要的共享保护。请你也遵守这个边界。
- 只在作者自己的一台 Allure 上验证过（Amlogic 4 核 Cortex‑A5，约 110 MB 内存，Linux 3.10.33，2019-09 固件）。
- 与哈曼卡顿、Amazon、Apple、百度、魔搭无任何关联；相关名称为各自所有者的商标。

## 功能

| 模块 | 说明 |
|---|---|
| **AirPlay 2** | Shairport Sync 5.6 + NQPTP，音频经原厂 ABI 桥进入 Harman DSP。本分支新增受测的 24 dB flat 范围：软件衰减 -30 至 -6 dB；默认协议输入 -30 映射到 -30 dB，发送端可覆盖 |
| **蓝牙 / 低音** | 保留原厂 A2DP 蓝牙，播放器进入 Harman DSP 分频，手机音量驱动 DSP 前的 33 级软件音量；低频单元是否实际工作仍需听测 |
| **防爆音** | AirPlay、蓝牙和语音统一经过 `hk-ap2-audio-hook.sh` 的 `guard`：先验证租约、时钟和静音基线，再开 I2S 数据流、解除原厂功放静音、开喇叭并渐入 |
| **灯效 / 音量环** | 播放时显示原厂播放灯效，音量环跟随发送端音量 |
| **实体按键** | 连着 AirPlay 时，播放/暂停、音量键控制发送端，并与原厂音量双向同步 |
| **语音助手** | 用原厂的 "Alexa" 唤醒词；百度短语音识别 → 魔搭 Qwen 大模型（流式）→ 百度语音合成（音色 4189），播报同样经过 Harman DSP |
| **语音交互** | 说完有提示音；回答后 5 秒内可直接追问；喊 "Alexa" 可打断；边生成边播报 |
| **蓝牙与语音** | 默认同一 PCM 内把背景音乐压至静音后混入回复，手机进度可能继续；明确“暂停”会真实执行并取消自动续播 |
| **本地指令** | 暂停 / 继续 / 下一首 / 上一首 / 大声 / 小声，不经大模型 |
| **天气** | open-meteo（免费、无需 key），默认城市按 IP 定位，可问任意城市和明后天 |
| **提醒 / 闹钟 / 定时器** | "十分钟后提醒我关火""明早七点叫我""倒计时五分钟""我有哪些提醒""取消提醒" |
| **AirPlay 与语音共存** | 放歌时唤醒：音乐淡出并暂停、让出声卡，回答完自动续播 |
| **可靠性** | 断网 / 服务出错有离线语音提示；可选进程保持正数 OOM 权重；AP2 回收保留有效蓝牙租约；预录及日志限制占用；不保证内核回收顺序 |
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

使用[构建与交付说明](docs/BUILD-AND-UPDATE.md)中的 `tools/build-local.sh`、`tools/sync-pins.py` 与 `tools/release.py`。源码包含 DSP 桥、蓝牙重定向、IPC、按键、NSS/能力 shim 和功放辅助库的构建输入。第三方运行时依赖由 `docs/runtime-inputs.lock.json` 固定；原厂专有 ALSA 库须从自己的固件提取。

AirPlay 使用 soft-float/glibc 2.27 隔离运行时；DSP/蓝牙/语音使用原厂 hard-float ABI。构建后检查 GLIBC 符号版本和两种 ABI，不能只验证编译器退出码。4.3.6 不再作为未经等价验收的自动回退版本。

## 安装要点

1. 把各模块放到 `/vendor/airplay2`、`/vendor/hk-lights`、`/vendor/hk-keys`、`/vendor/hk-voice`、`/vendor/hk-debloat`，启动脚本放到 `/etc/init.d/`。`hk-dsp-alsa.conf` 和 `hk-ap2-dsp-bridge` 必须位于 `/vendor/airplay2`；蓝牙 DSP 路径、安装和回滚见 [docs/BLUETOOTH.md](docs/BLUETOOTH.md)。
2. 用 `tools/release.py package --device-config <私有配置>` 保留已有设备 ID 并生成 `ap2-device.env` pin；新设备也可用 `--device-mac <MAC>` 生成。设备配置和 pin 只放在私有发布目录。
3. 语音助手：把 `voice/voice.conf.example` 复制成 `/vendor/hk-voice/voice.conf`（权限 600），填入百度语音（短语音识别 + 语音合成）和魔搭 API-Inference 的 key；运行 `hk-voice check` 检查；`hk-voice mkclips` 生成离线提示音。以后换 key 用 `voice/update-voice-keys.sh`（把 `HK_PUT` / `HK_SHELL` 设成你自己连接音箱的方式）。
4. 设备上的 CA 证书很旧，访问 open-meteo 需要额外的 ISRG Root X1/X2 根证书，合并成 `/vendor/hk-voice/ca.pem`。
5. 更新使用完整发布清单：先验证候选目录，上传后用 `tools/stage-update.sh` 放入 `.new`，再运行 `hk-ap2-replace.sh --manifest <发布目录>/update.manifest`。更新器检查候选、空间和全批备份，维护静音下停服/卸载/替换，验证精确版本；失败全批回滚，失败状态返回非零。依赖模块受控重载后再验收，成功也保留维护 hold，等待用户试听。
6. `touch /vendor/airplay2/disable` 关闭 AirPlay 接收，保留共享蓝牙/语音保护；keys/lights/voice 的持久 disable 分别作用于自己的模块。首次安装和恢复步骤与现有设备更新不同，详见交付说明。

## 致谢

[Shairport Sync](https://github.com/mikebrady/shairport-sync) 与 [NQPTP](https://github.com/mikebrady/nqptp)（Mike Brady），open-meteo，百度智能云语音技术，魔搭社区 API-Inference。

## 许可

本仓库中作者自己的代码与文档以 **GPL-3.0** 发布（见 `LICENSE`）。`airplay2/shairport-sync-5.6-hk.patch` 是对 Shairport Sync 的修改，遵循 Shairport Sync 原有的许可（见其 `LICENSES` 文件）。
