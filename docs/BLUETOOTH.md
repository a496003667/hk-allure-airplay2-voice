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
a01controller -> Master softvol -> harmandsp -> hw:0,0
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

1. 将 `hk-bt-controller-wrapper.sh`、`hk-bt-open-redirect.so` 和
   `hk-bt-alsa.conf` 放到 `/vendor/airplay2/`。
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

本分支尚未部署；下列历史记录不能替代新的实机验收。

## 验证状态

离线测试已确认私有配置能加载原厂 DSP、打开 `pcmC0D0p`，且测试期间
`Ext Spk=off`、`aml audio i2s mute=on`、`Auto Mute=on`。真实的手机蓝牙音量、
暂停/恢复、切歌、AirPlay 接管和低音听感仍需在用户在场时验证。

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
