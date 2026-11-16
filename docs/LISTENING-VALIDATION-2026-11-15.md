# 试听与语音续播记录 / Listening and voice handoff — 2026-11-15

本记录接续 [11 月 14 日部署记录](DEVICE-VALIDATION-2026-11-14.md)。结论只适用于作者这台音箱及本轮 Mac“音乐”AirPlay 测试。

## 2026-11-17：重连首播与 AirPlay 接管暂停

用户随后确认短暂爆音仅发生在重新连接手机后的第一次蓝牙播放，普通暂停/恢复正常；AirPlay 接管时手机蓝牙进度仍在前进。日志显示硬件已经释放并切换到 AirPlay 租约和 DSP 时钟，旧实现只停本地输出，没有发送手机暂停。

新修正按实际 PCM 持有者/FD 代次记录功放准备状态。新代次在数字为零、Ext Spk 关闭、时钟与租约有效时，通过原厂 GNOTIFY 将全局与 PA 静音同时确认为 1，然后才修改模拟基线和 I2S；时钟稳定后解除并回读，再开输出。普通暂停恢复保留快速路径；AirPlay client3、音量与 DSP 配置不变。

失败或取消先关闭物理输出，再清理本轮临时全局静音。恢复标记位于 `/tmp/hk-ap2/bt-cold-global-mute`，不随 run 目录重建丢失；如果清理未确认则保留标记，下一来源在受保护的静音时钟下恢复后才允许开声。AirPlay 接管先让蓝牙释放硬件，再发送 `setPlayerCmd:onepause` 并回读 stop/pause；通知成功但未确认暂停时保持关闭，不安排蓝牙自动续播。

**191 项完整 Linux/BusyBox 回归及最终 38 项复核通过。** 覆盖新/旧 PCM 代次、功放预静音不确认、启动失败清理、遗留全局状态恢复、真实 hook 的 park→手机暂停→开声顺序及暂停未确认拒绝输出。实机硬静音、声卡空闲的官方接口检查得到全局/PA 0→1→0，未写音频采样。两文件事务已提交、返回 0、pending 清除，哈希和运行时精确核验通过；AirPlay 配置逐字节保留。**本版重连首播是否无爆音、手机实际暂停、反向切换、语音与长测仍待验收。**

English: The user narrowed the pop to the first Bluetooth play after reconnect; ordinary pause/resume remained normal. AirPlay had acquired the hardware and lease, but the phone timeline continued because the old handoff only stopped local output. A fresh descriptor generation now requires a guarded zero-DAC baseline and acknowledged global/PA mute before analogue/I2S setup; warm resumes retain the fast path and client3/volume/DSP configuration are unchanged. Failed or cancelled starts close physical output before clearing their temporary override. The recovery marker lives outside the disposable run directory; an unconfirmed cleanup is recovered under the next source's guarded silent clock. AirPlay parks hardware, sends real phone pause and requires acknowledgement before opening, with no automatic Bluetooth resume. All 191 tests plus a final 38-test check pass, including full-hook takeover ordering, unacknowledged pause and mute, generation reuse, failed-start cleanup and orphan recovery. A no-audio stock-interface probe verified global/PA 0→1→0 under maintenance mute. The two-file transaction committed with return code 0, no pending journal, matching hashes and exact runtime verification; AirPlay configuration is preserved byte for byte. First-play listening, actual phone pause, reverse handoff, voice and sustained acceptance remain pending on this revision.

## 最新结果：蓝牙全局静音修复与启动加速（2026-11-17）

后续新蓝牙后端的多轮试听曾完全无声；下文早期通过记录不能替代该版本验收。最终只读诊断确认音乐已经进入实际 PCM 缓冲区，而原厂全局 `force_mute` 与 PA mute 仍为 1。播放器自身重复 `mute:0` 会去重，不重新通知 IOGuard。有效蓝牙租约和时钟下，DAC 数字级先降到零，通过共享 guard，再使用官方 `GNOTIFY=AUDIOMUTE:0` 并回读全局/PA 均解除后恢复原电平；没有增大手机或软件音量。用户确认音乐清楚、无爆音或音量突增。173 项回归的固化版安装后，用户又确认开始播放、暂停约三秒及恢复都正常，无异常声音。

用户接着指出点击播放/恢复后等待很久。旧 hook 流程约四秒。179 项完整 Linux/BusyBox 回归通过的启动优化版已安装并精确核验：蓝牙不再启动临时 AirPlay 功放助手，数字静音准备与 watchdog 确认重叠，空闲检测加快；模拟基线与输出开启仍等待确认，300 ms 静音斜坡及 200 ms 功放稳定等待保留。未修改 DSP、音量曲线、C 后端或缓冲区，也未重载原厂播放器。

用户反馈：**“明显更快，声音正常。”** 三次日志记录的 `start requested` 至 `speaker enabled` 间隔为 3.00、2.53、2.79 秒；它们不包含手机点击到接收端开始请求之间的时间。首轮自动清除了维护时为复现故障而设置的全局静音，随后回读 `force_mute=0`；播放结束没有故障锁存，正常暂停保留的 XRUN 处于无租约、物理静音状态。179 项测试日志 SHA-256 为 `46a5b365d4fda62a94277b3a40eb4203bba3fc9c9a6c9ecd1f78a53f3857f6db`。

当前继续验证双向蓝牙/AirPlay 来源切换、最新堆栈语音和持续运行；这些项目尚未验收。AirPlay 配置仍为 flat、24 dB 范围、软件输出 −30…−6 dB；本轮未调整其音量曲线。

English: Later live tests on the new Bluetooth backend were silent despite nonzero samples in the actual PCM buffer. Read-only stock state showed global force-mute and PA mute still set; a duplicate source mute=0 request did not notify IOGuard. The guarded zero-DAC sequence now uses the official notification, verifies both states and restores the same gain. The user confirmed clear music without pops or a gain jump, then normal startup and pause/resume on the persistent 173-test repair. The 179-test startup optimization is deployed and exactly verified without changing DSP, volume, backend buffers or the stock player process. It removes temporary AirPlay amplifier helpers from Bluetooth, overlaps digital closing with watchdog acknowledgement and probes idle playback more promptly while preserving output checks and ramp/warm-up delays. The user confirmed noticeably faster starts and resumes with normal sound. Three hook intervals were 3.00, 2.53 and 2.79 seconds, not phone-to-speaker measurements. The deliberately reproduced global mute cleared and no fault remained after playback. Bidirectional handoff, current-stack voice and sustained operation still need acceptance; the AirPlay −30…−6 dB configuration is preserved.

## 已完成的用户试听

| 项目 | 结果 |
|---|---|
| AirPlay 开始播放 | 音乐正常，用户未听到“啪”声 |
| 应用主音量、音箱旁滑块归零 | 两者都能实际静音；接收器收到协议静音值 −144 |
| 暂停约 3 秒后恢复 | 正常，无爆音、无音量突增 |
| 取消选择音箱后重新连接 | 正常，滑块继续有效，无爆音 |
| 机身音量加减、播放/暂停 | Mac“音乐”音量与播放状态同步，用户未报告异常 |
| 播放中唤醒（修正前） | 语音回复清楚、无爆音；用户随后更正：**音乐没有恢复** |
| 续播修正后复测 | 一次问答及追问后，用户确认音乐自动恢复、音量正常、无爆音；接收端核实 active 租约、DSP owner 和 RUNNING PCM |

发送端重建前，界面滑块变化未体现在接收器音量日志，Mac 报告音箱 selected=true、active=false。正常退出并重新打开“音乐”、重新建立 AirPlay 会话后，主音量和音箱旁滑块的实际静音、重连测试通过。只能据此确认重建会话恢复了控制，不能认定 Apple 内部故障的具体原因；本轮没有改音量协议代码或接收器身份。

音量配置仍是 `pipe → Harman DSP`、`flat`、`range=24`、`max=-6.0`、`default_airplay_volume=-30.0`，即软件衰减 −30…−6 dB，协议 −144 完全静音。真实响度还受 DSP、DAC 和功放影响。

## 语音后无声的定位与修正

故障现场 Mac“音乐”显示 playing，Shairport 存活且继续产生音频帧；接收端没有 hold/trip，却没有播放租约，PCM 为 closed。旧 `reopen_watch` 等待 PCM RUNNING 后才调用扬声器 hook；DSP 桥则必须先看到 AirPlay claim 和播放租约才能建立时钟。这形成了互相等待。通过原有防爆音 hook 重建租约后，PCM 恢复 RUNNING，DSP 桥重新成为声卡 owner；没有修改音量或绕过 DSP。

修正让恢复任务先调用共享 hook：发布租约、建立静音时钟、验证保护与功放顺序，再恢复数字增益。恢复成功必须同时核实 AirPlay active 租约、实际声卡 owner、RUNNING 状态和喇叭开关，不再把“已发送 play”写成“已续播”。暂停会话可以恢复静音输出路径；只有需要续播的调用方发送 play。任务保留 hold/trip，核对接收器进程身份，并在检查及共享锁内拒绝抢占蓝牙。恢复锁记录 PID 与进程起始时间，退出时释放，可回收已死亡任务留下的锁。

北京时间 17:34 的修正后复测通过：用户确认一次问答及追问结束后音乐自动恢复、音量正常、无爆音。日志在 09:34:44 UTC 请求续播，09:34:46 验证输出恢复；同一 Shairport、DSP 和 watchdog 保持运行，没有故障静音。这一结果不等同于蓝牙语音或所有来源交接已验收。

四文件修正包已在维护静音下完成事务安装，返回 0、状态 committed、pending 清除；控制器、hook 和语音脚本的安装哈希与候选一致，活动 Shairport 5.6 / DSP / 蓝牙桥 / watchdog 的精确验证通过。接收器详细协议日志已关闭，−30…−6 dB 配置不变。检查空闲声卡和无租约后解除维护 hold 与其 manual mute，等待用户手动播放；没有设置正常播放时限。

## 音量控制再次失效的会话对照

17:36 用户报告滑块再次失效，蓝牙验收因此推迟。音箱继续播放，但只保留开始播放时的 −27.6 dB 衰减；Mac 报告音箱 selected=true、active=false。把 Mac 主音量归零也没有更新接收端的音量状态。正常暂停后开启接收端详细诊断，保留 Mac“音乐”进程；用户重选、播放且没有唤醒 Alexa，滑块仍不能静音。因此不能将此次失效归因于语音交接。

保持同一个接收器进程不变，保存播放位置、正常退出并重新打开 Mac“音乐”，重新选择音箱后，用户确认滑块能真正静音。接收端收到并执行 −144，Mac 报告 active=true。该对照确认重建发送端会话恢复了控制；尚未建立 Apple 内部原因，也没有修改接收器身份、音量协议、曲线或扬声器增益。维护重启后仅重选音箱可能不足，需要确认实际静音有效；详见[交付步骤](BUILD-AND-UPDATE.md)。

18:00 组合复测通过：在有效发送端会话中低音量播放、唤醒并回复、音乐自动恢复，随后滑块归零仍能真正静音，用户确认无爆音。接收端再次验证 AirPlay active 租约、DSP 时钟恢复和新的 −144 静音状态。之后关闭临时详细诊断，并按上述顺序重建最终发送端会话；这项修正没有替换音量曲线或修改协议二进制。

正常日志级别下的最终检查也通过：用户确认播放正常、音量可调、无爆音；接收端收到多个不同音量值及 −144，active 租约与 DSP-owned RUNNING PCM 正常，精确版本核验返回 0，无故障锁存或播放时限。临时诊断、观察与安装文件归档到私有目录后清理，持久回滚事务保留。按用户要求，本轮暂不进入蓝牙调试。

## 诊断期间补充的可靠性修正

- 运行时日志写入改为追加模式；保留 inode 的截断轮转不再使仍持有旧偏移的 writer 写出 NUL 空洞。测试检查实际 stdout/stderr 的 `O_APPEND`、轮转后写入、inode 与文件大小。
- 外层通过 8 MiB 原始空闲 / 48 MiB 保守可用内存预算后，将同一原始空闲门槛传入内层运行时，避免内层遗留 30 MiB 门槛造成矛盾拒绝。运行期 4 / 32 MiB 保护不变。
- 临时 `HK_AP2_DEBUG=3` 可启用详细协议诊断。完整日志可能包含会话与配对信息，只保存在私有目录；正常使用应关闭详细诊断。

完整 **117 项 Linux/BusyBox 测试通过**，shell 语法与 diff 检查通过；测试日志 SHA-256：`933173e964142a2bf8b9bbd405f07eb08d94ac7a52cfef25c52d1fd079b8ad18`。新增用例从 closed PCM 开始，只有 claim 和 lease 同时存在才模拟建立时钟；另覆盖 hold/trip、新语音/来源占用，以及检查后出现蓝牙的竞态。

本轮蓝牙、蓝牙中语音、来源切换、低频单元和持续运行尚未验收；语音监督器单实例约束也尚未修复。没有整机重启、固件分区或未知寄存器写入。设备配置、维护证书、API key、回滚备份与完整日志不进入仓库。

## English summary

User-present Mac Music AirPlay tests passed for audible startup, both master and per-speaker mute, pause/resume, disconnect/reconnect and physical volume/playback controls. No pops or gain jumps were reported in those tests. The first voice test had a clear reply but no music resume. After the handoff repair, a question and follow-up completed with music automatically restored at normal volume and no reported pop; the receiver verified the active AirPlay lease and DSP-owned running PCM. Bluetooth, other source handoffs, bass hardware and sustained operation remain pending.

Rebuilding the Music AirPlay session restored working volume commands and audible mute. The earlier session was selected but inactive; that observation does not establish an Apple internal root cause. No receiver identity or volume protocol code changed. The existing DSP pipe and flat software range −30…−6 dB remain intact; protocol −144 is mute.

The failed voice handoff left Music playing and Shairport emitting frames, with no receiver fault or maintenance hold, but no playback lease and a closed PCM. The old watcher waited for a running PCM before invoking the hook, while the DSP bridge required the hook's claim and lease before opening its clock. The repair bootstraps the silent clock through the existing guarded hook and verifies the resulting AirPlay lease, PCM owner/state and speaker switch. It preserves faults and new voice/source ownership, rejects Bluetooth takeover under the speaker lock, and uses a process-identity lock that can recover dead owners. Reopening a paused session restores its silent path without sending play; only the interrupted-playback caller requests playback. The four-file guarded transaction committed successfully, exact deployed hashes and the active 5.6/DSP/protection stack were verified, and the subsequent voice listening test passed. No normal-playback deadline was added.

Volume control later failed again after maintenance, so Bluetooth testing was deferred. Music reported selected=true but active=false, and master mute did not update the receiver. Restarting only the receiver with detailed diagnostics and testing without a wake still reproduced the failure. Keeping that receiver process unchanged and normally reopening Music restored actual per-speaker mute: the receiver applied −144 and Music reported active=true. This confirms the sender-session workaround, not an established Apple internal cause or a new receiver protocol fix. Maintenance acceptance must check audible mute rather than relying on selection alone.

The subsequent combined test passed: a voice reply automatically returned to music, the per-speaker slider still produced audible mute, and no pop was reported. The receiver verified the restored DSP path and a new −144 mute state. Detailed diagnostics were then disabled and the final sender session rebuilt; no volume-curve or protocol-binary change was made for this session workaround.

The final normal-logging configuration also passed playback, volume adjustment and no-pop listening checks. The receiver applied multiple volume values and −144, retained an active AirPlay lease and a DSP-owned running PCM, and passed exact verification without a fault latch or playback deadline. Temporary test/install files were privately archived and removed; persistent rollback transactions remain. Bluetooth testing was deferred at the user's request.

Additional fixes make daemon log descriptors append across inode-preserving rotation and propagate the admitted 8 MiB raw-free floor into the nested runtime. Outer 8/48 MiB startup and 4/32 MiB runtime protection remain. All 117 Linux/BusyBox tests pass, including a gated-clock handoff reproduction and fault/source ownership regressions. Detailed protocol logs and all device configuration, secrets and backups remain private. Voice supervisor singleton enforcement is still outstanding; no reboot, firmware partition or unknown hardware-register writes were performed.


## 蓝牙后续验收与 XRUN 修正（2026-11-15）

用户后续确认蓝牙音乐正常出声、无爆音或突然静音；手机音量归零/恢复、暂停约三秒/恢复均正常；蓝牙音乐中语音回复清楚，音乐正常恢复，无爆音。启动阶段另修复了 watchdog 在静音 priming 期间误判前一来源模拟增益的竞态；活动播放仍须匹配来源的模拟基线。

切换到低频测试页面时，watchdog 在第一段 WAV 请求前已因原厂 PCM 进入 XRUN 而锁存保护静音。500 Hz 参考和所有低频均无声，不能据此诊断低音单元。原厂播放器可在暂停时保留 XRUN PCM，状态接口还可能保留 play；后续手动播放本来会重启 PCM。

本次 shell 修正让可信蓝牙 PCM 停止先立即静音，再通过 bridge 的串行 stop 释放旧租约；必须有新的手动播放和有效 RUNNING PCM，才能建立新租约并执行完整开声保护。暂停标记阻止依据过期 play 状态自动 pause/resume。错误 owner、桥退出/心跳失效、未知状态及维护 hold/trip 的保护保留。未修改 C 二进制、原厂进程、DSP 配置和 AirPlay −30…−6 dB 曲线。

完整 **125 项 Linux/BusyBox 测试通过**，测试日志 SHA-256 为 `15b2a0e1a718755e1f923cb12a7f4e3cb73ee065cdb2ee74e8f95e3babbea089`。覆盖 bridge/watchdog 先后发现 XRUN、暂停期间过期 play 不触发续播、新手动 PCM 取得新租约、错误 RUNNING/XRUN owner 故障锁存，以及确认断开后新连接的初始修复。shell 语法及 diff 检查通过。

四脚本受控更新事务已提交，更新返回 0、pending 清除、设备精确验证返回 0，四个 SHA-256 与候选完全相符。Shairport 5.6、watchdog、蓝牙桥和端口 7000 正常。安装后的 PCM 为原厂播放器持有的 XRUN，无租约；维护 hold/manual mute 仍有效，硬件静音。保存的蓝牙音量为 72/100，需先由用户调低并保持暂停，再解除已核验的维护静音。**本次 XRUN 恢复听测仍待完成**；还不能报告低音单元、来源切换或长时间运行通过。

## Bluetooth follow-up and XRUN repair (2026-11-15)

The user confirmed audible Bluetooth startup without pops or unexpected mute, effective phone mute/restore, normal pause/resume, and clear voice replies with music restored without pops. A separate startup repair allows only known previous analogue steps during digitally muted priming; active playback still requires the exact source baseline.

Switching to the listening page then exposed a persistent watchdog mute before the first WAV request: the stock player retained an XRUN PCM. Even the 500 Hz reference was inaudible, so this result cannot diagnose the woofer. The stock status may remain play while the phone is paused; manually starting audio can restart its PCM.

The shell repair immediately mutes a trusted Bluetooth clock stop, releases the old lease through the serialized bridge stop, and waits for a fresh manually running PCM before acquiring a new guarded lease. A pause marker prevents automatic transport resume based on stale play metadata. Wrong owners, stale/dead bridges, unknown PCM states and maintenance/fault latches retain protection. No C binary, stock process, DSP configuration or AirPlay −30…−6 dB curve changed.

All **125 Linux/BusyBox tests pass**, including both watchdog/bridge ordering cases, stale play while paused, manual PCM recovery, wrong running/stopped owners and confirmed disconnect/reconnect. The controlled four-script update committed with return code 0, no pending transaction, exact verification 0 and matching hashes. Shairport 5.6, watchdog, Bluetooth bridge and port 7000 were healthy. Maintenance mute remains active pending a user-confirmed low phone volume; XRUN recovery listening, source switching, bass hardware and sustained operation are still pending.


### 参考音恢复复测

用户调低手机音量并暂停后，设备仍保存 72/100；在维护静音下通过原厂接口同步至 12/100，回读确认 stop/12。持有扬声器锁、验证无租约/无语音占用/可信停止 PCM 后，移除维护 hold，确认 watchdog 心跳推进，再仅清除对应的 manual mute。未主动播放或设置定时静音。

用户手动重试后确认：**两次都能听到参考音，无啪声**。日志记录两次建立新蓝牙租约并打开输出；播放结束后的 XRUN 正常释放旧租约，随后新 PCM 可再建立新租约。watchdog 的正常停止恢复分支亦被实际触发。结束后无租约、tripped=no，保留正常空闲硬静音；没有再次形成持续故障静音。另一次在开场结束前停止的短流被识别为 source stopped during start，正常关闭，未锁存故障。这些结果验证本次恢复路径，尚不验证低音单元、长测或真实来源切换。

English: After the user lowered phone volume and paused, the device still retained 72/100. With maintenance mute active, the official command set and confirmed stop/12. The speaker lock protected verification of no lease, no voice owner and a trusted stopped PCM; the maintenance hold and only its exact manual-mute latch were cleared after watchdog heartbeat progress. No sound was started by the agent and no playback timeout was added. The user then confirmed **two audible reference replays without pops**. Device logs showed fresh leases, recoverable XRUN release and later PCM acquisition without a persistent fault. A further short stream stopped during priming closed normally without latching. Idle output was physically muted with no lease and tripped=no. Bass hardware, sustained operation and real source handoff remain pending.


### 低频序列试听

用户在同一蓝牙路径手动播放 500、200、120、80、60 Hz 序列，确认各段均能听到，低频有振动。文件为同相立体声 PCM16/48 kHz，每段 1.5 秒、100 ms 渐入渐出、峰值 −30 dBFS，开头保留 8 秒静音供保护流程建立时钟。相同数字峰值不代表各频段声压或主观响度相同，本轮没有校准声压测量。

后续设备检查无租约、tripped=no、正常空闲硬静音；原厂播放器映射了 Harman DSP 与蓝牙重定向库。该结果确认有低频输出，不能独立证明具体低音单元、接线或功放完全健康，也不能代替熟悉音乐的听感比较。来源切换及长测仍待验证。

English: The user manually played all 500/200/120/80/60 Hz segments over Bluetooth, heard each one and felt low-frequency vibration. Same-phase stereo PCM16/48 kHz files use 1.5-second tones with 100 ms fades, −30 dBFS peaks and an eight-second silent lead. Equal digital peaks do not imply equal sound pressure or perceived loudness; no calibrated acoustic measurement was performed. The receiver ended with no lease or fault latch and normal idle physical mute, and the stock player mapped Harman DSP and the Bluetooth redirect library. This confirms low-frequency output without independently certifying a specific driver, amplifier or wiring. Familiar-music comparison, source handoff and sustained operation remain pending.


熟悉音乐对比：用户随后用蓝牙重听《可以了》前奏或熟悉音乐，反馈相对最初“几乎没有低频”的状态**明显改善，听感正常**。这项主观试听通过；未做校准声学测试或逐单元电测。

Familiar-music comparison: the user subsequently reported that Bluetooth bass was **clearly improved and sounded normal** compared with the original near-absent bass. This subjective listening check passed; no calibrated acoustic or individual-driver electrical test was performed.


## 2026-11-16：来源切换失败与新后端静音部署

用户随后测试“手机蓝牙暂停且保持连接 → Mac AirPlay 播放”，反馈无声。原厂 PCM 仍由蓝牙线程持有，AirPlay 的新租约无法建立 DSP 时钟；这轮是已复现的接收器资源交接问题。保护静音后暂停了无声的 Mac 会话。原厂 stop / switchmode:wifi 命令未关闭 PCM，因此没有把成功响应当成交接完成。

新增可释放底层 DSP/hardware slave 的私有蓝牙 ALSA ioplug，保持原厂虚拟句柄、手机音量、Harman DSP 和蓝牙同 PCM 语音混音。共享 hook 在创建新租约前静音、释放并验证硬件 closed，语音独立输出亦遵守该步骤；来源意图与打开中标记防止并发争抢。旧 voice 标记按五分钟有效期处理，控制 socket 经 run 重建仍可用。

最终 **136 项 Linux/BusyBox 测试通过**，测试日志 SHA-256：`8455f12c6e25d92993c340a1b34d0d5aa3e69692841aa4d48a5ebdba20f33b37`。ARM 原厂 ALSA 空输出循环返回 0：同一虚拟 PCM 完成打开、写全零、park、占用期间丢弃、重新打开和关闭；真实声卡未被该检查打开。插件固定兼容原厂的 ioplug 1.0.1，最大 GLIBC 需求 2.17。一次测试失败来自模拟时钟文件直接截断写导致并发读到空值；改为与生产 heartbeat 一致的原子 rename 后，完整回归通过。

九文件更新事务成功提交，返回 0、pending 清除、九文件 SHA-256 全部相符、运行时精确验证返回 0。新后端 SHA-256 为 `271f2c304eb52337f0143ac9eea44ce31984ec06210e2e9b7ffec98fa0d7cbd8`。受控重载播放器和受影响自定义模块后，rootApp 未重启；AirPlay 5.6、蓝牙桥、独立保护及语音监督/主循环各正常运行，端口 7000 监听。PCM closed，无租约，Master/Digital 0、Ext Spk off、I2S mute on，安装维护 hold/manual mute 保留。AirPlay 配置逐字节保留为 −30…−6 dB。私有备份、配置和完整日志未进入仓库。

**本次尚未对新后端进行真实蓝牙、双向来源切换、语音或长时间听测。** 此前参考音、低频与熟悉音乐通过的结果属于上一版通路，不能替代本次回归。

English: The subsequent user-present Bluetooth-paused/still-connected → Mac AirPlay test was silent: the stock player retained the exclusive PCM, preventing the new AirPlay lease from establishing its DSP clock. Supported stop/source-switch commands did not close the handle on this device. The new private ALSA ioplug keeps the stock virtual handle while releasing its DSP/hardware slave through a mute-first guarded handoff. It preserves the existing volume/DSP/mixing path, discards blocked samples, and uses source intent plus an in-flight-open marker to avoid races. Control survives runtime-directory reconstruction. All 136 Linux/BusyBox tests and the stock-ALSA null-only open/park/reopen cycle passed. The nine-file update committed with return code 0, no pending transaction, exact hashes and runtime verification. The player was reloaded under maintenance mute; rootApp stayed running, and AP2/bridge/watchdog/voice were healthy with PCM closed. AirPlay configuration was preserved byte-for-byte. Audible Bluetooth, bidirectional handoff, voice and sustained tests on this backend remain pending; earlier bass/music acceptance does not substitute for them.


## 2026-11-16：MMAP 协商失败复现与修正版

首版可释放后端的真实蓝牙试听未通过，用户反馈无声。设备没有故障锁存，播放器显示 play，但始终没有硬件 PCM。恢复维护静音后，在原厂库上复现 Master softvol → 虚拟后端 → null：OPEN=0，PARAMS=−22（Broken configuration）。同样的软件层访问方式在主机真实 ALSA route/null 回归中复现；直接 writei/null 的早期成功未覆盖外层软件音量层。

原因是 softvol 要求 MMAP slave，而后端只提供了 RW_INTERLEAVED。修正版提供 RW_INTERLEAVED + MMAP_INTERLEAVED，保持 mmap_rw=0，使普通写入与 MMAP commit 均转交采样。新增两个实际数据流回归，覆盖软件层协商、MMAP 采样及 park/重新打开后无补播。

完整 **138 项 Linux/BusyBox 测试通过**，日志 SHA-256：`01e746b34aa1e3938605d350927829f39d2ea701f165cdccb4068a57e73bbe6b`。一次整套测试仅在收尾时遭遇孤立的模拟 mixer 子进程重建临时文件；调整为移除 armed 后等待 watchdog 正常结束，并提供进程组超时退出，未改变生产逻辑。

原厂完整软件音量链空输出复测返回 0：48 kHz/双声道/S16、period=6000、buffer=24000，首包 first=0、step=32；同一 PCM 完成写入、park、占用期间丢弃、重新打开与关闭。实际硬件 PCM 始终 closed，没有主动出声。修正版后端 SHA-256：`600f62501c79f33ecb4be9171eae03d81704a42a5dd32337d620d8890a5afd78`。五文件更新事务已提交，返回 0、无 pending、全部哈希匹配，运行时精确验证 0。维护 hold/manual mute 继续保留，声卡 closed；rootApp 保持运行，AirPlay 5.6、蓝牙桥、独立保护、语音监督与主循环正常。

**尚未进行 MMAP 修正版的真实蓝牙、双向来源切换、语音与长测；不得将首版无声或先前版本的听测改写为本版通过。**

English: The first live Bluetooth test on the releasable backend was silent with no fault latch and no hardware PCM. The complete stock Master-softvol/virtual/null path then reproduced OPEN=0 and PARAMS=−22; an outer software-layer regression reproduced it on native ALSA. Softvol requires an mmap slave, which the first version did not advertise. The fix supports interleaved RW and mmap, leaving mmap_rw=0 so samples reach transfer in both paths. All 138 Linux/BusyBox tests and the complete stock software-volume/null park/reopen cycle now pass. The test cleanup also waits for foreground mixer children rather than leaving writers behind after terminating their shell. The five-file update committed with exact hashes and runtime verification, retaining maintenance mute and an idle closed PCM. RootApp and the audio/voice protection stack are healthy. Audible Bluetooth, bidirectional source switching, voice and sustained checks on this mmap fix remain pending.


## 2026-11-16：真实 DSP 参数与 park/重新打开恢复

MMAP 修正版的下一次用户试听仍无声。现场显示原厂 status=play、手机采样已进入后端，但硬件 PCM closed、无租约或故障锁存；后端只记录释放，不记录成功打开。用户暂停后保持维护 hold/manual mute，用全零数据检查真实 DSP，复现 44.1 kHz period 参数 −22，48 kHz 正常。固定 DSP 48 kHz 的 plug 必须放在虚拟后端之前；放在内部会出现持续写入卡住。

完整零数据循环还复现满缓冲整圈指针别名与重新打开预填死锁，分别通过分段报告 modulo 进度和半缓冲启动修正。两项独立故障对照均能使新增精确满缓冲回归失败；修正后 **141 项 Linux/BusyBox 全套通过**，日志 SHA-256：`5c034f5983c22612fcbfe728cb65cbbd7f02df36ef51c80b69ff9d1db09035fc`。按真实时钟推进的测试 sink 与重采样样本计数共同覆盖了短 null 检查遗漏的情况。

原厂完整 Master softvol → plug(48 kHz) → 虚拟后端 → Harman DSP → 硬件，在 44.1/48 kHz 输入下各完成：61,440 帧零数据、实际 RUNNING、park 返回 0 且硬件 closed、占用期间接受并丢弃 61,440 帧、解除私有测试占用后再次写入 61,440 帧并 RUNNING、close 返回 0 且硬件 closed。两个循环均返回 0；全程没有解除真实维护静音。后端 SHA-256 为 `256250eeccb8986c9c87406f7e3a7d0fca351c42c404c3db5e7fac6a549be02c`，最大 GLIBC 需求仍为 2.17。

六文件静音更新事务 `20261116T051402-19482` 已提交，返回 0、无 pending、六文件哈希相符、精确验证返回 0。AirPlay 配置 SHA-1 保持 `1a4b6e188135e61ae99e32c3ed4578d5c7404bc1`，范围仍为 −30…−6 dB。rootApp 始终为 PID 139；AirPlay 5.6、独立保护、蓝牙桥及各一个语音监督/主循环正常，安装 hold/manual mute 保留、硬件 PCM closed。真实蓝牙、双向来源切换、语音及长测仍待本版试听；本节不将零数据验证描述为听觉验收。

English: The next user-present mmap-version test was still silent. Samples reached the backend, but real DSP parameter setup failed at 44.1 kHz; no output lease or fault latch existed. Muted probes reproduced −22 and verified 48 kHz. Moving conversion before the virtual PCM keeps its accounting at the hardware rate. Full-ring park/reopen probes then exposed separate modulo-pointer and startup-prefill deadlocks. Both negative controls fail the new regression; all 141 Linux/BusyBox tests pass with the fixes. The full stock Master/plug/virtual/Harman-DSP/hardware chain passed zero-only write, park, blocked discard, reopen and close at both input rates, with actual RUNNING and closed states verified. The six-file muted transaction committed with return code 0, no pending transaction, matching hashes and exact verification 0. The AirPlay configuration is byte-for-byte preserved; rootApp remains unchanged, and the playback/voice/protection stack is healthy under maintenance mute. Audible Bluetooth, bidirectional handoff, voice regression and sustained operation still await this version's listening tests.


## 2026-11-16：实际播放无租约与 FD 持有者修正

用户再次确认完全无声。与之前参数失败不同，这次 PCM 已持续 RUNNING，48 kHz/S16、DMA 指针前进，但输出硬静音、无租约也无故障锁存。记录的打开线程已经退出；原厂播放器仍持有真实播放设备 FD，已加载正确的后端和 Harman DSP。旧归属检查只读取打开线程的 comm，导致桥接拒绝申请输出。恢复维护静音、用户暂停后，在锁内通过已有后端控制接口 park，确认实际声卡 closed。

新后端原子发布并撤销实际持有者身份/启动时间/FD/代次；共享消费者复核真实设备句柄，不再依赖过期或被复用的 TID。实际 stock-ALSA/Master/48 kHz 适配/虚拟后端/Harman DSP 的 44.1、48 kHz 全零播放 → park → 占用期间丢弃 → 重新打开 → close 均返回 0，证明记录在关闭后撤销，全程维护静音。

完整 **150 项 Linux/BusyBox 测试通过**，日志 SHA-256：`4bd16d06e751fd24503a287fe64f3e2e28e1ca7309dba24e6035471995f5209c`。后端 SHA-256：`003513ff5222822d91db97dcf06fadbd458fd181f5a5f83bd7eb9fd0da093b72`，最大 GLIBC 需求仍为 2.17。九文件事务 `20261116T054126-31180` 已提交，返回 0、无 pending、九文件哈希一致、运行时精确验证返回 0。原厂播放器和语音依赖已重载，rootApp 保持 PID 139，各一个语音监督/主循环正常；AirPlay 配置逐字节保持原样。安装核验时声卡 closed、维护静音保留，随后在用户在场时解除安装 hold 等待手动播放。本版本的实际蓝牙出声、双向交接、语音和长测尚未验收。

English: The user again heard no Bluetooth music. This time real DSP playback and DMA were healthy, but no output lease existed: the recorded opener thread had died while the stock process retained the PCM FD. The obsolete thread-name checks rejected it. Under maintenance mute after the user paused, the existing guarded IPC parked and released the hardware. The fix publishes actual process/start-time/FD/generation proof and shares strict revalidation across Bluetooth consumers. All 150 tests and both complete real-DSP zero-only park/discard/reopen cycles pass; the proof is removed on close. The nine-file muted update committed with return code 0, matching hashes, no pending transaction and exact verification. Bluetooth and voice dependents reloaded; rootApp is unchanged, one voice supervisor/loop is healthy, and AirPlay configuration is byte-for-byte preserved. Installation hold was then released for user-initiated listening. Audible Bluetooth and subsequent handoff/voice/sustained checks remain pending.


## 2026-11-16：短写入保真修正与仍然无声的实机结果

FD 归属修正后，用户能听到蓝牙，但报告持续吱声和爆音；该次试听未通过。用户暂停并断开后，恢复维护 hold/manual mute，guarded park 确认声卡关闭。

原厂 ARM ALSA 文件输出探针按真实调用地址模拟原厂单次非阻塞写入，旧版在 180,000 帧中丢弃 90,000 帧。入口补写只解决 48 kHz；44.1 kHz 的 rate/softvol 还会在下游短 MMAP commit 后消耗输入。因此入口和 backend callback 都补齐整包；语音只混一次，永久背压有界且可被新来源取消，活动 XRUN 先请求静音与租约释放。

160 项完整回归通过，测试日志 SHA-256 为 `a2e63ae5e0c81948d445cd622eacaa0fc694763661f324c346b4abd043571bf4`。最终 ARM file/null 对照在相同非 drain 关闭下逐字节匹配直接 ALSA：48 kHz 为 720,000 字节，44.1 kHz 为 768,000 字节。后续真实 Harman DSP 全零 write/park/discard/reopen/close 两种输入采样率均通过，维护静音始终有效。这些结果没有证明实际音乐已正常播放。

六文件事务 `20261116T111155-11146` 提交成功，返回 0、pending 清除、哈希和精确版本核验均通过。解除维护静音并由用户手动播放后，用户反馈**完全无声**。现场手机音量 20/100、Master=6、Digital Playback Volume=207,207，Ext Spk/Digital 打开、I2S mute 关闭；active 蓝牙租约有效，PCM 持续 RUNNING 且 DMA 前进，无故障锁存。保留现场后恢复维护静音，用户暂停；没有自动增大增益，也没有把它当成 watchdog 误静音。

新增按需诊断以 `bt-pcm-diagnostics` 标记启用，分别记录播放器输入与 Master 后的帧数、非零采样数和峰值，默认关闭，不保存音乐内容。162 项完整 Linux/BusyBox 测试通过，日志 SHA-256 为 `4879484b259dedfa5554df90774e986d8809c7ae261380f7e36557e109baa591`；新诊断版本的 ARM 文件对照继续匹配。事务 `20261116T141746-11520` 已提交，返回 0、无 pending、六文件哈希与精确核验一致。rootApp 保持 PID 139，AirPlay 配置逐字节保留。安装后开启诊断标记，仍保持维护静音，等待用户重新连接并手动开始新一轮播放。无声原因、双向来源切换、语音回归和长测仍未验收。

English: The live holder-identity revision was audible but buzzed and popped. Stock ARM ALSA file-only probes reproduced 90,000 dropped frames out of 180,000. Both the verified player entry and backend callback now complete short writes; outer-only completion still lost 44.1 kHz rate output. Voice mixes once, backpressure is bounded/cancellable, and an active XRUN requests muted lease release before recovery. All 160 tests passed, both input rates matched direct ALSA file references with the same non-draining close, and muted full Harman DSP zero-only park/reopen cycles passed. The six-file update committed and exactly verified, yet the next user-initiated test was completely silent with a valid Bluetooth lease, running DMA and enabled output switches at phone volume 20/100 and Master=6. Maintenance mute was restored without increasing gain or blaming the watchdog. Opt-in pre/post-Master peak and frame diagnostics were then added without storing audio. All 162 tests and final ARM file references pass. The diagnostic transaction committed with matching hashes, no pending journal and exact verification; rootApp and AirPlay configuration are preserved. Diagnostics are enabled under maintenance mute while waiting for a new user-initiated stream. Silence and subsequent handoff/voice/sustained acceptance remain unresolved.


### 补充：实际音乐电平及 DSP 后段测量

按需诊断读到播放器入口峰值约 12,000，Master 后峰值约 100（约 −50 dBFS）；用户在手机上缓慢调高后，后者约 200–250，仍反馈完全无声。输入和 Master 后均持续非零，不能再归因于没有输入数据或仅凭 PCM RUNNING 判为成功。没有由维护程序自动增加增益。

外部只读映射探针未能取得 DMA 缓冲区数据，不能将读取失败解释为零信号。后端增加受诊断标记控制的 `levels` 查询，只扫描自身已映射的 PCM 数据页，返回峰值和非零计数；不读取寄存器、不修改缓冲区或音量。原生回归验证关闭诊断和缺少数据映射时拒绝查询，实际共享映射的电平正确，且读取后采样不变。

163 项完整 Linux/BusyBox 回归通过，日志 SHA-256 为 `c95655ba7eb52cf91459af5d680521c8fb3683a74c58a3cad8fcd91e10e26757`。六文件后段诊断事务 `20261116T143418-14865` 已提交，返回 0、无 pending、所有哈希匹配、精确版本验证 0。维护静音保留，待手机重新连接后手动播放并查询 DSP 后段；目前没有取得真实音乐的后段电平，也没有完成无声修复。

维护静音下还比较了只打开、设置参数、关闭且完全不写采样的 DSP 调用：S16/48 kHz 在有无原厂 client11 标记时均成功，硬件 period64/buffer24000；直接 S32 设置参数返回 −22。此结果只能排除 client11 单独改变本次硬件 period 的猜测，不能代替经 plug 的 AirPlay 路径，也不能证明 S16 音频处理正常。

English: Live diagnostics found nonzero music at both entry and post-Master: peaks around 12,000 and 100 initially, rising to roughly 200–250 after the user gradually increased phone volume. The user still heard nothing; the maintenance tool did not raise gain. External read-only DMA observation failed and produced no output-level evidence. A gated `levels` query now reads only the backend's own mapped PCM data and returns aggregate peaks/nonzero counts without changing samples, gains or registers. Native tests verify refusal when disabled/unmapped and correct, nonmutating reads of a real shared mapping. All 163 tests pass. The six-file output-diagnostic transaction committed with return code 0, matching hashes, no pending transaction and exact verification. It remains under maintenance mute pending a new user-started stream; silence is unresolved. Separate no-sample parameter-only probes accepted S16/48 kHz with and without client11 and identical hardware periods; direct S32 parameters failed. These results neither test the plug-based AirPlay route nor establish audible fidelity.

本轮完整回归日志 SHA-256：`457f4550b527ead5f6a9abb833b7bc7ccc58a3128124cd8c6234925c66b5644a`；恢复标记最终位置复核 SHA-256：`b6c2754cca8ff18b79c5de6f2abea536be70db024b2a455627b8a53ba48ec95e`。
