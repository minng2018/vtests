#!/usr/bin/env bash
set -euo pipefail

red='\033[0;31m'
green='\033[0;32m'
yellow='\033[0;33m'
plain='\033[0m'

CONF=/etc/vtests/config.json
RESULT=/etc/vtests/install-result.env
APP_ROOT=/opt/vtests
APP_PY=/opt/vtests/venv/bin/python
LOCAL_INSTALL=/opt/vtests/install.sh

need_root() {
    if [[ ${EUID} -ne 0 ]]; then
        echo -e "${red}请使用 sudo vtests${plain}"
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

show_info() {
    if [[ -f ${RESULT} ]]; then
        # shellcheck source=/dev/null
        . ${RESULT}
        echo -e "地址: ${green}${URL}${plain}"
        echo "端口: ${PORT}"
        echo "路径: ${BASE_PATH}/"
        echo "密码: ${PASSWORD}"
    else
        echo "未找到安装信息"
    fi
    if systemctl is-active --quiet vtests; then
        echo -e "面板服务: ${green}运行中${plain}"
    else
        echo -e "面板服务: ${yellow}已停止${plain}"
    fi
}

reset_password() {
    local pw
    pw=$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(12))
PY
)
    as_vtests PYTHONPATH="${APP_ROOT}" VTESTS_CONFIG="${CONF}" NEW_PASSWORD="${pw}" "${APP_PY}" - <<'PY'
import os, sys
sys.path.insert(0, "/opt/vtests")
from app.auth import reset_password
reset_password(os.environ["NEW_PASSWORD"])
PY
    if [[ -f ${RESULT} ]]; then
        sed -i "s|^PASSWORD=.*|PASSWORD=${pw}|" ${RESULT}
        chown root:root ${RESULT}
        chmod 600 ${RESULT}
    fi
    echo -e "新密码: ${green}${pw}${plain}"
}

change_port() {
    local port=${1:-}
    if [[ -z "${port}" ]]; then
        read -r -p "新端口: " port
    fi
    if [[ ! ${port} =~ ^[0-9]+$ ]] || [[ ${port} -lt 1 || ${port} -gt 65535 ]]; then
        echo "端口无效"
        return
    fi
    as_vtests PYTHONPATH="${APP_ROOT}" VTESTS_CONFIG="${CONF}" "${APP_PY}" - "${port}" <<'PY'
import sys
sys.path.insert(0, "/opt/vtests")
from app.config import update_config

port = int(sys.argv[1])

def mutate(cfg):
    cfg["port"] = port

update_config(mutate)
PY
    if [[ -f ${RESULT} ]]; then
        sed -i "s/^PORT=.*/PORT=${port}/" ${RESULT}
        sed -i "s#:[0-9][0-9]*#:${port}#" ${RESULT}
        chown root:root ${RESULT}
        chmod 600 ${RESULT}
    fi
    systemctl restart vtests
    echo "已改为 ${port} 并重启面板服务"
}

do_uninstall() {
    if [[ -f "${LOCAL_INSTALL}" ]]; then
        bash "${LOCAL_INSTALL}" uninstall
        return
    fi
    bash <(curl -Ls "https://raw.githubusercontent.com/minng2018/vtests/main/install.sh") uninstall
}

menu() {
    need_root
    echo "vtests 管理"
    echo "启动/停止 = 面板服务 vtests.service，不是加压引擎。加压请在 Web 面板操作。"
    echo "  1) 查看面板信息"
    echo "  2) 启动面板"
    echo "  3) 停止面板"
    echo "  4) 重启面板"
    echo "  5) 重置密码"
    echo "  6) 修改端口"
    echo "  7) 卸载"
    echo "  0) 退出"
    read -r -p "选择: " n
    case "${n}" in
        1) show_info ;;
        2) systemctl start vtests; show_info ;;
        3) systemctl stop vtests ;;
        4) systemctl restart vtests; show_info ;;
        5) reset_password ;;
        6) change_port ;;
        7)
            read -r -p "确认卸载? [y/N] " y
            if [[ ${y} == y || ${y} == Y ]]; then
                do_uninstall
            fi
            ;;
        0) exit 0 ;;
        *) echo "无效选择" ;;
    esac
}

case "${1:-}" in
    status|info) need_root; show_info ;;
    start) need_root; systemctl start vtests ;;
    stop) need_root; systemctl stop vtests ;;
    restart) need_root; systemctl restart vtests ;;
    password) need_root; reset_password ;;
    port) need_root; change_port "${2:-}" ;;
    uninstall) need_root; do_uninstall ;;
    *) menu ;;
esac
