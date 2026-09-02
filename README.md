# vtests

Ubuntu 24.04 上的 VPS 稳定性 / 模拟负载工具。一条命令安装，浏览器里设置 CPU 负载率和内存占用，并可按每天的时间段自动启停。

**实现以 [docs/DESIGN.md](docs/DESIGN.md) 为准。** 当前仓库代码是探索性 spike，将按该文档重写。

加压引擎使用发行版自带的 [stress-ng](https://github.com/ColinIanKing/stress-ng)。

## 一键安装

在目标主机上以 root 执行：

```bash
bash <(curl -Ls https://raw.githubusercontent.com/minng2018/vtests/main/install.sh)
```

安装完成后会打印面板地址、随机路径和密码。之后可用：

```bash
vtests
```

查看地址、启停服务、重置密码或卸载。

卸载：

```bash
bash <(curl -Ls https://raw.githubusercontent.com/minng2018/vtests/main/install.sh) uninstall
```

## 功能

- Web 设置页：CPU 目标负载（%）、内存占用（MB）、立即开始 / 停止
- 定时：每天开始和结束时刻，支持跨午夜，时区默认 `Asia/Shanghai`
- 安装后默认**不加压**，需在面板里手动开始，或打开定时
- 小内存机器会限制最大占用，避免把系统吃光
- 随机访问路径 + 面板密码

## 访问

若云厂商安全组未放行面板端口（默认 `8088`），可用 SSH 隧道：

```bash
ssh -L 8088:127.0.0.1:8088 用户@服务器IP
```

浏览器打开安装脚本打印的 `http://127.0.0.1:8088/随机路径/`。

## 注意

- 原型针对 Ubuntu 24.04。Debian 12 / Ubuntu 22.04 可能能装，未作为目标验证。
- 高 CPU / 高内存可能触发 OOM、邻居干扰或云厂商风控。1GB 内存的机器请用很低的占用做功能验证。
- 本工具会在本机产生真实负载，不要装在舍不得停的生产入口上做满载压测。
