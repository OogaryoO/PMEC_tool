#!/usr/bin/env bash
# 不用 Docker、在一台 Linux／WSL 機器上跑整套大腦（BellD 內部測試就是這樣跑）：
#   內嵌 PostgreSQL（.dev_db/）→ 升級 schema → 背景排程 →（有 TUNNEL_TOKEN 時）Cloudflare Tunnel → 網頁
#
#   bash scripts/local_stack.sh            # Ctrl+C 會一起關掉排程與 tunnel
#
# 網頁只綁 127.0.0.1；對外只經 Cloudflare Tunnel。網頁用唯讀角色 pmec_app，寫入只在排程。
# 需要：.venv（pip install -r requirements-dev.txt）、.env、.streamlit/secrets.toml（要登入時）、tenant.toml（選填）
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
source .venv/bin/activate
mkdir -p logs

set -a; [ -f .env ] && . ./.env; set +a
PORT="${WEB_PORT:-8501}"

if [ -n "${TUNNEL_TOKEN:-}" ] && [[ "${AUTH_DISABLED:-}" =~ ^(1|true|yes)$ ]]; then
  echo "拒絕啟動：有 TUNNEL_TOKEN（對外開放）但 AUTH_DISABLED=true。請把 .env 的 AUTH_DISABLED 改成 false。" >&2
  exit 1
fi
python -m src.worker check-tenant >/dev/null

OWNER_URL="$(python scripts/dev_db.py | sed -n 's/^DATABASE_URL=//p')"
[ -n "$OWNER_URL" ] || { echo "內嵌資料庫啟動失敗" >&2; exit 1; }
APP_PW="${DB_APP_PASSWORD:-$(python -c 'import secrets;print(secrets.token_urlsafe(24))')}"
DATABASE_URL="$OWNER_URL" PMEC_APP_DB_PASSWORD="$APP_PW" python -m src.worker migrate
APP_URL="$(python - "$OWNER_URL" "$APP_PW" <<'PY'
import sys
from sqlalchemy.engine import make_url
print(make_url(sys.argv[1]).set(username="pmec_app", password=sys.argv[2]).render_as_string(hide_password=False))
PY
)"

pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

DATABASE_URL="$OWNER_URL" python -m src.worker scheduler >>logs/scheduler.log 2>&1 &
pids+=($!)
echo "背景排程已啟動（logs/scheduler.log）"

if [ -n "${TUNNEL_TOKEN:-}" ]; then
  if command -v cloudflared >/dev/null; then
    TUNNEL_TOKEN="$TUNNEL_TOKEN" cloudflared tunnel --no-autoupdate run >>logs/cloudflared.log 2>&1 &
    pids+=($!)
    echo "Cloudflare Tunnel 已啟動（logs/cloudflared.log）；公開主機名稱的服務請設 http://localhost:${PORT}"
  else
    echo "有 TUNNEL_TOKEN 但找不到 cloudflared 指令；只在本機開放。" >&2
  fi
fi

echo "網頁：http://127.0.0.1:${PORT}"
DATABASE_URL="$APP_URL" SYNC_MODE=queue streamlit run app.py \
  --server.address 127.0.0.1 --server.port "$PORT" --server.headless true --browser.gatherUsageStats false
