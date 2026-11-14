#!/usr/bin/env bash
# 一次性建立 GCP 資源：API、Artifact Registry、服務帳號、Cloud SQL、Secret Manager。
# 可重複執行：已存在的資源會略過。需要對專案有 Owner 權限的 gcloud 登入。
#
#   cp deploy/deploy.env.example deploy/deploy.env   # 填好設定
#   bash deploy/gcp_setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/deploy.env

P="$GCP_PROJECT"; R="$GCP_REGION"
gcloud config set project "$P" >/dev/null

echo "== 啟用 API"
gcloud services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com cloudscheduler.googleapis.com \
  drive.googleapis.com iamcredentials.googleapis.com

echo "== Artifact Registry"
gcloud artifacts repositories describe pmec --location "$R" >/dev/null 2>&1 ||
  gcloud artifacts repositories create pmec --repository-format docker --location "$R"

echo "== 服務帳號（網頁 / 背景工作各一，不建立金鑰檔）"
for sa in pmec-web pmec-worker; do
  gcloud iam service-accounts describe "$sa@$P.iam.gserviceaccount.com" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "$sa" --display-name "$sa"
  gcloud projects add-iam-policy-binding "$P" --member "serviceAccount:$sa@$P.iam.gserviceaccount.com" \
    --role roles/cloudsql.client --condition None >/dev/null
done

echo "== Cloud SQL（PostgreSQL 16）"
if ! gcloud sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
  gcloud sql instances create "$SQL_INSTANCE" --database-version POSTGRES_16 --edition enterprise \
    --tier "$SQL_TIER" --region "$R" --storage-auto-increase --backup-start-time 18:00
fi
gcloud sql databases describe "$DB_NAME" --instance "$SQL_INSTANCE" >/dev/null 2>&1 ||
  gcloud sql databases create "$DB_NAME" --instance "$SQL_INSTANCE"

CONN="$P:$R:$SQL_INSTANCE"
secret_exists() { gcloud secrets describe "$1" >/dev/null 2>&1; }
put_secret() {  # put_secret 名稱 值（從 stdin 讀入，不留在命令列）
  secret_exists "$1" || gcloud secrets create "$1" --replication-policy automatic >/dev/null
  gcloud secrets versions add "$1" --data-file=- >/dev/null
}

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

echo "== 應用程式密鑰"
for name in pmec-rap-api-key pmec-github-token; do
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
for s in pmec-database-url-worker pmec-app-db-password pmec-rap-api-key pmec-github-token; do grant "$s" pmec-worker; done
for s in pmec-database-url-app pmec-rap-api-key; do grant "$s" pmec-web; done
secret_exists pmec-streamlit-secrets && grant pmec-streamlit-secrets pmec-web

echo
echo "完成。Google Drive 資料夾請分享給（檢視者）：pmec-worker@$P.iam.gserviceaccount.com"
