#!/usr/bin/env bash
# vtests one-click installer (Ubuntu 24.04 prototype)
set -euo pipefail

red='\033[0;31m'
green='\033[0;32m'
yellow='\033[0;33m'
plain='\033[0m'

REPO="${VTESTS_REPO:-minng2018/vtests}"
BRANCH="${VTESTS_BRANCH:-main}"
INSTALL_DIR="${VTESTS_INSTALL_DIR:-/opt/vtests}"
CONF_DIR="${VTESTS_CONF_DIR:-/etc/vtests}"
SERVICE=/etc/systemd/system/vtests.service
DROPIN_DIR=/etc/systemd/system/vtests.service.d
BIN=/usr/bin/vtests
STATE_DIR=/var/lib/vtests
LOG_DIR=/var/log/vtests

PROD_HOSTS=(beeman.beeorbit.net beenovel.beeorbit.net)

tls_paths() {
    NGINX_ROOT="${VTESTS_NGINX_ROOT:-/etc/nginx}"
    BACKUP_ROOT="${VTESTS_BACKUP_ROOT:-/var/backups}"
    WEBROOT="${VTESTS_WEBROOT:-/var/www/html}"
    LE_LIVE="${VTESTS_LE_LIVE:-/etc/letsencrypt/live}"
    LE_RENEWAL="${VTESTS_LE_RENEWAL:-/etc/letsencrypt/renewal}"
}

tls_paths

NGINX_BACKUP=""
TLS_ERROR=""
TLS_DID_BACKUP=0
TLS_DID_CERTBOT=0
TLS_DOMAIN=""

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
    if [[ -n "${VTESTS_PUBLIC_IP:-}" ]]; then
        printf '%s\n' "${VTESTS_PUBLIC_IP}"
        return
    fi
    curl -4 -fsS --connect-timeout 5 https://ifconfig.me 2>/dev/null \
        || curl -4 -fsS --connect-timeout 5 https://api.ipify.org 2>/dev/null \
        || hostname -I 2>/dev/null | awk '{print $1}' \
        || echo "服务器IP"
}

script_dir() {
    cd "$(dirname "${BASH_SOURCE[0]}")" && pwd
}

cfg_get() {
    local key=$1
    python3 - "${VTESTS_CONF_DIR:-${CONF_DIR}}/config.json" "${key}" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
try:
    cfg = json.load(open(path, encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)
val = cfg.get(key, "")
if isinstance(val, bool):
    print("true" if val else "false")
elif val is None:
    print("")
else:
    print(val)
PY
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
    local port=$1 path=$2 password=$3
    local ip listen domain ssl_flag url
    ip=$(public_ip)
    listen=$(cfg_get listen)
    domain=$(cfg_get domain)
    if [[ -z "${listen}" ]]; then
        listen="${VTESTS_LISTEN:-0.0.0.0}"
    fi
    if [[ "$(cfg_get ssl_enabled)" == "true" && -n "${domain}" ]]; then
        ssl_flag=1
        listen="127.0.0.1"
        url="https://${domain}${path}/"
    else
        ssl_flag=0
        domain=""
        url="http://${ip}:${port}${path}/"
    fi
    if [[ -z "${password}" && -f "${CONF_DIR}/install-result.env" ]]; then
        password=$(awk -F= '/^PASSWORD=/ {print substr($0, index($0,"=")+1); exit}' "${CONF_DIR}/install-result.env")
    fi
    cat > "${CONF_DIR}/install-result.env" <<EOF
PORT=${port}
BASE_PATH=${path}
PASSWORD=${password}
URL=${url}
LOCAL_URL=http://127.0.0.1:${port}${path}/
LISTEN=${listen}
SSL_ENABLED=${ssl_flag}
DOMAIN=${domain}
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

tls_dry_run() {
    [[ "${VTESTS_TLS_DRY_RUN:-}" == "1" ]]
}

tls_template_dir() {
    local d="${VTESTS_INSTALL_DIR:-${INSTALL_DIR}}"
    if [[ -d "${d}/nginx" ]]; then
        printf '%s\n' "${d}/nginx"
    else
        printf '%s\n' "$(script_dir)/nginx"
    fi
}

vhost_available() {
    tls_paths
    printf '%s\n' "${NGINX_ROOT}/sites-available/vtests.conf"
}

vhost_enabled() {
    tls_paths
    printf '%s\n' "${NGINX_ROOT}/sites-enabled/vtests.conf"
}

normalize_domain() {
    printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

protected_site_domain() {
    case "${1:-}" in
        beeman.beeorbit.net|beenovel.beeorbit.net) return 0 ;;
    esac
    return 1
}

valid_domain() {
    local d=$1
    [[ -n "${d}" ]] || return 1
    [[ "${d}" != *://* ]] || return 1
    [[ "${d}" != */* ]] || return 1
    [[ "${d}" != *:* ]] || return 1
    [[ "${d}" != localhost ]] || return 1
    [[ ! "${d}" =~ ^[0-9]+(\.[0-9]+){3}$ ]] || return 1
    if protected_site_domain "${d}"; then
        return 1
    fi
    [[ "${d}" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]
}

domain_re_escape() {
    printf '%s' "${1:-}" | sed 's/[.[*^$()+?{|]/\\&/g'
}

server_name_mentions() {
    local file=$1 domain=$2 escaped
    local real=$file
    [[ -e "${file}" || -L "${file}" ]] || return 1
    if [[ -L "${file}" ]]; then
        real=$(readlink -f "${file}" 2>/dev/null || printf '%s' "${file}")
    fi
    [[ -f "${real}" ]] || return 1
    escaped=$(domain_re_escape "${domain}")
    grep -E "^[[:space:]]*server_name[[:space:]]+([^;]*[[:space:]])?${escaped}([[:space:]]|;|$)" "${real}" >/dev/null 2>&1
}

prompt_domain() {
    local raw=""
    if [[ "${VTESTS_NONINTERACTIVE:-}" == "1" || ! -t 0 ]]; then
        raw="${VTESTS_DOMAIN:-}"
    elif [[ -n "${VTESTS_DOMAIN:-}" ]]; then
        raw="${VTESTS_DOMAIN}"
    else
        echo "请输入已解析到本机公网 IPv4 的域名"
        echo "（直接回车 = 使用 IP:端口 HTTP，不申请证书）:"
        read -r -p "" raw || raw=""
        raw=$(normalize_domain "${raw}")
        if [[ -n "${raw}" ]] && ! valid_domain "${raw}"; then
            warn "域名无效，请再输入一次"
            echo "请输入已解析到本机公网 IPv4 的域名"
            echo "（直接回车 = 使用 IP:端口 HTTP，不申请证书）:"
            read -r -p "" raw || raw=""
        fi
    fi
    raw=$(normalize_domain "${raw}")
    if [[ -n "${raw}" ]] && ! valid_domain "${raw}"; then
        warn "域名无效，继续使用 IP:端口 HTTP"
        raw=""
    fi
    printf '%s\n' "${raw}"
}

resolve_a_records() {
    local domain=$1 recs=""
    if [[ -n "${VTESTS_FAKE_A_RECORDS:-}" ]]; then
        printf '%s\n' ${VTESTS_FAKE_A_RECORDS}
        return 0
    fi
    if command -v getent >/dev/null 2>&1; then
        recs=$(getent ahostsv4 "${domain}" 2>/dev/null | awk '{print $1}' | sort -u || true)
    fi
    if [[ -z "${recs}" ]] && command -v dig >/dev/null 2>&1; then
        recs=$(dig +short A "${domain}" 2>/dev/null || true)
    fi
    if [[ -z "${recs}" ]]; then
        recs=$(python3 - "${domain}" <<'PY' || true
import socket, sys
host = sys.argv[1]
try:
    infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
except OSError:
    sys.exit(0)
seen = []
for info in infos:
    ip = info[4][0]
    if ip not in seen:
        seen.append(ip)
        print(ip)
PY
)
    fi
    printf '%s\n' "${recs}"
}

dns_ok_for_tls() {
    local domain=$1 ip recs
    ip=$(public_ip)
    if [[ -z "${ip}" || "${ip}" == "服务器IP" ]]; then
        warn "无法获取本机公网 IPv4，跳过证书"
        TLS_ERROR="无法获取本机公网 IPv4"
        return 1
    fi
    recs=$(resolve_a_records "${domain}" | sed '/^$/d' || true)
    if [[ -z "${recs}" ]]; then
        warn "域名未解析，跳过证书"
        TLS_ERROR="域名未解析"
        return 1
    fi
    if ! printf '%s\n' "${recs}" | grep -qx "${ip}"; then
        warn "解析到 $(printf '%s' "${recs}" | tr '\n' ' ')，本机是 ${ip}，跳过证书"
        TLS_ERROR="A 记录不含本机 IPv4"
        return 1
    fi
    if command -v getent >/dev/null 2>&1; then
        if getent ahostsv6 "${domain}" >/dev/null 2>&1; then
            warn "检测到 IPv6，HTTP-01 可能被 LE 走 AAAA；请保证 AAAA 也指向本机或不要发布 AAAA"
        fi
    fi
    return 0
}

has_global_ipv6() {
    if [[ "${VTESTS_LISTEN_IPV6:-}" == "0" ]]; then
        return 1
    fi
    if [[ "${VTESTS_LISTEN_IPV6:-}" == "1" ]]; then
        return 0
    fi
    ip -6 addr show scope global 2>/dev/null | grep -q "inet6"
}

port_held_by_nginx() {
    local port=$1 out
    if ! command -v ss >/dev/null 2>&1; then
        return 0
    fi
    out=$(ss -H -ltnp "( sport = :${port} )" 2>/dev/null || true)
    [[ -z "${out}" ]] && return 0
    printf '%s' "${out}" | grep -q nginx
}

http_ports_blocked() {
    local port
    for port in 80 443; do
        if port_used "${port}" && ! port_held_by_nginx "${port}"; then
            return 0
        fi
    done
    return 1
}

server_name_taken() {
    local domain=$1 f base had_nullglob=0
    tls_paths
    shopt -q nullglob && had_nullglob=1
    shopt -s nullglob
    for f in "${NGINX_ROOT}/sites-enabled/"*; do
        base=$(basename "${f}")
        [[ "${base}" == "vtests.conf" ]] && continue
        if server_name_mentions "${f}" "${domain}"; then
            [[ "${had_nullglob}" == "1" ]] || shopt -u nullglob
            return 0
        fi
    done
    [[ "${had_nullglob}" == "1" ]] || shopt -u nullglob
    return 1
}

vhost_is_ours() {
    local file=$1 domain=${2:-}
    local real base
    [[ -e "${file}" || -L "${file}" ]] || return 1
    real=$(readlink -f "${file}" 2>/dev/null || printf '%s' "${file}")
    [[ -f "${real}" ]] || return 1
    if head -n 20 "${real}" | grep -q "managed-by: vtests"; then
        return 0
    fi
    base=$(basename "${real}")
    if [[ "${base}" != "vtests.conf" ]]; then
        base=$(basename "${file}")
    fi
    [[ "${base}" == "vtests.conf" ]] || return 1
    [[ -n "${domain}" ]] || return 1
    server_name_mentions "${real}" "${domain}"
}

ssl_options_text() {
    if [[ -f /etc/letsencrypt/options-ssl-nginx.conf ]]; then
        printf '    include /etc/letsencrypt/options-ssl-nginx.conf;\n'
    else
        printf '    ssl_protocols TLSv1.2 TLSv1.3;\n'
        printf '    ssl_prefer_server_ciphers off;\n'
        printf '    ssl_ciphers HIGH:!aNULL:!MD5;\n'
    fi
}

ssl_dhparam_text() {
    if [[ -f /etc/letsencrypt/ssl-dhparams.pem ]]; then
        printf '    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;\n'
    fi
}

render_vhost() {
    local mode=$1 domain=$2 port=$3
    local src ipv6_80="" ipv6_443=""
    if [[ "${mode}" == "ssl" ]]; then
        src="$(tls_template_dir)/vtests-ssl.conf.template"
    else
        src="$(tls_template_dir)/vtests.conf.template"
    fi
    if [[ ! -f "${src}" ]]; then
        TLS_ERROR="缺少 nginx 模板 ${src}"
        return 1
    fi
    if has_global_ipv6; then
        ipv6_80="    listen [::]:80;"
        ipv6_443="    listen [::]:443 ssl;"
    fi
    python3 - "${src}" "${domain}" "${port}" "${ipv6_80}" "${ipv6_443}" \
        "$(ssl_options_text)" "$(ssl_dhparam_text)" <<'PY'
import sys
src, domain, port, ipv6_80, ipv6_443, ssl_opt, ssl_dh = sys.argv[1:8]
text = open(src, encoding="utf-8").read()
repl = {
    "__DOMAIN__": domain,
    "__PANEL_PORT__": port,
    "__LISTEN_IPV6_80__": ipv6_80,
    "__LISTEN_IPV6_443__": ipv6_443,
    "__SSL_OPTIONS__": ssl_opt.rstrip("\n"),
    "__SSL_DHPARAM__": ssl_dh.rstrip("\n"),
}
for key, val in repl.items():
    if val:
        text = text.replace(key, val)
    else:
        lines = []
        for line in text.splitlines(True):
            if key in line:
                continue
            lines.append(line)
        text = "".join(lines)
print(text, end="" if text.endswith("\n") else "\n")
PY
}

write_vhost_file() {
    local body=$1
    local available enabled tmp
    available=$(vhost_available)
    enabled=$(vhost_enabled)
    mkdir -p "$(dirname "${available}")" "$(dirname "${enabled}")"
    tmp=$(mktemp)
    printf '%s' "${body}" > "${tmp}"
    install -m 644 "${tmp}" "${available}"
    rm -f "${tmp}"
    ln -sfn "${available}" "${enabled}"
}

write_fixture_nginx_conf() {
    local dir wrap
    tls_paths
    dir="${VTESTS_BACKUP_ROOT:-/tmp}/vtests-nginx-t"
    mkdir -p "${dir}" "${NGINX_ROOT}/sites-enabled"
    wrap="${dir}/nginx.conf"
    cat > "${wrap}" <<EOF
worker_processes 1;
error_log ${dir}/error.log;
pid ${dir}/nginx.pid;
events { worker_connections 4; }
http {
    access_log off;
    include ${NGINX_ROOT}/sites-enabled/*;
}
EOF
    printf '%s\n' "${wrap}"
}

nginx_test() {
    local wrap
    tls_paths
    if ! command -v nginx >/dev/null 2>&1; then
        return 0
    fi
    if [[ -n "${VTESTS_NGINX_TEST_CONF:-}" ]]; then
        nginx -t -c "${VTESTS_NGINX_TEST_CONF}" >/dev/null 2>&1
        return
    fi
    if tls_dry_run && [[ -n "${VTESTS_NGINX_ROOT:-}" ]]; then
        wrap=$(write_fixture_nginx_conf) || return 1
        nginx -t -c "${wrap}" >/dev/null 2>&1
        return
    fi
    if [[ "${NGINX_ROOT}" == "/etc/nginx" ]] && ! tls_dry_run; then
        nginx -t >/dev/null 2>&1
        return
    fi
    return 0
}

nginx_reload() {
    if tls_dry_run; then
        return 0
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl reload nginx
        return
    fi
    nginx -s reload
}

nginx_test_reload() {
    local log
    if ! log=$(nginx_test 2>&1); then
        TLS_ERROR="nginx -t 失败: ${log}"
        return 1
    fi
    if ! tls_dry_run; then
        if ! log=$(nginx_reload 2>&1); then
            TLS_ERROR="nginx reload 失败: ${log}"
            return 1
        fi
    fi
    return 0
}

write_vhost_with_ipv6_fallback() {
    local mode=$1 domain=$2 port=$3
    local body
    body=$(render_vhost "${mode}" "${domain}" "${port}") || return 1
    write_vhost_file "${body}" || return 1
    if nginx_test_reload; then
        return 0
    fi
    if has_global_ipv6; then
        warn "含 IPv6 listen 的 nginx -t 失败，改为只听 IPv4"
        VTESTS_LISTEN_IPV6=0
        body=$(render_vhost "${mode}" "${domain}" "${port}") || return 1
        write_vhost_file "${body}" || return 1
        nginx_test_reload
        return
    fi
    return 1
}

backup_nginx() {
    local ts dest=""
    tls_paths
    ts=$(date +%Y%m%d%H%M%S)
    mkdir -p "${BACKUP_ROOT}" || return 1
    dest=$(mktemp -d "${BACKUP_ROOT}/vtests-nginx-${ts}-XXXXXX") || dest=""
    if [[ -z "${dest}" || "${dest}" != "${BACKUP_ROOT}/"* || ! -d "${dest}" ]]; then
        TLS_ERROR="备份 nginx 失败：无法创建备份目录"
        return 1
    fi
    if [[ -d "${NGINX_ROOT}" ]]; then
        if ! cp -a "${NGINX_ROOT}/." "${dest}/"; then
            rm -rf "${dest}"
            TLS_ERROR="备份 nginx 失败：无法复制"
            return 1
        fi
    fi
    NGINX_BACKUP="${dest}"
    TLS_DID_BACKUP=1
    ls -1dt "${BACKUP_ROOT}"/vtests-nginx-* 2>/dev/null | tail -n +4 | while read -r old; do
        rm -rf "${old}"
    done || true
    return 0
}

restore_nginx() {
    local sibling old
    tls_paths
    if [[ -z "${NGINX_BACKUP:-}" || ! -d "${NGINX_BACKUP}" ]]; then
        return 0
    fi
    mkdir -p "$(dirname "${NGINX_ROOT}")"
    sibling="${NGINX_ROOT}.restoring.$$"
    old="${NGINX_ROOT}.old.$$"
    rm -rf "${sibling}" "${old}"
    if ! cp -a "${NGINX_BACKUP}" "${sibling}"; then
        err "还原 nginx 失败：无法复制备份"
        rm -rf "${sibling}"
        return 1
    fi
    if [[ -e "${NGINX_ROOT}" ]] && ! mv "${NGINX_ROOT}" "${old}"; then
        err "还原 nginx 失败：无法移开当前树"
        rm -rf "${sibling}"
        return 1
    fi
    if ! mv "${sibling}" "${NGINX_ROOT}"; then
        err "还原 nginx 失败：无法就位备份"
        if [[ -e "${old}" ]]; then
            mv "${old}" "${NGINX_ROOT}" || true
        fi
        rm -rf "${sibling}"
        return 1
    fi
    rm -rf "${old}"
    if command -v nginx >/dev/null 2>&1 && [[ "${NGINX_ROOT}" == "/etc/nginx" ]] && ! tls_dry_run; then
        if nginx -t >/dev/null 2>&1; then
            nginx_reload || true
        else
            warn "还原后 nginx -t 仍失败，未 reload"
        fi
    fi
    return 0
}

other_vhosts_unchanged() {
    local f base had_nullglob=0
    tls_paths
    [[ -n "${NGINX_BACKUP:-}" && -d "${NGINX_BACKUP}" ]] || return 0
    if [[ -d "${NGINX_BACKUP}/sites-enabled" && -d "${NGINX_ROOT}/sites-enabled" ]]; then
        shopt -q nullglob && had_nullglob=1
        shopt -s nullglob
        for f in "${NGINX_BACKUP}/sites-enabled/"*; do
            base=$(basename "${f}")
            [[ "${base}" == "vtests.conf" ]] && continue
            if ! diff -q "${f}" "${NGINX_ROOT}/sites-enabled/${base}" >/dev/null 2>&1; then
                TLS_ERROR="sites-enabled 中非 vtests 文件被改动"
                [[ "${had_nullglob}" == "1" ]] || shopt -u nullglob
                return 1
            fi
        done
        [[ "${had_nullglob}" == "1" ]] || shopt -u nullglob
    fi
    return 0
}

probe_https_host() {
    local host=$1
    local code
    code=$(curl -4 -sS -o /dev/null -w '%{http_code}' --max-time 10 \
        --resolve "${host}:443:127.0.0.1" "https://${host}/" 2>/dev/null || true)
    if [[ -z "${code}" ]]; then
        code=000
    fi
    printf '%s\n' "${code}"
}

capture_prod_baseline() {
    BASELINE_BEEMAN=$(probe_https_host beeman.beeorbit.net)
    BASELINE_BEENOVEL=$(probe_https_host beenovel.beeorbit.net)
}

prod_sites_regressed() {
    local post
    post=$(probe_https_host beeman.beeorbit.net)
    if [[ "${BASELINE_BEEMAN:-}" == "200" && "${post}" != "200" ]]; then
        TLS_ERROR="beeman.beeorbit.net 基线 200，TLS 后 ${post}"
        return 0
    fi
    if [[ "${BASELINE_BEEMAN:-}" != "200" ]]; then
        warn "beeman.beeorbit.net 站点原先就不通，不视为 vtests 回归"
    fi
    post=$(probe_https_host beenovel.beeorbit.net)
    if [[ "${BASELINE_BEENOVEL:-}" == "200" && "${post}" != "200" ]]; then
        TLS_ERROR="beenovel.beeorbit.net 基线 200，TLS 后 ${post}"
        return 0
    fi
    if [[ "${BASELINE_BEENOVEL:-}" != "200" ]]; then
        warn "beenovel.beeorbit.net 站点原先就不通，不视为 vtests 回归"
    fi
    return 1
}

site_code_regressed() {
    local baseline=$1 post=$2
    [[ "${baseline}" == "200" && "${post}" != "200" ]]
}

certbot_lineage_list() {
    python3 -c 'import re,sys
text=sys.stdin.read()
name=None
for line in text.splitlines():
    m=re.search(r"Certificate Name:\s*(\S+)", line)
    if m:
        name=m.group(1)
        continue
    m=re.search(r"Domains:\s*(.+)", line)
    if m and name:
        print(name+"\t"+m.group(1).strip())
        name=None
'
}

vtests_cert_may_delete() {
    local panel=$1
    shift
    local domains=("$@")
    local d
    if [[ ${#domains[@]} -ne 1 ]]; then
        return 1
    fi
    d=${domains[0]}
    if [[ "${d}" != "${panel}" ]]; then
        return 1
    fi
    case "${d}" in
        beeman.beeorbit.net|beenovel.beeorbit.net) return 1 ;;
    esac
    return 0
}

certbot_certificates_text() {
    if [[ -n "${VTESTS_CERTBOT_CERTIFICATES_FILE:-}" && -f "${VTESTS_CERTBOT_CERTIFICATES_FILE}" ]]; then
        cat "${VTESTS_CERTBOT_CERTIFICATES_FILE}"
        return
    fi
    if tls_dry_run; then
        return 0
    fi
    if command -v certbot >/dev/null 2>&1; then
        certbot certificates 2>/dev/null || true
    fi
}

maybe_delete_panel_cert() {
    local panel=$1
    local listing name domains
    [[ -n "${panel}" ]] || return 0
    listing=$(certbot_certificates_text || true)
    [[ -n "${listing}" ]] || return 0
    while IFS=$'\t' read -r name domains; do
        [[ -n "${name}" ]] || continue
        # shellcheck disable=SC2086
        if vtests_cert_may_delete "${panel}" ${domains}; then
            if tls_dry_run; then
                echo "would certbot delete --cert-name ${name}"
            else
                certbot delete --cert-name "${name}" --non-interactive >/dev/null 2>&1 || true
            fi
        fi
    done < <(printf '%s\n' "${listing}" | certbot_lineage_list)
}

remove_owned_vhost() {
    local domain=${1:-}
    local available enabled
    available=$(vhost_available)
    enabled=$(vhost_enabled)
    if vhost_is_ours "${available}" "${domain}" || vhost_is_ours "${enabled}" "${domain}"; then
        rm -f "${available}" "${enabled}"
        if command -v nginx >/dev/null 2>&1 && [[ "${NGINX_ROOT}" == "/etc/nginx" ]] && ! tls_dry_run; then
            if nginx -t >/dev/null 2>&1; then
                nginx_reload || true
            fi
        fi
        return 0
    fi
    return 1
}

sync_vhost_proxy_pass() {
    local port=$1
    local domain=${2:-}
    local available enabled body bak
    available=$(vhost_available)
    enabled=$(vhost_enabled)
    [[ -f "${available}" ]] || return 0
    domain="${domain:-$(cfg_get domain)}"
    vhost_is_ours "${available}" "${domain}" || return 0
    bak=$(mktemp)
    cp -a "${available}" "${bak}"
    body=$(python3 - "${available}" "${port}" <<'PY'
import re, sys
path, port = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
text = re.sub(
    r"proxy_pass\s+http://127\.0\.0\.1:\d+",
    "proxy_pass http://127.0.0.1:" + port,
    text,
)
sys.stdout.write(text)
PY
)
    write_vhost_file "${body}"
    if nginx_test_reload; then
        rm -f "${bak}"
        return 0
    fi
    cp -a "${bak}" "${available}"
    rm -f "${bak}"
    ln -sfn "${available}" "${enabled}"
    TLS_ERROR="${TLS_ERROR:-nginx -t 失败，已还原 vhost}"
    return 1
}

ensure_nginx_certbot() {
    if tls_dry_run; then
        return 0
    fi
    export DEBIAN_FRONTEND=noninteractive
    if ! command -v nginx >/dev/null 2>&1; then
        apt-get install -y --no-upgrade nginx || return 1
        systemctl enable --now nginx >/dev/null 2>&1 || true
    fi
    if ! command -v certbot >/dev/null 2>&1; then
        apt-get install -y --no-upgrade certbot || return 1
    fi
    return 0
}

run_certbot_webroot() {
    local domain=$1
    local extra=() log
    tls_paths
    if tls_dry_run; then
        TLS_ERROR="dry-run 跳过 Let's Encrypt"
        return 1
    fi
    mkdir -p "${WEBROOT}"
    if [[ -n "${VTESTS_ACME_EMAIL:-}" ]]; then
        extra+=(--email "${VTESTS_ACME_EMAIL}")
    else
        extra+=(--register-unsafely-without-email)
    fi
    if ! log=$(certbot certonly --webroot \
        -w "${WEBROOT}" \
        --non-interactive --agree-tos \
        --keep-until-expiring \
        --cert-name "${domain}" \
        -d "${domain}" \
        "${extra[@]}" 2>&1); then
        TLS_ERROR=$(printf '%s\n' "${log}" | tail -n 20)
        return 1
    fi
    if [[ ! -f "${LE_LIVE}/${domain}/fullchain.pem" || ! -f "${LE_LIVE}/${domain}/privkey.pem" ]]; then
        TLS_ERROR="证书文件不存在"
        return 1
    fi
    return 0
}

add_renewal_deploy_hook() {
    local domain=$1
    local conf
    tls_paths
    conf="${LE_RENEWAL}/${domain}.conf"
    [[ -f "${conf}" ]] || return 0
    if grep -q '^deploy_hook' "${conf}"; then
        sed -i 's|^deploy_hook.*|deploy_hook = systemctl reload nginx|' "${conf}"
    else
        if grep -q '^\[renewalparams\]' "${conf}"; then
            sed -i '/^\[renewalparams\]/a deploy_hook = systemctl reload nginx' "${conf}"
        else
            printf '\n[renewalparams]\ndeploy_hook = systemctl reload nginx\n' >> "${conf}"
        fi
    fi
}

apply_tls_config() {
    local domain=$1 listen=$2 ssl=$3
    as_vtests \
        PYTHONPATH="${INSTALL_DIR}" \
        VTESTS_CONFIG="${CONF_DIR}/config.json" \
        VTESTS_TLS_DOMAIN="${domain}" \
        VTESTS_TLS_LISTEN="${listen}" \
        VTESTS_TLS_SSL="${ssl}" \
        "${INSTALL_DIR}/venv/bin/python" - <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("PYTHONPATH", "/opt/vtests"))
from app.config import update_config

domain = os.environ.get("VTESTS_TLS_DOMAIN") or ""
listen = os.environ.get("VTESTS_TLS_LISTEN") or "0.0.0.0"
ssl = os.environ.get("VTESTS_TLS_SSL") == "true"

def mutate(cfg):
    cfg["domain"] = domain
    cfg["listen"] = listen
    cfg["ssl_enabled"] = ssl
    if ssl and domain:
        cfg["cert_path"] = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
        cfg["key_path"] = f"/etc/letsencrypt/live/{domain}/privkey.pem"
    else:
        cfg["cert_path"] = ""
        cfg["key_path"] = ""

update_config(mutate)
PY
}

restart_panel() {
    if tls_dry_run; then
        return 0
    fi
    systemctl restart vtests
}

probe_panel_https() {
    local domain=$1 path=$2
    curl -fsS -o /dev/null -k --max-time 10 \
        --resolve "${domain}:443:127.0.0.1" \
        "https://${domain}${path}/healthz" >/dev/null 2>&1
}

# Must not exit. Any failure returns 1 so the caller can tls_fallback.
setup_tls() {
    local domain=$1
    local port path
    TLS_ERROR=""
    TLS_DID_BACKUP=0
    TLS_DID_CERTBOT=0
    NGINX_BACKUP=""
    TLS_DOMAIN="${domain}"
    tls_paths
    [[ -n "${domain}" ]] || return 1
    if protected_site_domain "${domain}"; then
        warn "拒绝把生产站点域名用作面板域名"
        TLS_ERROR="禁止使用 ${domain}"
        return 1
    fi
    port=$(cfg_get port)
    path=$(cfg_get base_path)
    if [[ -z "${port}" ]]; then
        port="${VTESTS_PORT:-8088}"
    fi
    if [[ -z "${path}" ]]; then
        path="${VTESTS_WEB_BASE_PATH:-/}"
    fi
    if tls_dry_run && [[ "${NGINX_ROOT}" == "/etc/nginx" ]]; then
        render_vhost http "${domain}" "${port}" >/dev/null || return 1
        TLS_ERROR="VTESTS_TLS_DRY_RUN=1，未改生产 /etc/nginx，未调用 Let's Encrypt"
        return 1
    fi
    if ! tls_dry_run; then
        ensure_nginx_certbot || { TLS_ERROR="安装 nginx/certbot 失败"; return 1; }
        if http_ports_blocked; then
            warn "80/443 被非 Nginx 占用，跳过 TLS"
            TLS_ERROR="80/443 被非 Nginx 占用"
            return 1
        fi
        if server_name_taken "${domain}"; then
            warn "sites-enabled 已有相同 server_name，跳过 TLS，不覆盖他人 vhost"
            TLS_ERROR="server_name 已被占用"
            return 1
        fi
        dns_ok_for_tls "${domain}" || return 1
        capture_prod_baseline
    fi
    backup_nginx || { TLS_ERROR="备份 nginx 失败"; return 1; }
    write_vhost_with_ipv6_fallback http "${domain}" "${port}" || return 1
    if tls_dry_run; then
        TLS_ERROR="VTESTS_TLS_DRY_RUN=1，已写 HTTP vhost，未调用 Let's Encrypt"
        return 1
    fi
    TLS_DID_CERTBOT=1
    run_certbot_webroot "${domain}" || return 1
    write_vhost_with_ipv6_fallback ssl "${domain}" "${port}" || return 1
    other_vhosts_unchanged || return 1
    if prod_sites_regressed; then
        return 1
    fi
    apply_tls_config "${domain}" "127.0.0.1" true || { TLS_ERROR="写入 ssl_enabled 失败"; return 1; }
    restart_panel
    if ! wait_healthz "${port}" "${path}"; then
        TLS_ERROR="TLS 后面板 healthz 失败"
        return 1
    fi
    if ! probe_panel_https "${domain}" "${path}"; then
        TLS_ERROR="本机 --resolve HTTPS healthz 失败"
        return 1
    fi
    add_renewal_deploy_hook "${domain}"
    return 0
}

tls_fallback() {
    local port path password
    warn "证书签发失败，已回退为 IP:端口 HTTP，安装本身成功。"
    if [[ -n "${TLS_ERROR:-}" ]]; then
        echo "原因: ${TLS_ERROR}"
    fi
    if ! restore_nginx; then
        err "还原 nginx 失败"
    fi
    if [[ "${TLS_DID_CERTBOT:-0}" == "1" ]]; then
        maybe_delete_panel_cert "${TLS_DOMAIN:-}" || true
    fi
    if ! tls_dry_run; then
        apply_tls_config "" "0.0.0.0" false || true
        port=$(cfg_get port)
        path=$(cfg_get base_path)
        password=""
        if [[ -n "${port}" && -n "${path}" ]]; then
            write_result "${port}" "${path}" "${password}" || true
            restart_panel || true
            wait_healthz "${port}" "${path}" || true
        fi
    fi
    return 0
}

enable_tls() {
    local domain=$1
    setup_tls "${domain}" || tls_fallback
}

tls_uninstall_should_delete_cert() {
    local ssl=${1:-} domain=${2:-}
    [[ "${ssl}" == "true" && -n "${domain}" ]]
}

uninstall() {
    local domain="" ssl_now=""
    need_root
    if [[ -f "${CONF_DIR}/config.json" ]]; then
        domain=$(cfg_get domain || true)
        ssl_now=$(cfg_get ssl_enabled || true)
    fi
    systemctl stop vtests 2>/dev/null || true
    systemctl disable vtests 2>/dev/null || true
    rm -f "${SERVICE}" "${BIN}"
    rm -rf "${DROPIN_DIR}"
    systemctl daemon-reload || true
    remove_owned_vhost "${domain}" || true
    if tls_uninstall_should_delete_cert "${ssl_now}" "${domain}"; then
        maybe_delete_panel_cert "${domain}"
    fi
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
    if [[ "${SSL_ENABLED:-0}" == "1" ]]; then
        echo -e "本机备用: ${green}${LOCAL_URL:-http://127.0.0.1:${port}${path}/}${plain}"
        echo -e "端口:     ${port}"
        echo -e "路径:     ${path}/"
        echo -e "密码:     ${green}${password}${plain}"
        echo
        echo "管理命令: vtests"
        echo "证书:     /etc/letsencrypt/live/${DOMAIN}/"
        echo "续期:     复用本机 certbot.timer，不要另开 timer"
        echo "默认不会加压。"
    else
        echo -e "端口:     ${port}"
        echo -e "路径:     ${path}/"
        echo -e "密码:     ${green}${password}${plain}"
        echo
        echo "管理命令: vtests"
        echo "默认不会加压。请在云安全组 / 安全列表放行端口 ${port}，或用 SSH 隧道："
        echo "  ssh -L ${port}:127.0.0.1:${port} 用户@服务器"
        echo "本安装未修改主机防火墙 / iptables。"
    fi
    if [[ -n "${TLS_ERROR:-}" && "${SSL_ENABLED:-0}" != "1" && -n "${VTESTS_DOMAIN:-}" ]]; then
        echo
        warn "已请求域名但未启用 HTTPS。"
    fi
    if [[ -r /proc/meminfo ]]; then
        local total
        total=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
        if [[ ${total} -le 1536 ]]; then
            echo
            warn "检测到内存约 ${total} MB。默认 CPU 10% / 内存 64 MB，且不会自动加压。"
        fi
    fi
}

maybe_enable_tls() {
    local port=$1 path=$2 password=$3
    local domain ssl_now
    ssl_now=$(cfg_get ssl_enabled)
    if [[ "${ssl_now}" == "true" ]]; then
        if ! sync_vhost_proxy_pass "${port}" "$(cfg_get domain)"; then
            warn "proxy_pass 同步失败，已还原 vhost"
        fi
        write_result "${port}" "${path}" "${password}"
        return 0
    fi
    domain=$(prompt_domain)
    if [[ -z "${domain}" ]]; then
        return 0
    fi
    enable_tls "${domain}"
    write_result "${port}" "${path}" "${password}"
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
    local healthy=0
    if wait_healthz "${port}" "${path}"; then
        healthy=1
    fi
    assert_config_owner
    if ! systemctl is-active --quiet vtests || [[ ${healthy} -ne 1 ]]; then
        err "服务启动失败，查看: journalctl -u vtests -e"
        systemctl status vtests --no-pager || true
        exit 1
    fi
    maybe_enable_tls "${port}" "${path}" "${password}"
    if [[ "$(cfg_get ssl_enabled)" != "true" ]]; then
        maybe_open_firewall "${port}"
    fi
    print_done "${port}" "${path}" "${password}"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi

