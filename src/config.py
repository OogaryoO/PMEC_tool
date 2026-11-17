"""
集中管理環境變數與應用程式設定。

所有需要密鑰 / 連線資訊的模組都應該從這裡讀取設定，
而不是各自呼叫 os.getenv，避免設定分散、難以維護。

本機開發讀取專案根目錄的 .env；部署在 Cloud Run 時由 Secret Manager 以環境變數注入。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# 專案根目錄 (.env 放這裡)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def get_logger(name: str) -> logging.Logger:
    """取得統一格式的 logger。"""
    return logging.getLogger(name)


class ConfigError(Exception):
    """設定缺漏或不合法時拋出，訊息需可直接呈現給使用者。"""


def _csv(name: str) -> List[str]:
    return [s.strip() for s in os.getenv(name, "").split(",") if s.strip()]


# ---------------------------------------------------------------------------
# 環境變數
# ---------------------------------------------------------------------------

# --- AI 模型：見 src/llm.py（LLM_PROVIDER／LLM_API_KEY／LLM_MODEL／LLM_BASE_URL）。
# 舊設定 RAP_API_KEY／RAP_BASE_URL／RAP_MODEL_NAME 仍可用（視為 LLM_PROVIDER=rap）。

# --- GitHub ---
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO: str = os.getenv("GITHUB_REPO", "")  # 格式: owner/repo

# --- 資料庫（PostgreSQL + pgvector） ---
# SQLAlchemy URL，例如本機 postgresql+psycopg://user:pass@localhost/pmec；
# Cloud Run 經 Cloud SQL 連線：postgresql+psycopg://user:pass@/pmec?host=/cloudsql/<project>:<region>:<instance>
DATABASE_URL: str = os.getenv("DATABASE_URL", "")
# fastembed 支援的 ONNX 模型；更換後需執行 `python -m src.worker reembed` 重算向量。
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")

# --- Google Drive（選填：GDRIVE_FOLDER_IDS 有值即啟用） ---
# 憑證採 Application Default Credentials：Cloud Run 上使用服務掛載的 Service Account；
# 本機開發以 GOOGLE_APPLICATION_CREDENTIALS 指向金鑰檔，或 `gcloud auth application-default login`。
GDRIVE_FOLDER_IDS: List[str] = _csv("GDRIVE_FOLDER_IDS")
GDRIVE_MAX_FILES: int = int(os.getenv("GDRIVE_MAX_FILES", "100"))
GDRIVE_ENABLED: bool = bool(GDRIVE_FOLDER_IDS)

# --- 登入（Streamlit st.login + Google OIDC） ---
ALLOWED_EMAIL_DOMAINS: List[str] = [d.lower() for d in _csv("ALLOWED_EMAIL_DOMAINS")]
ADMIN_EMAILS: List[str] = [e.lower() for e in _csv("ADMIN_EMAILS")]
# 僅供本機開發略過登入；在 Cloud Run（有 K_SERVICE 環境變數）上一律無效。
RUNNING_ON_CLOUD_RUN: bool = bool(os.getenv("K_SERVICE"))
AUTH_DISABLED: bool = (
    os.getenv("AUTH_DISABLED", "").strip().lower() in ("1", "true", "yes") and not RUNNING_ON_CLOUD_RUN
)

# --- 同步工作（Cloud Run Job）；未設定時由管理員在頁面上直接執行同步（本機開發用） ---
GCP_PROJECT: str = os.getenv("GCP_PROJECT", "")
GCP_REGION: str = os.getenv("GCP_REGION", "asia-east1")
SYNC_JOB_NAME: str = os.getenv("SYNC_JOB_NAME", "")
# 網頁「同步」按鈕怎麼執行：
#   cloudrun＝觸發 Cloud Run Job（有 SYNC_JOB_NAME 時的預設）
#   queue   ＝新增一筆請求，由 `python -m src.worker scheduler` 執行（Docker／自有主機用；網頁不需要寫入權限）
#   inline  ＝在網頁程序內直接同步（只適合單機開發；沒有其他設定時的預設）
SYNC_MODE: str = (os.getenv("SYNC_MODE") or ("cloudrun" if SYNC_JOB_NAME else "inline")).strip().lower()

# --- 其他可調參數 ---
PR_FETCH_LIMIT: int = int(os.getenv("PR_FETCH_LIMIT", "20"))
RAG_TOP_K: int = int(os.getenv("RAG_TOP_K", "8"))
LLM_REQUEST_TIMEOUT: float = float(os.getenv("LLM_REQUEST_TIMEOUT", "60"))


def validate(
    require_github: bool = True,
    require_rap: bool = True,
    require_gdrive: bool = False,
    require_database: bool = False,
) -> List[str]:
    """
    檢查必要設定是否齊全。

    Returns:
        缺漏項目的人類可讀說明列表；空列表代表設定完整。
        呼叫端可自行決定要 raise 還是顯示於 UI。
    """
    missing: List[str] = []

    if require_github:
        if not GITHUB_TOKEN:
            missing.append("GITHUB_TOKEN 未設定：請至 GitHub Settings > Developer settings 建立 Personal Access Token。")
        if not GITHUB_REPO or "/" not in GITHUB_REPO:
            missing.append("GITHUB_REPO 未設定或格式錯誤：需為 `owner/repo` 格式，例如 `my-org/my-service`。")

    if require_rap:  # 參數名沿用舊版；檢查的是客戶選的 AI 模型
        from src.llm import missing_settings

        missing.extend(missing_settings())

    if require_gdrive and not GDRIVE_FOLDER_IDS:
        missing.append(
            "GDRIVE_FOLDER_IDS 未設定：請填入 Google Drive 資料夾 ID"
            "（網址 https://drive.google.com/drive/folders/<ID> 的 <ID>），多個以逗號分隔。"
        )

    if require_database and not DATABASE_URL:
        missing.append("DATABASE_URL 未設定：請填入 PostgreSQL（需安裝 pgvector）的連線字串。")

    return missing


def require_valid(
    require_github: bool = True,
    require_rap: bool = True,
    require_gdrive: bool = False,
    require_database: bool = False,
) -> None:
    """設定不齊全時直接拋出 ConfigError，訊息可直接顯示給使用者。"""
    missing = validate(
        require_github=require_github,
        require_rap=require_rap,
        require_gdrive=require_gdrive,
        require_database=require_database,
    )
    if missing:
        raise ConfigError("設定不完整，請檢查環境變數：\n- " + "\n- ".join(missing))
