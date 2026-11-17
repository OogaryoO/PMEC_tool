#!/usr/bin/env bash
# 建置映像並部署：schema 遷移 → 同步工作 → 網頁服務 → 每日排程。每次更新程式碼後執行。
#
#   bash deploy/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/deploy.env
DB_MODE="${DB_MODE:-cloudsql}"
if [ "${DRY_RUN:-}" = 1 ]; then gcloud() { echo "gcloud $*"; [[ "$*" == *"--data-file=-"* ]] && cat >/dev/null; [[ "$*" == *" describe "* ]] && return 1; return 0; }; fi

P="$GCP_PROJECT"; R="$GCP_REGION"
gcloud config set project "$P" >/dev/null
IMAGE="$R-docker.pkg.dev/$P/pmec/pmec:$(git rev-parse --short HEAD)$(git diff --quiet || echo -dirty)"
# Cloud SQL 才需要掛 Cloud SQL 連線；外部資料庫（Neon 等）走一般網路＋TLS
JOB_SQL=(); WEB_SQL=()
if [ "$DB_MODE" = cloudsql ]; then
  CONN="$P:$R:$SQL_INSTANCE"
  JOB_SQL=(--set-cloudsql-instances "$CONN"); WEB_SQL=(--add-cloudsql-instances "$CONN")
fi
# AI 模型：沿用舊設定 RAP_* 時自動換成 LLM_*
if [ -z "${LLM_PROVIDER:-}" ] && [ -n "${RAP_MODEL_NAME:-}" ]; then
  LLM_PROVIDER=rap; LLM_MODEL="$RAP_MODEL_NAME"; LLM_BASE_URL="${RAP_BASE_URL:-}"
fi
LLM_SECRET=pmec-llm-api-key
gcloud secrets describe pmec-llm-api-key >/dev/null 2>&1 || LLM_SECRET=pmec-rap-api-key
WEB_SA="pmec-web@$P.iam.gserviceaccount.com"
WORKER_SA="pmec-worker@$P.iam.gserviceaccount.com"

echo "== 建置映像（Cloud Build，不需要本機 Docker）：$IMAGE"
gcloud builds submit --tag "$IMAGE" .

# 以 ^;^ 指定分隔符，讓逗號分隔的清單（Drive 資料夾、網域、管理員）可以放進單一變數
APP_ENV="^;^GITHUB_REPO=$GITHUB_REPO;GDRIVE_FOLDER_IDS=$GDRIVE_FOLDER_IDS;GDRIVE_MAX_FILES=$GDRIVE_MAX_FILES"
APP_ENV="$APP_ENV;LLM_PROVIDER=$LLM_PROVIDER;LLM_MODEL=$LLM_MODEL;LLM_BASE_URL=${LLM_BASE_URL:-}"
APP_ENV="$APP_ENV;GCP_PROJECT=$P;GCP_REGION=$R;SYNC_JOB_NAME=pmec-sync"
WEB_ENV="$APP_ENV;ALLOWED_EMAIL_DOMAINS=$ALLOWED_EMAIL_DOMAINS;ALLOWED_EMAILS=${ALLOWED_EMAILS:-};ADMIN_EMAILS=$ADMIN_EMAILS"

job() {  # job 名稱 指令 額外參數...
  local name=$1 cmd=$2; shift 2
  gcloud run jobs deploy "$name" --image "$IMAGE" --region "$R" --service-account "$WORKER_SA" \
    "${JOB_SQL[@]}" --command python --args="-m,src.worker,$cmd" \
    --memory 2Gi --cpu 1 --max-retries 0 --set-env-vars "$APP_ENV" "$@"
}

echo "== 資料庫 schema 遷移（含網頁唯讀角色）"
job pmec-migrate migrate --task-timeout 600 \
  --set-secrets "DATABASE_URL=pmec-database-url-worker:latest,PMEC_APP_DB_PASSWORD=pmec-app-db-password:latest"
gcloud run jobs execute pmec-migrate --region "$R" --wait

echo "== 同步工作"
job pmec-sync sync --task-timeout 3600 \
  --set-secrets "DATABASE_URL=pmec-database-url-worker:latest,LLM_API_KEY=$LLM_SECRET:latest,GITHUB_TOKEN=pmec-github-token:latest"
for member in "$WEB_SA" "$WORKER_SA"; do  # 網頁的「同步」按鈕、每日排程（以 worker 身分呼叫）
  gcloud run jobs add-iam-policy-binding pmec-sync --region "$R" \
    --member "serviceAccount:$member" --role roles/run.invoker >/dev/null
done

echo "== 網頁服務"
gcloud run deploy pmec-web --image "$IMAGE" --region "$R" --service-account "$WEB_SA" \
  "${WEB_SQL[@]}" --memory 2Gi --cpu 1 --min-instances 0 --max-instances 3 \
  --session-affinity --timeout 3600 --allow-unauthenticated \
  --set-env-vars "$WEB_ENV" \
  --set-secrets "DATABASE_URL=pmec-database-url-app:latest,LLM_API_KEY=$LLM_SECRET:latest,/app/.streamlit/secrets.toml=pmec-streamlit-secrets:latest"

echo "== 每日同步排程"
SCHED_ARGS=(--location "$R" --schedule "$SYNC_SCHEDULE" --time-zone Asia/Taipei --http-method POST
  --uri "https://run.googleapis.com/v2/projects/$P/locations/$R/jobs/pmec-sync:run"
  --oauth-service-account-email "$WORKER_SA")
if gcloud scheduler jobs describe pmec-sync-daily --location "$R" >/dev/null 2>&1; then
  gcloud scheduler jobs update http pmec-sync-daily "${SCHED_ARGS[@]}"
else
  gcloud scheduler jobs create http pmec-sync-daily "${SCHED_ARGS[@]}"
fi

URL=$(gcloud run services describe pmec-web --region "$R" --format 'value(status.url)' || echo "<dry-run-url>")
echo
echo "完成：$URL"
echo "OAuth 用戶端的已授權重新導向 URI 需包含：$URL/oauth2callback（並與 secrets.toml 的 redirect_uri 一致）"
