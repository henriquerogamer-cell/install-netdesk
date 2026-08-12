#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Execute como root: curl ... | sudo bash" >&2
  exit 1
fi

if [[ ! -f /etc/os-release ]]; then
  echo "Não foi possível identificar o sistema operacional." >&2
  exit 1
fi

. /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
  echo "Este bootstrap suporta somente Ubuntu Server 24.04 LTS." >&2
  echo "Detectado: ${PRETTY_NAME:-desconhecido}" >&2
  exit 1
fi

ARCH="$(dpkg --print-architecture)"
if [[ "$ARCH" != "amd64" ]]; then
  echo "Arquitetura não suportada nesta V1: $ARCH. Esperado: amd64." >&2
  exit 1
fi

REPO_RAW="https://raw.githubusercontent.com/henriquerogamer-cell/install-netdesk/main"
APP_ROOT="/opt/netdesk-appliance"
ETC_ROOT="/etc/netdesk-appliance"
STATE_ROOT="/var/lib/netdesk-appliance"
UNIT="/etc/systemd/system/netdesk-appliance.service"
PORT="8443"
NETDESK_ROOT="/opt/netdesk"

INSTALL_MODE="clean"
if [[ -d "$NETDESK_ROOT" ]] || systemctl list-unit-files 2>/dev/null | grep -q '^netdesk-backend\.service'; then
  INSTALL_MODE="existing"
  echo "[NETDESK Appliance] Instalação NETDESK existente detectada."
  echo "[NETDESK Appliance] A appliance será instalada em paralelo, sem alterar a produção."
fi

echo "[NETDESK Appliance] Preparando Ubuntu 24.04..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends ca-certificates curl openssl python3

install -d -o root -g root -m 0755 "$APP_ROOT"
install -d -o root -g root -m 0700 "$ETC_ROOT"
install -d -o root -g root -m 0700 "$STATE_ROOT"

curl -fsSL "$REPO_RAW/appliance/server.py" -o "$APP_ROOT/server.py"
curl -fsSL "$REPO_RAW/appliance/index.html" -o "$APP_ROOT/index.html"
chmod 0755 "$APP_ROOT/server.py"
chmod 0644 "$APP_ROOT/index.html"

printf '%s\n' "$INSTALL_MODE" > "$STATE_ROOT/install-mode"
chmod 0600 "$STATE_ROOT/install-mode"
if [[ "$INSTALL_MODE" == "existing" ]]; then
  printf '%s\n' "$NETDESK_ROOT" > "$STATE_ROOT/netdesk-root"
  chmod 0600 "$STATE_ROOT/netdesk-root"
fi

if [[ ! -s "$ETC_ROOT/initial-code" ]]; then
  openssl rand -hex 4 | tr '[:lower:]' '[:upper:]' > "$ETC_ROOT/initial-code"
  chmod 0600 "$ETC_ROOT/initial-code"
fi

if [[ ! -s "$ETC_ROOT/session-secret" ]]; then
  openssl rand -hex 32 > "$ETC_ROOT/session-secret"
  chmod 0600 "$ETC_ROOT/session-secret"
fi

if [[ ! -s "$ETC_ROOT/tls.key" || ! -s "$ETC_ROOT/tls.crt" ]]; then
  PUBLIC_IP="$(curl -4fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
  HOST_CN="${PUBLIC_IP:-netdesk-appliance}"
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
    -keyout "$ETC_ROOT/tls.key" \
    -out "$ETC_ROOT/tls.crt" \
    -subj "/CN=$HOST_CN" >/dev/null 2>&1
  chmod 0600 "$ETC_ROOT/tls.key"
  chmod 0644 "$ETC_ROOT/tls.crt"
fi

cat > "$UNIT" <<'EOF'
[Unit]
Description=NETDESK Appliance
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/opt/netdesk-appliance
ExecStart=/usr/bin/python3 /opt/netdesk-appliance/server.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=/etc/netdesk-appliance /opt/netdesk-appliance /var/lib/netdesk-appliance /tmp

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$UNIT"

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  ufw allow "$PORT/tcp" >/dev/null
fi

systemctl daemon-reload
systemctl enable --now netdesk-appliance.service

sleep 1
if ! systemctl is-active --quiet netdesk-appliance.service; then
  echo "Falha ao iniciar a NETDESK Appliance." >&2
  systemctl status netdesk-appliance.service --no-pager >&2 || true
  exit 1
fi

PUBLIC_IP="$(curl -4fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
ACCESS_IP="${PUBLIC_IP:-${LOCAL_IP:-IP-DA-VM}}"
CODE="$(cat "$ETC_ROOT/initial-code")"

if [[ "$INSTALL_MODE" == "existing" ]]; then
  MODE_LABEL="NETDESK existente detectado e preservado"
else
  MODE_LABEL="VM pronta para nova instalação ou recuperação"
fi

cat <<EOF

============================================================
                    NETDESK APPLIANCE
============================================================

Instalação da appliance concluída com sucesso.

Modo:
  ${MODE_LABEL}

Acesse no navegador:

  https://${ACCESS_IP}:${PORT}

Código inicial de acesso:

  ${CODE}

Observação: no primeiro acesso o navegador poderá alertar sobre
certificado local. Isso é esperado até a configuração dos domínios.

Status do serviço:
  systemctl status netdesk-appliance

============================================================
EOF
