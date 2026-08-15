#!/usr/bin/env python3
import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import ssl
import subprocess
import tempfile
import time
from datetime import date, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from restore_engine import clear_pending_restore, pending_restore_status, save_restore_upload, start_pending_restore

HOST = "0.0.0.0"
PORT = 8443
ROOT = Path("/opt/netdesk-appliance")
ETC = Path("/etc/netdesk-appliance")
STATE = Path("/var/lib/netdesk-appliance")
INDEX = ROOT / "index.html"
INITIAL_CODE = ETC / "initial-code"
SESSION_SECRET = ETC / "session-secret"
ADMIN_PASSWORD = ETC / "admin-password.json"
TLS_CERT = ETC / "tls.crt"
TLS_KEY = ETC / "tls.key"
INSTALLATION_ID = STATE / "installation-id"
LICENSE_STATE = STATE / "license-state.json"
ACTIVE_LICENSE = STATE / "active-license.ndlic"
LICENSE_HISTORY = STATE / "license-history"
SESSION_TTL = 8 * 60 * 60
PBKDF2_ITERATIONS = 310000
TLS_HANDSHAKE_TIMEOUT = 8
CLIENT_TIMEOUT = 30

TRUSTED_LICENSE_PUBLIC_KEY_B64 = "PFWxwPoeXyP/co89fojLwvS4TcV9gILpMMbwAv7y1r8="
TRUSTED_LICENSE_PUBLIC_KEY = f"LICENSE-STUDIO-ED25519:{TRUSTED_LICENSE_PUBLIC_KEY_B64}"


def read_text(path: Path, default=""):
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return default


def write_private_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def installation_id():
    current = read_text(INSTALLATION_ID)
    if current:
        return current
    value = f"NDI-{secrets.token_hex(16).upper()}"
    write_private_text(INSTALLATION_ID, value + "\n")
    return value


def default_license_state():
    today = date.today()
    ident = installation_id()
    return {
        "schema": "netdesk-appliance/license-state-v1",
        "product": "NETDESK",
        "installation_id": ident,
        "customer": "",
        "license_id": f"DEMO-{ident[-12:]}",
        "status": "demo",
        "issued_at": today.isoformat(),
        "expires_at": (today + timedelta(days=30)).isoformat(),
    }


def license_state():
    try:
        data = json.loads(LICENSE_STATE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("installation_id"):
            return data
    except Exception:
        pass
    data = default_license_state()
    write_private_text(LICENSE_STATE, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return data


def save_license_customer(customer: str):
    data = license_state()
    data["customer"] = customer.strip()
    write_private_text(LICENSE_STATE, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return data


def license_request(customer=None):
    data = license_state()
    if customer is not None and customer.strip():
        data = save_license_customer(customer)
    customer_name = str(data.get("customer") or "").strip()
    if not customer_name:
        raise ValueError("Informe o nome da empresa antes de exportar a solicitação de licença.")
    return {
        "schema": "license-request-v1",
        "product": "NETDESK",
        "customer": customer_name,
        "installation_id": data["installation_id"],
        "current_license_id": data.get("license_id"),
        "current_expires_at": data.get("expires_at"),
    }


def operational_license():
    data = license_state()
    status = str(data.get("status") or "").lower()
    expires_at = str(data.get("expires_at") or "")[:10]
    try:
        expiry = date.fromisoformat(expires_at)
    except Exception:
        expiry = None
    return bool(status in {"active", "demo"} and expiry and expiry >= date.today())


def canonical_license_payload(payload):
    required = (
        "format",
        "license_uuid",
        "product",
        "license_id",
        "customer",
        "installation_id",
        "issued_at",
        "expires_at",
        "replaces_license_id",
        "features",
    )
    missing = [name for name in required if name not in payload]
    if missing:
        raise ValueError(f"Licença incompleta: {', '.join(missing)}")
    ordered = {name: payload[name] for name in required}
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def trusted_public_pem():
    raw = base64.b64decode(TRUSTED_LICENSE_PUBLIC_KEY_B64, validate=True)
    if len(raw) != 32:
        raise ValueError("Chave pública de licença inválida.")
    der = bytes.fromhex("302a300506032b6570032100") + raw
    encoded = base64.b64encode(der).decode("ascii")
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(encoded[i:i+64] for i in range(0, len(encoded), 64)) + "\n-----END PUBLIC KEY-----\n"


def verify_ed25519(payload_bytes: bytes, signature_b64: str):
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except Exception as exc:
        raise ValueError("Assinatura da licença inválida.") from exc
    if len(signature) != 64:
        raise ValueError("Assinatura da licença inválida.")

    try:
        with tempfile.TemporaryDirectory(prefix="netdesk-license-", dir=str(STATE)) as tmp:
            tmp_path = Path(tmp)
            pub = tmp_path / "public.pem"
            msg = tmp_path / "payload.bin"
            sig = tmp_path / "signature.bin"
            pub.write_text(trusted_public_pem(), encoding="ascii")
            msg.write_bytes(payload_bytes)
            sig.write_bytes(signature)
            result = subprocess.run(
                [
                    "openssl", "pkeyutl", "-verify",
                    "-pubin", "-inkey", str(pub),
                    "-rawin", "-in", str(msg),
                    "-sigfile", str(sig),
                ],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
    except FileNotFoundError as exc:
        raise ValueError("OpenSSL não está disponível para validar a licença.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("A validação criptográfica da licença excedeu o tempo limite.") from exc

    if result.returncode != 0:
        raise ValueError("Assinatura da licença não confere.")


def install_license(raw: str):
    try:
        envelope = json.loads(raw)
    except Exception as exc:
        raise ValueError("Arquivo de licença inválido.") from exc

    if not isinstance(envelope, dict) or envelope.get("schema") != "license-studio/ndlic-v2":
        raise ValueError("Formato de licença não reconhecido.")
    if envelope.get("public_key") != TRUSTED_LICENSE_PUBLIC_KEY:
        raise ValueError("A licença não foi emitida por uma autoridade reconhecida.")

    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Conteúdo da licença inválido.")

    payload_bytes = canonical_license_payload(payload)
    verify_ed25519(payload_bytes, str(envelope.get("signature") or ""))

    current = license_state()
    if str(payload.get("product") or "").upper() != "NETDESK":
        raise ValueError("Esta licença não pertence ao produto NETDESK.")
    if payload.get("installation_id") != current.get("installation_id"):
        raise ValueError("Esta licença pertence a outra instalação.")
    if payload.get("replaces_license_id") != current.get("license_id"):
        raise ValueError("Esta licença foi emitida para substituir outra licença e não pode ser aplicada ao estado atual.")

    customer = str(payload.get("customer") or "").strip()
    license_id = str(payload.get("license_id") or "").strip()
    issued_at = str(payload.get("issued_at") or "").strip()
    expires_at = str(payload.get("expires_at") or "").strip()
    if not customer or not license_id or not issued_at or not expires_at:
        raise ValueError("A licença não possui todos os dados obrigatórios.")

    try:
        expiry = date.fromisoformat(expires_at[:10])
    except Exception as exc:
        raise ValueError("A validade da licença é inválida.") from exc
    if expiry < date.today():
        raise ValueError("A licença informada já está expirada.")

    LICENSE_HISTORY.mkdir(parents=True, exist_ok=True)
    if ACTIVE_LICENSE.exists():
        previous_id = str(current.get("license_id") or "license")
        history_name = f"{int(time.time())}-{previous_id}.ndlic"
        write_private_text(LICENSE_HISTORY / history_name, ACTIVE_LICENSE.read_text(encoding="utf-8"))

    write_private_text(ACTIVE_LICENSE, json.dumps(envelope, ensure_ascii=False, indent=2) + "\n")
    next_state = {
        "schema": "netdesk-appliance/license-state-v1",
        "product": "NETDESK",
        "installation_id": current["installation_id"],
        "customer": customer,
        "license_id": license_id,
        "status": "active",
        "issued_at": issued_at,
        "expires_at": expires_at[:10],
        "replaces_license_id": payload.get("replaces_license_id"),
        "activated_at": int(time.time()),
    }
    write_private_text(LICENSE_STATE, json.dumps(next_state, ensure_ascii=False, indent=2) + "\n")
    return next_state


LICENSE_IMPORT_UI = r"""
<script>
window.addEventListener('DOMContentLoaded', () => {
  const actions = document.querySelector('.license-actions');
  if (!actions || document.getElementById('importLicenseBtn')) return;

  const input = document.createElement('input');
  input.type = 'file';
  input.id = 'importLicenseFile';
  input.style.display = 'none';

  const button = document.createElement('button');
  button.id = 'importLicenseBtn';
  button.className = 'primary';
  button.textContent = 'Importar licença';

  button.addEventListener('click', () => {
    input.value = '';
    input.click();
  });

  input.addEventListener('change', async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    button.disabled = true;
    try {
      const raw = await file.text();
      const result = await request('/api/license/import', {
        method: 'POST',
        body: JSON.stringify({ license_raw: raw })
      });
      log(`Licença ${result.license.license_id} instalada. Validade: ${formatDate(result.license.expires_at)}.`);
      await loadLicense();
    } catch (error) {
      log(`Licença rejeitada: ${error.message}`);
    } finally {
      button.disabled = false;
    }
  });

  actions.appendChild(button);
  actions.appendChild(input);
});
</script>
"""

def public_ip():
    try:
        result = subprocess.run(["curl", "-4fsS", "--max-time", "4", "https://api.ipify.org"], capture_output=True, text=True, timeout=6, check=False)
        value = result.stdout.strip()
        return value if result.returncode == 0 else None
    except Exception:
        return None


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        value = s.getsockname()[0]
        s.close()
        return value
    except Exception:
        return None


def disk_info():
    stat = os.statvfs("/")
    return {"total_bytes": stat.f_frsize * stat.f_blocks, "free_bytes": stat.f_frsize * stat.f_bavail}


def memory_info():
    total_kb = None
    available_kb = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                total_kb = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                available_kb = int(line.split()[1])
    except Exception:
        pass
    return {"total_bytes": total_kb * 1024 if total_kb else None, "available_bytes": available_kb * 1024 if available_kb else None}


def ubuntu_info():
    info = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                info[key] = value.strip().strip('"')
    except Exception:
        pass
    return {"id": info.get("ID"), "version": info.get("VERSION_ID"), "pretty_name": info.get("PRETTY_NAME")}


def service_active(name):
    try:
        result = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=4, check=False)
        return result.stdout.strip() == "active"
    except Exception:
        return False


def status_payload():
    return {
        "product": "NETDESK Appliance",
        "mode": "bootstrap",
        "ready": True,
        "public_ip": public_ip(),
        "local_ip": local_ip(),
        "hostname": socket.gethostname(),
        "os": ubuntu_info(),
        "disk": disk_info(),
        "memory": memory_info(),
        "services": {"appliance": service_active("netdesk-appliance.service"), "netdesk": service_active("netdesk-backend.service")},
        "netdesk_installed": Path("/opt/netdesk").exists(),
        "chat_installed": Path("/opt/chat").exists(),
        "timestamp": int(time.time()),
    }


def password_configured():
    return ADMIN_PASSWORD.is_file() and ADMIN_PASSWORD.stat().st_size > 0


def password_record(password):
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return {"algorithm": "pbkdf2_sha256", "iterations": PBKDF2_ITERATIONS, "salt": salt.hex(), "hash": digest.hex(), "created_at": int(time.time())}


def save_password(password):
    tmp = ADMIN_PASSWORD.with_suffix(".tmp")
    tmp.write_text(json.dumps(password_record(password), indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ADMIN_PASSWORD)


def verify_password(password):
    try:
        data = json.loads(ADMIN_PASSWORD.read_text(encoding="utf-8"))
        salt = bytes.fromhex(data["salt"])
        expected = bytes.fromhex(data["hash"])
        iterations = int(data.get("iterations", PBKDF2_ITERATIONS))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def valid_password_shape(password):
    return isinstance(password, str) and len(password) >= 10 and len(password) <= 128


def sign_session(timestamp, nonce):
    secret = read_text(SESSION_SECRET).encode()
    payload = f"{timestamp}:{nonce}".encode()
    digest = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return f"{timestamp}:{nonce}:{digest}"


def valid_session(token):
    try:
        timestamp_s, nonce, digest = token.split(":", 2)
        timestamp = int(timestamp_s)
        if timestamp < int(time.time()) - SESSION_TTL:
            return False
        expected = sign_session(timestamp, nonce).split(":", 2)[2]
        return hmac.compare_digest(expected, digest)
    except Exception:
        return False


def session_cookie():
    timestamp = int(time.time())
    token = sign_session(timestamp, secrets.token_hex(16))
    return f"netdesk_appliance_session={token}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; Secure; SameSite=Strict"


class SecureThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, request_handler_class, ssl_context):
        self.ssl_context = ssl_context
        super().__init__(server_address, request_handler_class)

    def process_request_thread(self, request, client_address):
        tls_socket = None
        try:
            request.settimeout(TLS_HANDSHAKE_TIMEOUT)
            tls_socket = self.ssl_context.wrap_socket(request, server_side=True)
            tls_socket.settimeout(CLIENT_TIMEOUT)
            self.finish_request(tls_socket, client_address)
        except (ssl.SSLError, TimeoutError, socket.timeout, ConnectionError, OSError):
            pass
        except Exception:
            self.handle_error(request, client_address)
        finally:
            try:
                if tls_socket is not None:
                    tls_socket.close()
                else:
                    request.close()
            except Exception:
                pass


class Handler(BaseHTTPRequestHandler):
    server_version = "NETDESK-Appliance/0.6"

    def log_message(self, fmt, *args):
        print(f"[appliance] {self.address_string()} {fmt % args}")

    def send_json(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def session_token(self):
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            morsel = jar.get("netdesk_appliance_session")
            return morsel.value if morsel else None
        except Exception:
            return None

    def authenticated(self):
        if not password_configured():
            return False
        token = self.session_token()
        return bool(token and valid_session(token))

    def body_json(self):
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 262144)
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            try:
                html = INDEX.read_text(encoding="utf-8")
                body = html.replace("</body>", LICENSE_IMPORT_UI + "\n</body>").encode("utf-8")
            except Exception:
                return self.send_json(500, {"error": "interface_missing"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/session":
            return self.send_json(200, {"authenticated": self.authenticated(), "password_configured": password_configured()})

        if path == "/api/status":
            if not self.authenticated():
                return self.send_json(401, {"error": "authentication_required"})
            return self.send_json(200, status_payload())

        if path == "/api/license/status":
            if not self.authenticated():
                return self.send_json(401, {"error": "authentication_required"})
            return self.send_json(200, license_state())

        if path == "/api/restore/pending":
            if not self.authenticated():
                return self.send_json(401, {"error": "authentication_required"})
            return self.send_json(200, {"pending": pending_restore_status()})

        if path == "/api/preflight":
            if not self.authenticated():
                return self.send_json(401, {"error": "authentication_required"})
            data = status_payload()
            checks = [
                {"id": "ubuntu", "label": "Ubuntu Server 24.04", "ok": data["os"].get("id") == "ubuntu" and data["os"].get("version") == "24.04"},
                {"id": "internet", "label": "Acesso à internet / IP público", "ok": bool(data.get("public_ip"))},
                {"id": "disk", "label": "Espaço livre mínimo de 20 GB", "ok": (data["disk"].get("free_bytes") or 0) >= 20 * 1024**3},
                {"id": "memory", "label": "Memória total mínima de 4 GB", "ok": (data["memory"].get("total_bytes") or 0) >= 4 * 1024**3},
            ]
            return self.send_json(200, {"checks": checks, "apt": all(item["ok"] for item in checks)})

        return self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/activate":
            if password_configured():
                return self.send_json(409, {"error": "already_activated", "message": "A appliance já possui senha administrativa."})
            data = self.body_json()
            supplied = str(data.get("code", "")).strip().upper()
            expected = read_text(INITIAL_CODE).strip().upper()
            password = data.get("password")
            if not expected or not hmac.compare_digest(supplied, expected):
                time.sleep(0.4)
                return self.send_json(401, {"error": "invalid_code", "message": "Código inicial inválido."})
            if not valid_password_shape(password):
                return self.send_json(400, {"error": "weak_password", "message": "A senha deve ter entre 10 e 128 caracteres."})
            save_password(password)
            try:
                INITIAL_CODE.unlink()
            except FileNotFoundError:
                pass
            license_state()
            return self.send_json(201, {"ok": True, "activated": True}, {"Set-Cookie": session_cookie()})

        if path == "/api/login":
            if not password_configured():
                return self.send_json(409, {"error": "activation_required", "message": "Ative a appliance usando o código inicial antes do primeiro login."})
            password = str(self.body_json().get("password", ""))
            if not verify_password(password):
                time.sleep(0.4)
                return self.send_json(401, {"error": "invalid_password", "message": "Senha inválida."})
            return self.send_json(200, {"ok": True}, {"Set-Cookie": session_cookie()})

        if path == "/api/logout":
            return self.send_json(200, {"ok": True}, {"Set-Cookie": "netdesk_appliance_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"})

        if path == "/api/license/request":
            if not self.authenticated():
                return self.send_json(401, {"error": "authentication_required"})
            data = self.body_json()
            try:
                payload = license_request(str(data.get("customer", "")))
                return self.send_json(200, payload)
            except ValueError as exc:
                return self.send_json(400, {"error": "customer_required", "message": str(exc)})

        if path == "/api/license/import":
            if not self.authenticated():
                return self.send_json(401, {"error": "authentication_required"})
            data = self.body_json()
            raw = data.get("license_raw")
            if not isinstance(raw, str) or not raw.strip():
                return self.send_json(400, {"error": "license_required", "message": "Selecione um arquivo .ndlic."})
            try:
                installed = install_license(raw)
                return self.send_json(200, {"ok": True, "license": installed})
            except ValueError as exc:
                return self.send_json(400, {"error": "invalid_license", "message": str(exc)})
            except Exception:
                return self.send_json(500, {"error": "license_install_failed", "message": "Não foi possível instalar a licença."})

        if path == "/api/restore/upload":
            if not self.authenticated():
                return self.send_json(401, {"error": "authentication_required"})
            if not operational_license():
                return self.send_json(423, {"error": "license_inactive", "message": "Ative ou renove a licença da appliance antes de iniciar uma recuperação."})
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except Exception:
                content_length = 0
            filename = unquote(str(self.headers.get("X-Restore-Filename", "") or ""))
            try:
                result = save_restore_upload(self.rfile, content_length, filename)
                return self.send_json(201, {"ok": True, "restore": result})
            except ValueError as exc:
                return self.send_json(400, {"error": "restore_upload_invalid", "message": str(exc)})
            except Exception as exc:
                print(f"[restore] upload/preflight failed: {exc}")
                return self.send_json(500, {"error": "restore_upload_failed", "message": "Não foi possível receber ou validar o backup."})

        if path == "/api/restore/execute":
            if not self.authenticated():
                return self.send_json(401, {"error": "authentication_required"})
            if not operational_license():
                return self.send_json(423, {"error": "license_inactive", "message": "Ative ou renove a licença antes de restaurar."})
            if str(self.body_json().get("confirmation") or "").strip().upper() != "RESTAURAR":
                return self.send_json(400, {"error": "restore_confirmation_required", "message": "Digite RESTAURAR para confirmar."})
            try:
                return self.send_json(202, {"ok": True, "restore": start_pending_restore()})
            except ValueError as exc:
                return self.send_json(400, {"error": "restore_not_started", "message": str(exc)})
            except Exception as exc:
                print(f"[restore] execution start failed: {exc}")
                return self.send_json(500, {"error": "restore_start_failed", "message": "Não foi possível iniciar a restauração."})

        if path == "/api/restore/clear":
            if not self.authenticated():
                return self.send_json(401, {"error": "authentication_required"})
            return self.send_json(200, clear_pending_restore())

        return self.send_json(404, {"error": "not_found"})


def main():
    for required in (INDEX, SESSION_SECRET, TLS_CERT, TLS_KEY):
        if not required.exists():
            raise SystemExit(f"Arquivo obrigatório ausente: {required}")
    if not password_configured() and not INITIAL_CODE.exists():
        raise SystemExit(f"Arquivo obrigatório ausente: {INITIAL_CODE}")

    STATE.mkdir(parents=True, exist_ok=True)
    installation_id()
    license_state()

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(TLS_CERT), str(TLS_KEY))
    server = SecureThreadingHTTPServer((HOST, PORT), Handler, context)
    print(f"[NETDESK Appliance] HTTPS ativo em 0.0.0.0:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
