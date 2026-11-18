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
a01controller -> Master softvol -> plug (48 kHz) -> hkbtpark -> harmandsp -> hw:0,0
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


## 原厂软件音量层的 MMAP 兼容修正

新后端首轮真实蓝牙播放无声，设备没有故障锁存，但硬件 PCM 一直 closed。空输出进一步复现：直接虚拟 PCM 可写入，外面加上原厂 Master softvol 后，open 返回 0，参数协商返回 −22。原厂 softvol 要求底层提供 MMAP access；第一版后端只有 RW_INTERLEAVED，软件层无法完成初始化。

修正版同时提供 RW_INTERLEAVED 和 MMAP_INTERLEAVED，仍保持 `mmap_rw=0`，保证普通 writei 和 MMAP commit 都真正调用 transfer。回归先复现软件层协商失败，再验证修正；另覆盖直接 MMAP 客户端的音频数据、park/重新打开和禁止补播。初始化参数和第一包格式只做一次元数据记录，不记录音频内容。

完整 **138 项 Linux/BusyBox 测试通过**。原厂实际的 Master softvol → 新后端 → null 整链完成 OPEN/PARAMS/WRITE/PARK/占用期间写入/REOPEN/CLOSE，全部返回预期成功，检查期间真实声卡保持 closed。五文件受控更新已提交并通过精确核验，维护静音保留；**修正版真实蓝牙、双向来源切换、语音及长测仍待试听**。

English: The first live backend test was silent with no fault latch and no hardware PCM. The complete stock Master-softvol/virtual/null probe reproduced successful open followed by −22 during parameter negotiation: softvol requires an mmap slave, while the first backend exposed only interleaved RW. The fix offers both access modes while retaining mmap_rw=0 so neither ordinary writes nor mmap commits bypass sample transfer. Two new regressions verify software-layer negotiation and actual mmap park/reopen data flow. All 138 Linux/BusyBox tests and the full stock software-volume/null cycle passed. The five-file fix is installed and exactly verified under maintenance mute; audible Bluetooth, handoff, voice and sustained testing remain pending.


## 44.1 kHz DSP 适配与整圈缓冲恢复

MMAP 修正版实机仍无声：手机输入已到达 transfer，但 DSP slave 参数设置返回 −22，硬件 PCM 随即关闭。全程维护静音、仅写全零的实际 DSP 对比确认：当前参数调用在 44.1 kHz 下拒绝 period time，48 kHz 成功；未限定采样率的 plug 不能解决它。最终配置在 Master 之后、虚拟 PCM 之前以 plug 固定 slave.rate=48000，使虚拟帧计数与实际 DSP 同速。把转换器放在虚拟后端内部虽可协商，实际持续写入仍会停住。

完整缓冲的 park/重新打开检查还复现了两处死锁：释放满缓冲使 modulo 指针整圈跳回原值，旧 ALSA 误认为没有前进；重新打开又可能在外层仍有待处理帧时等待整缓冲启动。修正用 64 位计数分次报告小于一圈的进度，并以不少于一个 period 的半缓冲预填启动新 slave。独立对照分别移除两项修正，均能使同一满缓冲回归失败。

完整 **141 项 Linux/BusyBox 测试通过**，包含真实采样数据的 44.1→48 kHz 转换、按时钟消耗的测试声卡、精确满缓冲释放后继续丢弃和重新打开。实机使用原厂 Master softvol、原厂 ALSA 和真实 Harman DSP，对 44.1/48 kHz 输入分别完成持续写入 61,440 帧 → park 后硬件 closed → 占用期间继续接受并丢弃 61,440 帧 → 重新打开并写入 61,440 帧 → 关闭。两个循环都返回 0，播放阶段实际 PCM 为 RUNNING；保护 hold/manual mute 全程有效，仅有零数据。

六文件静音更新已提交：返回 0、pending 清除、六文件哈希相符、运行时精确验证 0。维护 hold/manual mute 保留，硬件 PCM closed。AirPlay 配置与 −30…−6 dB 范围保持原样；真实蓝牙、双向来源交接、语音与长测仍待用户试听，前述历史通过不能替代本后端验收。

English: The mmap fix remained silent in the live test. Phone samples reached transfer, but the real DSP rejected the 44.1 kHz period parameters with −22 and the hardware PCM closed. Muted zero-only probes accepted 48 kHz. The final plug adapter converts before the retained virtual handle, keeping its frame counter at the DSP rate; a converter inside the backend negotiated but stalled under sustained stock-ALSA writes. Parking a full ring also aliased modulo progress to zero, and a reopened slave could wait for more prefill than the retained pipeline could supply. Bounded pointer advances with 64-bit counters and half-buffer slave startup fix these separate deadlocks; removing either fix makes the full-ring regression fail. All 141 Linux/BusyBox tests pass, including real resampling and a clock-paced sink. Full stock Master/virtual/DSP zero-data cycles at both input rates passed continuous write, park, blocked discard, reopen and close, with RUNNING playback and closed hardware while parked. The six-file muted update committed with return code 0, matching hashes, no pending transaction and exact verification 0; maintenance mute and an idle closed PCM remain. AirPlay configuration and volume behavior are preserved; actual listening and bidirectional handoff are still pending.


## 已退出的打开线程与真实声卡持有者

上一版用户手动播放仍完全无声。实际硬件 PCM 已是 RUNNING、48 kHz/S16，DMA 持续前进，原厂播放器确实持有 `/dev/snd/pcmC0D0p`，但没有蓝牙租约。`owner_pid` 记录的打开线程已退出，`/proc/<owner_pid>/comm` 不存在；旧的蓝牙桥、共享保护、交接 hook 和语音混音检查因此误拒绝真实来源。此处不是 DSP 参数或低音单元的问题。

后端现在在实际 slave 初始化成功后，以权限 0600 原子发布 `/tmp/hk-ap2/bt-pcm-owner`，记录进程 PID、启动时间、匹配真实播放设备的 FD 和本次打开代次。共享检查核验进程身份仍有效、进程名正确、该 FD 当前仍指向目标设备，并复查记录未被替换。已退出或被复用的打开线程不再代替真实持有者；旧身份、错误 FD/进程名、不完整记录和符号链接均拒绝。没有新记录的旧后端仍保留原有存活线程检查。

park/close 先撤销属于当前 slave 的记录，再关闭设备；进程退出、FD 关闭/复用或启动时间变化也会使证明失效。记录位于可重建的 run 目录之外。蓝牙桥、watchdog 共享校验、hook 的时钟/park 检查及语音蓝牙混音统一使用这一规则，AirPlay 和独立语音输出的归属分支不变。

完整 **150 项回归通过**，包括真实 FD 记录的创建/撤销/重新打开、失效和复用线程 ID、错误身份/句柄、真实桥接取得租约及丢失句柄后释放。实机的两种采样率全零 DSP 循环仍通过。九文件归属修正版已安装，事务返回 0、pending 清除、九文件哈希与运行时精确验证均通过；实际出声与来源切换仍待试听。

English: Manual playback on the previous revision was still silent although the real 48 kHz PCM and DMA were running. The recorded opener TID had exited; the stock player still held the actual playback descriptor. Name checks against that stale TID prevented a Bluetooth lease. The backend now atomically publishes a private live-holder record after slave setup: process PID, start time, actual device FD and open generation. Shared consumers revalidate identity, process name, current descriptor target and an unchanged record. Dead/reused opener IDs no longer override real ownership; stale identity, wrong descriptors/names and malformed or symbolic records fail closed. Park/close revoke only the matching proof, outside disposable run state. Bridge, watchdog, hook and Bluetooth voice mixing share the rule; AirPlay/standalone-voice ownership branches are unchanged. All 150 regressions and both real-DSP zero-data cycles pass. The nine-file muted update committed with return code 0, no pending transaction, matching hashes and exact verification; audible acceptance is pending.

## 非阻塞短写入与持续失真（2026-11-16）

归属修正版的下一次试听能出声，但用户报告持续“吱吱响”和爆音。已恢复维护静音，用户暂停并断开蓝牙。该结果不能写成蓝牙验收通过。

原厂调用方以 NONBLOCK 打开蓝牙 PCM；每包只调用一次 writei，返回任何非负值都会进入下一包，不补齐短写入。在原厂 ARM ALSA 的 file/null 输出中，以真实调用地址运行未放宽身份检查的 shim，注入 127 帧短写入和周期 EAGAIN：旧版请求 180,000 帧，丢弃 90,000 帧。补齐外层写入后，48 kHz 正常，但 44.1 kHz 仍少于直接 ALSA 参考：rate/softvol 在下游 MMAP commit 短写入时可能已经消耗输入，因此后端 callback 也必须完成整包。

修正先混音一次，再在同一结果上补写；后端补齐 slave 写入。永久背压有时间预算，新来源或维护保护可取消等待并丢弃待写采样，避免延迟补播。活动租约下的 XRUN 请求桥接先静音并撤销租约，之后才允许时钟恢复；错误时钟读数也请求受保护重同步。

完整 **160 项 Linux/BusyBox 回归通过**，包括两层数据保真、语音只混一次、重采样、背压取消及活动 XRUN 禁止直接重置。最终 ARM 文件对照在相同非 drain 关闭方式下，与直接 ALSA 参考逐字节一致：48 kHz 为 720,000 字节，44.1 kHz 为 768,000 字节；两轮客户端均无短包丢弃或 recovery。该对照不包含原厂 DSP 输出，也不能单独证明听感正常。

六文件更新事务已提交，返回 0、无 pending、全部哈希与运行时精确核验通过。真实 DSP、共享保护与 AirPlay −30…−6 dB 配置保留。**当前包的失真、双向来源切换、语音与持续播放仍待真实试听。**

English: The holder-identity revision became audible but failed listening acceptance because of persistent buzzing and pops. After the user paused and disconnected, maintenance mute was restored. The stock nonblocking caller discards every short-write remainder. A file/null probe on stock ARM ALSA, using the verified real call address, reproduced 90,000 dropped frames out of 180,000. Completing only outer writes fixed 48 kHz, but 44.1 kHz still lost downstream rate output; backend callbacks must also complete the slave packet. Voice is mixed once before retries. Bounded, cancellable backpressure allows prompt muted handoff; an active XRUN requests lease release before clock recovery. All 160 tests pass. Both ARM input rates match direct ALSA file references byte for byte with the same non-draining close: 720,000 and 768,000 bytes respectively, with no client packet drops or recovery. This comparison excludes proprietary DSP output and does not prove audible fidelity. The six-file repair committed and passed exact verification under maintenance mute; distortion, handoff, voice and sustained acceptance remain pending.

随后真实试听反馈完全无声。此时手机音量为 20/100、Master=6、DAC 数字级为 207，Ext Spk/Digital 已打开、I2S 已解除静音，无故障锁存；PCM 持续 RUNNING、DMA 指针前进、active 蓝牙租约有效。没有将这一现场判为保护误静音，也没有自动提高音量。恢复维护静音后，增加仅在 `bt-pcm-diagnostics` 标记存在时启用的播放器入口和后端峰值/非零采样数/帧数诊断；不写音频内容。需要新的实机电平结果才能区分输入静音、软件衰减和 DSP 之后的问题，当前不能宣称修好。

English: The following live test was completely silent. Phone volume was 20/100 and Master=6; DAC digital level was 207, speaker/digital switches were enabled, I2S was unmuted, the active Bluetooth lease was valid and DSP DMA kept advancing without a fault latch. This was not classified as another false protection mute, and gain was not automatically raised. After restoring maintenance mute, opt-in aggregate level diagnostics were added at the player entry and backend, gated by `bt-pcm-diagnostics`. They record frame/nonzero counts and peaks, never audio content. New device evidence is needed to distinguish silent input, software attenuation and the downstream DSP/output path; the repair is not accepted.

## 全局强制静音残留与恢复（2026-11-16）

后端自身的只读 `levels` 查询确认实际声卡缓冲区也有音乐：两路峰值约 881/404，非零采样持续存在。按已核验的原厂 `libmvmsg` 布局，只读获取声音控制字段：蓝牙 client11 和 AirPlay client3 的 mute 都为 0，但全局 `is_force_mute=1`、PA mute=1。这解释了为什么 DAC 开关、播放租约和 DMA 都正常却无声。

原厂播放器的 `setPlayerCmd:mute:0` 在其内部已为 0 时会直接返回，不重新通知 IOGuard，因此不能把播放器的 mute=0 当作全局功放已解除静音。实际固件的官方通知是发往 `/tmp/RequestIOGuard` 的 `GNOTIFY=AUDIOMUTE:0`，由原厂处理全局标志与 MCU/功放状态。

在有效蓝牙租约与数据时钟下，先将 DAC 数字电平降为零并通过共享 guard，发送该通知、回读全局和 PA 都为 0，再经过稳定等待和 guard 恢复原电平。未增加手机或软件音量。用户确认**音乐清楚，无爆音或音量突增**；随后固化版的开始播放、暂停约三秒及恢复也均正常。

固化流程只加入蓝牙开声分支：数字为零、Ext Spk 关闭、PCM 时钟与保护有效时，核验原厂库 SHA-256 与播放器的蓝牙/播放/非静音状态，按需发送官方通知；核实全局与 PA 均已解除后才打开输出和恢复数字电平。通知送达但回读未变、用户声源静音、维护 hold、错误固件布局等均保持数字静音。状态工具使用 SHM_RDONLY，只读取三个已验证的声音字段；它不修改共享内存、寄存器、音量或其他设备设置。AirPlay 分支不调用这项蓝牙同步。

固化版完整 **173 项回归通过**，包括真实 System V 共享内存的只读测试、通知成功但状态未解除、原厂声源静音、新 hold、错误库布局，以及 AirPlay 不进入蓝牙同步。

English: The backend's read-only query found nonzero music in both hardware PCM channels. The verified stock sound context showed source mutes clear but global force-mute and PA mute both set. A repeated player mute=0 command can be deduplicated without notifying IOGuard. Under the existing guarded zero-DAC sequence, the official `GNOTIFY=AUDIOMUTE:0` notification cleared both global and PA state, and the same gain was restored. The user confirmed clear music without pops or a gain jump, then normal startup and pause/resume on the persistent repair. Bluetooth startup pins the stock library layout, verifies explicit source intent and a silent clock baseline, sends the notification only when needed, and checks both global and PA acknowledgement before opening output and restoring gain. The new SHM_RDONLY helper reads only three sound flags. All 173 tests passed, including real shared memory, failed acknowledgement, source mute, hold, firmware mismatch and AirPlay isolation.

## 启动等待优化（2026-11-17）

用户反馈点击播放或恢复后等待过长，旧日志的开声流程约四秒。蓝牙开声现不再启动两次临时 AirPlay 功放辅助进程，也不修改 AirPlay client3 的静音位；改为核验蓝牙自身来源意图与官方全局/PA 状态。数字静音及原有 300 ms 斜坡等待与 watchdog 确认重叠；模拟基线写入和任何输出开启仍须等确认完成。200 ms 功放稳定等待保留。

空闲检测和 hook 子进程等待约为 200 ms，稳定播放保持一秒轮询；暂停代次和 transport repair 的原厂 RPC 最多每秒一次。12 秒启动等待上限保留。完整 **179 项回归通过**，三文件事务已提交、哈希及运行时精确核验通过，未重载原厂播放器或修改音量/DSP 配置。

用户确认开始和暂停恢复**明显更快，声音正常**。日志中三次 `start requested` 至 `speaker enabled` 为 3.00、2.53、2.79 秒；这只是 hook 内部流程，不是完整手机端到端延迟。首次还自动清除了人为复现的原厂全局静音，回读 `force_mute=0`，结束无故障锁存。双向来源交接、当前版本语音与持续运行仍待验收。

English: Slow playback starts and resumes were traced to roughly four seconds inside the old opening hook. Bluetooth no longer launches temporary AirPlay amplifier helpers or changes client3 mute. Its existing 300 ms digital fade overlaps watchdog acknowledgement; baseline writes and every output enable still wait for acknowledgement, and the 200 ms amplifier warm-up remains. Idle/hook waits use roughly 200 ms polling, with stock metadata/repair RPCs limited to once per second and stable playback still polled once per second. All 179 tests pass and the three-file transaction exactly verified without reloading the stock player or changing volume/DSP configuration. The user confirmed noticeably faster starts and resumes with normal sound. Three hook intervals were 3.00, 2.53 and 2.79 seconds, not phone-to-speaker latency. The deliberately reproduced global mute was cleared and no fault remained after playback. Bidirectional handoff, current-version voice and sustained operation remain pending.

## 2026-11-17：重连首播与 AirPlay 接管暂停

用户随后确认短暂爆音仅发生在重新连接手机后的第一次蓝牙播放，普通暂停/恢复正常；AirPlay 接管时手机蓝牙进度仍在前进。日志显示硬件已经释放并切换到 AirPlay 租约和 DSP 时钟，旧实现只停本地输出，没有发送手机暂停。

新修正按实际 PCM 持有者/FD 代次记录功放准备状态。新代次在数字为零、Ext Spk 关闭、时钟与租约有效时，通过原厂 GNOTIFY 将全局与 PA 静音同时确认为 1，然后才修改模拟基线和 I2S；时钟稳定后解除并回读，再开输出。普通暂停恢复保留快速路径；AirPlay client3、音量与 DSP 配置不变。

失败或取消先关闭物理输出，再清理本轮临时全局静音。恢复标记位于 `/tmp/hk-ap2/bt-cold-global-mute`，不随 run 目录重建丢失；如果清理未确认则保留标记，下一来源在受保护的静音时钟下恢复后才允许开声。AirPlay 接管先让蓝牙释放硬件，再发送 `setPlayerCmd:onepause` 并回读 stop/pause；通知成功但未确认暂停时保持关闭，不安排蓝牙自动续播。

**191 项完整 Linux/BusyBox 回归及最终 38 项复核通过。** 覆盖新/旧 PCM 代次、功放预静音不确认、启动失败清理、遗留全局状态恢复、真实 hook 的 park→手机暂停→开声顺序及暂停未确认拒绝输出。实机硬静音、声卡空闲的官方接口检查得到全局/PA 0→1→0，未写音频采样。两文件事务已提交、返回 0、pending 清除，哈希和运行时精确核验通过；AirPlay 配置逐字节保留。用户随后确认**本版重连首次出声正常、无爆音**；日志确认新代次预静音和解除回读均执行，临时全局标记已清除、无故障锁存。用户随后确认 AirPlay 接管时手机自动暂停；手机重新播放未暂停 AirPlay，反向切换、语音与长测仍待验收。

English: The user narrowed the pop to the first Bluetooth play after reconnect; ordinary pause/resume remained normal. AirPlay had acquired the hardware and lease, but the phone timeline continued because the old handoff only stopped local output. A fresh descriptor generation now requires a guarded zero-DAC baseline and acknowledged global/PA mute before analogue/I2S setup; warm resumes retain the fast path and client3/volume/DSP configuration are unchanged. Failed or cancelled starts close physical output before clearing their temporary override. The recovery marker lives outside the disposable run directory; an unconfirmed cleanup is recovered under the next source's guarded silent clock. AirPlay parks hardware, sends real phone pause and requires acknowledgement before opening, with no automatic Bluetooth resume. All 191 tests plus a final 38-test check pass, including full-hook takeover ordering, unacknowledged pause and mute, generation reuse, failed-start cleanup and orphan recovery. A no-audio stock-interface probe verified global/PA 0→1→0 under maintenance mute. The two-file transaction committed with return code 0, no pending journal, matching hashes and exact runtime verification; AirPlay configuration is preserved byte for byte. The user subsequently confirmed normal first play after reconnect without a pop. Logs confirm the cold pre-mute and unmute acknowledgements, no remaining recovery marker and no fault latch. The user then confirmed automatic phone pause on AirPlay takeover. Pressing phone play did not pause AirPlay; reverse handoff, voice and sustained acceptance remain pending.


## AirPlay → 蓝牙反向切换更新（2026-11-17）

用户确认蓝牙播放中选择 AirPlay 后，手机音乐会自动暂停；随后在手机点击播放时，AirPlay 没有暂停。反向交接更新已安装，实际切换仍待用户试听。

本次更新为蓝牙私有 PCM 添加只读 `activity` 查询，返回播放器 PID、PCM 代次、累计接收帧数和最近接收时间；不保存音频、样本或电平。桥接器在同一 AirPlay 租约里先观察暂停或断开；正常路径连续采样确认没有新增帧，再要求播放状态及新鲜 PCM 帧同时成立。如果手机在第二次安静采样前就播放，则要求同一播放器与 PCM 代次的帧数在至少三个采样点持续增加、时间约两秒（按整秒计数），避免仅凭一个残留包接管。查询失败、较长采样间断、手机暂停或语音、维护状态变化会取消或重新开始确认。旧 `play`、过期音频、播放器重启或不同租约的旧观察都不能单独触发接管。手机从断开状态新连接并直接播放也按同样的帧验证处理。

如果手机在桥接器观察到这一租约的任何暂停状态之前就已经播放，更新会保守地不抢占；这个极快切换边界仍需实机检查。

确定接管后，通过带原租约代次的共享 hook 先淡出并关闭 AirPlay 输出，向本地 MQTT 发送 `pause`，等待原声卡释放或确认声卡已由蓝牙持有，再允许蓝牙取得租约和执行原有防爆音开声。维护静音、语音占用、新来源租约与错误所有者保留优先级；请求发送失败或声卡超时会锁存保护静音。**MQTT 命令送达本地代理不能证明 Mac 已暂停，实际发送端状态、双向切换与声音仍需用户在场验证。**

七文件更新包含蓝牙 PCM 模块、包装器、启动器、共享 hook、桥接器、控制器及依赖重载脚本。在设备空闲和维护静音下，事务已提交，全部文件哈希、AirPlay 配置和服务状态核验通过；原厂播放器已受控重载，rootApp 保持运行。维护静音已解除，设备恢复空闲待播。用户稍后再验证实际切换和听感。

English: The user confirmed that AirPlay now pauses the phone, but manually resuming the phone did not pause AirPlay. The reverse-handoff update is installed and awaits user listening. It adds a read-only private PCM `activity` query reporting process, instance, received-frame count and last-receive time; it stores no audio, samples or levels. During the same AirPlay lease, the normal path establishes a quiet paused/disconnected baseline and then requires both a phone-play state and fresh Bluetooth PCM. If play follows before the second quiet sample, at least three observations with increasing frames from the same player and PCM instance across roughly two seconds are required, using whole-second clock ticks. A failed query, long sampling gap, pause, voice or maintenance change cancels or restarts confirmation. Cached play state, old audio, a restarted player or an old lease cannot independently trigger a takeover. A newly connected phone that starts playing follows the same check.

If the phone starts before the bridge observes any paused state for that lease, the update conservatively refuses takeover; that very fast transition still requires device testing.

For takeover the bridge uses the existing generation-checked hook to fade and close output, requests AirPlay pause through local MQTT, checks the old hardware has been released or is held by Bluetooth, and only then allows Bluetooth to claim the guarded output. Hold, voice ownership, a new lease and wrong owners retain priority; command failure or clock-release timeout latches protection. MQTT broker delivery alone does not establish that the Mac actually paused: sender state, source switching and audible behavior still require user-present validation. The seven-file update includes the PCM module, wrapper/boot pins, hook/bridge/controller and verified dependent-reload helper. The maintenance-muted transaction committed with matching hashes, unchanged AirPlay configuration and verified services. The stock player was reloaded while rootApp remained running; maintenance mute was cleared after confirming idle. The user will verify real switching and audio later.

最终本地完整 **227 项 Linux/BusyBox 回归通过**，包括快速播放、单个残留包、查询失败或采样间断、用户再次暂停、维护 hold 与语音介入、不同 AirPlay 租约/播放器代次、无效时间戳和远程命令失败等情况。ARM 产物为 hard-float，最高 GLIBC 符号需求为 2.17。本地 QEMU 使用原厂 ALSA 库、glibc 2.28 和独占合成共享上下文，只向 AirPlay 正占用的虚拟 PCM 写全零帧：`activity` 从 0 增至 512，返回 0；没有打开真实声卡。此检查不代替固件环境、Harman DSP 和用户试听。

English: At that point, all **227 local Linux/BusyBox regressions passed**, covering quick play, a single residual packet, query failures or sampling gaps, phone pause, hold or voice interference, changed AirPlay/player generations, invalid timestamps and remote-command failure. The ARM artifact uses the hard-float ABI and requires GLIBC symbols no newer than 2.17. Offline QEMU with the stock ALSA library, glibc 2.28 and an isolated synthetic shared context wrote zero frames only to a blocked virtual PCM: the `activity` count advanced from 0 to 512 and the probe returned 0, without opening hardware. This does not replace validation on the firmware, real Harman DSP or user listening.

## 手机只连接不播放与 AirPlay 功放恢复（2026-11-18）

最新现场组合是：Mac 已经通过 AirPlay 2 正常播放，手机随后连接蓝牙，但用户尚未点击手机播放。原厂蓝牙状态可能残留 `status=play`；仅凭该字符串判断会把一次连接误当成真实播放，导致错误暂停 AirPlay 或切换输出。同时，蓝牙连接动作可能改变 AirPlay 正在使用的原厂功放静音。

桥接器的活动判据现以后端持续增长的新鲜 PCM 帧为准，原厂状态只是附加确认：

- 没有新鲜手机帧时，不进入蓝牙接管；如果 AirPlay 功放被连接动作改成静音，就在同一有效 AirPlay 租约和运行时钟下，经数字级先归零再恢复原厂全局/PA 状态。
- 有连续新鲜帧时，才按既有暂停基线、租约代次和播放器身份进入受保护的蓝牙抢占。
- 原厂播放器暂停 RPC 暂时失败时，AirPlay 继续正常出声，同时保留待办暂停标记供桥接器稍后安全重试；不能为了发送暂停命令而让 AirPlay 留在静音。
- AirPlay 的增益恢复记录绑定租约 token 与租约时间。旧租约留下的临时零增益不会应用到新租约。

完整 **335 项 Linux/BusyBox 回归通过**，日志 SHA-256 为 `e10c554e3598648b2745bbda62318ce7e83f4f636071e81d84efbc109228baef`。覆盖 AirPlay 播放时手机仅连接、陈旧 play、真实新鲜帧、暂停 RPC 失败、维护 hold、新租约、旧增益记录、双向抢占、语音、watchdog 和更新事务。三文件事务 `/vendor/airplay2/.updates/20261118T035933-27009` 已在维护静音下部署：返回 0、committed、无 pending、三个文件 SHA-256 一致、`hk-ap2-prodctl verify=0`，AirPlay 5.6 和端口 7000、watchdog、蓝牙桥、语音监督均正常，AirPlay 配置 SHA-1 仍为 `1a4b6e188135e61ae99e32c3ed4578d5c7404bc1`。当前仍为维护静音、无播放租约、PCM closed。

**以上是自动化与静默部署证据，不是组合听感验收。** 下一步仍需用户在场、低音量依次验证：AirPlay 播放时手机只连接不影响声音；手机开始播放后 AirPlay 暂停且蓝牙出声；反向接管；连续穿插 2–3 次；两种来源各唤醒一次语音；最终恢复、爆音、音量突增和持续无声检查。

English: The latest live sequence had AirPlay playing when a phone connected over Bluetooth without starting playback. Stock Bluetooth can retain `status=play`, so that string alone is unsafe; the connection action can also change the amplifier mute already used by AirPlay. The bridge now treats continuously increasing fresh frames from the private PCM backend as the decisive activity evidence. Without fresh frames it restores AirPlay's stock amplifier state under the same valid lease and running clock; with fresh frames it follows the guarded takeover path. A failed stock-player pause RPC no longer makes AirPlay fail closed: AirPlay keeps playing and a pause-retry marker is retained. AirPlay gain restoration is tied to the lease token and timestamp so an old temporary zero cannot be applied to a new session.

All **335 Linux/BusyBox regressions pass**. The log SHA-256 is `e10c554e3598648b2745bbda62318ce7e83f4f636071e81d84efbc109228baef`. Coverage includes phone-connect-only while AirPlay plays, stale play, real fresh frames, pause RPC failure, hold, changed leases, old gain state, bidirectional takeover, voice, watchdog and update transactions. The three-file transaction `/vendor/airplay2/.updates/20261118T035933-27009` committed under maintenance mute with return 0, no pending journal, exact hashes and `verify=0`. AirPlay 5.6, port 7000, watchdog, Bluetooth bridge and voice supervision are healthy. The byte-identical AirPlay configuration retains SHA-1 `1a4b6e188135e61ae99e32c3ed4578d5c7404bc1`. The device remains muted with no lease and a closed PCM.

This is automated and muted-deployment evidence, not listening acceptance. User-present low-volume tests still need to cover phone-connect-only, each takeover direction, several interleaved switches, Alexa from each source, final music restoration, pops, gain jumps and sustained silence.

## 三方交叉交替重构与可用性加固（2026-11-18）

在继续实机组合验收前，进一步用真实守护进程（`hk-ap2-safe-test`、`hk-bt-bridge.sh`、`hk-ap2-audio-hook.sh`、`hk-voice.sh`、`hk-ap2-guard.sh`、`hk-ap2-lights.sh`）构造连续多轮交叉测试，定位并消除了多轮 AirPlay / 蓝牙 / Alexa 穿插切换中的状态残留与锁争用：

- **反向接管状态跨会话清理**：`yield_airplay_to_bluetooth` 成功让出与 `release_output` 结束蓝牙会话时均清理 `reverse_require_quiet` 与旧观察记录，避免首轮蓝牙接管并切回 AirPlay 后，手机紧接再次播放被误判为中断残留而拒绝接管。
- **陈旧 `status=play` 下待办暂停收敛**：手机已无音频但原厂仍返回 `status=play` 时，`retry_pending_phone_pause` 在送出 `onepause` 后结合连续两拍静止帧基线（`>2 s` 无新帧）完成收敛，不再无限每秒重发暂停或锁死后续真实抢占。
- **非 AirPlay 释放不误删 `ap2-claim`**：watchdog 在蓝牙或独立语音释放过渡期仅清理对应来源标记，不再无条件删除正在等待的 AirPlay 启动声明。
- **蓝牙两轮追问与语音听音期保护**：蓝牙同 PCM 混音多轮问答在每轮结束后将数字级压回 `0,0`，保证追问轮 `guard active` 稳定通过；唤醒听音阶段即持有 `voice-owns`，中途手机断开可自动回退独立 `aplay` 并在语音期间记录的延迟 AirPlay 请求于收尾后有序接管。
- **watchdog 非阻塞空闲静音与干净缓存回收**：空闲 `mute_without_active_generation` 先跳过进行中的租约请求并使用短锁探测，配合 200 ms 微轮询把租约 ACK 缩短至亚秒级；资源守卫与运行时挂载前在满足可用内存门槛时先回收干净页缓存，避免高缓存占用触发误停服，同时完整保留 `8192/49152`、`47104` 与 `4096/32768 KiB` 安全下限。

完整 **345 项 Linux/BusyBox 回归与 13 项 1:1 真实时间尺度验收预演全部通过**（新增 10 项真实多进程交叉交替与 `audio_lock` 并发解锁边界用例）。十脚本事务 `/vendor/airplay2/.updates/20261118T063814-13768` 与底层锁修正事务 `/vendor/airplay2/.updates/20261118T071506-19692` 均已在设备空闲与维护静音下提交并通过精确核验（`UPDATE_RC=0`、`VERIFY_RC=0`、`CONF_SHA1=1a4b6e188135e61ae99e32c3ed4578d5c7404bc1`），随后按两阶段原子目录锁（`audio_lock`）解除维护静音，当前空闲待测。真实组合试听仍需用户在场低音量完成。

English: Before live combination acceptance, real-daemon cross-source integration tests and a 13-step 1:1 real-time rehearsal exposed and fixed multi-round state-carryover and lock-contention bugs across interleaved AirPlay, Bluetooth and Alexa sessions: clearing `reverse_require_quiet` upon completed Bluetooth yield/release so second-round phone takeovers are not blocked; converging `bt-pause-pending` via confirmed silent PCM frame baselines when stock retains stale `status=play`; preserving `ap2-claim` during non-AirPlay release recovery in the watchdog; re-ducking `Digital Playback Volume` to `0,0` between multi-round Bluetooth voice-mix follow-ups; holding `voice-owns` throughout user speech with clean standalone fallback on mid-question phone disconnect and synchronous/deferred AirPlay reopen; eliminating two `audio_lock` concurrent unlock-edge races; non-blocking idle watchdog lock probes with 200 ms lease-request wakeups; and clean page-cache reclamation before falsely tripping low-free-RAM counters while keeping all `8192/49152`, `47104` and `4096/32768 KiB` floors intact. All **345 Linux/BusyBox tests and the 13-case rehearsal pass**, transactions `/vendor/airplay2/.updates/20261118T063814-13768` and `/vendor/airplay2/.updates/20261118T071506-19692` verified cleanly on the speaker, and maintenance mute was released in two directory-locked phases at idle. User-present low-volume listening acceptance remains required.
