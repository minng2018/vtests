#!/usr/bin/env bash
# vtests one-click installer (Ubuntu 24.04 prototype)
set -euo pipefail

red='\033[0;31m'
green='\033[0;32m'
yellow='\033[0;33m'
plain='\033[0m'

REPO="${VTESTS_REPO:-minng2018/vtests}"
BRANCH="${VTESTS_BRANCH:-main}"
INSTALL_DIR=/opt/vtests
CONF_DIR=/etc/vtests
SERVICE=/etc/systemd/system/vtests.service
BIN=/usr/bin/vtests

ok() { echo -e "${green}$*${plain}"; }
warn() { echo -e "${yellow}$*${plain}"; }
err() { echo -e "${red}$*${plain}"; }

need_root() {
    if [[ ${EUID} -ne 0 ]]; then
        err "请使用 root 运行："
        echo "  bash <(curl -Ls https://raw.githubusercontent.com/${REPO}/${BRANCH}/install.sh)"
        exit 1
    fi
}

detect_os() {
    if [[ -f /etc/os-release ]]; then
        # shellcheck source=/dev/null
        . /etc/os-release
        OS_ID=${ID:-}
        OS_VER=${VERSION_ID:-}
    else
        err "无法识别系统"
        exit 1
    fi
    if [[ "${OS_ID}" != "ubuntu" && "${OS_ID}" != "debian" ]]; then
        err "当前原型仅支持 Ubuntu / Debian，检测到: ${OS_ID}"
        exit 1
    fi
    if [[ "${OS_ID}" == "ubuntu" && "${OS_VER}" != "24.04" ]]; then
        warn "原型针对 Ubuntu 24.04，当前是 ${OS_ID} ${OS_VER}，继续尝试安装。"
    fi
}

download() {
    local url=$1 dest=$2
    if curl -fL --connect-timeout 15 --retry 2 -o "${dest}" "${url}"; then
        return 0
    fi
    if curl -fL --connect-timeout 15 --retry 2 -o "${dest}" "https://ghproxy.net/${url}"; then
        return 0
    fi
    return 1
}

public_ip() {
    curl -4 -fsS --connect-timeout 5 https://ifconfig.me 2>/dev/null \
        || curl -4 -fsS --connect-timeout 5 https://api.ipify.org 2>/dev/null \
        || hostname -I 2>/dev/null | awk '{print $1}' \
        || echo "服务器IP"
}

open_port() {
    local port=$1
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi "Status: active"; then
        ufw allow "${port}/tcp" comment vtests >/dev/null 2>&1 || true
        warn "已尝试通过 ufw 放行 ${port}/tcp"
    elif command -v iptables >/dev/null 2>&1; then
        if ! iptables -C INPUT -p tcp --dport "${port}" -j ACCEPT >/dev/null 2>&1; then
            iptables -I INPUT -p tcp --dport "${port}" -j ACCEPT || true
            warn "已尝试通过 iptables 放行 ${port}/tcp（可能还需在云厂商安全组放行）"
        fi
    fi
}

install_pkgs() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y python3 python3-venv python3-pip curl tar ca-certificates stress-ng
    if ! command -v stress-ng >/dev/null 2>&1; then
        err "stress-ng 安装失败（Ubuntu 需启用 universe 仓库）"
        exit 1
    fi
}

fetch_app() {
    local tmp tarball
    tmp=$(mktemp -d)
    tarball="${tmp}/vtests.tar.gz"
    ok "正在从 GitHub 下载 ${REPO}@${BRANCH} ..."
    if ! download "https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz" "${tarball}"; then
        err "下载失败，请检查本机是否能访问 GitHub"
        exit 1
    fi
    tar -xzf "${tarball}" -C "${tmp}"
    local src
    src=$(find "${tmp}" -mindepth 1 -maxdepth 1 -type d | head -n1)
    if [[ -z "${src}" || ! -f "${src}/app/main.py" ]]; then
        err "下载的源码不完整"
        exit 1
    fi
    mkdir -p "${INSTALL_DIR}"
    rm -rf "${INSTALL_DIR}/app" "${INSTALL_DIR}/systemd" "${INSTALL_DIR}/requirements.txt" "${INSTALL_DIR}/VERSION" "${INSTALL_DIR}/vtests.sh"
    cp -a "${src}/app" "${INSTALL_DIR}/"
    cp -a "${src}/systemd" "${INSTALL_DIR}/"
    cp "${src}/requirements.txt" "${INSTALL_DIR}/"
    cp "${src}/VERSION" "${INSTALL_DIR}/"
    cp "${src}/vtests.sh" "${INSTALL_DIR}/"
    cp "${src}/install.sh" "${INSTALL_DIR}/"
    rm -rf "${tmp}"
}

setup_venv() {
    if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
        python3 -m venv "${INSTALL_DIR}/venv"
    fi
    "${INSTALL_DIR}/venv/bin/pip" install -U pip
    "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
}

init_config() {
    mkdir -p "${CONF_DIR}"
    chmod 700 "${CONF_DIR}"
    if [[ ! -f "${CONF_DIR}/config.json" ]]; then
        "${INSTALL_DIR}/venv/bin/python" - <<'PY'
import json, os, sys
sys.path.insert(0, "/opt/vtests")
os.environ["VTESTS_CONFIG"] = "/etc/vtests/config.json"
from app.main import load_config, save_config, meminfo
cfg = load_config()
mem = meminfo()["total_mb"]
if mem and mem <= 1536:
    cfg["cpu_percent"] = 10
    cfg["memory_mb"] = min(64, cfg.get("memory_mb", 64))
    cfg["enabled"] = False
    cfg["schedule_enabled"] = False
save_config(cfg)
print(json.dumps({
    "port": cfg["port"],
    "base_path": cfg["base_path"],
    "password": cfg["password"],
}))
PY
    else
        "${INSTALL_DIR}/venv/bin/python" - <<'PY'
import json, os, sys
sys.path.insert(0, "/opt/vtests")
os.environ["VTESTS_CONFIG"] = "/etc/vtests/config.json"
from app.main import load_config
cfg = load_config()
print(json.dumps({
    "port": cfg["port"],
    "base_path": cfg["base_path"],
    "password": cfg["password"],
}))
PY
    fi
}

write_result() {
    local port=$1 path=$2 password=$3 ip
    ip=$(public_ip)
    cat > "${CONF_DIR}/install-result.env" <<EOF
PORT=${port}
BASE_PATH=${path}
PASSWORD=${password}
URL=http://${ip}:${port}${path}/
EOF
    chmod 600 "${CONF_DIR}/install-result.env"
}

install_service() {
    cp "${INSTALL_DIR}/systemd/vtests.service" "${SERVICE}"
    cp "${INSTALL_DIR}/vtests.sh" "${BIN}"
    chmod +x "${BIN}" "${INSTALL_DIR}/app/main.py"
    systemctl daemon-reload
    systemctl enable vtests
    systemctl restart vtests
}

uninstall() {
    need_root
    systemctl stop vtests 2>/dev/null || true
    systemctl disable vtests 2>/dev/null || true
    rm -f "${SERVICE}" "${BIN}"
    systemctl daemon-reload || true
    rm -rf "${INSTALL_DIR}" "${CONF_DIR}"
    ok "vtests 已卸载"
}

print_done() {
    local port=$1 path=$2 password=$3
    # shellcheck source=/dev/null
    . "${CONF_DIR}/install-result.env"
    echo
    ok "安装完成"
    echo -e "面板地址: ${green}${URL}${plain}"
    echo -e "端口:     ${port}"
    echo -e "路径:     ${path}/"
    echo -e "密码:     ${green}${password}${plain}"
    echo
    echo "管理命令: vtests"
    echo "若浏览器打不开，请在云厂商安全组放行端口 ${port}，或用 SSH 隧道："
    echo "  ssh -L ${port}:127.0.0.1:${port} 用户@服务器"
    if [[ -r /proc/meminfo ]]; then
        local total
        total=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
        if [[ ${total} -le 1536 ]]; then
            echo
            warn "检测到内存约 ${total} MB。默认不会自动加压，请在面板里用较低 CPU/内存测试。"
        fi
    fi
}

main() {
    if [[ "${1:-}" == "uninstall" ]]; then
        uninstall
        exit 0
    fi
    need_root
    detect_os
    install_pkgs
    fetch_app
    setup_venv
    local info port path password
    info=$(init_config)
    port=$(echo "${info}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["port"])')
    path=$(echo "${info}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["base_path"])')
    password=$(echo "${info}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')
    write_result "${port}" "${path}" "${password}"
    install_service
    open_port "${port}"
    local healthy=0
    for _ in $(seq 1 20); do
        if curl -fsS "http://127.0.0.1:${port}${path}/healthz" >/dev/null 2>&1; then
            healthy=1
            break
        fi
        sleep 0.4
    done
    if systemctl is-active --quiet vtests && [[ ${healthy} -eq 1 ]]; then
        print_done "${port}" "${path}" "${password}"
    else
        err "服务启动失败，查看: journalctl -u vtests -e"
        systemctl status vtests --no-pager || true
        exit 1
    fi
}

main "$@"
