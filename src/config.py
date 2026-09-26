"""
集中管理環境變數與應用程式設定。

所有需要密鑰 / 連線資訊的模組都應該從這裡讀取設定，
而不是各自呼叫 os.getenv，避免設定分散、難以維護。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
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
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)


def get_logger(name: str) -> logging.Logger:
    """取得統一格式的 logger。"""
    return logging.getLogger(name)


class ConfigError(Exception):
    """設定缺漏或不合法時拋出，訊息需可直接呈現給使用者。"""


# ---------------------------------------------------------------------------
# 環境變數
# ---------------------------------------------------------------------------

# --- RAP (國網 RAP 平台，相容 OpenAI SDK) ---
RAP_API_KEY: str = os.getenv("RAP_API_KEY", "")
RAP_BASE_URL: str = os.getenv("RAP_BASE_URL", "https://portal.genai.nchc.org.tw/api/v1")
RAP_MODEL_NAME: str = os.getenv("RAP_MODEL_NAME", "Meta-Llama-3-70B-Instruct")

# --- GitHub ---
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO: str = os.getenv("GITHUB_REPO", "")  # 格式: owner/repo

# --- ChromaDB ---
CHROMA_PERSIST_DIR: str = os.getenv("CHROMA_PERSIST_DIR", "./chroma_data")
CHROMA_COLLECTION_NAME: str = os.getenv("CHROMA_COLLECTION_NAME", "github_feature_cards")
# fastembed 支援的 ONNX 模型；換模型後既有 collection 會自動清空，需從快取還原或重新同步。
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")

# --- 功能卡片快取（寫回 GitHub repo，避免 ephemeral 部署環境重複消耗 RAP 額度） ---
# CACHE_REPO 留空 = 停用快取，每次同步都會對所有抓到的文件重新呼叫 RAP LLM 摘要。
CACHE_ENABLED: bool = os.getenv("CACHE_ENABLED", "true").strip().lower() in ("1", "true", "yes")
CACHE_REPO: str = os.getenv("CACHE_REPO", "")
CACHE_FILE_PATH: str = os.getenv("CACHE_FILE_PATH", "pmec_cache/feature_cards.json")
CACHE_BRANCH: str = os.getenv("CACHE_BRANCH", "")  # 空字串 = 該 repo 的預設分支
# 讀寫 CACHE_REPO 用的 token。刻意獨立於 GITHUB_TOKEN：GITHUB_TOKEN 只需對
# GITHUB_REPO（V2 商業邏輯來源）有唯讀權限，但快取需要對 CACHE_REPO 有寫入權限；
# 兩者權限範圍不同，不應共用同一把 token。留空則 fallback 回 GITHUB_TOKEN
# （相容舊設定，但代表 GITHUB_TOKEN 需同時具備兩邊權限）。
CACHE_GITHUB_TOKEN: str = os.getenv("CACHE_GITHUB_TOKEN", "") or GITHUB_TOKEN

# --- Google Drive（選填：GDRIVE_FOLDER_IDS 有值即啟用） ---
GDRIVE_FOLDER_IDS: List[str] = [
    s.strip() for s in os.getenv("GDRIVE_FOLDER_IDS", "").split(",") if s.strip()
]
# Service Account 金鑰：JSON 檔路徑（相對路徑以 PROJECT_ROOT 為準），或直接貼上 JSON 內容（以 "{" 開頭）。
GDRIVE_SERVICE_ACCOUNT_JSON: str = os.getenv("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
GDRIVE_MAX_FILES: int = int(os.getenv("GDRIVE_MAX_FILES", "100"))
GDRIVE_ENABLED: bool = bool(GDRIVE_FOLDER_IDS)

# --- 其他可調參數 ---
PR_FETCH_LIMIT: int = int(os.getenv("PR_FETCH_LIMIT", "20"))
RAG_TOP_K: int = int(os.getenv("RAG_TOP_K", "8"))
LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.2"))
LLM_REQUEST_TIMEOUT: float = float(os.getenv("LLM_REQUEST_TIMEOUT", "60"))


@dataclass(frozen=True)
class Settings:
    """方便一次傳遞、單元測試時可覆寫的設定快照。"""

    rap_api_key: str = RAP_API_KEY
    rap_base_url: str = RAP_BASE_URL
    rap_model_name: str = RAP_MODEL_NAME
    github_token: str = GITHUB_TOKEN
    github_repo: str = GITHUB_REPO
    chroma_persist_dir: str = CHROMA_PERSIST_DIR
    chroma_collection_name: str = CHROMA_COLLECTION_NAME
    embedding_model: str = EMBEDDING_MODEL
    cache_enabled: bool = CACHE_ENABLED
    cache_repo: str = CACHE_REPO
    cache_file_path: str = CACHE_FILE_PATH
    cache_branch: str = CACHE_BRANCH
    pr_fetch_limit: int = PR_FETCH_LIMIT
    rag_top_k: int = RAG_TOP_K
    llm_temperature: float = LLM_TEMPERATURE
    llm_request_timeout: float = LLM_REQUEST_TIMEOUT


def get_settings() -> Settings:
    return Settings()


def resolve_gdrive_credentials_path(raw: str) -> Path:
    """GDRIVE_SERVICE_ACCOUNT_JSON 為檔案路徑時的實際位置；相對路徑以專案根目錄為準。"""
    return PROJECT_ROOT / Path(raw).expanduser()


def validate(
    require_github: bool = True, require_rap: bool = True, require_gdrive: bool = False
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

    if require_rap:
        if not RAP_API_KEY:
            missing.append("RAP_API_KEY 未設定：請向國網 RAP 平台申請 API Key。")
        if not RAP_BASE_URL:
            missing.append("RAP_BASE_URL 未設定：請確認 RAP 端點網址。")
        if not RAP_MODEL_NAME:
            missing.append("RAP_MODEL_NAME 未設定：請確認要使用的模型名稱。")

    if require_gdrive:
        if not GDRIVE_FOLDER_IDS:
            missing.append(
                "GDRIVE_FOLDER_IDS 未設定：請填入 Google Drive 資料夾 ID"
                "（網址 https://drive.google.com/drive/folders/<ID> 的 <ID>），多個以逗號分隔。"
            )
        if not GDRIVE_SERVICE_ACCOUNT_JSON:
            missing.append(
                "GDRIVE_SERVICE_ACCOUNT_JSON 未設定：請填入 Service Account 金鑰 JSON 檔路徑，或直接貼上金鑰 JSON 內容。"
            )
        elif (
            not GDRIVE_SERVICE_ACCOUNT_JSON.startswith("{")
            and not resolve_gdrive_credentials_path(GDRIVE_SERVICE_ACCOUNT_JSON).is_file()
        ):
            missing.append(
                "GDRIVE_SERVICE_ACCOUNT_JSON 指定的金鑰檔不存在："
                f"{resolve_gdrive_credentials_path(GDRIVE_SERVICE_ACCOUNT_JSON)}"
            )

    return missing


def require_valid(
    require_github: bool = True, require_rap: bool = True, require_gdrive: bool = False
) -> None:
    """設定不齊全時直接拋出 ConfigError，訊息可直接顯示給使用者。"""
    missing = validate(require_github=require_github, require_rap=require_rap, require_gdrive=require_gdrive)
    if missing:
        raise ConfigError("設定不完整，請檢查 .env：\n- " + "\n- ".join(missing))
