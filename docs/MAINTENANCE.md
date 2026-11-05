# Harman Kardon Allure：mTLS 常驻 root 维护通道

[返回项目说明](../README.md) · [首次取得 root 的 UPnP 方法](ROOTING.md)

首次通过 UPnP 取得 root 后，我们又安装了**可随开机恢复的维护通道**。它使用原厂 `/system/workdir/bin/stunnel`，在 TCP `23232` 上提供需要客户端证书的 TLS root shell。平时通过它运行命令、传文件，不需要重复触发 UPnP 闹钟，也不是普通 SSH。

本文依据本地 `secure-shell/` 配置、`hk_secure.py`、`hk_put.py`、`S99hk-secure` 和 2026-11-02 至 11-04 的部署、重启及交接记录整理。已记录过完整重启后认证 shell 恢复；本文整理时没有重新操作音箱，也不把历史验证当作实时在线状态。

## 1. 后来实际采用的结构

```text
电脑：hk_secure.py / hk_put.py + CA + 客户端证书与私钥
  → TLS，TCP 23232
音箱：stunnel 验证客户端证书
  → /bin/sh（root）

开机：/etc/init.d/S99hk-secure
  → /vendor/user/user.sh
  → /vendor/user/hk-secure-supervisor.sh
  → stunnel /vendor/secure-shell/stunnel.conf
```

| 路径 / 组件 | 作用 |
|---|---|
| `/vendor/secure-shell/stunnel.conf` | 端口、证书认证、执行 shell 的配置 |
| `/vendor/secure-shell/ca.pem` | 服务端验证客户端证书所信任的 CA |
| `/vendor/secure-shell/server.pem`、`server.key` | 服务端身份与私钥材料 |
| `/vendor/user/hk-secure-supervisor.sh` | 保活；stunnel 退出后重试 |
| `/vendor/user/user.sh` | 启动 supervisor 的包装脚本 |
| `/etc/init.d/S99hk-secure` | 独立开机入口，和 AP2 启动项分开 |
| `/tmp/hk-secure-supervisor.pid` | supervisor 的运行时 PID |
| `/tmp/hk-secure-shell-23232.pid` | stunnel 的运行时 PID |
| `/tmp/hk-secure-supervisor.log`、`/tmp/hk-secure-shell-23232.log` | 运行日志 |

早期曾由原厂 `rootApp` 调用 `/vendor/user/user.sh`；后来补上了独立的 `/etc/init.d/S99hk-secure`。不要只根据早期 `user.sh` 的注释判断最终启动链。

“常驻”指配置和脚本持久保存在文件系统、重启后自动启动，不是写进 bootloader。它仍依赖系统能正常启动、`/vendor` 和证书可用以及网络可达。

## 2. stunnel 的实测配置

以下是已部署配置的结构，只有路径，没有真实证书或私钥：

```ini
foreground = yes
pid = /tmp/hk-secure-shell-23232.pid
output = /tmp/hk-secure-shell-23232.log
debug = notice

[secure-shell]
accept = 0.0.0.0:23232
cert = /vendor/secure-shell/server.pem
key = /vendor/secure-shell/server.key
CAfile = /vendor/secure-shell/ca.pem
verify = 2
requireCert = yes
exec = /bin/sh
execArgs = sh -i
TIMEOUTidle = 600
TIMEOUTclose = 0
```

客户端需要受信任 CA 签发的证书及对应私钥。端口开放不等于任何人都能获得 shell；普通 `nc` 也不能作为这个 TLS 通道的登录客户端。`TIMEOUTidle=600` 限制连接的空闲时间，**不是十分钟后停掉常驻服务**。

历史部署的 supervisor 会检查 PID 文件、避免重复启动，并在 stunnel 退出后等待约 3 秒重试。它对自身及 stunnel 设置了 `oom_score_adj=-1000`，用于优先保留维护入口。这个历史设置不应照搬给 AP2 或语音服务；后者采用可退让的正数优先级。

## 3. 开机持久化

最终的 `/etc/init.d/S99hk-secure` 内容为：

```sh
#!/bin/sh

# Keep the authenticated maintenance shell independent of vendor service startup.
if [ -x /vendor/user/user.sh ]; then
    /vendor/user/user.sh start >/dev/null 2>&1 &
fi
```

`/vendor/user/user.sh` 的核心为：

```sh
#!/bin/sh
exec /vendor/user/hk-secure-supervisor.sh
```

supervisor 使用 `/vendor/secure-shell/stunnel.conf` 拉起原厂 stunnel。入口独立，因此禁用 AP2 的 `/etc/init.d/S99hk-airplay2` 不需要同时停止维护通道。

重现这套结构需要先有一次 root 访问：为自己的设备生成独立的 CA、服务端和客户端证书，安装服务端材料与配置、supervisor 和启动脚本，再验证认证连接及重启恢复。音箱只需要服务端材料和 CA 公钥；客户端私钥、CA 私钥保留在自己的电脑。所有私钥和可能包含私钥的 PEM 文件都应限制权限。

首次切换时应先在另一个端口验证新 TLS 通道，再替换早期临时 inetd，避免两个服务抢占 `23232` 或同时丢失访问能力。本文记录已有部署，不提供盲目覆盖现有维护入口的一键安装器。

## 4. 日常连接和文件上传

作者本地工作目录保存了这些文件，真实凭据没有放进 GitHub：

```text
hk_secure.py
hk_put.py
secure-shell/
  ca.pem
  client.crt
  client.key
```

在上述**私有工作目录**中，可以执行只读命令：

```sh
python3 hk_secure.py 'id; cat /proc/uptime'
printf '%s\n' 'ps w' 'cat /proc/meminfo | head -20' | python3 hk_secure.py
```

`hk_secure.py` 接收命令参数或标准输入，通过 mTLS 运行命令并回传输出。其本地版本内置了设备地址；迁移到另一台设备时需修改地址和证书位置。它还支持 `--port`、`--timeout` 及带边界标记的 `--binary` 输出。

上传到设备临时目录的用法为：

```sh
python3 hk_put.py ./example.txt /tmp/example.txt
```

`hk_put.py` 通过同一认证通道发送 Base64，先写目标临时文件，再移动到目标路径，并检查回传的 SHA-1 是否等于本地文件。不要直接用它覆盖正在 bind mount 或 mmap 的程序文件；AP2 文件更新见项目的安全替换流程。

也可以用通用 TLS 客户端访问同样的协议。下面的地址是示例，应替换为自己的音箱地址；这段只演示证书链认证：

```sh
openssl s_client -connect 192.0.2.10:23232 \
  -CAfile ./secure-shell/ca.pem \
  -cert ./secure-shell/client.crt \
  -key ./secure-shell/client.key \
  -verify_return_error -quiet
```

连接后输入 `id`；结束时输入 `exit`。它仍然是 shell 流，不是 SSH 或 HTTPS API。

旧版 Python 客户端验证指定 CA 的证书链，但设置了 `check_hostname=False`；为兼容旧 CA，它还关闭了 Python 3.13 起默认开启的严格 X.509 检查。因此不能把旧客户端描述成已完成主机名/IP 身份校验。新证书应正确配置 SAN，并在客户端匹配预期主机名或 IP；不要用 `CERT_NONE` 代替证书链验证。

## 5. 状态核查与维护边界

通过已认证的 root shell 做只读检查：

```sh
ls -l /etc/init.d/S99hk-secure /vendor/user/user.sh
ls -l /vendor/user/hk-secure-supervisor.sh /vendor/secure-shell/stunnel.conf
ps w | grep -E '[s]tunnel|[h]k-secure-supervisor'
cat /tmp/hk-secure-supervisor.pid /tmp/hk-secure-shell-23232.pid
```

PID 每次启动都可能变化，不能把旧记录中的 PID 当作固定身份。已部署版本的 `user.sh` / supervisor **没有实现 `stop` 子命令**；向 `user.sh` 传入 `stop` 也会走启动代码。不要据此编造通用的启停命令。

回退 AP2、按键或语音功能时保留这个入口及其证书、启动项和保活脚本。需要真正卸载维护通道时，必须先准备并验证其他访问方式；单纯删除启动项不等于停止当前实例，单纯结束进程也可能被 supervisor 再次拉起。

## 6. 与早期临时入口的区别

| 项目 | 早期 UPnP / inetd | 后来的常驻维护通道 |
|---|---|---|
| 主要用途 | 首次获得 root | 日常命令、上传与修复 |
| 常用端口 | `23232` | `23232` |
| 协议 | 明文 TCP shell | stunnel 包装的 TLS shell |
| 认证 | 无 | 客户端证书认证 |
| 配置位置 | `/tmp/hk-inetd.conf` | `/vendor/secure-shell/stunnel.conf` |
| 重启后 | 临时配置消失 | 独立启动项恢复 |
| 客户端 | `nc` | `hk_secure.py` / `hk_put.py` 或 TLS 客户端 |

本仓库公开通道结构、配置示例和操作说明，不公开实际 CA、客户端/服务端证书、任何私钥、包含这些材料的归档或原始会话日志。尤其不要按扩展名判断：`server.pem` 也可能包含私钥。
