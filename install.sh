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
DROPIN_DIR=/etc/systemd/system/vtests.service.d
BIN=/usr/bin/vtests
STATE_DIR=/var/lib/vtests
LOG_DIR=/var/log/vtests

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

as_vtests() {
    if command -v sudo >/dev/null 2>&1; then
        sudo -u vtests env "$@"
    else
        runuser -u vtests -- env "$@"
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

port_used() {
    local port=$1 out
    if command -v ss >/dev/null 2>&1; then
        out=$(ss -H -ltn "( sport = :${port} )" 2>/dev/null || true)
        [[ -n "${out}" ]]
        return
    fi
    python3 - "${port}" <<'PY'
import socket, sys
port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind(("0.0.0.0", port))
except OSError:
    sys.exit(0)
finally:
    sock.close()
sys.exit(1)
PY
}

pick_port() {
    local port i
    if [[ -n "${VTESTS_PORT:-}" ]]; then
        printf '%s\n' "${VTESTS_PORT}"
        return
    fi
    for i in $(seq 1 80); do
        port=$(python3 -c 'import secrets; print(secrets.randbelow(60977) + 1024)')
        case "${port}" in
            22|80|443|7000|8080) continue ;;
        esac
        if port_used "${port}"; then
            continue
        fi
        printf '%s\n' "${port}"
        return
    done
    err "无法分配空闲端口"
    exit 1
}

pick_base_path() {
    local p="${VTESTS_WEB_BASE_PATH:-}"
    if [[ -n "${p}" ]]; then
        [[ "${p}" == /* ]] || p="/${p}"
        printf '%s\n' "${p}"
        return
    fi
    python3 -c 'import secrets; print("/" + secrets.token_urlsafe(8).rstrip("="))'
}

pick_password() {
    if [[ -n "${VTESTS_PASSWORD:-}" ]]; then
        printf '%s\n' "${VTESTS_PASSWORD}"
        return
    fi
    python3 -c 'import secrets; print(secrets.token_urlsafe(12))'
}

maybe_open_firewall() {
    local port=$1
    if [[ "${VTESTS_OPEN_FIREWALL:-}" != "1" ]]; then
        return
    fi
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi "Status: active"; then
        ufw allow "${port}/tcp" comment vtests >/dev/null 2>&1 || true
        warn "VTESTS_OPEN_FIREWALL=1，已尝试通过 ufw 放行 ${port}/tcp"
    else
        warn "VTESTS_OPEN_FIREWALL=1 但没有活动的 ufw，未改防火墙"
    fi
}

install_pkgs() {
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y python3 python3-venv python3-pip curl tar ca-certificates tzdata stress-ng
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
    rm -rf "${INSTALL_DIR}/app" "${INSTALL_DIR}/systemd" "${INSTALL_DIR}/nginx" \
        "${INSTALL_DIR}/requirements.txt" "${INSTALL_DIR}/VERSION" \
        "${INSTALL_DIR}/vtests.sh" "${INSTALL_DIR}/install.sh"
    cp -a "${src}/app" "${INSTALL_DIR}/"
    cp -a "${src}/systemd" "${INSTALL_DIR}/"
    if [[ -d "${src}/nginx" ]]; then
        cp -a "${src}/nginx" "${INSTALL_DIR}/"
    else
        mkdir -p "${INSTALL_DIR}/nginx"
    fi
    cp "${src}/requirements.txt" "${INSTALL_DIR}/"
    cp "${src}/VERSION" "${INSTALL_DIR}/"
    cp "${src}/vtests.sh" "${INSTALL_DIR}/"
    cp "${src}/install.sh" "${INSTALL_DIR}/"
    chmod +x "${INSTALL_DIR}/vtests.sh" "${INSTALL_DIR}/install.sh"
    rm -rf "${tmp}"
}

setup_venv() {
    if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
        python3 -m venv "${INSTALL_DIR}/venv"
    fi
    "${INSTALL_DIR}/venv/bin/pip" install -U pip
    "${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"
}

ensure_user() {
    if ! getent passwd vtests >/dev/null 2>&1; then
        useradd --system --home "${STATE_DIR}" --shell /usr/sbin/nologin vtests
    fi
    install -d -o vtests -g vtests -m 700 "${CONF_DIR}" "${STATE_DIR}" "${LOG_DIR}"
}

write_limits_dropin() {
    local tmp
    install -d -o root -g root -m 755 "${DROPIN_DIR}"
    tmp=$(mktemp)
    PYTHONPATH="${INSTALL_DIR}" "${INSTALL_DIR}/venv/bin/python" - <<'PY' > "${tmp}"
import sys
sys.path.insert(0, "/opt/vtests")
from app.config import render_systemd_limits
sys.stdout.write(render_systemd_limits())
PY
    install -o root -g root -m 644 "${tmp}" "${DROPIN_DIR}/limits.conf"
    rm -f "${tmp}"
}

init_config() {
    local port path password listen
    listen="${VTESTS_LISTEN:-0.0.0.0}"
    if [[ ! -f "${CONF_DIR}/config.json" ]]; then
        port=$(pick_port)
        path=$(pick_base_path)
        password=$(pick_password)
        as_vtests \
            PYTHONPATH="${INSTALL_DIR}" \
            VTESTS_CONFIG="${CONF_DIR}/config.json" \
            VTESTS_PORT="${port}" \
            VTESTS_WEB_BASE_PATH="${path}" \
            VTESTS_PASSWORD="${password}" \
            VTESTS_LISTEN="${listen}" \
            "${INSTALL_DIR}/venv/bin/python" - <<'PY'
import json, os, sys
sys.path.insert(0, os.environ.get("PYTHONPATH", "/opt/vtests"))
from app.auth import hash_password
from app.config import default_config, save_config

cfg = default_config()
cfg["port"] = int(os.environ["VTESTS_PORT"])
cfg["base_path"] = os.environ["VTESTS_WEB_BASE_PATH"]
cfg["listen"] = os.environ.get("VTESTS_LISTEN") or "0.0.0.0"
cfg["password_hash"] = hash_password(os.environ["VTESTS_PASSWORD"])
cfg.pop("password", None)
cfg["mode"] = "off"
save_config(cfg)
print(json.dumps({
    "port": cfg["port"],
    "base_path": cfg["base_path"],
    "password": os.environ["VTESTS_PASSWORD"],
}))
PY
    else
        chown vtests:vtests "${CONF_DIR}/config.json"
        chmod 600 "${CONF_DIR}/config.json"
        if [[ -f "${CONF_DIR}/config.json.lock" ]]; then
            chown vtests:vtests "${CONF_DIR}/config.json.lock"
            chmod 600 "${CONF_DIR}/config.json.lock"
        fi
        as_vtests \
            PYTHONPATH="${INSTALL_DIR}" \
            VTESTS_CONFIG="${CONF_DIR}/config.json" \
            "${INSTALL_DIR}/venv/bin/python" - <<'PY'
import json, os, sys
sys.path.insert(0, os.environ.get("PYTHONPATH", "/opt/vtests"))
from app.auth import hash_password
from app.config import load_config, update_config

cfg = load_config()
plain = cfg.get("password")
hashed = str(cfg.get("password_hash") or "")
out_pw = ""
if isinstance(plain, str) and plain and not hashed.startswith("scrypt$"):
    digest = hash_password(plain)

    def mutate(cur):
        cur["password_hash"] = digest
        cur.pop("password", None)

    cfg = update_config(mutate)
    out_pw = plain
elif hashed.startswith("scrypt$") and "password" in cfg:
    def mutate(cur):
        cur.pop("password", None)

    cfg = update_config(mutate)
print(json.dumps({
    "port": cfg["port"],
    "base_path": cfg["base_path"],
    "password": out_pw,
}))
PY
    fi
}

write_result() {
    local port=$1 path=$2 password=$3 ip listen
    ip=$(public_ip)
    listen="${VTESTS_LISTEN:-0.0.0.0}"
    if [[ -z "${password}" && -f "${CONF_DIR}/install-result.env" ]]; then
        password=$(awk -F= '/^PASSWORD=/ {print substr($0, index($0,"=")+1); exit}' "${CONF_DIR}/install-result.env")
    fi
    cat > "${CONF_DIR}/install-result.env" <<EOF
PORT=${port}
BASE_PATH=${path}
PASSWORD=${password}
URL=http://${ip}:${port}${path}/
LISTEN=${listen}
SSL_ENABLED=0
DOMAIN=
EOF
    chown root:root "${CONF_DIR}/install-result.env"
    chmod 600 "${CONF_DIR}/install-result.env"
}

assert_config_owner() {
    local st
    if [[ ! -f "${CONF_DIR}/config.json" ]]; then
        err "缺少 ${CONF_DIR}/config.json"
        exit 1
    fi
    st=$(stat -c '%U:%G %a' "${CONF_DIR}/config.json")
    if [[ "${st}" != "vtests:vtests 600" ]]; then
        err "config.json 所有权异常: ${st}（期望 vtests:vtests 600）"
        exit 1
    fi
}

install_service() {
    cp "${INSTALL_DIR}/systemd/vtests.service" "${SERVICE}"
    cp "${INSTALL_DIR}/vtests.sh" "${BIN}"
    chmod +x "${BIN}" "${INSTALL_DIR}/app/main.py" "${INSTALL_DIR}/install.sh"
    systemctl daemon-reload
    systemctl enable vtests
    systemctl restart vtests
}

wait_healthz() {
    local port=$1 path=$2 i
    for i in $(seq 1 40); do
        if curl -fsS --max-time 1 "http://127.0.0.1:${port}${path}/healthz" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

uninstall() {
    need_root
    systemctl stop vtests 2>/dev/null || true
    systemctl disable vtests 2>/dev/null || true
    rm -f "${SERVICE}" "${BIN}"
    rm -rf "${DROPIN_DIR}"
    systemctl daemon-reload || true
    rm -rf "${INSTALL_DIR}" "${CONF_DIR}" "${STATE_DIR}" "${LOG_DIR}"
    if getent passwd vtests >/dev/null 2>&1; then
        userdel vtests 2>/dev/null || true
    fi
    ok "vtests 已卸载"
}

print_done() {
    local port=$1 path=$2 password=$3
    # shellcheck source=/dev/null
    . "${CONF_DIR}/install-result.env"
    password="${password:-${PASSWORD:-}}"
    echo
    ok "安装完成"
    echo -e "面板地址: ${green}${URL}${plain}"
    echo -e "端口:     ${port}"
    echo -e "路径:     ${path}/"
    echo -e "密码:     ${green}${password}${plain}"
    echo
    echo "管理命令: vtests"
    echo "默认不会加压。请在云安全组 / 安全列表放行端口 ${port}，或用 SSH 隧道："
    echo "  ssh -L ${port}:127.0.0.1:${port} 用户@服务器"
    echo "本安装未修改主机防火墙 / iptables。"
    if [[ -r /proc/meminfo ]]; then
        local total
        total=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
        if [[ ${total} -le 1536 ]]; then
            echo
            warn "检测到内存约 ${total} MB。默认 CPU 10% / 内存 64 MB，且不会自动加压。"
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
    ensure_user
    local info port path password
    info=$(init_config | tail -n 1)
    port=$(echo "${info}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["port"])')
    path=$(echo "${info}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["base_path"])')
    password=$(echo "${info}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')
    write_result "${port}" "${path}" "${password}"
    if [[ -z "${password}" && -f "${CONF_DIR}/install-result.env" ]]; then
        password=$(awk -F= '/^PASSWORD=/ {print substr($0, index($0,"=")+1); exit}' "${CONF_DIR}/install-result.env")
    fi
    write_limits_dropin
    install_service
    maybe_open_firewall "${port}"
    local healthy=0
    if wait_healthz "${port}" "${path}"; then
        healthy=1
    fi
    assert_config_owner
    if systemctl is-active --quiet vtests && [[ ${healthy} -eq 1 ]]; then
        print_done "${port}" "${path}" "${password}"
    else
        err "服务启动失败，查看: journalctl -u vtests -e"
        systemctl status vtests --no-pager || true
        exit 1
    fi
}

main "$@"
