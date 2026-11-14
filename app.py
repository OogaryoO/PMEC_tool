"""
Streamlit 前端：PMEC 知識庫助理。

登入：Google 帳號（st.login），僅限 ALLOWED_EMAIL_DOMAINS 的公司帳號。
側邊欄：知識庫筆數；管理員另可看到連線設定、上次同步結果，並觸發同步。
主畫面：多輪對話聊天介面，回答依問題意圖產生，並附上引用來源。
權限：只檢索提問者看得到的資料（tenant.toml）；每次提問寫入操作紀錄，回答下方可按「有用／沒幫助」。
"""
from __future__ import annotations

import traceback

import streamlit as st

from src import audit, config
from src.auth import Principal, authorize
from src.config import ConfigError
from src.db import get_engine
from src.index_store import IndexStore, IndexStoreError
from src.rag_engine import RAGAnswer, RAGEngine, RAGEngineError, is_no_source_answer
from src.tenant import TenantConfigError, get_tenant

st.set_page_config(
    page_title="PMEC 知識庫助理",
    page_icon="🧭",
    layout="wide",
)


# ---------------------------------------------------------------------------
# 登入關卡
# ---------------------------------------------------------------------------
def require_login() -> Principal:
    if config.AUTH_DISABLED:
        return Principal(email="dev@localhost", name="本機開發", is_admin=True)

    # 未設定 [auth] 時 st.user 沒有 is_logged_in 屬性，一律視為未登入
    if not st.user.get("is_logged_in", False):
        st.title("🧭 PMEC 知識庫助理")
        st.write("請使用公司 Google 帳號登入。")
        if st.button("使用 Google 帳號登入", type="primary"):
            st.login()
        st.stop()

    principal = authorize(
        email=st.user.get("email"),
        email_verified=st.user.get("email_verified"),
        name=st.user.get("name"),
        allowed_domains=config.ALLOWED_EMAIL_DOMAINS,
        admin_emails=config.ADMIN_EMAILS,
    )
    if principal is None:
        st.error(f"帳號 {st.user.get('email')} 沒有使用權限，請改用公司 Google 帳號登入。")
        if st.button("登出"):
            st.logout()
        st.stop()
    return principal


principal = require_login()

try:
    tenant = get_tenant()
except TenantConfigError as exc:
    st.error("系統設定有誤，請聯絡管理員。")
    if principal.is_admin:
        st.code(str(exc))
    st.stop()


# ---------------------------------------------------------------------------
# Cached resources
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_index_store() -> IndexStore:
    return IndexStore(get_engine())


@st.cache_resource(show_spinner=False)
def get_rag_engine(_index_store: IndexStore) -> RAGEngine:
    return RAGEngine(index_store=_index_store)


def try_get_index_store():
    try:
        return get_index_store(), None
    except (ConfigError, IndexStoreError) as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001
        return None, f"資料庫初始化發生未預期錯誤：{exc}"


if "messages" not in st.session_state:
    st.session_state.messages = []  # list of dicts: role, content, answer(optional), error(optional)


# ---------------------------------------------------------------------------
# 同步（僅管理員）
# ---------------------------------------------------------------------------
def start_sync(index_store: IndexStore) -> None:
    if config.SYNC_JOB_NAME:
        from src.jobs import JobTriggerError, trigger_sync_job

        try:
            trigger_sync_job()
            st.success("已啟動背景同步工作，完成後重新整理頁面即可看到結果。")
        except Exception as exc:  # noqa: BLE001 - 含 google.auth 錯誤
            st.error(str(exc) if isinstance(exc, JobTriggerError) else f"觸發同步工作失敗：{exc}")
        return

    # 未設定 Cloud Run Job（本機開發）：直接在這個程序內同步
    from src.sync_pipeline import run_recorded_sync

    with st.status("同步知識庫中...", expanded=True) as status:
        try:
            run_recorded_sync(get_engine(), index_store, trigger=f"web:{principal.email}", progress=st.write)
            status.update(label="✅ 同步完成！", state="complete")
        except Exception as exc:  # noqa: BLE001 - 錯誤已記錄在 sync_runs
            status.update(label="同步失敗。", state="error")
            st.error(str(exc))
            st.code(traceback.format_exc())


def render_admin_panel(index_store) -> None:
    st.divider()
    st.subheader("管理")
    from src.connectors import ConnectorError, build_connectors
    from src.tenant import STAGE_NAMES

    st.markdown(f"**企業設定**：{tenant.name}；第 {tenant.stage} 階段（{STAGE_NAMES[tenant.stage]}）")
    missing = config.validate(require_github=False, require_rap=True)
    try:
        st.markdown("**資料來源**：" + "、".join(c.describe() for c in build_connectors(tenant.connectors)))
    except ConnectorError as exc:
        missing.append(str(exc))
    st.markdown(f"**RAP 模型**：`{config.RAP_MODEL_NAME or '（未設定）'}`")

    for m in missing:
        st.caption(f"⚠️ {m}")

    if index_store is not None:
        from src.sync_pipeline import last_sync_run

        try:
            run = last_sync_run(get_engine())
        except Exception as exc:  # noqa: BLE001
            run = None
            st.caption(f"⚠️ 讀取同步紀錄失敗：{exc}")
        if run:
            when = run["started_at"].astimezone().strftime("%Y-%m-%d %H:%M")
            label = {"running": "進行中", "succeeded": "成功", "failed": "失敗"}[run["status"]]
            st.caption(f"上次同步：{when}（{label}，{run['trigger']}）")
            s = run["summary"] or {}
            if s:
                st.caption(
                    f"抓取文件 {s['doc_count']} 份 → 重用快取 {s['reused_count']} 份／"
                    f"新摘要 {s['new_count']} 份（{s['failed_count']} 份失敗）；"
                    f"移除過期 Drive 卡片 {s['removed_count']} 張；重算向量 {s['embedded_count']} 筆"
                )
            if run["error"]:
                st.caption(f"⚠️ {run['error']}")

    if st.button("🔄 同步並更新知識庫", use_container_width=True, disabled=bool(missing or index_store is None)):
        start_sync(index_store)

    if index_store is not None:
        with st.expander("📊 使用週報（最近 7 天）"):
            try:
                st.markdown(audit.usage_report(get_engine(), days=7).to_markdown())
            except Exception as exc:  # noqa: BLE001
                st.caption(f"⚠️ 讀取操作紀錄失敗（資料庫是否已執行 migrate？）：{exc}")


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
index_store, store_error = try_get_index_store()

with st.sidebar:
    st.markdown(f"👤 **{principal.name}**" + ("（管理員）" if principal.is_admin else ""))
    if not config.AUTH_DISABLED and st.button("登出", use_container_width=True):
        st.logout()

    if store_error:
        st.error("知識庫目前無法使用，請聯絡管理員。")
        if principal.is_admin:
            st.caption(store_error)
    else:
        try:
            c1, c2 = st.columns(2)
            c1.metric("功能卡片", index_store.count("card"))
            c2.metric("原文段落", index_store.count("chunk"))
        except IndexStoreError as exc:
            st.error("讀取知識庫失敗，請聯絡管理員。")
            if principal.is_admin:
                st.caption(str(exc))

    if principal.is_admin:
        render_admin_panel(index_store)

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


def render_feedback(audit_id) -> None:
    """回答下方的「有用／沒幫助」：每則回答只記一次，是試點週報的成功指標。"""
    if audit_id is None:
        return
    done = st.session_state.setdefault("feedback_done", set())
    value = st.feedback("thumbs", key=f"fb_{audit_id}", disabled=audit_id in done)
    if value is not None and audit_id not in done:
        if audit.record_feedback(get_engine(), audit_id, principal.email, helpful=(value == 1)):
            done.add(audit_id)
            st.caption("已記錄，謝謝！")


def render_answer(answer: RAGAnswer, audit_id=None) -> None:
    st.markdown(answer.markdown)
    if tenant.reveal_denied_count_to_users and answer.denied_count:
        st.caption(f"另有 {answer.denied_count} 筆相關資料你沒有權限查看；需要的話請向資料負責人申請。")

    with st.expander(f"🔍 參考資料（{len(answer.retrieved)} 筆）"):
        if not answer.retrieved:
            st.caption("知識庫中查無相關資料。")
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
    render_feedback(audit_id)


def build_history(messages: list) -> list:
    """把 session 中成功的 (使用者問題, 助理回答) 配對起來；錯誤回應不列入上下文。"""
    pairs = []
    for prev, cur in zip(messages, messages[1:]):
        if prev["role"] == "user" and cur["role"] == "assistant" and cur.get("answer") is not None:
            pairs.append((prev["content"], cur["content"]))
    return pairs


def _fail(text: str, detail: str = "") -> None:
    st.error(text)
    if detail and principal.is_admin:
        st.code(detail)
    st.session_state.messages.append({"role": "assistant", "content": text, "error": True})


for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        if msg.get("error"):
            st.error(msg["content"])
        elif msg.get("answer") is not None:
            render_answer(msg["answer"], msg.get("audit_id"))
        else:
            st.markdown(msg["content"])

question = st.chat_input("輸入問題 ...")

if question:
    history = build_history(st.session_state.messages)
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        if store_error:
            _fail("知識庫目前無法使用，請聯絡管理員。", store_error)
        else:
            try:
                rag_engine = get_rag_engine(index_store)
                with st.spinner("檢索知識庫並產生回答中 ..."):
                    answer = rag_engine.answer(
                        question, history=history, principals=tenant.principals_for(principal.email)
                    )
                audit_id = audit.record_question(
                    get_engine(), principal.email, question, answer.source_ids, answer.denied_count,
                    no_source=is_no_source_answer(answer.markdown),
                )
                render_answer(answer, audit_id)
                st.session_state.messages.append(
                    {"role": "assistant", "content": answer.markdown, "answer": answer, "audit_id": audit_id}
                )
            except (RAGEngineError, ConfigError) as exc:
                audit.record_question(get_engine(), principal.email, question, [], 0, error=str(exc)[:500])
                _fail(str(exc))
            except Exception as exc:  # noqa: BLE001
                _fail(f"產生回答時發生未預期錯誤：{exc}", traceback.format_exc())
