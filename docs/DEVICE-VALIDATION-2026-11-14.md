# 实机部署记录 / Device deployment record — 2026-11-14

状态：已完成作者设备的安装与静音核验；保留维护 hold 和 `manual mute` 等待用户在场试听。没有主动播放音乐、测试音或触发唤醒。不能据此宣称爆音、低频或完整语音交接已解决。

## 已核验

| 项目 | 实机结果 |
|---|---|
| 交付 | 31 文件私有最小包完成事务安装，返回 0；state 为 committed，pending 已清除 |
| 接收器 | 实际活动二进制报告 5.6；新 Shairport SHA-256 为 `14bd1d206380bb5496dfcebe0fa7f49f2bca36115d734da18bea116915761150`，不是 4.3.6 fallback |
| 音量 | `range=24`、`flat`、`max=-6.0`、`default_airplay_volume=-30.0`；配置解析并常驻启动通过，实际滑块/响度待测试 |
| 路由与保护 | DSP 桥、蓝牙桥、AP2 资源守卫和独立 watchdog 存活；实际挂载及配置 pin 通过 `verify` |
| 相依组件 | 原厂播放器与按键进程已重载，映射新库且无 deleted 标记；语音监督/循环、灯效桥运行 |
| 静音 | 无播放租约、PCM closed、Master=0、数字音量=0、Digital off、Ext Spk off、I2S mute on |
| 资源 | 静音检查时 MemFree 约 11–14 MiB，vendor 约 4 MiB 可用；AP2 辅助进程 OOM adj=11，语音=5；未见本轮新增 OOM/报警进程 |
| 维护边界 | rootApp PID 未改变，mTLS 维护可用，整机没有重启；未写固件分区或未知硬件寄存器 |
| IPC | 新 ARM 请求器在实机可读取原厂状态；不存在的 socket 明确返回非零 |

用户专用配置、pin、事务备份与完整日志只保存于私有发布/备份目录，没有进入本仓库。当前设备仍保留本次事务的回滚文件。

## 部署中补充的修复

原厂 BusyBox 缺少 `stat`；权限读取改用 POSIX `ls` 并保留特殊位。第一次启动被原始空闲内存门槛拒绝，初次自动回退又因运行目录已被停服清除而无法建立静音锁。恢复逻辑现在重建锁父目录，并兼容旧控制器没有 `verify` 的情况；兼容验收核对整批旧文件、实际 5.6 绑定、进程身份和 watchdog 心跳，不能用端口监听或 4.3.6 代替。

两次内存拒绝均以非零返回并最终恢复完整旧版本；失败和恢复日志已保存。没有把回退成功记录为更新成功。最后一次候选安装返回 0 并独立复核通过。

上传包、解包目录和 `.new` 副本会占 tmpfs 或文件缓存。安装可用 `HK_AP2_UPDATE_RELEASE_STAGING=1`，在整批安装校验后释放 `.new`；独立事务备份继续保留。启动同时要求 8 MiB 原始空闲和 48 MiB 估算可用内存，估算扣除 tmpfs、脏页及写回，仅计一半可回收 slab。实际启动读到 MemFree=33916 KiB、available≈59166 KiB。运行期 4/32 MiB 门槛保持。

## 自动验证与剩余验收

当前 Linux/BusyBox 完整 **111 项通过**。完整日志 SHA-256：`a16651855fade7ac1ec669c46924c41d6f29caab98ccc6c7ad0eb1e78ce53775`。新权限/恢复逻辑也通过 POSIX shell/awk 测试；新增 7 项内存快照测试覆盖干净缓存、低原始空闲、tmpfs、脏页/写回、slab 和无效字段。shell 语法、私有包哈希/权限和 `git diff --check` 通过。此前 ARM/ABI 与可重复构建证据见 [OFFLINE-VALIDATION.json](OFFLINE-VALIDATION.json)。

下一步先由用户低音量 AirPlay 播放，测试开始、暂停、恢复、切歌和重连；再测试蓝牙、来源切换、语音插入/恢复和机身按键。默认蓝牙语音是同 PCM 静音背景音乐，手机进度可能继续，不等同于暂停。低频单元、2 小时播放及 24 小时运行仍待验收。正常长测不加定时静音，保留无租约和故障保护。

## English summary

The 31-file private minimal update completed successfully and passed muted checks on the author's speaker. The active receiver reports Shairport 5.6 with the patched 24 dB flat range (software attenuation −30…−6 dB), verified runtime/configuration bindings, DSP, Bluetooth bridge and independent watchdog. Voice, lights, stock player and key-hook reloads were checked. The stock rootApp and authenticated maintenance channel stayed available; there was no device reboot or firmware/register write.

Deployment exposed stock BusyBox's missing `stat`, a recovery lock-directory dependency after failed startup, and the old controller's missing `verify` command. These now have regressions and guarded recovery. Two startup refusals returned failure and were fully rolled back; the final candidate returned success. Low-memory staging can be released after the installed set is verified, while independent rollback backups remain. Startup requires 8 MiB raw free plus 48 MiB conservatively estimated available memory; tmpfs and dirty/writeback cache are excluded and only half the reclaimable slab is credited. Runtime protection is unchanged.

All 111 Linux/BusyBox tests pass, with additional POSIX recovery/memory checks and the earlier ARM/ABI evidence retained. Maintenance hold and manual mute remain set, with no playback lease and physical output disabled. No listening acceptance is claimed: pops, source switching, voice loudness/handoff, buttons, bass hardware and sustained playback still require user-present tests. Private configuration, credentials, backups and device logs are not published.
