#!/usr/bin/env bash
# ark 环境变量同步（Linux）。用法：bash ark-env.sh --pass <pw> [--apply|--diff]
set -euo pipefail
BASE="${ARK_BASE_URL:-}"
USER_NAME="${ARK_HTTP_USER:-ark}"
PASS="${ARK_HTTP_PASS:-}"
APPLY=0; DIFF=0; SYSTEM=0
while [ $# -gt 0 ]; do
  case "$1" in
    --pass) PASS="$2"; shift 2;;
    --apply) APPLY=1; shift;;
    --diff) DIFF=1; shift;;
    --system) SYSTEM=1; shift;;
    --base) BASE="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
if [ "$SYSTEM" -eq 1 ]; then
  ENV_DIR="/etc/ark"; ENV_FILE="$ENV_DIR/env"; AUTH_FILE="$HOME/.config/ark/auth"
else
  ENV_DIR="$HOME/.config/ark"; ENV_FILE="$ENV_DIR/env"; AUTH_FILE="$ENV_DIR/auth"
fi
if [ -z "$PASS" ]; then
  [ -f "$AUTH_FILE" ] && PASS="$(cat "$AUTH_FILE")" || { echo "缺少口令：--pass 或 ARK_HTTP_PASS" >&2; exit 2; }
else
  mkdir -p "$ENV_DIR"; chmod 700 "$ENV_DIR"; printf '%s' "$PASS" > "$AUTH_FILE"; chmod 600 "$AUTH_FILE"
fi
if [ -z "$BASE" ]; then
  for cand in http://ark-env-a.internal:8090 http://ark-env-b.internal:8080; do
    if curl -fsS -m 5 "$cand/health" >/dev/null 2>&1; then BASE="$cand"; break; fi
  done
  [ -z "$BASE" ] && { echo "两个入口都不通（8090 / 8080）" >&2; exit 3; }
fi
echo "使用入口: $BASE"
curl_get() { curl -fsS -u "${USER_NAME}:${PASS}" "$BASE$1"; }

VER="$(curl_get /ark/version.json | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')"
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
curl_get /ark/env.json > "$TMP"

LOCAL_VER="$(grep -s '^ARK_ENV_VERSION=' "$ENV_FILE" | cut -d= -f2- || true)"
CHANGED=0
while IFS='=' read -r k v; do
  case "$k" in ""|\#*|__*) continue;; esac
  cur="$(grep -s "^${k}=" "$ENV_FILE" | cut -d= -f2- || true)"
  if [ "$cur" = "$v" ]; then continue; fi
  CHANGED=$((CHANGED+1))
  if [ "$APPLY" -eq 1 ]; then
    TMPF="$(mktemp)"
    { grep -v "^${k}=" "$ENV_FILE" 2>/dev/null || true; printf '%s=%s\n' "$k" "$v"; } > "$TMPF"
    if [ "$SYSTEM" -eq 1 ]; then sudo mkdir -p /etc/ark; sudo cp "$TMPF" "$ENV_FILE"; sudo chmod 600 "$ENV_FILE"; sudo chown root:root "$ENV_FILE"
    else mv "$TMPF" "$ENV_FILE"; chmod 600 "$ENV_FILE"; fi
    rm -f "$TMPF"
  else
    echo "  would set: $k"
  fi
done < <(python3 - "$TMP" <<'PY'
import json,sys
for k,v in json.load(open(sys.argv[1])).items():
    if k.startswith("__"): continue
    if v: print(f"{k}={v}")
PY
)
echo "remote version=$VER local=$LOCAL_VER changed=$CHANGED"
if [ "$APPLY" -eq 1 ]; then
  TMPF="$(mktemp)"
  { grep -v '^ARK_ENV_VERSION=' "$ENV_FILE" 2>/dev/null || true; echo "ARK_ENV_VERSION=$VER"; } > "$TMPF"
  if [ "$SYSTEM" -eq 1 ]; then
    sudo cp "$TMPF" "$ENV_FILE"; sudo chmod 600 "$ENV_FILE"; sudo chown root:root "$ENV_FILE"
    echo '[ -f /etc/ark/env ] && . /etc/ark/env' | sudo tee /etc/profile.d/ark-env.sh >/dev/null
  else
    mv "$TMPF" "$ENV_FILE"; chmod 600 "$ENV_FILE"
    grep -q "config/ark/env" "$HOME/.bashrc" 2>/dev/null || echo '[ -f ~/.config/ark/env ] && . ~/.config/ark/env' >> "$HOME/.bashrc"
  fi
  rm -f "$TMPF"
else
  echo "（预演；加 --apply 才写入）"
fi
