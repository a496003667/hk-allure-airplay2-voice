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
