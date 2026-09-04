#!/usr/bin/env bash
# TLS policy: owned vhost claim, certbot delete whitelist, dry-run render, rollback.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
export VTESTS_TLS_DRY_RUN=1
export VTESTS_LISTEN_IPV6=0
export VTESTS_INSTALL_DIR="${ROOT}"
export VTESTS_PUBLIC_IP=158.101.29.241
# shellcheck source=../install.sh
source "${ROOT}/install.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "ok - $*"; }

# --- domain validation ---
valid_domain "vt-frp.beeorbit.net" || fail "valid panel domain"
valid_domain "example.co.uk" || fail "valid multi-label"
valid_domain "localhost" && fail "localhost must be rejected"
valid_domain "127.0.0.1" && fail "ipv4 literal must be rejected"
valid_domain "https://vt-frp.beeorbit.net" && fail "protocol must be rejected"
valid_domain "vt-frp.beeorbit.net/path" && fail "path must be rejected"
valid_domain "" && fail "empty must be rejected"
valid_domain "beeman.beeorbit.net" && fail "beeman must be refused as panel domain"
valid_domain "beenovel.beeorbit.net" && fail "beenovel must be refused as panel domain"
protected_site_domain "beeman.beeorbit.net" || fail "beeman is protected"
protected_site_domain "vt-frp.beeorbit.net" && fail "panel domain is not protected"
pass "domain validation"

# --- container/CI smoke: empty domain stays IP HTTP (no LE) ---
(
    export VTESTS_NONINTERACTIVE=1
    unset VTESTS_DOMAIN || true
    got=$(prompt_domain)
    [[ -z "${got}" ]] || fail "noninteractive empty domain must be empty, got ${got}"
)
pass "empty domain noninteractive smoke"

# --- DNS precheck (injected A records; no live Let's Encrypt) ---
TLS_ERROR=""
VTESTS_FAKE_A_RECORDS="158.101.29.241" dns_ok_for_tls "panel.example.test" \
    || fail "A containing public IP must pass"
TLS_ERROR=""
VTESTS_FAKE_A_RECORDS="1.2.3.4 158.101.29.241 9.9.9.9" dns_ok_for_tls "panel.example.test" \
    || fail "extra A records still pass if public IP present"
TLS_ERROR=""
VTESTS_FAKE_A_RECORDS="1.2.3.4" dns_ok_for_tls "panel.example.test" \
    && fail "A without public IP must fail"
[[ "${TLS_ERROR}" == "A 记录不含本机 IPv4" ]] || fail "mismatch A should set TLS_ERROR"
TLS_ERROR=""
VTESTS_FAKE_A_RECORDS=$'\n' dns_ok_for_tls "panel.example.test" \
    && fail "empty A must fail"
[[ "${TLS_ERROR}" == "域名未解析" ]] || fail "empty A should set 域名未解析"
TLS_ERROR=""
VTESTS_PUBLIC_IP="服务器IP" VTESTS_FAKE_A_RECORDS="158.101.29.241" dns_ok_for_tls "panel.example.test" \
    && fail "missing public IP must fail"
[[ "${TLS_ERROR}" == "无法获取本机公网 IPv4" ]] || fail "missing public IP should set TLS_ERROR"
TLS_ERROR=""
pass "DNS precheck"

# --- cert delete whitelist ---
vtests_cert_may_delete "vt-frp.beeorbit.net" "vt-frp.beeorbit.net" \
    || fail "panel-only lineage must be deletable"
vtests_cert_may_delete "vt-frp.beeorbit.net" "beeman.beeorbit.net" "beenovel.beeorbit.net" \
    && fail "beeman+beenovel lineage must never be deleted"
vtests_cert_may_delete "vt-frp.beeorbit.net" "vt-frp.beeorbit.net" "extra.example" \
    && fail "SAN larger than panel domain must not be deleted"
vtests_cert_may_delete "beeman.beeorbit.net" "beeman.beeorbit.net" \
    && fail "beeman cert-name must never be deleted"
pass "cert delete policy"

listing=$(cat "${ROOT}/tests/fixtures/certbot-certificates-beeman.txt")
parsed=$(printf '%s\n' "${listing}" | certbot_lineage_list)
printf '%s\n' "${parsed}" | grep -q $'beeman.beeorbit.net\tbeeman.beeorbit.net beenovel.beeorbit.net' \
    || fail "parser must keep beeman+beenovel on one lineage"
printf '%s\n' "${parsed}" | grep -q $'vt-frp.beeorbit.net\tvt-frp.beeorbit.net' \
    || fail "parser must see panel lineage"
would=$(
    VTESTS_CERTBOT_CERTIFICATES_FILE="${ROOT}/tests/fixtures/certbot-certificates-beeman.txt" \
    VTESTS_TLS_DRY_RUN=1 \
    maybe_delete_panel_cert "vt-frp.beeorbit.net"
)
printf '%s\n' "${would}" | grep -q "would certbot delete --cert-name vt-frp.beeorbit.net" \
    || fail "panel cert should be offered for delete"
printf '%s\n' "${would}" | grep -q beeman && fail "must never delete beeman lineage"
printf '%s\n' "${would}" | grep -q beenovel && fail "must never delete beenovel lineage"
pass "certbot certificates fixture never deletes beeman+beenovel"

# --- vhost claim ---
claim=$(mktemp -d)
trap 'rm -rf "${claim}" "${scratch:-}"' EXIT
mkdir -p "${claim}"

cat > "${claim}/managed.conf" <<'EOF'
# managed-by: vtests
# vtests-domain: vt-frp.beeorbit.net
server { server_name vt-frp.beeorbit.net; }
EOF
vhost_is_ours "${claim}/managed.conf" "vt-frp.beeorbit.net" || fail "managed-by marker should claim"

cat > "${claim}/vtests.conf" <<'EOF'
server {
    server_name vt-frp.beeorbit.net;
}
EOF
vhost_is_ours "${claim}/vtests.conf" "vt-frp.beeorbit.net" || fail "filename+server_name should claim"
vhost_is_ours "${claim}/vtests.conf" "other.example" && fail "wrong server_name must not claim"

cat > "${claim}/beeman.conf" <<'EOF'
server {
    server_name beeman.beeorbit.net;
}
EOF
vhost_is_ours "${claim}/beeman.conf" "vt-frp.beeorbit.net" && fail "beeman vhost must not be claimed"
vhost_is_ours "${claim}/beeman.conf" "beeman.beeorbit.net" && fail "foreign filename must not be claimed"
pass "vhost claim (managed-by / filename+server_name)"

# --- server_name occupancy (typical single-space line) ---
export VTESTS_NGINX_ROOT="${claim}/nginx"
mkdir -p "${VTESTS_NGINX_ROOT}/sites-enabled"
cat > "${VTESTS_NGINX_ROOT}/sites-enabled/beeman.conf" <<'EOF'
server {
    listen 80;
    server_name beeman.beeorbit.net;
}
EOF
server_name_taken "beeman.beeorbit.net" || fail "single-space server_name must match"
server_name_taken "beenovel.beeorbit.net" && fail "other name must not match beeman vhost"
server_name_taken "vt-frp.beeorbit.net" && fail "panel name must not match beeman vhost"
cat > "${VTESTS_NGINX_ROOT}/sites-enabled/multi.conf" <<'EOF'
    server_name foo.example.com vt-frp.beeorbit.net;
EOF
server_name_taken "vt-frp.beeorbit.net" || fail "domain among several server_name tokens must match"
# skip our own vhost
cat > "${VTESTS_NGINX_ROOT}/sites-enabled/vtests.conf" <<'EOF'
    server_name already.example;
EOF
server_name_taken "already.example" && fail "must ignore sites-enabled/vtests.conf"
# nullglob must not leak
shopt -u nullglob
server_name_taken "no-such.example" && fail "no-such should not be taken"
if shopt -q nullglob; then
    fail "nullglob leaked from server_name_taken"
fi
unset VTESTS_NGINX_ROOT
tls_paths
pass "server_name_taken matches typical vhosts and restores nullglob"

empty=$(mktemp -d)
export VTESTS_NGINX_ROOT="${empty}/nginx"
export VTESTS_BACKUP_ROOT="${empty}/backups"
mkdir -p "${VTESTS_NGINX_ROOT}/sites-enabled" "${VTESTS_NGINX_ROOT}/sites-available"
backup_nginx || fail "backup empty nginx tree"
[[ -n "${NGINX_BACKUP:-}" && "${NGINX_BACKUP}" == "${VTESTS_BACKUP_ROOT}/"* && -d "${NGINX_BACKUP}" ]] \
    || fail "backup dest must be a dir under BACKUP_ROOT"
other_vhosts_unchanged || fail "empty sites-enabled must not fail-close TLS"
TLS_ERROR=""
other_vhosts_unchanged
[[ -z "${TLS_ERROR:-}" ]] || fail "empty sites-enabled must not set TLS_ERROR"
rm -rf "${empty}"
unset VTESTS_NGINX_ROOT VTESTS_BACKUP_ROOT
NGINX_BACKUP=""
tls_paths
pass "empty sites-enabled other_vhosts_unchanged returns 0"

# --- dry-run vhost render ---
http_body=$(render_vhost http vt-frp.beeorbit.net 41234) || fail "render http"
printf '%s\n' "${http_body}" | grep -q "managed-by: vtests" || fail "http header marker"
printf '%s\n' "${http_body}" | grep -q "vtests-domain: vt-frp.beeorbit.net" || fail "http domain comment"
printf '%s\n' "${http_body}" | grep -q "server_name vt-frp.beeorbit.net;" || fail "http server_name"
printf '%s\n' "${http_body}" | grep -q "proxy_pass http://127.0.0.1:41234;" || fail "http proxy_pass"
printf '%s\n' "${http_body}" | grep -q "listen 80;" || fail "http listen 80"
printf '%s\n' "${http_body}" | grep -q "listen \\[::\\]" && fail "ipv6 must be omitted"
printf '%s\n' "${http_body}" | grep -q default_server && fail "must not use default_server"
printf '%s\n' "${http_body}" | grep -q "acme-challenge" || fail "http acme location"

ssl_body=$(render_vhost ssl vt-frp.beeorbit.net 41234) || fail "render ssl"
printf '%s\n' "${ssl_body}" | grep -q "listen 443 ssl;" || fail "ssl listen 443"
printf '%s\n' "${ssl_body}" | grep -q "ssl_certificate     /etc/letsencrypt/live/vt-frp.beeorbit.net/fullchain.pem;" \
    || fail "ssl fullchain"
printf '%s\n' "${ssl_body}" | grep -q "return 301 https://\$host\$request_uri;" || fail "http->https redirect"
printf '%s\n' "${ssl_body}" | grep -q "X-Forwarded-Proto https;" || fail "https forwarded proto"
printf '%s\n' "${ssl_body}" | grep -q default_server && fail "ssl must not use default_server"
pass "dry-run vhost render"

# --- backup / restore ---
scratch=$(mktemp -d)
export VTESTS_NGINX_ROOT="${scratch}/nginx"
export VTESTS_BACKUP_ROOT="${scratch}/backups"
mkdir -p "${VTESTS_NGINX_ROOT}/sites-available" "${VTESTS_NGINX_ROOT}/sites-enabled"
printf 'keep-beeman\n' > "${VTESTS_NGINX_ROOT}/sites-available/beeman.conf"
ln -sfn "${VTESTS_NGINX_ROOT}/sites-available/beeman.conf" "${VTESTS_NGINX_ROOT}/sites-enabled/beeman.conf"
printf 'keep-beenovel\n' > "${VTESTS_NGINX_ROOT}/sites-available/beenovel.conf"
ln -sfn "${VTESTS_NGINX_ROOT}/sites-available/beenovel.conf" "${VTESTS_NGINX_ROOT}/sites-enabled/beenovel.conf"

backup_nginx || fail "backup_nginx"
[[ -n "${NGINX_BACKUP:-}" && -d "${NGINX_BACKUP}" ]] || fail "backup dir missing"
grep -qx "keep-beeman" "${NGINX_BACKUP}/sites-available/beeman.conf" || fail "backup should copy beeman"

printf 'tampered-beeman\n' > "${VTESTS_NGINX_ROOT}/sites-available/beeman.conf"
printf '# managed-by: vtests\nserver { server_name vt-frp.beeorbit.net; }\n' \
    > "${VTESTS_NGINX_ROOT}/sites-available/vtests.conf"
ln -sfn "${VTESTS_NGINX_ROOT}/sites-available/vtests.conf" "${VTESTS_NGINX_ROOT}/sites-enabled/vtests.conf"

restore_nginx || fail "restore_nginx"
grep -qx "keep-beeman" "${VTESTS_NGINX_ROOT}/sites-available/beeman.conf" || fail "beeman must be restored"
grep -qx "keep-beenovel" "${VTESTS_NGINX_ROOT}/sites-available/beenovel.conf" || fail "beenovel must be restored"
[[ -e "${VTESTS_NGINX_ROOT}/sites-available/vtests.conf" ]] && fail "vtests vhost must not survive restore"
pass "rollback restores nginx backup"

# setup_tls dry-run writes then tls_fallback restores (valid nginx so fixture -t can run)
cat > "${VTESTS_NGINX_ROOT}/sites-available/beeman.conf" <<'EOF'
server {
    listen 80;
    server_name beeman.beeorbit.net;
    return 200;
}
EOF
cat > "${VTESTS_NGINX_ROOT}/sites-available/beenovel.conf" <<'EOF'
server {
    listen 80;
    server_name beenovel.beeorbit.net;
    return 200;
}
EOF
export VTESTS_PORT=41234
export VTESTS_CERTBOT_CERTIFICATES_FILE="${ROOT}/tests/fixtures/certbot-certificates-beeman.txt"
setup_tls "vt-frp.beeorbit.net" && fail "dry-run setup_tls should return 1"
[[ "${TLS_DID_CERTBOT:-0}" == "1" ]] && fail "dry-run must not set TLS_DID_CERTBOT"
fb=$(tls_fallback)
printf '%s\n' "${fb}" | grep -q "would certbot delete" && fail "fallback without certbot must not delete certs"
grep -q "server_name beeman.beeorbit.net" "${VTESTS_NGINX_ROOT}/sites-available/beeman.conf" \
    || fail "fallback must keep beeman"
pass "setup_tls || tls_fallback dry-run restores tree"

backup_nginx || fail "backup before certbot-flag fallback"
TLS_DID_CERTBOT=1
TLS_DOMAIN="vt-frp.beeorbit.net"
flag_fb=$(tls_fallback)
printf '%s\n' "${flag_fb}" | grep -q "would certbot delete --cert-name vt-frp.beeorbit.net" \
    || fail "after certbot invoke, fallback may delete panel-only cert"
printf '%s\n' "${flag_fb}" | grep -q beeman && fail "fallback must never delete beeman"
TLS_DID_CERTBOT=0
pass "tls_fallback deletes cert only when TLS_DID_CERTBOT=1"

tls_uninstall_should_delete_cert true "vt-frp.beeorbit.net" || fail "ssl+domain may delete panel cert"
tls_uninstall_should_delete_cert false "vt-frp.beeorbit.net" && fail "ssl_enabled=false must not delete cert"
tls_uninstall_should_delete_cert true "" && fail "empty domain must not delete cert"
pass "uninstall cert delete gated on ssl_enabled"

setup_tls "beeman.beeorbit.net" && fail "must refuse beeman as panel domain"
[[ "${TLS_DID_CERTBOT:-0}" == "1" ]] && fail "refused domain must not invoke certbot"
pass "protected production domains refused before write/certbot"

if command -v nginx >/dev/null 2>&1; then
    cat > "${VTESTS_NGINX_ROOT}/sites-available/vtests.conf" <<'EOF'
# managed-by: vtests
server {
    listen 80;
    server_name vt-frp.beeorbit.net;
    location / { proxy_pass http://127.0.0.1:41234; }
}
EOF
    ln -sfn "${VTESTS_NGINX_ROOT}/sites-available/vtests.conf" "${VTESTS_NGINX_ROOT}/sites-enabled/vtests.conf"
    nginx_test || fail "dry-run fixture nginx -t should pass"
    sync_vhost_proxy_pass 55551 "vt-frp.beeorbit.net" || fail "proxy_pass sync should pass nginx -t"
    grep -q "proxy_pass http://127.0.0.1:55551;" "${VTESTS_NGINX_ROOT}/sites-available/vtests.conf" \
        || fail "proxy_pass should update"
    pass "dry-run VTESTS_NGINX_ROOT nginx -t"
else
    pass "nginx binary absent; skipped fixture nginx -t"
fi

# --- uninstall refuses foreign vhost ---
printf '# not ours\nserver { server_name beeman.beeorbit.net; }\n' \
    > "${VTESTS_NGINX_ROOT}/sites-available/beeman.conf"
ln -sfn "${VTESTS_NGINX_ROOT}/sites-available/beeman.conf" "${VTESTS_NGINX_ROOT}/sites-enabled/beeman.conf"
remove_owned_vhost "vt-frp.beeorbit.net" && true
[[ -f "${VTESTS_NGINX_ROOT}/sites-available/beeman.conf" ]] || fail "uninstall must not delete beeman vhost"

printf '# managed-by: vtests\nserver { server_name vt-frp.beeorbit.net; }\n' \
    > "${VTESTS_NGINX_ROOT}/sites-available/vtests.conf"
ln -sfn "${VTESTS_NGINX_ROOT}/sites-available/vtests.conf" "${VTESTS_NGINX_ROOT}/sites-enabled/vtests.conf"
remove_owned_vhost "vt-frp.beeorbit.net" || fail "should remove owned vhost"
[[ -e "${VTESTS_NGINX_ROOT}/sites-available/vtests.conf" ]] && fail "owned vhost should be deleted"
[[ -f "${VTESTS_NGINX_ROOT}/sites-available/beeman.conf" ]] || fail "beeman still present after owned delete"
pass "uninstall deletes only owned vhost"

# --- regression helper ---
site_code_regressed 200 500 || fail "200 -> 500 is regression"
site_code_regressed 200 200 && fail "200 -> 200 is not regression"
site_code_regressed 000 000 && fail "non-200 baseline is not regression"
site_code_regressed 503 200 && fail "already-broken baseline is not regression"
pass "fail-closed only if baseline was 200"

echo "ALL TLS TESTS PASSED"
