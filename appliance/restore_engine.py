#!/usr/bin/env python3
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tarfile
import threading
import time
from pathlib import Path

STATE = Path("/var/lib/netdesk-appliance")
RESTORE_ROOT = STATE / "restore"
UPLOAD_DIR = RESTORE_ROOT / "uploads"
PENDING_META = RESTORE_ROOT / "pending.json"
RESTORE_LOG = RESTORE_ROOT / "restore.log"
MAX_UPLOAD_BYTES = int(os.environ.get("NETDESK_RESTORE_MAX_UPLOAD_BYTES", str(20 * 1024**3)))
MIN_FREE_AFTER_UPLOAD = int(os.environ.get("NETDESK_RESTORE_MIN_FREE_BYTES", str(512 * 1024**2)))
MAX_MANIFEST_BYTES = 1024 * 1024
NETDESK_ROOT = Path("/opt/netdesk")
BACKUP_DIR = NETDESK_ROOT / "backups"
JOB_DIR = NETDESK_ROOT / "restore-jobs"
BACKUP_SCRIPT = NETDESK_ROOT / "backend/scripts/backup-netdesk.js"
RESTORE_UNIT = "netdesk-restore-agent@{job_id}.service"
MANAGED_BACKUP_RE = __import__("re").compile(r"^netdesk-\d{8}-\d{6}\.tar\.gz$")
_execution_lock = threading.Lock()


def _ensure_dirs():
    RESTORE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(RESTORE_ROOT, 0o700)
    os.chmod(UPLOAD_DIR, 0o700)


def _write_private_json(path: Path, value):
    _ensure_dirs()
    # Cada thread usa um temporário exclusivo. Upload, fila e agente podem
    # atualizar o mesmo estado quase simultaneamente.
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    )
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass



def _restore_log(message):
    _ensure_dirs()
    stamp = time.strftime("%H:%M:%S")
    with RESTORE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")
    os.chmod(RESTORE_LOG, 0o600)


def _log_tail(limit=160):
    lines = []
    try:
        lines.extend(RESTORE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:])
    except Exception:
        pass
    return lines[-limit:]

def _sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member_name(name: str):
    value = str(name or "").replace("\\", "/")
    if not value or value.startswith("/"):
        return False
    parts = [part for part in value.split("/") if part not in ("", ".")]
    return bool(parts) and all(part != ".." for part in parts)


def _check(checks, check_id, label, ok, detail=None):
    item = {"id": check_id, "label": label, "ok": bool(ok)}
    if detail:
        item["detail"] = str(detail)
    checks.append(item)


def preflight_archive(path: Path, original_name: str, upload_id: str):
    checks = []
    stat = path.stat()
    manifest = None
    member_count = 0
    database_member = None

    _check(checks, "archive_size", "Arquivo recebido e não vazio", stat.st_size > 0, f"{stat.st_size} bytes")

    try:
        with tarfile.open(path, mode="r:gz") as archive:
            members = archive.getmembers()
            member_count = len(members)
            _check(checks, "archive_readable", "Pacote tar.gz íntegro e legível", True, f"{member_count} entradas")

            unsafe = []
            special = []
            names = set()
            for member in members:
                names.add(member.name.rstrip("/"))
                if not _safe_member_name(member.name):
                    unsafe.append(member.name)
                if member.issym() or member.islnk() or member.isdev():
                    special.append(member.name)

            _check(checks, "safe_paths", "Pacote sem caminhos perigosos", not unsafe, unsafe[0] if unsafe else None)
            _check(checks, "safe_types", "Pacote sem links ou dispositivos especiais", not special, special[0] if special else None)

            manifest_member = next((item for item in members if item.name.rstrip("/") == "manifest.json"), None)
            manifest_ok = bool(manifest_member and manifest_member.isfile() and 0 < manifest_member.size <= MAX_MANIFEST_BYTES)
            if manifest_ok:
                try:
                    raw_manifest = archive.extractfile(manifest_member).read()
                    manifest = json.loads(raw_manifest.decode("utf-8"))
                    manifest_ok = isinstance(manifest, dict)
                except Exception:
                    manifest_ok = False
                    manifest = None
            _check(checks, "manifest", "Manifesto do backup válido", manifest_ok)

            if manifest_ok:
                format_ok = manifest.get("format") == "netdesk-backup" and int(manifest.get("format_version") or 0) == 1
                _check(checks, "format", "Formato NETDESK Backup v1 reconhecido", format_ok)

                database_member = str((manifest.get("database") or {}).get("archive_path") or "database/netdesk.dump").strip("/")
                dump = next((item for item in members if item.name.rstrip("/") == database_member), None)
                dump_ok = bool(dump and dump.isfile() and dump.size > 0)
                _check(checks, "database", "Dump PostgreSQL presente", dump_ok, f"{dump.size} bytes" if dump_ok else database_member)

                has_runtime_env = "state/backend.env" in names
                _check(checks, "runtime_env", "Configuração runtime do backend presente", has_runtime_env)

                has_uploads = any(name == "state/uploads" or name.startswith("state/uploads/") for name in names)
                _check(checks, "uploads", "Diretório de uploads incluído", has_uploads)
            else:
                _check(checks, "format", "Formato NETDESK Backup v1 reconhecido", False)
                _check(checks, "database", "Dump PostgreSQL presente", False)
                _check(checks, "runtime_env", "Configuração runtime do backend presente", False)
                _check(checks, "uploads", "Diretório de uploads incluído", False)
    except (tarfile.TarError, OSError, EOFError) as exc:
        _check(checks, "archive_readable", "Pacote tar.gz íntegro e legível", False, str(exc))
        _check(checks, "safe_paths", "Pacote sem caminhos perigosos", False)
        _check(checks, "safe_types", "Pacote sem links ou dispositivos especiais", False)
        _check(checks, "manifest", "Manifesto do backup válido", False)
        _check(checks, "format", "Formato NETDESK Backup v1 reconhecido", False)
        _check(checks, "database", "Dump PostgreSQL presente", False)
        _check(checks, "runtime_env", "Configuração runtime do backend presente", False)
        _check(checks, "uploads", "Diretório de uploads incluído", False)

    digest = _sha256(path)
    apt = all(item["ok"] for item in checks)
    application = (manifest or {}).get("application") or {}
    database = (manifest or {}).get("database") or {}

    return {
        "schema": "netdesk-appliance/restore-pending-v1",
        "upload_id": upload_id,
        "filename": original_name,
        "stored_name": path.name,
        "size_bytes": stat.st_size,
        "sha256": digest,
        "uploaded_at": int(time.time()),
        "preflight": {
            "apt": apt,
            "checks": checks,
        },
        "manifest": {
            "backup_id": (manifest or {}).get("backup_id"),
            "created_at": (manifest or {}).get("created_at"),
            "timezone": (manifest or {}).get("timezone"),
            "hostname": (manifest or {}).get("hostname"),
            "git_branch": application.get("git_branch"),
            "git_commit": application.get("git_commit"),
            "git_worktree_dirty": application.get("git_worktree_dirty"),
            "database": database.get("database"),
            "database_engine": database.get("engine"),
            "database_version": database.get("version"),
            "database_archive_path": database_member,
            "member_count": member_count,
        },
        "stage": "preflight_ok" if apt else "preflight_failed",
    }


def clear_pending_restore():
    _ensure_dirs()
    current = pending_restore_status(include_missing=True)
    stored_name = str((current or {}).get("stored_name") or "")
    if stored_name:
        candidate = UPLOAD_DIR / Path(stored_name).name
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
    try:
        PENDING_META.unlink()
    except FileNotFoundError:
        pass
    for part in UPLOAD_DIR.glob("*.part"):
        try:
            part.unlink()
        except OSError:
            pass
    return {"ok": True}


def pending_restore_status(include_missing=False):
    try:
        data = json.loads(PENDING_META.read_text(encoding="utf-8"))
        stored_name = str(data.get("stored_name") or "")
        exists = bool(stored_name and (UPLOAD_DIR / Path(stored_name).name).is_file())
        if not exists and not include_missing:
            return None
        data["file_available"] = exists
        data["log_tail"] = _log_tail()
        execution_id = str(data.get("execution_id") or "")
        if execution_id.isdigit():
            state_path = JOB_DIR / f"{execution_id}.state.json"
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                data["execution"] = state
                status = str(state.get("status") or "")
                if status in {"success", "rolled_back", "rollback_failed", "failed_before_changes"}:
                    data["stage"] = status
                journal = subprocess.run(
                    ["journalctl", "-u", RESTORE_UNIT.format(job_id=execution_id),
                     "--no-pager", "-n", "120", "-o", "cat"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                journal_lines = (journal.stdout or "").splitlines()
                data["log_tail"] = (_log_tail(80) + journal_lines)[-180:]
            except Exception:
                pass
        return data
    except Exception:
        return None



def _update_pending(**patch):
    current = pending_restore_status(include_missing=True) or {}
    current.update(patch)
    current["updated_at"] = int(time.time())
    _write_private_json(PENDING_META, current)
    return current


def _command(args, cwd=None, timeout=3600):
    result = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Comando falhou ({result.returncode}): {' '.join(args)}{': ' + detail if detail else ''}")
    return (result.stdout or "").strip()


def _managed_target_name(pending):
    original = Path(str(pending.get("filename") or "")).name
    if MANAGED_BACKUP_RE.fullmatch(original):
        return original
    created = str((pending.get("manifest") or {}).get("created_at") or "")
    digits = "".join(ch for ch in created if ch.isdigit())[:14]
    if len(digits) == 14:
        return f"netdesk-{digits[:8]}-{digits[8:]}.tar.gz"
    return time.strftime("netdesk-%Y%m%d-%H%M%S.tar.gz")


def _update_netdesk_source(github_token):
    try:
        RESTORE_LOG.unlink()
    except FileNotFoundError:
        pass
    _restore_log("Solicitação de restore confirmada pelo administrador.")
    token = str(github_token or "").strip()
    if len(token) < 20:
        raise RuntimeError("Token GitHub temporário inválido.")
    # O processo Git roda como netdesk; /var/lib/netdesk-appliance é 0700/root.
    # Use /tmp (privado pelo systemd da appliance) para que o filho consiga
    # atravessar o diretório e executar o helper sem expor o token em argumentos.
    askpass = Path("/tmp") / f"netdesk-restore-askpass-{secrets.token_hex(6)}.sh"
    askpass.write_text(
        '#!/usr/bin/env bash\n'
        'case "$1" in\n'
        '  *Username*) printf "%s\\n" "x-access-token" ;;\n'
        '  *) printf "%s\\n" "$NETDESK_GITHUB_TOKEN" ;;\n'
        'esac\n',
        encoding="utf-8",
    )
    os.chmod(askpass, 0o700)
    shutil.chown(askpass, user="netdesk", group="netdesk")
    env = os.environ.copy()
    env.update({
        "GIT_ASKPASS": str(askpass),
        "GIT_TERMINAL_PROMPT": "0",
        "NETDESK_GITHUB_TOKEN": token,
    })
    try:
        result = subprocess.run(
            ["runuser", "-u", "netdesk", "--", "git", "-C", str(NETDESK_ROOT),
             "pull", "--ff-only", "origin", "agent/campaign-execution-history"],
            capture_output=True, text=True, timeout=600, check=False, env=env,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Não foi possível atualizar o código privado do NETDESK: {detail}")
    finally:
        try:
            askpass.unlink()
        except FileNotFoundError:
            pass
        env["NETDESK_GITHUB_TOKEN"] = ""
        token = ""


def _execution_worker(github_token):
    with _execution_lock:
        try:
            pending = pending_restore_status(include_missing=True)
            if not pending or not pending.get("file_available") or not pending.get("preflight", {}).get("apt"):
                raise RuntimeError("Não existe backup aprovado e disponível para restauração.")
            if not NETDESK_ROOT.is_dir() or not BACKUP_SCRIPT.is_file():
                raise RuntimeError("Instalação NETDESK atual não foi encontrada.")
            if not Path("/etc/systemd/system/netdesk-restore-agent@.service").is_file():
                raise RuntimeError("Agente privilegiado de restore não está instalado.")

            _restore_log("Atualizando código privado do NETDESK...")
            _update_pending(stage="source_update", execution={"status": "preparing", "message": "Atualizando código privado do NETDESK."})
            _update_netdesk_source(github_token)
            _restore_log("Código privado atualizado com sucesso.")
            github_token = ""
            _restore_log("Criando backup de segurança da instalação atual...")
            _update_pending(stage="safety_backup", execution={"status": "preparing", "message": "Criando backup de segurança da instalação atual."})
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            shutil.chown(BACKUP_DIR, user="netdesk", group="netdesk")
            os.chmod(BACKUP_DIR, 0o700)
            safety_output = _command(["runuser", "-u", "netdesk", "--", "/usr/bin/node", str(BACKUP_SCRIPT)], cwd=NETDESK_ROOT / "backend", timeout=7200)
            safety_line = next((line for line in safety_output.splitlines() if line.startswith("Arquivo:")), "")
            safety_path = Path(safety_line.split(":", 1)[1].strip()) if safety_line else None
            if not safety_path or not safety_path.is_file():
                raise RuntimeError("Backup de segurança não retornou um arquivo válido.")
            _restore_log(f"Backup de segurança concluído: {safety_path.name}")

            source = UPLOAD_DIR / Path(str(pending["stored_name"])).name
            target_name = _managed_target_name(pending)
            target_path = BACKUP_DIR / target_name
            if target_path.exists() and _sha256(target_path) != pending["sha256"]:
                target_name = time.strftime("netdesk-%Y%m%d-%H%M%S.tar.gz")
                target_path = BACKUP_DIR / target_name
            if not target_path.exists():
                _restore_log(f"Copiando backup alvo para o motor gerenciado: {target_name}")
                shutil.copy2(source, target_path)
            os.chmod(target_path, 0o600)
            shutil.chown(target_path, user="netdesk", group="netdesk")
            checksum_path = Path(str(target_path) + ".sha256")
            checksum_path.write_text(f"{pending['sha256']}  {target_name}\n", encoding="utf-8")
            os.chmod(checksum_path, 0o600)
            shutil.chown(checksum_path, user="netdesk", group="netdesk")

            job_id = str(int(time.time() * 1000))
            JOB_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            job = {
                "restore_id": int(job_id),
                "requested_by": "netdesk-appliance",
                "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "target_backup_id": 0,
                "pre_restore_backup_id": 0,
                "target_archive_name": target_name,
                "pre_restore_archive_name": safety_path.name,
                "target_manifest": pending.get("manifest"),
            }
            job_path = JOB_DIR / f"{job_id}.json"
            state_path = JOB_DIR / f"{job_id}.state.json"
            _write_private_json(job_path, job)
            _write_private_json(state_path, {
                "restore_id": int(job_id),
                "status": "queued",
                "production_touched": False,
                "created_at": job["requested_at"],
            })
            shutil.chown(job_path, user="netdesk", group="netdesk")
            shutil.chown(state_path, user="netdesk", group="netdesk")
            _restore_log(f"Entregando restore {job_id} ao agente privilegiado...")
            _update_pending(
                stage="execution_started",
                execution_id=job_id,
                safety_backup=safety_path.name,
                target_archive=target_name,
                execution={"status": "queued", "message": "Restore entregue ao agente privilegiado."},
            )
            _command(["systemctl", "start", "--no-block", RESTORE_UNIT.format(job_id=job_id)], timeout=30)
            _restore_log("Agente privilegiado iniciado. Aguardando validação, restore e health-check.")
        except Exception as exc:
            _restore_log(f"ERRO: {exc}")
            _update_pending(stage="execution_failed", execution={"status": "failed", "message": str(exc)})


def start_pending_restore(github_token):
    pending = pending_restore_status(include_missing=True)
    if not pending or not pending.get("file_available"):
        raise ValueError("Envie e valide um backup antes de iniciar a restauração.")
    if not pending.get("preflight", {}).get("apt"):
        raise ValueError("O backup não foi aprovado no preflight.")
    current = pending.get("execution") or {}
    if current.get("status") in {"preparing", "queued", "validating", "maintenance", "restoring", "starting", "rollback"}:
        raise ValueError("Já existe uma restauração em andamento.")
    token = str(github_token or "").strip()
    if len(token) < 20:
        raise ValueError("Informe o token GitHub temporário para atualizar o código privado.")
    thread = threading.Thread(target=_execution_worker, args=(token,), name="netdesk-appliance-restore", daemon=True)
    thread.start()
    return _update_pending(stage="queued", execution={"status": "preparing", "message": "Preparando restauração e backup de segurança."})

def save_restore_upload(stream, content_length: int, original_name: str):
    _ensure_dirs()
    if content_length <= 0:
        raise ValueError("O arquivo de backup está vazio.")
    if content_length > MAX_UPLOAD_BYTES:
        raise ValueError("O arquivo excede o limite de upload configurado na appliance.")

    clean_name = Path(str(original_name or "").strip()).name
    if not clean_name or not clean_name.lower().endswith(".tar.gz"):
        raise ValueError("Selecione um backup NETDESK no formato .tar.gz.")

    free_bytes = shutil.disk_usage(RESTORE_ROOT).free
    if free_bytes < content_length + MIN_FREE_AFTER_UPLOAD:
        raise ValueError("Não há espaço livre suficiente para receber este backup com margem de segurança.")

    clear_pending_restore()
    upload_id = secrets.token_hex(12)
    stored_name = f"{upload_id}-{clean_name}"
    final_path = UPLOAD_DIR / stored_name
    part_path = UPLOAD_DIR / f"{stored_name}.part"

    remaining = content_length
    try:
        with part_path.open("wb") as output:
            os.chmod(part_path, 0o600)
            while remaining > 0:
                chunk = stream.read(min(4 * 1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("Upload interrompido antes do arquivo completo ser recebido.")
                output.write(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(part_path, final_path)
        os.chmod(final_path, 0o600)
        result = preflight_archive(final_path, clean_name, upload_id)
        _write_private_json(PENDING_META, result)
        return result
    except Exception:
        try:
            part_path.unlink()
        except FileNotFoundError:
            pass
        try:
            final_path.unlink()
        except FileNotFoundError:
            pass
        raise
