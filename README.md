# vtests

Ubuntu 24.04 上的 VPS 稳定性 / 模拟负载工具。一条命令安装，浏览器里设置 CPU 负载率和内存占用，并可按每天的时间段自动启停。

**实现以 [docs/DESIGN.md](docs/DESIGN.md) 为准。** 当前仓库代码是探索性 spike，将按该文档重写。

加压引擎使用发行版自带的 [stress-ng](https://github.com/ColinIanKing/stress-ng)。

## 一键安装

在目标主机上以 root 执行：

```bash
bash <(curl -Ls https://raw.githubusercontent.com/minng2018/vtests/main/install.sh)
```

安装脚本默认拉最新 GitHub Release（`vtests-<tag>.tar.gz`，并用 `SHA256SUMS` 校验）。尚未发布 Release 时回退到 `main` 源码包。钉版本把 tag 当作参数：

```bash
bash <(curl -Ls https://raw.githubusercontent.com/minng2018/vtests/main/install.sh) v0.2.0
```

打 tag `v0.2.0` 会触发 Actions，上传源码包并发布 GitHub Release。

交互安装会询问已解析到本机公网 IPv4 的域名。直接回车则走 IP:端口 HTTP，不申请证书。非交互 HTTPS 示例：

```bash
VTESTS_NONINTERACTIVE=1 VTESTS_DOMAIN=vt-frp.beeorbit.net \
  bash <(curl -Ls https://raw.githubusercontent.com/minng2018/vtests/main/install.sh)
```

安装完成后默认**不加压**，并打印面板地址、随机路径和密码：

- **HTTPS 成功**：公网 `https://<domain>/<base_path>/`，以及本机备用 `http://127.0.0.1:<port>/<base_path>/`。
- **未提供域名，或证书失败**：`http://<ip>:<port>/<base_path>/`。证书失败回退 HTTP，**不中止安装**。

面板端口安装时随机分配（约 1024–62000），**不是**固定的 8088。

安装脚本只为自己的面板域名写独立 Nginx vhost 和 Let's Encrypt 证书，**不会改写** `beeman.beeorbit.net` / `beenovel.beeorbit.net`。

之后可用 `vtests` 查看地址、启停面板、重置密码或卸载。

`vtests start` / `vtests stop` 控制的是面板服务 `vtests.service`，不是加压引擎。加压只在 Web 面板里开始或停止。

卸载走 `vtests uninstall`（菜单选项 7 相同），优先本机 `/opt/vtests/install.sh uninstall`；仅当该文件不存在时才回退到 GitHub 上的 `install.sh uninstall`。卸载只清理本工具及其 vhost，不触碰既有站点。

## 功能

- Web 设置页：CPU 目标负载（%）、内存占用（MB）、立即开始 / 停止
- 定时：每天开始和结束时刻，支持跨午夜，时区默认 `Asia/Shanghai`
- 安装后默认**不加压**，需在面板里手动开始，或打开定时
- 随机访问路径 + 面板密码
- 可选域名 HTTPS；空域名则 IP:端口 HTTP

## 访问

安装默认不修改主机防火墙 / 安全组。

**HTTPS 模式**：公网走已放行的 80/443，不必再开放面板随机端口。本机备用地址可在服务器上打开。

**IP HTTP 模式**：若云厂商安全组未放行安装时打印的随机端口，用 SSH 隧道（端口以安装输出为准）：

```bash
ssh -L <打印的端口>:127.0.0.1:<打印的端口> 用户@服务器IP
```

浏览器打开安装脚本打印的地址。

## 1 GB 主机上限

| 档位 | 默认 CPU | CPU 硬顶 | 默认内存 | 内存硬顶 |
| --- | --- | --- | --- | --- |
| ≤ 1.5 GB（含 1 GB 试验机） | 10% | 30% | 64 MB | 128 MB |

试验机 smoke（生产 FRP 入口，同居 beeman/beenovel）：CPU **30%**、内存 **≤ 64 MB**、时长 **≤ 5 分钟**。禁止过夜，禁止 CPU ≥ 50%。不要在该机做满载压测。

## 注意

- 原型针对 Ubuntu 24.04。Debian 12 / Ubuntu 22.04 可能能装，未作为目标验证。
- 高 CPU / 高内存可能触发 OOM、邻居干扰或云厂商风控。
- 本工具会在本机产生真实负载，**不要在生产 FRP 入口上做满载压测**。
- 不要修改 `beeman.beeorbit.net` / `beenovel.beeorbit.net` 的 Nginx 或证书。
