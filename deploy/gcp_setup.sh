#!/usr/bin/env bash
# 一次性建立 GCP 資源：API、Artifact Registry、服務帳號、Cloud SQL、Secret Manager。
# 可重複執行：已存在的資源會略過。需要對專案有 Owner 權限的 gcloud 登入。
#
#   cp deploy/deploy.env.example deploy/deploy.env   # 填好設定
#   bash deploy/gcp_setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/deploy.env
DB_MODE="${DB_MODE:-cloudsql}"
# DRY_RUN=1：只印出會執行的 gcloud 指令（檢查流程用，不需要 gcloud 登入）
if [ "${DRY_RUN:-}" = 1 ]; then gcloud() { echo "gcloud $*"; [[ "$*" == *"--data-file=-"* ]] && cat >/dev/null; [[ "$*" == *" describe "* ]] && return 1; return 0; }; fi

P="$GCP_PROJECT"; R="$GCP_REGION"
gcloud config set project "$P" >/dev/null

echo "== 啟用 API"
APIS="run.googleapis.com secretmanager.googleapis.com"
[ "$DB_MODE" = cloudsql ] && APIS="$APIS sqladmin.googleapis.com"
# shellcheck disable=SC2086
gcloud services enable $APIS \
  artifactregistry.googleapis.com cloudbuild.googleapis.com cloudscheduler.googleapis.com \
  drive.googleapis.com iamcredentials.googleapis.com

echo "== Artifact Registry"
gcloud artifacts repositories describe pmec --location "$R" >/dev/null 2>&1 ||
  gcloud artifacts repositories create pmec --repository-format docker --location "$R"

echo "== 服務帳號（網頁 / 背景工作各一，不建立金鑰檔）"
for sa in pmec-web pmec-worker; do
  gcloud iam service-accounts describe "$sa@$P.iam.gserviceaccount.com" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "$sa" --display-name "$sa"
  if [ "$DB_MODE" = cloudsql ]; then
    gcloud projects add-iam-policy-binding "$P" --member "serviceAccount:$sa@$P.iam.gserviceaccount.com" \
      --role roles/cloudsql.client --condition None >/dev/null
  fi
done

secret_exists() { gcloud secrets describe "$1" >/dev/null 2>&1; }
put_secret() {  # put_secret 名稱 值（從 stdin 讀入，不留在命令列）
  secret_exists "$1" || gcloud secrets create "$1" --replication-policy automatic >/dev/null
  gcloud secrets versions add "$1" --data-file=- >/dev/null
}

if [ "$DB_MODE" = external ]; then
  echo "== 外部 PostgreSQL（例如 Neon；需 pgvector）"
  if ! secret_exists pmec-database-url-worker; then
    read -r -s -p "貼上資料庫連線字串（schema 擁有者，postgresql://user:pw@host/db?sslmode=require；不會顯示）: " ext; echo
    pw=$(python3 -c "import secrets; print(secrets.token_hex(24))")
    printf '%s' "$ext" | put_secret pmec-database-url-worker
    printf '%s' "$pw" | put_secret pmec-app-db-password
    python3 - "$ext" "$pw" <<'PY' | put_secret pmec-database-url-app
import sys
from urllib.parse import quote, urlsplit, urlunsplit
u = urlsplit(sys.argv[1])
host = u.netloc.rsplit("@", 1)[-1]
sys.stdout.write(urlunsplit((u.scheme, f"pmec_app:{quote(sys.argv[2], safe="")}@{host}", u.path, u.query, "")))
PY
  fi
elif ! gcloud sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
  echo "== Cloud SQL（PostgreSQL 16）"
  gcloud sql instances create "$SQL_INSTANCE" --database-version POSTGRES_16 --edition enterprise \
    --tier "$SQL_TIER" --region "$R" --storage-auto-increase --backup-start-time 18:00
fi
if [ "$DB_MODE" = cloudsql ]; then
gcloud sql databases describe "$DB_NAME" --instance "$SQL_INSTANCE" >/dev/null 2>&1 ||
  gcloud sql databases create "$DB_NAME" --instance "$SQL_INSTANCE"

CONN="$P:$R:$SQL_INSTANCE"
echo "== 資料庫帳號與連線字串（密碼隨機產生，只存在 Secret Manager）"
if ! secret_exists pmec-database-url-worker; then
  pw=$(python3 -c "import secrets; print(secrets.token_hex(24))")
  gcloud sql users create pmec_worker --instance "$SQL_INSTANCE" --password "$pw"
  printf 'postgresql+psycopg://pmec_worker:%s@/%s?host=/cloudsql/%s' "$pw" "$DB_NAME" "$CONN" | put_secret pmec-database-url-worker
fi
if ! secret_exists pmec-database-url-app; then
  pw=$(python3 -c "import secrets; print(secrets.token_hex(24))")
  printf '%s' "$pw" | put_secret pmec-app-db-password
  printf 'postgresql+psycopg://pmec_app:%s@/%s?host=/cloudsql/%s' "$pw" "$DB_NAME" "$CONN" | put_secret pmec-database-url-app
fi
fi

echo "== 應用程式密鑰"
# 舊部署用 pmec-rap-api-key：已存在時沿用，不再另外要 pmec-llm-api-key
LLM_SECRET=pmec-llm-api-key
secret_exists pmec-rap-api-key && ! secret_exists pmec-llm-api-key && LLM_SECRET=pmec-rap-api-key
for name in "$LLM_SECRET" pmec-github-token; do
  if ! secret_exists "$name"; then
    read -r -s -p "輸入 $name 的值（不會顯示）: " val; echo
    printf '%s' "$val" | put_secret "$name"
  fi
done
if ! secret_exists pmec-streamlit-secrets; then
  echo "!! 尚未建立 pmec-streamlit-secrets：依 .streamlit/secrets.toml.example 填好後執行"
  echo "   gcloud secrets create pmec-streamlit-secrets --data-file=<你的 secrets.toml>"
fi

echo "== 密鑰存取權限（各服務只拿得到自己需要的密鑰）"
grant() { gcloud secrets add-iam-policy-binding "$1" --member "serviceAccount:$2@$P.iam.gserviceaccount.com" \
  --role roles/secretmanager.secretAccessor >/dev/null; }
for s in pmec-database-url-worker pmec-app-db-password "$LLM_SECRET" pmec-github-token; do grant "$s" pmec-worker; done
for s in pmec-database-url-app "$LLM_SECRET"; do grant "$s" pmec-web; done
secret_exists pmec-streamlit-secrets && grant pmec-streamlit-secrets pmec-web

echo
echo "完成。Google Drive 資料夾請分享給（檢視者）：pmec-worker@$P.iam.gserviceaccount.com"
