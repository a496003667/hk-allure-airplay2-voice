# 试听与语音续播记录 / Listening and voice handoff — 2026-11-15

本记录接续 [11 月 14 日部署记录](DEVICE-VALIDATION-2026-11-14.md)。结论只适用于作者这台音箱及本轮 Mac“音乐”AirPlay 测试。

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
