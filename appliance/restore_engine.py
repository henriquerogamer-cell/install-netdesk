#!/usr/bin/env python3
import hashlib
import json
import os
import secrets
import shutil
import tarfile
import time
from pathlib import Path

STATE = Path("/var/lib/netdesk-appliance")
RESTORE_ROOT = STATE / "restore"
UPLOAD_DIR = RESTORE_ROOT / "uploads"
PENDING_META = RESTORE_ROOT / "pending.json"
MAX_UPLOAD_BYTES = int(os.environ.get("NETDESK_RESTORE_MAX_UPLOAD_BYTES", str(20 * 1024**3)))
MIN_FREE_AFTER_UPLOAD = int(os.environ.get("NETDESK_RESTORE_MIN_FREE_BYTES", str(512 * 1024**2)))
MAX_MANIFEST_BYTES = 1024 * 1024


def _ensure_dirs():
    RESTORE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(RESTORE_ROOT, 0o700)
    os.chmod(UPLOAD_DIR, 0o700)


def _write_private_json(path: Path, value):
    _ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


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
        return data
    except Exception:
        return None


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
