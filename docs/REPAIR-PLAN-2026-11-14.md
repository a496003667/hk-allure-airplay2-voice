# 2026-11-14 审计修复方案 / Audit repair plan

状态：**离线开发已完成，已部署并通过静音核验；本轮听测、硬件低频和长测待完成**。基线提交：`741ce5848646717f44026634cb978dfd4c915294`；修复分支：`fix/audit-20261114`。本文件保留修复设计，实际结果见[实机记录](DEVICE-VALIDATION-2026-11-14.md)。

## 目标与基线

优先解决可误杀进程、更新失败误报成功、无声、突然静音和交接爆音，再处理语音控制、音量、长期运行与安装复现。修复覆盖审计的 F01–F12；低频单元是否实际工作另行验收，不能以软件路由正确代替结论。

审计基线是 54 项现有测试通过，同时隔离复现了 9 个异常行为。旧复现程序退出 0 表示“缺陷成功复现”，后续须改成断言修复行为的回归测试。

原审计期间设备不可达；后续一次只读连接已成功，设备约连续运行 9.7 天，空闲内存约 8.6 MB，无播放租约，物理输出静音。这是上次检查的快照，部署前须重新读取。尚无本轮修复后的实机听测结论。

保持以下约束：

- AirPlay、蓝牙、语音继续经 Harman DSP，保留原厂分频；不靠增加低频增益掩盖问题。
- AirPlay 最终目标是**最低非静音软件衰减 −30 dB、最高 −6 dB**；蓝牙不恢复原先 30/100 限制。
- 保留可用的灯效、原厂蓝牙按键和独立 mTLS 维护通道；可选进程使用正数 `oom_score_adj`。
- 不写 bootloader、kernel、MTD、OTA，不尝试未知硬件寄存器。密钥、证书、NVRAM、备份和设备专用配置不进入公开仓库。
- 维护时保持静音；不主动播放音乐或测试音。日常长测不加定时静音，正常无租约静音与故障保护保留。

## 修复顺序与交付

第一批进程/IPC 安全修复单独提交；其余音频、更新和校验相互依赖，作为完整候选提交并附回归证据，避免中间版本 pin 不一致。批次是代码与验证的边界，不表示每批都重启音箱；完整候选包准备好后才部署。

| 批次 | 覆盖范围 | 主要改动 | 本地通过标准 |
|---|---|---|---|
| 1：进程与 IPC | F01、F06 | 检查所有 `fork()` 结果；signal/wait 只接受正 PID；Unix socket 使用总期限和完整 I/O | fork 失败无负 PID 信号；连接、写入、慢回复、不关闭连接均受同一总期限限制 |
| 2：安全交付基础 | F05、F12 的安装前置 | 更新锁、候选 manifest、全批备份、事务状态、精确版本与配置验收、可靠回滚 | 逐阶段注入失败均返回非零；回退不冒充更新成功；回退失败保持静音 |
| 3：统一输出生命周期 | F02、F03、F04 | 来源租约、预留与时钟确认、共享开关顺序、来源级回收与全局故障分离 | 独立语音可从空闲开声；回收 AP2 不误关有效蓝牙；所有正常退出先静音再停 PCM |
| 4：语音与控制 | F07、F08、F09；提醒确认机制 | 正确识别 AirPlay；按来源派发指令；统一中断协议和轮次 ID；软件播放完成后确认提醒 | BT 指令不发给 AP2；暂停不被自动续播覆盖；旧轮回复不能写入新队列 |
| 5：音量映射 | 音量需求差距、F07 增益消费端、按键同步 | 实现 AP2 的 −30 至 −6 dB 映射；来源音量适配；语音与音乐恢复使用同一轮有效状态 | 端点、全静音、步进、重连、按键及来源切换均有数值测试；无恢复满音量路径 |
| 6：长期运行与交付收敛 | F10、F11、F12 其余部分 | disable 语义、有界日志、最后音量状态、可重复构建、权限及双语文档 | 禁用立即及重启生效；持续写日志不遗留增长 inode；空目录打包安装验证通过 |

## 关键设计

### 1. 进程安全与有界请求

`asr_best()`、prepare hook、流式回答等每个 fork 点分别定义失败返回和清理责任。双 ASR 分支 fork 失败时退到可用的单路识别，不能传递负 PID；不依靠“这个可选功能目前没开”规避修复。

原始 ARM IPC 工具与语音 C 客户端使用同一超时语义：单调时钟总期限，覆盖 connect、全部 request 写入和全部 response 读取。解析现有 `1200` 毫秒参数，处理短写、`EINTR`、`EAGAIN`，限制响应大小；超时或截断回复返回明确失败。服务持续发送少量字节也不能无限延长期限。IPC 失败不得使用残缺状态恢复扬声器或音量。

测试用本机 socket 夹具运行真实 I/O 代码，并对 ARM 产物做构建与 ABI 检查；设备上再做不触碰真实声卡的超时验证。

### 2. 单一租约与输出时序

沿用共享 audio hook 和独立 watchdog，不为此新增常驻大服务。共享状态至少明确：当前来源、owner PID 与进程启动标识、轮次、租约状态、单调时间心跳、静音/故障原因。

输出生命周期：

```text
IDLE（物理静音）
  → RESERVED（独占预留，禁止开声）
  → PRIMING（watchdog 确认，建立静音 PCM 时钟）
  → ACTIVE（guard 核对 owner、时钟与静音基线后渐入）
  → DRAINING（时钟继续，数字淡出，关闭物理输出）
  → IDLE（停止 PCM，再释放租约）

任一状态 → HOLD / FAULT（阻止开声，记录原因，按策略恢复）
```

预留必须有短期限，不能让挂死的 prepare 永久占住声卡。独立语音有自己的受监督租约，不依赖 Shairport 存活；蓝牙 PCM 内语音混入是原蓝牙租约的子轮次，不夺取蓝牙归属。

锁覆盖状态转换和相关硬件操作，不能只保护标记文件。锁要核对 owner 身份，不能仅等待超时就抢走仍有效的锁。退出或异常清理必须核对轮次，防止旧 stop 删除新来源的租约。watchdog 的紧急静音不应被挂死的普通锁或 IPC 阻塞。

正常开声统一为：输出预留 → 数字静音 → 静音时钟与路由确认 → guard → 一次性开启原厂功放与物理输出 → 等待稳定 → 数字渐入。正常关闭统一为：数字淡出 → 保持时钟等待衰减 → 物理输出静音 → 停止 PCM/DSP → 释放租约。重复 stop/mute 应幂等，避免反复切换模拟增益。

紧急静音单独实现：监督丢失、来源身份不符、时钟异常时优先关闭输出，不依赖云端或原厂 IPC。若时钟已经丢失，不能承诺与正常关闭同样无爆音，需分别记录和验收。

### 3. 回收 AP2 与全局故障分离

资源保护器只能回收自己负责的 AP2 进程。Shairport 退出、AP2 重启或 AP2 级低内存回收，不能删除仍有效的蓝牙租约、停共享 DSP 或关闭蓝牙输出。

真正影响共享输出安全的故障才进入全局 FAULT，并记录来源与原因。有效 owner 的判断须核对进程身份、PCM owner 和心跳，不能仅判断文件存在。恢复 AP2 有退避和内存余量检查；蓝牙正在使用共享资源时，恢复不能抢声卡。故障恢复不自动清除用户手动静音。

### 4. 语音、提醒与按键

按列解析 `/proc/net/tcp` 和 `tcp6` 的本地端口、连接状态，排除“远端恰好 7000”等误判。连接不等于正在播放；最终来源还应参考受监督租约和 PCM 状态。

在唤醒时记录来源、用户播放意图及本轮是否有恢复责任。AirPlay 指令通过其现有控制接口；蓝牙使用经核实的原厂接口，并回读状态。暂停、继续、上一首、下一首、音量键均通过当前来源适配器；用户手动暂停或语音要求暂停后，不得在回答结束时擅自续播。

离线实现采用已知声卡约束：原厂蓝牙暂停后仍可能占有独占 PCM，因此默认保留同 PCM 混音，唤醒时将背景音乐平滑压至静音；手机上的曲目进度可能继续，文档不能将它称作暂停。明确的“暂停”指令必须真实派发并确认，回复结束也不得自动续播。另保留 HK_VOICE_BT_MODE=pause 路径：淡出、暂停、确认释放 PCM 后再用独立语音租约；若 PCM 未释放则保持静音并报明原因，留到晚间验证。不能依靠退出时写固定数字音量“恢复”。语音清晰度偏低的问题在交接通过后调独立语音相对增益。

所有 feeder 统一使用 `q.abort` 协议，播放中也检查中断；引入轮次 ID，新唤醒先中断旧轮，旧生产者不能继续提交给新队列。

提醒使用待播、播放中、已确认状态：软件播放完成才确认消费；开声失败保留并有限重试。崩溃恢复可能重复提醒，需用 ID 和状态限制重复，不能宣称可保证物理世界的“恰好播一次”。

原厂蓝牙按键已由用户确认可用，保留该路径；重点补 AirPlay 和语音期间的来源识别、音量反馈与防循环同步。

### 5. 音量目标及 DSP 边界

当前配置 `flat + range=30 + max=-6` 的软件衰减范围是 **−36 至 −6 dB**。`default_airplay_volume=-30` 是协议输入值，映射到约 −36 dB，不能当成最低输出值。

目标 **−30 至 −6 dB** 需要 24 dB 范围。现用 Shairport 5.6 拒绝低于 30 dB 的范围，因此先在固定上游版本和现有补丁上增加受测的 24 dB 支持，检查下游音量算法，重编译、更新产物 pin。不能只改配置到 24 后容忍回退 4.3.6，也不能只提高 default 来声称完成目标。

非静音协议输入 `v ∈ [−30, 0]` 的 flat 目标映射为：

```text
output_db = −30 + (v + 30) × 24 / 30
v = −30 → −30 dB；v = −15 → −18 dB；v = 0 → −6 dB
协议静音单独处理，不能钳成最低非静音值。
```

以上是该软件音量级的衰减，不是声压级，也不包含 DSP、DAC、模拟功放的总增益。

统一的是权威音量状态和控制入口，保留各来源必要的增益适配：AirPlay 使用上述映射；蓝牙保留原厂 33 级软件音量及已取消的 30/100 限制；语音按当前有效来源衰减和独立相对增益计算。DSP 负责既有音效和分频，不能假定它提供了一个已经验证的统一音量 API。

状态记录至少区分协议值、软件衰减、mute、来源和轮次，使用原子更新；语音和灯效读取最后有效状态，而非扫描全部历史日志。任何恢复都不能把未知或陈旧音量替换成满音量。DAC/模拟基线不用于追随每次滑块变化；是否可以统一基线，待增益预算和静音测试确认后再决定。

### 6. 更新事务、disable 与日志

更新流程固定为：

1. 不停服完成候选 manifest、哈希、大小、可执行权限、脚本语法、ELF ABI、依赖与空间检查；保存设备专用配置和 pin。
2. 取得更新锁，记录原 hold/manual mute、运行服务和版本；建立维护静音。全批次备份验证成功后才允许替换。
3. 按正常关闭时序停服，卸载全部 runtime bind mount，确认无残留，再安装候选；对每一步检查返回码。
4. 启动并验证实际活动版本、二进制及配置哈希、挂载来源、DSP 路由、watchdog/bridge 和端口 7000。仅有端口或 fallback 进程不算成功。
5. 任一步失败则回滚全部事务文件并验收旧版本。更新失败即使成功回退也返回非零并报告；回退失败保持静音、留下状态和手动恢复说明。

未单独验收的 4.3.6 fallback 不得作为等价健康版本；优先恢复最后验收通过的完整版本。事务不得替换维护通道或修改它的密钥。

持久禁用文件统一为 `/vendor/<module>/disable`，运行中和下次启动都检查；明确区分普通 stop 与持久 disable，按模块验证停止后的输出归属。不能让关闭 AP2 的操作误关仍需使用的共享蓝牙输出。

日志给出明确上限，区分按次打开的 shell 日志和长时间持有 stdout 的进程。持续写入者使用有界日志接收器，或经过验证的原 inode 截断策略；不再用 rename 假装释放仍被写入的旧文件。验收包含持续写入、打开但已删除文件、重启循环和 tmpfs 总占用。最后音量状态独立保存。

构建清单固定上游提交、补丁、工具链与依赖，生成产物和权限 manifest；补齐运行时及 shim 的源码/步骤。原厂专有库由用户从自己的固件提取，公开仓库不携带私有固件、凭据或设备配置。提供设备 ID 配置和 pin 生成器，避免手工修改后无法启动。

## 验证与部署门槛

| 层级 | 验证内容 | 通过后允许的动作 |
|---|---|---|
| A：本机回归 | 旧缺陷反向断言、fork 故障注入、真实 socket 超时、状态组合、音量数学测试、更新逐点失败 | 生成候选包 |
| B：Linux / BusyBox 与 ARM | 可推进的单调时钟、PID 身份、并发 start/stop、中断、shell 行为、ABI/符号版本、安装与回滚夹具 | 上传候选并建立维护静音 |
| C：实机静音检查 | 私有备份、安装、活动版本/哈希、DSP/PCM owner、租约/监督、IPC 超时、日志边界；不发测试音 | 准备用户听测 |
| D：用户在场听测 | 低音量开始；AirPlay/BT 暂停恢复、切歌、重连、来源切换、语音插话/暂停/续播、机身按键、提醒 | 标记功能通过，开始长测 |
| E：持续运行 | 目标先做 2 小时播放/交接观察，再做 24 小时运行；记录重启、租约、内存和日志趋势 | 更新已验证状态，准备合并 |

完整现有测试、shell 语法、ARM 编译/ABI、哈希一致性和 `git diff --check` 是候选包的必需检查，但不能替代 C–E。

真实低内存不通过故意耗尽整机内存制造；先用可注入阈值/状态及假音频后端验证回收，实机只做受控服务退出与保护检查。断电与整机重启不在默认验收内，需在维护可恢复且用户安排后单独验证。

本轮实机验收必须回答：

- 开始、暂停、恢复、切歌和来源交接，是否再出现“啪”声、突增或无故静音？每次同步记录开关与 PCM 事件。
- 回收 Shairport 时，蓝牙是否继续；watchdog 或 owner 失效时，输出是否确实受保护？
- 独立语音和蓝牙播放期间唤醒的回复是否清楚，音乐是否按约暂停，恢复是否回到正确音量；语音要求暂停后是否保持暂停？
- AirPlay 最低非静音/最高值是否实际达到 −30/−6 dB，静音与物理按键是否正确？
- 灯效、维护入口和原厂蓝牙按键是否保留；有无 OOM、预录失控或日志持续吃内存？

### 低频单元单独诊断

功能交接稳定后，由用户播放同一音源，以接近响度比较蓝牙与 AirPlay，再按需要手动播放短时低电平 60/80/120/200 Hz 信号。先验证信号同相、声道和进入 DSP 的路由，再结合实际听感/振动判断。项目记录的左低频、右中高频映射仍须和实机表现核对。

若软件链正确而两种来源都缺低频，应转为功放通道、连接或单元的硬件诊断；没有测量证据时不写“低音炮已经启动/损坏”。本计划不包含拆机或硬件寄存器试写。

## 公开交付与完成定义

在 `fix/audit-20261114` 保留按批次组织的提交，准备可审阅的 PR；不直接把大改动合入 main。更新中文/英文 README、蓝牙文档、维护/构建/回滚说明及审计修复对照表，区分“代码修复”“自动验证”“实机静音验证”“用户听测”和“长测”。

F01–F12 每项均需有对应改动、回归证据和剩余限制；未完成的听测、低频硬件或长测留为明确待验收项。安装结束保持静音到用户实际试听，交接时明确当前 hold/trip 状态；试听通过后恢复用户要求的持续播放方式，保留正常故障保护。

## English summary

The offline implementation is complete and has passed muted deployment checks; this round's listening, bass hardware and sustained acceptance remain pending. The baseline is commit `741ce58`, with 54 original tests and nine reproduced abnormal behaviors. The current complete suite passes 111 tests; see the device record for actual installation and recovery evidence. The design below remains the repair plan, not a claim of audible acceptance.

The six implementation batches are:

1. Prevent nonpositive-PID signaling after fork failure and implement bounded Unix-socket requests with a single monotonic deadline (F01, F06).
2. Make updates transactional: validate a complete candidate, back up every target, verify the exact active version/configuration, and report update or rollback failures accurately (F05, initial F12 work).
3. Unify output reservation, supervised leases, silent-clock startup, guarded opening, fade-before-close shutdown, and source-aware resource reclamation (F02–F04).
4. Repair source detection, Bluetooth versus AirPlay command dispatch, playback intent, queue interruption/generation handling, and reminder acknowledgement (F07–F09).
5. Implement the requested AirPlay software attenuation range of −30 to −6 dB. This needs a tested 24 dB range patch; changing the protocol default alone cannot achieve it. Keep Bluetooth's existing steps without restoring the 30/100 cap, and derive voice gain from valid source state.
6. Finish persistent disable semantics, bounded logging, reproducible builds/install manifests, and bilingual documentation (F10–F12).

All sources continue through the existing Harman DSP. No new large resident service is planned. Normal shutdown must fade and mute while the PCM clock still runs, then stop PCM and release ownership. Emergency protection remains separate. AP2 recycling must preserve a valid Bluetooth owner.

Validation progresses through host fault injection, Linux/BusyBox and ARM checks, muted device deployment, user-initiated listening tests, then a proposed two-hour playback observation and 24-hour runtime observation. Tests cannot substitute for audible pop, bass, or real handoff acceptance. Hardware bass diagnosis remains a separate, evidence-based task.

Use a reviewable repair branch and PR. Keep bootloader/kernel/MTD/OTA, unknown hardware registers, maintenance credentials, private backups, and device-specific configuration outside the change. Do not initiate audio without the user present, and do not add a timed mute to normal long playback.
