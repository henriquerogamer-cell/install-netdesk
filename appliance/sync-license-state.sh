#!/usr/bin/env bash
set -euo pipefail

SOURCE="/var/lib/netdesk-appliance/license-state.json"
TARGET_DIR="/var/lib/netdesk-license"
TARGET="$TARGET_DIR/license-state.json"
TMP="$TARGET.tmp"

[[ -s "$SOURCE" ]] || exit 0

install -d -o root -g root -m 0755 "$TARGET_DIR"

python3 - "$SOURCE" "$TMP" <<'PY'
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
raw = json.loads(source.read_text(encoding="utf-8"))
allowed = (
    "schema",
    "product",
    "installation_id",
    "customer",
    "license_id",
    "status",
    "issued_at",
    "expires_at",
    "replaces_license_id",
    "activated_at",
)
out = {key: raw.get(key) for key in allowed if key in raw}
target.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

chown root:root "$TMP"
chmod 0644 "$TMP"
mv -f "$TMP" "$TARGET"
