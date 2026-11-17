"""G6：Microsoft 365 連接器，用模擬的 Microsoft Graph 測（沒有真的租用戶）。"""
from __future__ import annotations

import httpx
import pytest

from src.connectors import ConnectorError, M365Connector, build_connectors

DRIVE = "b!drive"
FILES = {
    "f-md": b"# \xe5\xa0\xb1\xe5\x83\xb9\xe6\xb5\x81\xe7\xa8\x8b\nstep 1",
    "f-csv": "客戶,金額\nA,100\n".encode("utf-8"),
}


def graph(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "login.microsoftonline.com/t1/oauth2/v2.0/token" in url:
        body = request.content.decode()
        if "client_secret=good" not in body:
            return httpx.Response(401, json={"error": "invalid_client"})
        return httpx.Response(200, json={"access_token": "tok"})
    assert request.headers["Authorization"] == "Bearer tok"
    path = request.url.raw_path.decode()
    if path.endswith(f"/drives/{DRIVE}/root:%2F%E6%A5%AD%E5%8B%99") or path.endswith(f"/drives/{DRIVE}/root:/%E6%A5%AD%E5%8B%99"):
        return httpx.Response(200, json={"id": "root"})
    if f"/items/root/children" in path:
        if "skiptoken" not in url:
            return httpx.Response(200, json={
                "value": [
                    {"id": "f-md", "name": "報價.md", "file": {}, "size": 20, "webUrl": "https://sp/報價.md",
                     "lastModifiedDateTime": "2026-11-01T00:00:00Z"},
                    {"id": "d1", "name": "子資料夾", "folder": {"childCount": 1}},
                ],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/drives/b!drive/items/root/children?$skiptoken=2",
            })
        return httpx.Response(200, json={"value": [{"id": "f-exe", "name": "tool.exe", "file": {}, "size": 1}]})
    if "/items/d1/children" in path:
        return httpx.Response(200, json={"value": [{"id": "f-csv", "name": "名單.csv", "file": {}, "size": 30}]})
    if path.endswith("/content"):
        fid = path.split("/items/")[1].split("/")[0]
        return httpx.Response(200, content=FILES[fid])
    if path.endswith("/permissions"):
        fid = path.split("/items/")[1].split("/")[0]
        if fid == "f-md":
            return httpx.Response(200, json={"value": [
                {"grantedToV2": {"user": {"email": "Amy@Corp.com"}}},
                {"grantedToIdentitiesV2": [{"group": {"email": "Sales@Corp.com"}}]},
            ]})
        return httpx.Response(403, json={})
    return httpx.Response(404, json={"path": path})


@pytest.fixture
def mocked(monkeypatch):
    monkeypatch.setattr(M365Connector, "transport", httpx.MockTransport(graph))


SETTINGS = {"type": "m365", "name": "sp", "tenant_id": "t1", "client_id": "c", "client_secret": "good",
            "drive_id": DRIVE, "folder_path": "/業務"}


def test_extracts_files_recursively_with_paging_and_acl(mocked):
    (conn,) = build_connectors([SETTINGS])
    result = conn.extract()
    by_path = {d.files[0]: d for d in result.docs}
    assert set(by_path) == {"sp/報價.md", "sp/子資料夾/名單.csv"}
    md = by_path["sp/報價.md"]
    assert "報價流程" in md.content and md.metadata["url"] == "https://sp/報價.md"
    assert md.source_acl == ["amy@corp.com", "group:sales@corp.com"]
    assert md.source_path == "m365/sp/報價.md"
    assert by_path["sp/子資料夾/名單.csv"].source_acl is None  # 讀不到權限 → 交給路徑規則
    assert any("tool.exe" in s for s in result.skipped)
    assert result.complete and len(result.seen_file_ids) == 2


def test_bad_secret_and_missing_settings(mocked):
    with pytest.raises(ConnectorError, match="存取權杖"):
        M365Connector(dict(SETTINGS, client_secret="bad")).extract()
    with pytest.raises(ConnectorError, match="drive_id"):
        M365Connector({"type": "m365", "tenant_id": "t", "client_id": "c", "client_secret": "s"})


def test_secret_from_env(monkeypatch, mocked):
    monkeypatch.setenv("M365_CLIENT_SECRET", "good")
    s = dict(SETTINGS)
    s.pop("client_secret")
    assert len(M365Connector(s).extract().docs) == 2


def test_max_files_marks_incomplete(mocked):
    result = M365Connector(dict(SETTINGS, max_files=1)).extract()
    assert not result.complete and len(result.docs) == 1
