# 审计修复状态 / Audit repair status

基线：`741ce58`。修复分支：`fix/audit-20261114`。**仅离线开发，没有连接、部署、重启或播放音箱。** 新代码不能视为实机验收通过。

原审计 12 项（5 P1、7 P2）的代码与回归对应：

| 审计项 | 离线候选改动 | 验证证据 / 剩余验收 |
|---|---|---|
| F01 fork 失败误杀 | 所有 fork 失败有明确分支；signal/wait 只接受成功创建的正 PID | 原函数注入 fork=-1，确认无信号/等待并走单路识别 |
| F02 独立语音无租约 | reserve → guard → start；voice 独立 owner、PID 启动时间及轮次；提醒播放完成才 ack | 实际 hook 空闲语音开关、旧轮 stop、提醒 peek/ack；实机独立播报待验收 |
| F03 AP2 回收误关蓝牙 | 资源保护器保留有效独立租约；回收完整 AP2 服务含 broker；共享监督先于恢复判断 | 实际 trip 函数与蓝牙租约集成；实机回收/恢复待验收 |
| F04 关时钟早于静音 | 主控/运行时/voice 清理先静音再停 PCM；watchdog 不切模拟增益；hold/trip 阻止新开声但保留已有静音时钟到租约释放 | mixer 事件顺序、真实 cleanup、DSP 时钟策略；实际“啪”声仍需人耳验证 |
| F05 更新失败误报成功 | 事务锁、整批备份、权限/哈希/路径/空间校验、精确 5.6 活动身份、完整回滚与持久 journal；相依模块重载 | 候选/备份/权限/空间/回滚失败注入；实机更新和旧原厂播放器重载待验收 |
| F06 IPC 假超时 | raw ARM 工具与语音共用单调总期限、短写处理、响应上限；失败不暴露残缺状态 | native 与 ARM 模拟器真实 socket：慢回复、不 EOF、超长回复、大请求与错误参数 |
| F07 漏判 AirPlay/错误增益 | 按列识别 IPv4/IPv6 本地 7000 ESTABLISHED；读取原子 volume.state 的实际衰减；BT 混音避免重复 Master 衰减 | 原生编译真实语音 C，端口真假、音量端点、静音与异常状态；声压匹配待听测 |
| F08 蓝牙指令发到 AP2 | 源相关派发；明确暂停真实执行并撤销自动恢复，暂停状态不切回播放 | 执行真实 dispatch 函数：BT next/prev/pause 与 AP2 next；原厂命令效果待验收 |
| F09 abort 协议不一致 | q.abort 优先于排队语音，播放中可中断；队列/prepare/action 按轮次隔离；shim 有 abort/aborted 握手 | 旧音频已排队时中断优先、旧轮 stop 不动新租约；实机追问待验收 |
| F10 disable 漂移 | AP2 持久 disable 保留共享保护；灯效/按键运行时检查 vendor flag；保留临时 flag 兼容 | 按来源转发/持久禁用夹具，源码与双语语义表；重启语义待实机验收 |
| F11 日志增长/旧 inode | 日志按大小轮转并保留 writer inode；原子 owner lock 可回收；volume.state 不扫描历史 | 持续打开的 writer、旧 rotator 死亡恢复；实机内存/日志趋势长测待验收 |
| F12 安装不可复现 | 补 shim/入口源码、构建器、依赖锁、cramfs 打包、权限清单、设备 pin 生成及受控重载 | 空目录发布夹具、真实候选包校验与 ARM 产物；依赖树需自行准备，首次新机安装未验收 |

## 本轮验证

Linux/BusyBox 完整 **99 项通过**；当前 ARM raw IPC 的 **6 项 socket 用例通过**（同组另重跑 1 项本机 fork 失败用例）。12 个 ARM 产物的 ABI/GLIBC 检查通过，30 文件最小候选包验证通过，基础 cramfs 重复打包两次哈希一致。所有 shell 语法通过。具体哈希和验证边界见 [OFFLINE-VALIDATION.json](OFFLINE-VALIDATION.json)。基线的 54 项通过不再作为新增修复的唯一证据。

- 没有访问音箱或调用实际麦克风/扬声器；所有 audio 控件是夹具，媒体输出仅为本机文件或 `/dev/null`。
- ARM 模拟器运行新 Shairport `--version` 正常。配置显示已解析 24 dB 范围，但 `-X` 随后退出为 139；旧基线 `b6c5263c…` 在相同环境同样崩溃。因此不宣称常驻启动已通过模拟验证。
- 当前候选 AirPlay 范围是软件衰减 −30 至 −6 dB；实际声压还受 DSP、DAC、功放和单元影响。
- 默认蓝牙语音是同 PCM 将音乐压至静音，手机进度可能继续；`HK_VOICE_BT_MODE=pause` 的声卡释放路径待实机确认。

## 晚间设备验收顺序

1. 只读核对维护入口、原版本、内存、当前源、hold/trip 和静音；保存私有备份并核对空间。
2. 使用最小候选更新，不替换基础 cramfs/内核/固件；执行精确版本验证、依赖重载和静音状态检查。
3. 用户在场先低音量试听 AirPlay，再蓝牙：开始、暂停、恢复、切歌、重连和来源交接，记录是否有爆音或突然静音。
4. 再测试独立语音、蓝牙中唤醒、明确暂停/继续/换曲、提醒与机身按键；核对恢复音量、灯效和进程存活。
5. 同一音乐比较低频；必要时用户手动触发低电平分段测试音。未验证功放/线束/单元前不写“低音硬件已修好”。
6. 条件满足后做 2 小时播放观察和 24 小时运行观察。整机重启/断电单独安排，不自动纳入当晚步骤。

## English summary

All twelve audited findings have corresponding offline candidate changes and regression evidence listed above. The Linux/BusyBox suite passed all 99 tests, with another six ARM IPC tests; 12 ARM artifacts passed compatibility checks, the 30-file candidate was verified, and two runtime packings produced the same hash. Nothing has been deployed to the speaker. Coverage includes process safety, real IPC deadlines, source-aware ownership, shutdown ordering, transactional update/rollback, command dispatch, interruption, disable semantics, bounded inode-preserving log rotation and packaging/pin generation.

The AirPlay candidate maps software attenuation from −30 to −6 dB. Bluetooth voice defaults to in-place PCM mixing with music ducked to silence; an explicit pause really pauses and cancels automatic resume. Physical loudness calibration, complete source/voice handoff, audible pops, bass hardware, dependent stock-process reloads, fresh installation and sustained runtime remain device acceptance work. The Shairport `-X` simulator crash also affects the old baseline and is recorded as a validation limit.
