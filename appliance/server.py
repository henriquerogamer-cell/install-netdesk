#!/usr/bin/env python3
import hashlib
import hmac
import json
import os
import secrets
import socket
import ssl
import subprocess
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

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
SESSION_TTL = 8 * 60 * 60
PBKDF2_ITERATIONS = 310000
TLS_HANDSHAKE_TIMEOUT = 8
CLIENT_TIMEOUT = 30


def read_text(path: Path, default=""):
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return default


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
    server_version = "NETDESK-Appliance/0.3"

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
            length = min(int(self.headers.get("Content-Length", "0")), 65536)
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
                body = INDEX.read_bytes()
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

        return self.send_json(404, {"error": "not_found"})


def main():
    for required in (INDEX, SESSION_SECRET, TLS_CERT, TLS_KEY):
        if not required.exists():
            raise SystemExit(f"Arquivo obrigatório ausente: {required}")
    if not password_configured() and not INITIAL_CODE.exists():
        raise SystemExit(f"Arquivo obrigatório ausente: {INITIAL_CODE}")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(TLS_CERT), str(TLS_KEY))
    server = SecureThreadingHTTPServer((HOST, PORT), Handler, context)
    print(f"[NETDESK Appliance] HTTPS ativo em 0.0.0.0:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
