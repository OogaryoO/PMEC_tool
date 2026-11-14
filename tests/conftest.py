from __future__ import annotations

import hashlib
import math
import sys
import tempfile
from pathlib import Path
from typing import List, Sequence

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import get_engine, upgrade_schema  # noqa: E402


@pytest.fixture(scope="session")
def db_url():
    pgserver = pytest.importorskip("pgserver")
    server = pgserver.get_server(tempfile.mkdtemp(prefix="pmec_test_db_"), cleanup_mode="stop")
    url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    upgrade_schema(url)
    yield url
    server.cleanup()


@pytest.fixture
def engine(db_url):
    eng = get_engine(db_url)
    with eng.begin() as conn:
        conn.execute(text("TRUNCATE feature_cards, retrieval_items, index_meta, sync_runs, audit_log, feedback"))
    yield eng
    eng.dispose()


class HashEmbedder:
    """以字元 bigram 雜湊產生 384 維向量：共用字詞越多越相近，且記錄每次被要求計算的文字。"""

    model_name = "test-hash-embedder"

    def __init__(self) -> None:
        self.calls: List[str] = []

    def __call__(self, texts: Sequence[str]) -> List[List[float]]:
        out = []
        for t in texts:
            self.calls.append(t)
            vec = [0.0] * 384
            for a, b in zip(t, t[1:] + " "):
                vec[int(hashlib.md5((a + b).encode()).hexdigest(), 16) % 384] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


@pytest.fixture
def embedder():
    return HashEmbedder()
