# 构建、发布与受控更新 / Build, package and update

本分支为离线候选，实机部署与听测待完成。本说明区分已有设备更新与首次安装；不会自动连接设备。

## 构建输入

| 输入 | 约束 |
|---|---|
| Shairport Sync | 上游提交 `9118bbce71f287df2dfdfb5bed3911d92a9fdb0f`，应用本仓库补丁；新增范围仅允许 `flat + 24 dB` 或原有合法范围 |
| soft-float 工具链 | `arm-unknown-linux-gnueabi`，本轮使用 GCC 8.5.0、glibc 2.27、Linux 3.10.108 头文件 |
| hard-float 工具链 | `armv7-linux-gnueabihf`，本轮 GCC 15.2.0；产物需检查最大 GLIBC 符号不超过设备 2.19 |
| 原厂 ALSA | 从自己固件提取的 hard-float `libasound.so.2`，仅供本地链接，不随仓库分发 |
| 隔离运行时依赖 | `runtime-inputs.lock.json` 固定第三方 ELF 输入的 SHA-256；需要自行准备对应依赖树 |
| 服务端配置 | 设备 ID、API key、证书和私钥均为私有输入，不进入公开构建输入或 Git |

本轮 soft-float 依赖前缀使用 ALSA 1.2.11、Avahi 0.8、FFmpeg 6.1.4、soxr 0.1.3、libsodium 1.0.20、libgcrypt 1.11.2、libgpg-error 1.58、libplist 2.7.0、Mosquitto 2.0.22、libconfig 1.7.3、libdaemon 0.14、popt 1.19、OpenSSL 3.5.5、util-linux 2.40.2。锁定依赖树与版本信息不是第三方源码构建器，也不意味着可用任意同名系统库替换它们。

在主机上构建自己的代码及 Shairport（示例路径均需替换）：

```sh
export HK_STOCK_ASOUND=/absolute/path/to/stock/libasound.so.2
export HK_SOFT_DEPS=/absolute/path/to/soft-float-prefix
export HK_SPS_SOURCE=/absolute/path/to/shairport-checkout
export HK_SOFT_PREFIX=/absolute/path/to/toolchain/bin/arm-unknown-linux-gnueabi-
sh tools/build-local.sh
python3 tools/sync-pins.py
```

构建器不重置已有源码目录；只接受锁定提交且可正向或反向校验的补丁。自己的小型 ARM 工具有明确 `_start`，raw IPC 没有动态依赖；语音 C 客户端共享同一 IPC 期限实现，避免新 glibc 的 `fcntl@2.28` 依赖。NSS、能力 shim、功放 mute/unmute 辅助库现在有源码；5.6 使用自己的稳定设备身份，不再采用旧 4.3.6 shim 的固定设备 UUID。

构建后检查 ELF32/ARM、soft/hard-float ABI、动态依赖和符号版本。GLIBC 兼容不是“编译成功”的同义词。源码检查与单元测试不能验证原厂 DSP 或功放的实际声音。

## 现有设备的最小更新包

现有设备继续使用已校验的基础 cramfs、NQPTP 与 loopback Mosquitto；本次最小更新不替换基础镜像，也不改 root/维护通道。先在私有目录保存设备的当前 Shairport 配置，再生成候选：

```sh
python3 tools/release.py package \
  --output build/private-candidate \
  --device-config /absolute/path/to/private/shairport-sync.conf
python3 tools/release.py verify build/private-candidate
```

已有配置只改变候选要求的三个音量参数，保留真实 ID；必须保持 `pipe` 后端和 `flat` 曲线。设备专用 `ap2-device.env` pin 权限为 600。新设备可使用 `--device-mac 02:00:00:00:00:01` 生成模板；示例地址不是作者设备的地址。

发布器只从显式文件列表打包，输出 SHA-256/权限清单 `update.manifest`、`release.json`，并生成一致的控制器及蓝牙包装器 pin。输出目录必须为空；不会打包 `voice.conf`、CA 私钥、NVRAM 或整个工作目录。发布目录包含设备配置时不得公开上传。

## 首次安装与基础运行时

首次安装还需要基础 cramfs、与 5.6 配套的 NQPTP、loopback Mosquitto，以及已有文档中的 root/维护和启动管理；最小更新包不能代替这一步。

Linux 上可从锁定依赖树重建基础镜像：

```sh
python3 tools/make-runtime.py \
  --dependencies /absolute/path/to/runtime-opt \
  --binaries /absolute/path/to/build/bin \
  --output /absolute/path/to/hk-ap2-runtime.cramfs
```

该工具仅复制锁定的库及 D-Bus/Avahi，加入从本仓库构建的脚本、Shairport、NSS 和功放辅助库、通用配置及 bind mount 目标。它在临时目录规范所有者，再用 `mkfs.cramfs -N little` 打包，不修改原输入树。需要在隔离 Linux 构建环境运行；修改依赖必须显式更新锁定输入并重新验收。

新镜像可用 `release.py package --runtime-image … --nqptp … --mosquitto …` 加入完整私有包，控制器同时更新镜像与 NQPTP pin。当前机器 `/vendor` 只有约 19 MB，必须先核对空间；带镜像的整批备份/双份安装可能不适合已有设备，更新器会拒绝空间不足。不要为绕过空间检查删除唯一回滚副本。首次安装尚无本轮新设备验收，不能据打包夹具通过宣称一键刷机完成。

## 设备更新事务

以下均是回到内网、核查设备空闲后才执行的设备侧步骤，本轮离线开发不执行：

1. 读取当前播放、内存、版本、hold/trip 和维护状态；用户暂停音源。私有备份保存到电脑，核对 SHA-256 和权限。
2. 上传经过验证的候选目录到有足够空间的临时位置。用 `tools/stage-update.sh <候选目录>` 生成目标的 `.new` 文件，它不替换运行文件。
3. 首次使用修复更新器时，从候选目录直接运行经过 SHA-256 校验的 `vendor/airplay2/hk-ap2-replace.sh`，传入 `--manifest <候选目录>/update.manifest`，不能调用设备上原来有缺陷的旧更新器。
4. 更新器取得锁，验证全批候选、祖先路径/符号链接、脚本语法、权限及回滚空间；先完成全部备份，再进入维护 hold、静音、停服和卸载。
5. 安装后核对实际运行的 5.6 绑定、二进制和设备配置 pin、端口、DSP/蓝牙桥、保护进程；不是仅看端口 7000。未验收的 4.3.6 不能冒充成功。
6. 若包涉及语音、灯效、按键或蓝牙库，由事务内校验并保存的 `hk-audio-dependents.sh` 重载相应组件。原厂播放器只在静音、独立 watchdog 已恢复时受控重载；不重启 `rootApp` 或维护通道。原厂播放器未成功重载即更新失败。
7. 成功保留维护 hold 等待用户在场试听；不用定时静音打断后续长测。不能直接删除 hold 而不检查 latched trip 和正常关闭顺序。

失败会恢复整批文件；即使回退成功，原更新命令仍返回非零。回退失败保留 hold 与 `/vendor/airplay2/.updates/pending`，日志给出：

```sh
hk-ap2-replace.sh --recover /vendor/airplay2/.updates/TRANSACTION
```

pending 未完成时普通启动保持静音并拒绝开始。保存事务内的恢复 manifest、备份和依赖重载器；恢复完成前不清理它们。

## 禁用语义

| 模块 | 持久禁用文件 / 命令 | 运行时与下次启动 |
|---|---|---|
| AirPlay 接收 | `/vendor/airplay2/disable` 或 `hk-ap2-prodctl disable` | 命令立即请求停止 AP2，单独创建文件由监督循环检查；蓝牙/语音所需的共享保护继续运行，下次启动同样只开共享部分 |
| 全部共享音频维护停服 | `hk-ap2-prodctl stop` | 正常淡出、静音后停共享进程；不是持久 disable，更新使用此入口 |
| 语音 | `/vendor/hk-voice/disable` | 循环/进行中的轮次检查并退出，监督器不再重启，下次启动跳过 |
| 灯效桥 | `/vendor/hk-lights/disable` | 运行循环释放自己取得的显示状态并停止转发，下次启动跳过；保留原厂灯控服务 |
| 按键转发 | `/vendor/hk-keys/disable` | 每次转发检查，改走原厂；下次不装 wrapper。已经加载的库不会立刻从进程卸载 |
| 蓝牙 DSP 包装器 | `/vendor/airplay2/disable-bt-dsp` | 下次启动跳过；当前进程需在静音下受控重载才能换路径 |
| 精简服务 | `/vendor/hk-debloat/disable` | 下次跳过精简；已停的原厂服务按其恢复说明处理 |

旧 `/tmp/hk-keys/disable`、灯效临时 flag 保留兼容。`ap2-56/disable` 不再表示可以自动切到等价 4.3.6。不要把通用 `touch …/disable` 当作所有模块相同的停止/卸载操作。

## 验收与实际限制

完整测试运行 `python3 -m unittest discover -s tests -v`；另有 BusyBox、ARM raw IPC、ELF/GLIBC、哈希和打包检查。Shairport 的 `--version` 可在 ARM 模拟器运行；`-X` 读取配置后在当前模拟环境崩溃，旧实机基线二进制也有同样表现，不能把配置打印成功当作常驻启动通过。

软件音量统一的是有效状态与控制来源：AirPlay 的 flat 映射、蓝牙的 33 级 Master、语音的源相关增益仍各在自己的必要位置。蓝牙混音已处于 Master 之前，不能再按 Master 重复衰减。机身按键保留对应来源控制；已经合成的语音片段不会重合成，正在播报时的实时响度仍待听测。DSP 汇流不等于已找到一个可安全调用的 DSP 主音量 API。

## English summary

This branch is an offline candidate. `build-local.sh` cross-builds project code and the pinned Shairport patch; `sync-pins.py` updates public artifact pins. `release.py` packages an explicit allowlist, checks ELF ABI and shell syntax, generates hashes/modes, preserves a private device configuration and generates its private pin. No tool connects to a speaker or reads credentials.

Existing-device updates reuse the verified base runtime and update only the candidate overrides. A fresh installation additionally needs the locked third-party dependency tree, runtime image, NQPTP, broker and prior root/boot setup. `make-runtime.py` creates a little-endian cramfs from explicit inputs; it is not a third-party source builder or proof of a tested fresh installation. The small vendor partition can prevent a full image transaction; keep the rollback-space check.

On the device, stage `.new` siblings, then run the verified candidate updater with `--manifest`. It backs up the full set, holds output muted, closes audio before stopping clocks, unmounts, installs, verifies the exact 5.6/configuration/protection stack, and reloads affected dependents. It never restarts rootApp or the maintenance channel. Failure returns nonzero; failed recovery retains mute and a persistent journal. Successful installation also stays held until user-present verification.

Disabling AirPlay retains shared Bluetooth/voice protection. Other modules have distinct persistent-disable and reload behavior as listed above. Automated and emulated tests do not establish audible pop prevention, bass output, physical-button loudness or sustained playback acceptance. The simulator's Shairport `-X` crash is shared by the old baseline; persistent startup remains an on-device test.
