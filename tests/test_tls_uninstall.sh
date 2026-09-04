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
pass "domain validation"

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

# setup_tls dry-run writes then tls_fallback restores
printf 'keep-beeman\n' > "${VTESTS_NGINX_ROOT}/sites-available/beeman.conf"
export VTESTS_PORT=41234
setup_tls "vt-frp.beeorbit.net" && fail "dry-run setup_tls should return 1"
tls_fallback || fail "tls_fallback must return 0"
grep -qx "keep-beeman" "${VTESTS_NGINX_ROOT}/sites-available/beeman.conf" || fail "fallback must keep beeman"
pass "setup_tls || tls_fallback dry-run restores tree"

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
