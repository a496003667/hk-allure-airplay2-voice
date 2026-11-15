# 蓝牙输出、音量与低音

## 声音为什么必须经过 Harman DSP

Allure 的 PCM5121 两个输出通道不是一个普通立体声对：

- 左声道驱动机身的 90 mm 低音单元；
- 右声道驱动三个 38 mm 中高音单元；
- 原厂 `libasound_module_pcm_harmandsp.so` 先把左右相加成单声道，再用约
  300 Hz 的分频、65 Hz 低音 EQ 和限幅器生成这两路信号。

把普通立体声直接送到 `hw:0,0` 会绕过整套分频和调音。功放板只有一路用于
90 mm 低音单元的声道和一路用于三个 38 mm 单元的声道；MSP430 通过串口控制
整机功放静音/使能，没有单独的“低音炮启动”命令。中高音能正常播放，说明共享的
总功放使能已经打开。拆机资料和原厂控制协议见本文末尾的参考链接。

原厂固件同时带了两个 ALSA 模板：

- `lib/alsa/asoundrc` 是普通 `softvol -> dmixer -> hw`，没有分频；
- `lib/alsa/asoundrc_harman` 才是 `softvol -> harmandsp -> dmixer -> hw`。

此前核查的本机原厂播放器从字面设备名 `default` 打开蓝牙 PCM，而启动脚本没有选择
`asoundrc_harman`。因此蓝牙可以出声，却绕过了 Harman 的左右声道分频和低频调音。
这能说明软件分频路径存在差异，不能单凭它排除低音功放、接线或单元故障。当前未找到可独立启用低音单元的已验证控制接口。蓝牙路径应为：

```text
a01controller -> Master softvol -> hkbtpark -> harmandsp -> hw:0,0
```

本次离线修复分支的 AirPlay 使用 Shairport 软件音量与新增的 24 dB flat 范围，
输出衰减为 `-30.0` 至 `-6.0 dB`；默认协议输入 `-30.0` 对应 `-30.0 dB` 输出衰减，发送端可以覆盖默认值；
这些数值不是声压级。因为 Shairport
5.6 是 soft-float、原厂 DSP 是 hard-float，它先把固定格式 PCM 交给
`hk-ap2-dsp-bridge`，再由桥进入同一个 `harmandsp`；蓝牙仍使用自己的调用点配置。

## 构建重定向库

重定向库必须使用与原厂进程一致的 32 位 ARM hard-float ABI。例如：

```sh
armv7-linux-gnueabihf-gcc -Os -fPIC -shared \
  -o airplay2/hk-bt-open-redirect.so \
  airplay2/hk-bt-open-redirect.c
```

使用 `tools/build-local.sh` 构建并由 `tools/sync-pins.py` 同步公版 pin，或由
`tools/release.py` 为候选目录生成一致的 pin；不要只手动改一处校验值。

## 只改蓝牙调用点

`a01controller` 的版本必须与 `hk-bt-controller-wrapper.sh` 中的 SHA-256 相符。
启动包装器只在以下条件全部满足时预加载 `hk-bt-open-redirect.so`：

1. 原厂 `a01controller` 的 SHA-256 正确；
2. 重定向库和私有 ALSA 配置的 SHA-256 正确；
3. 进程环境里没有已有的 `LD_PRELOAD`。

重定向库同时检查 ALSA 设备名 `default`、播放方向，以及原厂蓝牙调用点的返回地址
`0x7e34c`。其他 PCM open 继续使用原厂路径。条件不满足或文件损坏时，包装器直接
执行原厂程序。

## 音量和播放交接

- 蓝牙不再使用旧测试里的 `30/100` 上限；`0..100` 会映射到原厂 33 级
  `Master` 软件音量。任何非零百分比至少使用最低一级，`0` 才是静音。
- 音量作用在 Harman DSP 之前，因此限幅器看到的正是送给功放的电平。
- PCM5121 的 `Digital Playback Volume` 是反向刻度：`0` 为静音，`207` 为
  `0 dB`。蓝牙把数字级固定为 `0 dB`，模拟级保留原厂 `-6 dB`；手机音量只控制
  DSP 前的 `Master`。不要使用 `255`，它属于可选的 `+24 dB` 数字增益区间。
- 蓝牙独占逻辑写在桥接器和 watchdog 中。播放时建立租约，暂停/停止时释放；
  AirPlay 要接管时会先等蓝牙释放。
- Bluetooth watchdog 在桥接器退出、心跳过期或来源停止时立即恢复硬静音。
- 打开顺序为 DAC 静音、解除 I2S 静音并等待数据时钟、解除一次原厂功放静音、
  开 `Ext Spk`、等功放稳定后数字渐入。整个开场不再反复切换原厂功放静音；
  关闭先数字淡出，再依次关闭功放和 I2S，避免暂停/恢复时的“啪”声。
- 所有来源共用 `hk-ap2-audio-hook.sh` 的 `guard`/`start`/`stop`：调用扬声器前
  先验证租约、watchdog、hold/trip 和 PCM 时钟，再确认功放与数字静音基线。
  蓝牙语音混音恢复数字增益前也会先淡出到 0 并调用 `guard active`；检查失败
  就保持静音，不再直接切换功放或渐入。

## 安装

在已经取得 root 维护通道、且手机蓝牙暂停、音箱保持三层硬静音时：

1. 将 `hk-bt-controller-wrapper.sh`、`hk-bt-open-redirect.so`、
   `libasound_module_pcm_hkbtpark.so` 和 `hk-bt-alsa.conf` 放到 `/vendor/airplay2/`。
2. 将 `hk-bt-boot` 放到 `/vendor/airplay2/` 并设为可执行。
3. 用 `init/S05linkplay.sh` 替换 `/etc/init.d/S05linkplay.sh`。原脚本是符号
   链接；替换前要留下回滚副本。
4. 下次启动时，`S05linkplay.sh` 在挂载 `/vendor` 后、启动 `rootApp` 前调用
   `hk-bt-boot`，把经过校验的包装器安装到 `/tmp/bin/a01controller`。

检查：

```sh
ps | grep '[a]01controller'
pid=$(ps | awk '$3 ~ /a01controller/ {print $1; exit}')
tr '\000' '\n' </proc/$pid/environ | grep -E '^(ALSA_CONFIG_PATH|LD_PRELOAD)='
grep hk-bt /proc/$pid/maps
```

## 回滚

`touch /vendor/airplay2/disable-bt-dsp` 会让下一次启动跳过包装器。已经运行的
播放器只有在暂停、硬静音并受控重启后才会恢复原路径；不要为了回滚直接强杀
`rootApp` 或其他原厂核心进程。

设备侧还应保留：

- `/etc/init.d/S05linkplay.sh` 替换前的备份；
- `/vendor/airplay2/hk-bt-alsa.conf` 的旧版本；
- 原厂 `a01controller` 与重定向库的 SHA-256 记录。

## 2026-11-14 离线修复

- 播放租约记录来源、owner PID、进程启动时间、轮次和阶段。独立语音先预留租约再检查时钟；旧轮 stop 不能撤销新轮租约。
- AP2 的资源回收保留有效蓝牙/语音租约，恢复前先维护共享监督。常规停服先淡出/物理静音，再停 DSP/PCM；维护 hold 阻止新开声，同时保留现有时钟直到租约释放。
- 默认同 PCM 语音混音会把背景音乐平滑压至静音，手机进度可能继续；“暂停”命令是真实的原厂暂停，取消本轮自动续播。下一首/上一首派发至蓝牙，而非 AirPlay MQTT。
- `HK_VOICE_BT_MODE=pause` 是待实机验证的独立播报路径。若原厂暂停后仍占声卡，记录原因并保持静音；不强杀原厂服务争抢声卡。
- 机身按键在蓝牙时保留原厂路径；AirPlay 接管后走发送端，AirPlay 语音交接期间仍可调发送端音量。已生成的播报片段不会因为滑块改变而重新合成，实时响度效果仍需听测。
- 安装必须使用发布清单与受控依赖重载；更换 `.so` 文件不会自动改变已经映射它的原厂播放器。见 [BUILD-AND-UPDATE.md](BUILD-AND-UPDATE.md)。

本分支已部署到作者设备；2026-11-15 的实际验收和随后暴露的问题见下文，历史记录不能替代当前版本的实机验收。

## 验证状态

2026-11-15 用户确认蓝牙启动出声、手机音量归零与恢复、暂停约三秒后恢复均正常，
无爆音或音量突增；蓝牙音乐中唤醒语音助手，回复清楚，音乐正常恢复，无爆音。
这轮先修复了新租约在数字静音阶段被旧模拟增益误触发保护的竞态。

随后切换到测试页面时，原厂播放器保留了进入 `XRUN` 的 PCM，watchdog 将其误判为
持续故障并锁存静音；连 500 Hz 参考音也无法听到，因此这次结果不能判断低音单元。
新的 shell 修正区分可信蓝牙暂停与真正的 owner/心跳故障：暂停立即静音并释放旧租约，
等待手机手动播放建立新的 `RUNNING` PCM，再通过完整保护序列打开输出。
暂停后即使原厂状态仍为 `play`，也不会据此发送自动续播命令。
错误 owner、失效心跳、未知 PCM 状态和维护静音仍保留故障保护。

最新 **125 项 Linux/BusyBox 测试通过**，覆盖 watchdog 与 bridge 先后发现 XRUN、
新 PCM 恢复、错误 owner 锁存以及断开重连。修正已完成受控部署、事务提交和精确版本核验，
用户随后确认参考音两次都能听到、无啪声；设备核对播放结束后的可信 XRUN 正常释放租约，未锁存故障。
随后用户确认 500、200、120、80、60 Hz 各段均能听到，低频有振动；播放结束未触发故障锁存。
用户随后确认熟悉音乐的低频明显改善、听感正常。该听测确认低频输出存在，不是对具体单元或功放的独立测量；单元完整健康、真实来源切换及长测仍待验证。
本轮不修改 C 二进制、DSP 配置或音量曲线；AirPlay 仍为 −30…−6 dB。

离线测试还确认私有配置能加载原厂 DSP、打开 `pcmC0D0p`，且测试期间
`Ext Spk=off`、`aml audio i2s mute=on`、`Auto Mute=on`。

English: Bluetooth startup, phone mute/restore, pause/resume and voice reply/music restoration
passed user-present listening tests without reported pops or gain jumps. Switching to test tones
later exposed a persistent mute after a trusted stock PCM underrun. The shell repair is deployed
and exactly verified; it mutes and releases the old lease, waits for manually resumed PCM, and
retains protection against wrong owners, stale heartbeats and unknown states. All 125 Linux/BusyBox
tests pass. Two reference-tone replays were audible without pops; stopped PCM released its lease without a persistent fault latch.
The user also heard every 500/200/120/80/60 Hz segment and felt low-frequency vibration, with no persistent fault after playback. This establishes low-frequency output, not a measurement of an individual driver or amplifier. The user subsequently reported clearly improved, normal-sounding bass in familiar music. Full driver health, real source switching and sustained operation remain unverified. Neither the DSP configuration nor the AirPlay −30…−6 dB curve changed.

2026-11-08 的整机硬静音测试又向实际私有路由送入 6 秒全零 S16 采样：PCM 进入
`RUNNING`，进程映射了 `libasound_module_pcm_harmandsp.so`，插件日志为
`bypass flag = 0`，测试前后三层静音均未解除。这证明软件 DSP 路由可以执行；
它不能代替左功放通道、连线和 90 mm 单元的真实听测。

真实听测时先检查 `/proc/$pid/maps` 中是否加载了
`libasound_module_pcm_harmandsp.so`。如果中高音正常但 60–120 Hz 测试音仍既听
不到也感觉不到箱体振动，再用受控测试区分 DSP 路径和低音声道硬件；不要在未确认
板级电路前写 GPIO、I²C 或功放寄存器。

## 硬件参考

- HK Allure 拆机和信号链：
  <https://zaage.it/projects/harman-kardon-allure-hardware/>
- Harman DSP 参数和原厂音量曲线：
  <https://zaage.it/projects/harman-kardon-allure-software/>
- 独立复刻项目中的原厂音量模型：
  <https://git.zaage.it/robert/allure-os/src/branch/main/allure/volume.py>


## 可释放的蓝牙 PCM（2026-11-16）

后续实机切换复现了另一个问题：手机暂停但保留蓝牙连接时，原厂播放器仍持有硬件 PCM。AirPlay 已获得新租约，却无法打开自己的 DSP 时钟，最终保护静音。原厂 `setPlayerCmd:stop` 和 `setPlayerCmd:switchmode:wifi` 的命令均成功返回，后者也改变了模式，但本机的声卡句柄仍未关闭；这些实机观察不能由通用 [HTTP API 文档](https://developer.arylic.com/httpapi/)的命令返回值代替。

新增 `hk-bt-pcm.c` / `libasound_module_pcm_hkbtpark.so`，作为蓝牙私有配置里的 ALSA ioplug。原厂播放器继续持有同一个虚拟 PCM；只有允许的新手机采样到达时才打开原有 Harman DSP 硬件通路。其他来源占用时丢弃蓝牙采样，恢复后不补播。每次重开仍调用原厂可选的 `snd_pcm_info_set(..., 11)` 标记蓝牙客户端；Master 音量仍在 DSP 之前。

共享 hook 在新来源租约产生前，先发布来源意图并等待正在进行的蓝牙 open 完成，再淡出、硬静音并验证数字 0 / Ext Spk off / I2S mute on。私有 Unix socket 的 park 请求需要匹配当前轮次标记、旧租约已释放且没有蓝牙 owner；ACK 后还必须实际读到硬件 PCM closed。语音的独立输出也先经过这一释放步骤；蓝牙同 PCM 混音保持原路径。

控制 socket 为 `/tmp/hk-ap2/bt-pcm-control`，打开中标记为同目录 `bt-pcm-opening`，均位于可被重建的 `run/` 之外。回归覆盖运行时删除并重建 run 后仍能释放声卡，以及新来源意图与蓝牙 open 并发时退让。语音占用标记沿用共享脚本的五分钟有效期。控制线程位于原厂播放器内，不新增网络监听服务。

本机原厂 ALSA 1.0.25 只接受 ioplug 1.0.0/1.0.1。插件明确使用 1.0.1 协议；所用字段与[旧 SDK](https://raw.githubusercontent.com/alsa-project/alsa-lib/v1.0.24/include/pcm_ioplug.h)一致，ARM 产物最大 GLIBC 需求为 2.17。原厂 aplay 的 info 查询依赖全局硬件 FD，连直接 null 都会失败；兼容性检查使用与播放器一致的 open/set_params/writei 调用，并只向 null 写全零，完整打开/park/占用期间丢弃/重新打开/关闭循环返回 0。该检查不代替真实音频试听。

完整 **136 项 Linux/BusyBox 测试通过**；九文件受控更新返回 0、事务提交、pending 清除、全部文件哈希及运行时精确验证通过。播放器在保护静音下受控重载，rootApp 保持运行，硬件 PCM 已 closed，维护 hold 仍保留。AirPlay 配置逐字节保留，软件范围仍为 −30…−6 dB。**新后端的真实蓝牙播放、来源双向切换、语音回归及长测仍待完成**；上文蓝牙与低频通过记录属于加入本后端之前的版本。

English: The retained stock hardware handle reproduced a silent Bluetooth-to-AirPlay handoff. Stop and Wi-Fi source commands returned successfully but did not close PCM on this speaker. The new private ALSA ioplug keeps the stock virtual handle while returning its DSP/hardware slave through the guarded hook before a new lease. It discards blocked phone samples and reopens only for new allowed samples, retaining pre-DSP Master volume and the stock Bluetooth client tag. Source intent and an in-flight-open marker protect concurrent takeover; control survives run-directory reconstruction. Native tests cover actual file-descriptor release/reopen, discarded samples, guards, socket lifecycle and source races. The stock-ALSA null-only cycle and all 136 Linux/BusyBox tests passed. The nine-file deployment committed with exact hashes and runtime verification; maintenance mute remains. AirPlay configuration and its −30…−6 dB range are preserved. Audible Bluetooth, bidirectional handoff, voice regression and sustained testing on this backend remain pending.
