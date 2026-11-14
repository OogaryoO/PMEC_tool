"""權限過濾、排除規則、共用資料夾連接器、操作紀錄與週報的整合測試（不連 RAP、不連 Google）。"""
from __future__ import annotations

import datetime as dt

import docx

from src import audit
from src.card_cache import load_cards
from src.chunker import Chunk
from src.connectors import ConnectorResult, FileShareConnector, build_connectors
from src.documents import ExtractedDocument
from src.gdrive_extractor import drive_acl
from src.index_store import IndexStore
from src.summarizer import FeatureCard
from src.sync_pipeline import run_sync
from src.tenant import parse_tenant

TENANT = parse_tenant(
    {
        "exclude": ["fileshare/nas/人資/薪資/*"],
        "groups": {"業務部": ["amy@corp.com"]},
        "permissions": {
            "default": ["domain:corp.com"],
            "rules": [{"match": "fileshare/nas/業務/*", "allow": ["group:業務部"]}],
        },
    }
)


class FakeSummarizer:
    def __init__(self):
        self.seen = []

    def summarize_batch(self, docs, repo_name, progress_callback=None):
        out = []
        for doc in docs:
            self.seen.append(doc.title)
            out.append(
                (
                    doc,
                    FeatureCard(
                        feature_name=doc.title,
                        business_scenario=doc.content[:50],
                        status="已上線",
                        doc_type=doc.doc_type,
                        source_title=doc.title,
                        source_ref=doc.files[0],
                        files=doc.files,
                    ),
                )
            )
        return out


def _make_share(root):
    (root / "業務").mkdir(parents=True)
    (root / "公告").mkdir()
    (root / "人資" / "薪資").mkdir(parents=True)
    (root / "業務" / "報價.md").write_text("# A 客戶報價\nA 客戶報價單價每台 12,000 元", encoding="utf-8")
    (root / "公告" / "週會.md").write_text("# 週會\n每週一上午十點開週會", encoding="utf-8")
    (root / "人資" / "薪資" / "2026.csv").write_text("姓名,月薪\n王小明,99999\n", encoding="utf-8")
    d = docx.Document()
    d.add_paragraph("出差旅費每日上限 2,500 元")
    d.save(root / "公告" / "出差規定.docx")
    (root / "公告" / "照片.jpg").write_bytes(b"\xff\xd8")
    (root / "公告" / "~$暫存.docx").write_bytes(b"x")


def test_fileshare_connector_reads_supported_files(tmp_path):
    _make_share(tmp_path)
    result = FileShareConnector({"type": "fileshare", "name": "nas", "root": str(tmp_path)}).extract()
    paths = sorted(d.files[0] for d in result.docs)
    assert paths == ["nas/人資/薪資/2026.csv", "nas/公告/出差規定.docx", "nas/公告/週會.md", "nas/業務/報價.md"]
    assert any("照片.jpg" in s for s in result.skipped)
    assert result.complete and len(result.seen_file_ids) == 4
    assert "2,500" in next(d for d in result.docs if d.files[0].endswith(".docx")).content


def test_build_connectors_rejects_unknown_type():
    import pytest

    from src.connectors import ConnectorError

    with pytest.raises(ConnectorError):
        build_connectors([{"type": "ftp"}])


def test_drive_permissions_mapping():
    item = {
        "permissions": [
            {"type": "user", "emailAddress": "A@corp.com"},
            {"type": "group", "emailAddress": "sales@corp.com"},
            {"type": "domain", "domain": "CORP.com"},
        ]
    }
    assert drive_acl(item) == ["a@corp.com", "group:sales@corp.com", "domain:corp.com"]
    assert drive_acl({}) is None


def test_sync_applies_exclude_and_acl_then_query_filters(tmp_path, engine, embedder):
    _make_share(tmp_path)
    connectors = build_connectors([{"type": "fileshare", "name": "nas", "root": str(tmp_path)}])
    store = IndexStore(engine, embedder)
    summarizer = FakeSummarizer()

    result = run_sync(engine, store, tenant=TENANT, connectors=connectors, summarizer=summarizer)

    assert result.doc_count == 3 and result.excluded_count == 1
    assert not any("薪資" in t for t in summarizer.seen)  # 敏感檔不送去摘要
    assert not any("薪資" in (c.get("source_ref") or "") for c in load_cards(engine).values())

    amy = TENANT.principals_for("amy@corp.com")
    carl = TENANT.principals_for("carl@corp.com")
    outsider = TENANT.principals_for("x@other.com")

    hits, denied = store.query_with_denied("A 客戶報價單價", top_k=20, principals=amy)
    assert any("12,000" in h["document"] for h in hits)

    hits, denied = store.query_with_denied("A 客戶報價單價", top_k=20, principals=carl)
    assert not any("12,000" in h["document"] for h in hits)
    assert denied >= 1  # 有相關資料，但 carl 沒權限
    assert any("週會" in h["document"] for h in hits)  # 全公司資料照常看得到

    assert store.query("週會", top_k=20, principals=outsider) == []
    assert not any("99999" in h["document"] for h in store.query("月薪", top_k=50))  # 連不過濾都查不到


def test_rule_change_updates_acl_without_reembedding(tmp_path, engine, embedder):
    _make_share(tmp_path)
    connectors = build_connectors([{"type": "fileshare", "name": "nas", "root": str(tmp_path)}])
    store = IndexStore(engine, embedder)
    run_sync(engine, store, tenant=TENANT, connectors=connectors, summarizer=FakeSummarizer())
    embedder.calls.clear()

    opened = parse_tenant({"exclude": TENANT.exclude, "permissions": {"default": ["*"]}})
    run_sync(engine, store, tenant=opened, connectors=connectors, summarizer=FakeSummarizer())
    assert embedder.calls == []
    assert any("12,000" in h["document"] for h in store.query("報價", top_k=20, principals=["*"]))


def test_deleted_share_file_removed_from_cache_and_index(tmp_path, engine, embedder):
    _make_share(tmp_path)
    connectors = build_connectors([{"type": "fileshare", "name": "nas", "root": str(tmp_path)}])
    store = IndexStore(engine, embedder)
    run_sync(engine, store, tenant=TENANT, connectors=connectors, summarizer=FakeSummarizer())
    (tmp_path / "公告" / "週會.md").unlink()

    result = run_sync(engine, store, tenant=TENANT, connectors=connectors, summarizer=FakeSummarizer())
    assert result.removed_count == 1
    assert not any("週會" in h["document"] for h in store.query("週會", top_k=50))


def test_chunks_without_acl_are_visible_to_everyone(engine, embedder):
    store = IndexStore(engine, embedder)
    store.sync_chunks([Chunk("gdrive", "Drive 文件：a", "x", "", "公開內容", 0)])
    assert store.query("公開內容", principals=["*"])


def test_audit_feedback_and_report(engine):
    a1 = audit.record_question(engine, "Amy@corp.com", "報價多少？", ["id1"], 0)
    a2 = audit.record_question(engine, "carl@corp.com", "薪資規定？", [], 2)
    audit.record_question(engine, "carl@corp.com", "壞掉的問題", [], 0, error="RAP 逾時")
    assert audit.record_feedback(engine, a1, "amy@corp.com", helpful=True)
    assert audit.record_feedback(engine, a2, "carl@corp.com", helpful=False, comment="找不到")

    report = audit.usage_report(engine, days=7)
    assert report.questions == 3 and report.active_users == 2
    assert report.answered_with_sources == 1 and report.questions_with_denied == 1
    assert report.helpful_ratio == 0.5
    assert report.unhelpful_questions == ["薪資規定？"]
    assert report.no_source_questions == ["薪資規定？"]
    assert "50%" in report.to_markdown()

    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)
    assert audit.usage_report(engine, days=7, now=future).questions == 0
    assert audit.purge_old(engine, retention_days=0) == 3


def test_connector_result_defaults():
    r = ConnectorResult(name="x", docs=[ExtractedDocument("readme", "R", "c")])
    assert r.complete and r.doc_type is None
