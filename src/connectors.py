"""
資料來源連接器（插頭）。每種來源一個類別，同步流程只認 Connector 介面，新增來源不改核心。

tenant.toml 範例：
    [[connectors]]
    type = "gdrive"
    folder_ids = ["1AbC..."]

    [[connectors]]
    type = "fileshare"          # 公司共用資料夾／NAS：先掛載到同步程式所在的機器
    name = "nas"
    root = "/mnt/nas/公司文件"

tenant.toml 沒有 [[connectors]] 時，依 .env 決定（GITHUB_REPO 有值 → github；GDRIVE_FOLDER_IDS 有值 → gdrive），
與加入本模組之前的行為相同。

狀態：github、gdrive、fileshare 可用；m365 只有介面，等訪談確認企業用 Microsoft 365 後實作
（見 docs/INTERVIEW_TO_CONFIG.md 第 7 題）。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from src import config
from src.documents import ExtractedDocument

logger = config.get_logger(__name__)


class ConnectorError(Exception):
    """連接器設定或連線錯誤，訊息需可直接呈現給管理員。"""


@dataclass
class ConnectorResult:
    name: str
    docs: List[ExtractedDocument]
    # 以下只對「一個檔案一份文件」的來源有意義（gdrive／fileshare／m365），用來清掉已刪除檔案的舊卡片
    doc_type: Optional[str] = None
    seen_file_ids: Set[str] = field(default_factory=set)
    complete: bool = True
    skipped: List[str] = field(default_factory=list)
    truncated: List[str] = field(default_factory=list)


class Connector:
    type: str = ""

    def __init__(self, settings: Dict[str, Any]) -> None:
        self.settings = settings
        self.name = str(settings.get("name") or self.type)

    def describe(self) -> str:
        return self.name

    def extract(self) -> ConnectorResult:  # pragma: no cover - 介面
        raise NotImplementedError


# ---------------------------------------------------------------------------
class GitHubConnector(Connector):
    type = "github"

    def describe(self) -> str:
        return f"GitHub `{self.settings.get('repo') or config.GITHUB_REPO}`"

    def extract(self) -> ConnectorResult:
        from src.github_extractor import GitHubExtractor

        kwargs = {}
        if self.settings.get("repo"):
            kwargs["repo_full_name"] = self.settings["repo"]
        docs = GitHubExtractor(**kwargs).extract_all()
        return ConnectorResult(name=self.name, docs=docs)


class GDriveConnector(Connector):
    type = "gdrive"

    def describe(self) -> str:
        ids = self.settings.get("folder_ids") or config.GDRIVE_FOLDER_IDS
        return f"Google Drive（{len(ids)} 個資料夾）"

    def extract(self) -> ConnectorResult:
        from src.gdrive_extractor import GDriveExtractor

        result = GDriveExtractor(
            folder_ids=self.settings.get("folder_ids") or None,
            max_files=self.settings.get("max_files") or None,
        ).extract_all()
        return ConnectorResult(
            name=self.name,
            docs=result.docs,
            doc_type="gdrive",
            seen_file_ids=result.seen_file_ids,
            complete=result.complete,
            skipped=result.skipped,
            truncated=result.truncated,
        )


class FileShareConnector(Connector):
    """讀取已掛載到本機的共用資料夾／NAS（SMB、NFS 掛載點或一般目錄）。

    部署方式：同步程式裝在公司內網的機器上，只往外連到資料庫與模型端點，不用把 NAS 開放到外部。
    權限：檔案系統的 ACL（NTFS／POSIX）不讀；用 tenant.toml 的 [permissions] 規則依路徑對應群組。
    """

    type = "fileshare"

    def __init__(self, settings: Dict[str, Any]) -> None:
        super().__init__(settings)
        root = settings.get("root")
        if not root:
            raise ConnectorError(f"fileshare 連接器「{self.name}」缺少 root（共用資料夾的掛載路徑）。")
        self.root = Path(root)
        self.max_files = int(settings.get("max_files", 2000))

    def describe(self) -> str:
        return f"共用資料夾「{self.name}」`{self.root}`"

    @staticmethod
    def _parser_for(suffix: str) -> Optional[Callable[[bytes], str]]:
        from src import gdrive_extractor as g

        return {
            ".txt": g._decode_text,
            ".md": g._decode_text,
            ".json": g._decode_text,
            ".csv": lambda b: g._delimited_to_text(b, ","),
            ".tsv": lambda b: g._delimited_to_text(b, "\t"),
            ".pdf": g._pdf_to_text,
            ".docx": g._docx_to_text,
            ".xlsx": g._xlsx_to_text,
            ".pptx": g._pptx_to_text,
        }.get(suffix.lower())

    def extract(self) -> ConnectorResult:
        from src.gdrive_extractor import MAX_DOWNLOAD_BYTES, MAX_TEXT_CHARS

        if not self.root.is_dir():
            raise ConnectorError(f"找不到共用資料夾 `{self.root}`：請確認已掛載，且同步程式的帳號有讀取權限。")

        result = ConnectorResult(name=self.name, docs=[], doc_type="fileshare")
        walk_errors: List[str] = []
        files: List[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root, onerror=lambda e: walk_errors.append(str(e))):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fn in sorted(filenames):
                if not fn.startswith(("~$", ".")):
                    files.append(Path(dirpath) / fn)
        if walk_errors:
            result.complete = False
            result.skipped.extend(f"（資料夾讀取失敗：{e}）" for e in walk_errors)

        for path in files:
            rel = path.relative_to(self.root).as_posix()
            logical = f"{self.name}/{rel}"
            parser = self._parser_for(path.suffix)
            if parser is None:
                result.skipped.append(f"{logical}（不支援的格式）")
                continue
            if len(result.seen_file_ids) >= self.max_files:
                result.complete = False
                logger.warning("共用資料夾「%s」已達 max_files=%d 上限，停止掃描。", self.name, self.max_files)
                break
            file_id = hashlib.sha256(logical.encode("utf-8")).hexdigest()[:24]
            result.seen_file_ids.add(file_id)
            try:
                stat = path.stat()
                if stat.st_size > MAX_DOWNLOAD_BYTES:
                    result.skipped.append(f"{logical}（檔案超過 20MB）")
                    continue
                text = parser(path.read_bytes()).strip()
            except Exception as exc:  # noqa: BLE001
                result.skipped.append(f"{logical}（讀取失敗：{exc}）")
                continue
            if not text:
                result.skipped.append(f"{logical}（沒有可擷取的文字，可能是掃描影像）")
                continue
            if len(text) >= MAX_TEXT_CHARS:
                result.truncated.append(logical)
            result.docs.append(
                ExtractedDocument(
                    doc_type="fileshare",
                    title=f"共用資料夾文件：{logical}",
                    content=text[:MAX_TEXT_CHARS],
                    files=[logical],
                    metadata={
                        "file_id": file_id,
                        "modified_time": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat(),
                        "path": str(path),
                    },
                )
            )
        logger.info("共用資料夾「%s」：讀取 %d 份、略過 %d 份。", self.name, len(result.docs), len(result.skipped))
        return result


class M365Connector(Connector):
    """Microsoft 365（SharePoint／OneDrive）——只有介面，尚未實作。

    實作計畫（訪談第 7 題答「Microsoft 365」才做，估 3～5 天）：
      1. Entra ID 註冊應用程式，Graph 應用程式權限 Sites.Selected（只授權指定網站，最小權限）。
      2. 用 Graph delta API 列出檔案（可增量同步），下載後重用 gdrive_extractor 的 docx/xlsx/pptx/pdf 解析。
      3. 權限：GET /drives/{id}/items/{id}/permissions → 換成主體字串放 source_acl。
      4. 參考：Grok 的 SharePoint 連接器也用 Sites.Selected（docs.x.ai/grok/connectors/sharepoint）。
    """

    type = "m365"

    def extract(self) -> ConnectorResult:
        raise ConnectorError(
            "Microsoft 365 連接器尚未實作（等訪談確認企業使用 Microsoft 365 後開發）；"
            "請先從 tenant.toml 移除 type = \"m365\" 的連接器。"
        )


REGISTRY: Dict[str, type] = {
    c.type: c for c in (GitHubConnector, GDriveConnector, FileShareConnector, M365Connector)
}


def build_connectors(connector_settings: List[Dict[str, Any]]) -> List[Connector]:
    """依 tenant.toml 的 [[connectors]] 建立連接器；沒有設定時依 .env 決定（舊行為）。"""
    if not connector_settings:
        connector_settings = []
        if config.GITHUB_REPO:
            connector_settings.append({"type": "github"})
        if config.GDRIVE_ENABLED:
            connector_settings.append({"type": "gdrive"})
    out = []
    for settings in connector_settings:
        kind = settings.get("type")
        if kind not in REGISTRY:
            raise ConnectorError(f"不認得的連接器類型 `{kind}`，可用：{', '.join(sorted(REGISTRY))}。")
        out.append(REGISTRY[kind](settings))
    if not out:
        raise ConnectorError("沒有設定任何資料來源：請在 tenant.toml 加 [[connectors]]，或在 .env 設定 GITHUB_REPO／GDRIVE_FOLDER_IDS。")
    return out
