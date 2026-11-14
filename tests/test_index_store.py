from __future__ import annotations

import pytest

from src.card_cache import load_cards, save_cards
from src.chunker import Chunk
from src.index_store import IndexStore, IndexStoreError
from src.summarizer import FeatureCard


def _chunk(text: str, index: int = 0, title: str = "Drive 文件：規格.xlsx") -> Chunk:
    return Chunk(doc_type="gdrive", source_title=title, source_ref="https://drive/x", section="", text=text, index=index)


def test_sync_chunks_embeds_only_new_and_removes_stale(engine, embedder):
    store = IndexStore(engine, embedder)
    a, b, c = _chunk("負載平衡器逾時 60 秒", 0), _chunk("資料庫連線池上限 50", 1), _chunk("即時推送尚在規劃", 2)

    assert store.sync_chunks([a, b]) == (2, 0)
    embedder.calls.clear()

    assert store.sync_chunks([b, c]) == (1, 1)
    assert len(embedder.calls) == 1 and "即時推送" in embedder.calls[0]
    assert {h["document"].split("\n", 1)[1] for h in store.query("任何問題", top_k=10)} == {b.text, c.text}


def test_sync_cards_reembeds_card_whose_content_changed(engine, embedder):
    store = IndexStore(engine, embedder)
    card = FeatureCard(feature_name="CSV 匯入", business_scenario="批次匯入名單", status="已上線", doc_type="pr",
                       source_title="PR #1", source_ref="https://github.com/o/r/pull/1")
    store.sync_cards([card])
    embedder.calls.clear()

    store.sync_cards([card])
    assert embedder.calls == []

    card.status = "已下線"
    assert store.sync_cards([card]) == (1, 0)
    assert store.count("card") == 1
    assert "已下線" in store.query("CSV", top_k=1)[0]["document"]


def test_query_ranks_closest_first_across_cards_and_chunks(engine, embedder):
    store = IndexStore(engine, embedder)
    store.sync_chunks([_chunk("負載平衡器的逾時設定為 60 秒", 0), _chunk("年度員工旅遊地點投票", 1)])
    store.sync_cards([FeatureCard(feature_name="資料庫備份", business_scenario="每日自動備份", status="已上線",
                                  doc_type="readme", source_title="README")])

    hits = store.query("負載平衡器逾時設定", top_k=3)
    assert "負載平衡器" in hits[0]["document"]
    assert hits[0]["metadata"]["kind"] == "chunk"
    assert [h["distance"] for h in hits] == sorted(h["distance"] for h in hits)


def test_vectors_from_another_model_are_rejected(engine, embedder):
    IndexStore(engine, embedder).sync_chunks([_chunk("內容", 0)])

    class OtherModel(type(embedder)):
        model_name = "another-model"

    other = IndexStore(engine, OtherModel())
    with pytest.raises(IndexStoreError, match="reembed"):
        other.query("內容")

    assert other.reembed() == 1
    assert other.query("內容")[0]["document"].endswith("內容")


def test_card_cache_save_matches_given_mapping(engine):
    card = {"feature_name": "x", "business_scenario": "y", "status": "z"}
    save_cards(engine, {"pr:1": card, "gdrive:a:old": card})
    save_cards(engine, {"pr:1": card, "gdrive:a:new": card})
    assert set(load_cards(engine)) == {"pr:1", "gdrive:a:new"}
