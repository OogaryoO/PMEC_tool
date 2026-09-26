"""
Streamlit 前端：PMEC 知識庫助理。

側邊欄：連線狀態、目標 Repo / Google Drive 資料夾、一鍵同步知識庫。
主畫面：多輪對話聊天介面，回答依問題意圖產生，並附上引用來源。
"""
from __future__ import annotations

import datetime as dt
import traceback

import streamlit as st

from src import config
from src.config import ConfigError
from src.gdrive_extractor import GDriveExtractorError
from src.github_extractor import GitHubExtractorError
from src.rag_engine import RAGAnswer, RAGEngine, RAGEngineError
from src.summarizer import SummarizerError
from src.card_cache import CardCacheError
from src.sync_pipeline import bootstrap_from_cache, run_sync
from src.vector_store import VectorStore, VectorStoreError

st.set_page_config(
    page_title="PMEC 知識庫助理",
    page_icon="🧭",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Cached resources
# ---------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def get_vector_store() -> VectorStore:
    return VectorStore()


@st.cache_resource(show_spinner=False)
def get_rag_engine(_vector_store: VectorStore) -> RAGEngine:
    return RAGEngine(vector_store=_vector_store)


def try_get_vector_store():
    try:
        return get_vector_store(), None
    except VectorStoreError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001
        return None, f"向量資料庫初始化發生未預期錯誤：{exc}"


def try_get_rag_engine(vector_store: VectorStore):
    try:
        return get_rag_engine(vector_store), None
    except (RAGEngineError, ConfigError) as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001
        return None, f"RAG 引擎初始化發生未預期錯誤：{exc}"


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []  # list of dicts: role, content, answer(optional), error(optional)
if "last_sync_info" not in st.session_state:
    st.session_state.last_sync_info = None


# ---------------------------------------------------------------------------
# Sync pipeline: Extractor -> Summarizer -> VectorStore
# ---------------------------------------------------------------------------
def run_sync_pipeline(vector_store: VectorStore) -> None:
    with st.status("同步知識庫中...", expanded=True) as status:
        try:
            def _progress(msg: str) -> None:
                st.write(msg)

            result = run_sync(vector_store, progress=_progress)

            st.session_state.last_sync_info = {
                "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "doc_count": result.doc_count,
                "reused_count": result.reused_count,
                "new_count": result.new_count,
                "failed_count": result.failed_count,
                "removed_count": result.removed_count,
                "chunk_count": result.chunk_count,
                "total_in_db": result.total_in_db,
                "cache_used": result.cache_used,
            }
            if result.cache_warning:
                st.warning(result.cache_warning)
            status.update(label="✅ 同步完成！", state="complete")

        except ConfigError as exc:
            status.update(label="設定不完整，同步已中止。", state="error")
            st.error(str(exc))
        except GitHubExtractorError as exc:
            status.update(label="GitHub 資料抓取失敗。", state="error")
            st.error(str(exc))
        except GDriveExtractorError as exc:
            status.update(label="Google Drive 資料抓取失敗。", state="error")
            st.error(str(exc))
        except SummarizerError as exc:
            status.update(label="LLM 摘要失敗。", state="error")
            st.error(str(exc))
        except VectorStoreError as exc:
            status.update(label="向量資料庫寫入失敗。", state="error")
            st.error(str(exc))
        except Exception as exc:  # noqa: BLE001
            status.update(label="發生未預期錯誤。", state="error")
            st.error(f"同步過程發生未預期錯誤：{exc}")
            st.code(traceback.format_exc())


# ---------------------------------------------------------------------------
# Auto-bootstrap: 冷啟動時本地向量資料庫是空的，但 GitHub 快取可能已有先前累積的
# 卡片；自動從快取還原，不呼叫 GitHub extractor、不呼叫 RAP LLM，讓使用者一進入
# 頁面就能直接提問，不必手動點擊「同步並更新」並等待完整流程。
# ---------------------------------------------------------------------------
if "cache_bootstrap_attempted" not in st.session_state:
    st.session_state.cache_bootstrap_attempted = False
if "cache_bootstrap_warning" not in st.session_state:
    st.session_state.cache_bootstrap_warning = None

if not st.session_state.cache_bootstrap_attempted:
    _bootstrap_vs, _bootstrap_vs_error = try_get_vector_store()
    if not _bootstrap_vs_error:
        try:
            if _bootstrap_vs.count() == 0:
                st.session_state.cache_bootstrap_attempted = True
                with st.spinner("首次載入：從功能卡片快取還原知識庫，並重新抓取文件原文建立段落索引 ..."):
                    _result = bootstrap_from_cache(_bootstrap_vs)
                if _result:
                    st.session_state.last_sync_info = {
                        "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "doc_count": _result.card_count,
                        "reused_count": _result.card_count,
                        "new_count": 0,
                        "failed_count": 0,
                        "removed_count": 0,
                        "chunk_count": _result.chunk_count,
                        "total_in_db": _result.total_in_db,
                        "cache_used": True,
                    }
                    st.session_state.cache_bootstrap_warning = _result.chunk_warning
            else:
                st.session_state.cache_bootstrap_attempted = True
        except VectorStoreError:
            pass  # 交由下方 Sidebar 區塊統一顯示向量資料庫錯誤
        except CardCacheError as exc:
            st.session_state.cache_bootstrap_attempted = True
            st.session_state.cache_bootstrap_warning = f"自動從快取還原知識庫失敗：{exc}"


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("⚙️ 連線狀態")

    st.markdown(f"**目標 Repo**：`{config.GITHUB_REPO or '（未設定）'}`")
    if config.GDRIVE_ENABLED:
        st.markdown("**Google Drive 資料夾**：" + "、".join(f"`{fid}`" for fid in config.GDRIVE_FOLDER_IDS))
    else:
        st.markdown("**Google Drive 資料夾**：未設定（僅同步 GitHub）")
    st.markdown(f"**RAP 模型**：`{config.RAP_MODEL_NAME or '（未設定）'}`")
    st.markdown(f"**RAP 端點**：`{config.RAP_BASE_URL or '（未設定）'}`")
    if config.CACHE_ENABLED and config.CACHE_REPO:
        st.markdown(f"**功能卡片快取**：`{config.CACHE_REPO}` / `{config.CACHE_FILE_PATH}`")
    else:
        st.markdown("**功能卡片快取**：未啟用（每次同步都會重新呼叫 RAP LLM 摘要）")

    if st.session_state.get("cache_bootstrap_warning"):
        st.caption(
            f"⚠️ {st.session_state.cache_bootstrap_warning}"
            "（可手動點擊下方「同步並更新」重建知識庫）"
        )

    github_missing = config.validate(require_github=True, require_rap=False)
    rap_missing = config.validate(require_github=False, require_rap=True)
    gdrive_missing = (
        config.validate(require_github=False, require_rap=False, require_gdrive=True)
        if config.GDRIVE_ENABLED
        else []
    )

    if not github_missing:
        st.success("GitHub 設定完整")
    else:
        st.error("GitHub 設定不完整")
        for m in github_missing:
            st.caption(f"⚠️ {m}")

    if not rap_missing:
        st.success("RAP LLM 設定完整")
    else:
        st.error("RAP LLM 設定不完整")
        for m in rap_missing:
            st.caption(f"⚠️ {m}")

    if config.GDRIVE_ENABLED:
        if not gdrive_missing:
            st.success("Google Drive 設定完整")
        else:
            st.error("Google Drive 設定不完整")
            for m in gdrive_missing:
                st.caption(f"⚠️ {m}")

    st.divider()

    vector_store, vs_error = try_get_vector_store()
    if vs_error:
        st.error(f"向量資料庫無法使用：{vs_error}")
    else:
        try:
            c1, c2 = st.columns(2)
            c1.metric("功能卡片", vector_store.count("card"))
            c2.metric("原文段落", vector_store.count("chunk"))
        except VectorStoreError as exc:
            st.error(str(exc))

    sync_disabled = bool(github_missing or rap_missing or gdrive_missing or vs_error)
    if st.button("🔄 同步並更新知識庫", use_container_width=True, disabled=sync_disabled):
        run_sync_pipeline(vector_store)

    if sync_disabled and not vs_error:
        st.caption("請先完成上方 GitHub / RAP / Google Drive 設定才能同步。")

    if st.session_state.last_sync_info:
        info = st.session_state.last_sync_info
        st.caption(
            f"上次同步：{info['time']}\n\n"
            f"抓取文件 {info['doc_count']} 份 → 重用快取 {info['reused_count']} 份／"
            f"新摘要 {info['new_count']} 份（{info['failed_count']} 份失敗）\n\n"
            + (f"移除過期 Drive 卡片 {info['removed_count']} 張\n\n" if info.get("removed_count", 0) > 0 else "")
            + f"原文段落 {info['chunk_count']} 段\n\n"
            + f"資料庫目前共 {info['total_in_db']} 筆\n\n"
            f"快取：{'已啟用' if info['cache_used'] else '未啟用'}"
        )

    st.divider()
    if st.button("🗑️ 清空對話紀錄", use_container_width=True):
        st.session_state.messages = []
        st.rerun()


# ---------------------------------------------------------------------------
# Main: chat UI
# ---------------------------------------------------------------------------
st.title("🧭 PMEC 知識庫助理")
st.caption(
    "可詢問產品功能、開發進度、規劃文件或對外說法，例如「目前支援哪些部署方式？」、"
    "「負載平衡器的逾時設定是多少？」、「我們能不能承諾客戶支援即時推送？」；支援延續上文的追問。"
)


def render_answer(answer: RAGAnswer) -> None:
    st.markdown(answer.markdown)

    with st.expander(f"🔍 參考資料（{len(answer.retrieved)} 筆）"):
        if not answer.retrieved:
            st.caption("知識庫中查無相關資料，建議先執行「同步並更新知識庫」。")
        for idx, item in enumerate(answer.retrieved, start=1):
            ref = item.source_ref
            source = f"[{item.source_title}]({ref})" if ref.startswith("http") else f"{item.source_title} {ref}".strip()
            if item.section:
                source += f"（{item.section}）"
            if item.kind == "chunk":
                quoted = "\n".join(f"> {line}" for line in item.summary.splitlines())
                st.markdown(f"**[{idx}] 原文段落** — 來源：{source}\n\n{quoted}")
            else:
                st.markdown(
                    f"**[{idx}] {item.title}**（{item.status}）— {item.summary}"
                    f"\n\n來源：{source}"
                )


def build_history(messages: list) -> list:
    """把 session 中成功的 (使用者問題, 助理回答) 配對起來；錯誤回應不列入上下文。"""
    pairs = []
    for prev, cur in zip(messages, messages[1:]):
        if prev["role"] == "user" and cur["role"] == "assistant" and cur.get("answer") is not None:
            pairs.append((prev["content"], cur["content"]))
    return pairs


for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg.get("error"):
            st.error(msg["content"])
        elif msg.get("answer") is not None:
            render_answer(msg["answer"])
        else:
            st.markdown(msg["content"])

question = st.chat_input("輸入問題 ...")

if question:
    history = build_history(st.session_state.messages)
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        if vs_error:
            error_text = f"向量資料庫目前無法使用：{vs_error}"
            st.error(error_text)
            st.session_state.messages.append({"role": "assistant", "content": error_text, "error": True})
        else:
            rag_engine, rag_error = try_get_rag_engine(vector_store)
            if rag_error:
                st.error(rag_error)
                st.session_state.messages.append({"role": "assistant", "content": rag_error, "error": True})
            else:
                try:
                    with st.spinner("檢索知識庫並產生回答中 ..."):
                        answer = rag_engine.answer(question, history=history)
                    render_answer(answer)
                    st.session_state.messages.append({"role": "assistant", "content": answer.markdown, "answer": answer})
                except RAGEngineError as exc:
                    st.error(str(exc))
                    st.session_state.messages.append({"role": "assistant", "content": str(exc), "error": True})
                except Exception as exc:  # noqa: BLE001
                    err_text = f"產生回答時發生未預期錯誤：{exc}"
                    st.error(err_text)
                    st.code(traceback.format_exc())
                    st.session_state.messages.append({"role": "assistant", "content": err_text, "error": True})
