#!/usr/bin/env bash
set -euo pipefail

red='\033[0;31m'
green='\033[0;32m'
yellow='\033[0;33m'
plain='\033[0m'

CONF=/etc/vtests/config.json
RESULT=/etc/vtests/install-result.env

need_root() {
    if [[ ${EUID} -ne 0 ]]; then
        echo -e "${red}请使用 sudo vtests${plain}"
        exit 1
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
    systemctl is-active --quiet vtests && echo -e "服务: ${green}运行中${plain}" || echo -e "服务: ${yellow}已停止${plain}"
}

reset_password() {
    local pw
    pw=$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(12))
PY
)
    python3 - "${pw}" <<'PY'
import json, sys
from pathlib import Path
pw = sys.argv[1]
p = Path("/etc/vtests/config.json")
cfg = json.loads(p.read_text())
cfg["password"] = pw
p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
PY
    if [[ -f ${RESULT} ]]; then
        sed -i "s/^PASSWORD=.*/PASSWORD=${pw}/" ${RESULT}
    fi
    echo -e "新密码: ${green}${pw}${plain}"
}

change_port() {
    read -r -p "新端口: " port
    if [[ ! ${port} =~ ^[0-9]+$ ]] || [[ ${port} -lt 1 || ${port} -gt 65535 ]]; then
        echo "端口无效"
        return
    fi
    python3 - "${port}" <<'PY'
import json, sys
from pathlib import Path
port = int(sys.argv[1])
p = Path("/etc/vtests/config.json")
cfg = json.loads(p.read_text())
cfg["port"] = port
p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
PY
    if [[ -f ${RESULT} ]]; then
        sed -i "s/^PORT=.*/PORT=${port}/" ${RESULT}
        sed -i "s#:[0-9][0-9]*#:${port}#" ${RESULT}
    fi
    systemctl restart vtests
    echo "已改为 ${port} 并重启服务"
}

menu() {
    need_root
    echo "vtests 管理"
    echo "  1) 查看面板信息"
    echo "  2) 启动"
    echo "  3) 停止"
    echo "  4) 重启"
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
                if [[ -x /opt/vtests/install.sh ]]; then
                    bash /opt/vtests/install.sh uninstall
                else
                    bash <(curl -Ls "https://raw.githubusercontent.com/minng2018/vtests/main/install.sh") uninstall
                fi
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
    uninstall) need_root; bash <(curl -Ls "https://raw.githubusercontent.com/minng2018/vtests/main/install.sh") uninstall ;;
    *) menu ;;
esac
