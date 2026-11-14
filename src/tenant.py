"""
每家導入企業的設定（tenant.toml）：用哪些連接器、誰能看哪些資料、哪些資料不收、助理開放到哪個階段。

設計目標（Kai 11/07「模組化、少客製化」）：換一家公司只換這個檔案，不改程式。
檔案位置：環境變數 TENANT_CONFIG，未設定時讀專案根目錄的 tenant.toml；兩者都沒有時用預設值，
行為與加入本模組之前相同（所有登入者看得到所有資料、只讀、連接器依 .env 的 GitHub／Drive 設定）。

權限用「主體」(principal) 字串表示：
    "*"                 所有登入者
    "user@corp.com"     單一使用者
    "domain:corp.com"   某網域的所有人
    "group:業務部"      tenant.toml [groups] 定義的群組（或 Google 群組 email）
一份資料的 acl 是主體清單；使用者的主體與 acl 有交集才看得到。

待訪談決定的設定都在 docs/INTERVIEW_TO_CONFIG.md 列出對應題號。
"""
from __future__ import annotations

import fnmatch
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src import config

EVERYONE = "*"
STAGE_NAMES = {1: "只讀", 2: "擬稿", 3: "改檔（逐次確認）", 4: "寄信（逐次確認）"}


class TenantConfigError(Exception):
    """tenant.toml 內容不合法時拋出，訊息需可直接呈現給管理員。"""


@dataclass(frozen=True)
class AclRule:
    match: str  # 對 source_path 做 glob 比對，例如 "fileshare/nas/人資/*"
    allow: List[str]


@dataclass
class TenantConfig:
    name: str = "default"
    stage: int = 1
    # 沒有任何規則命中、也沒有沿用來源權限時的預設 acl
    default_acl: List[str] = field(default_factory=lambda: [EVERYONE])
    # true：Google Drive 等來源有權限資訊時，直接沿用（飛書／Grok 的做法）；false：只看 acl_rules
    inherit_source_permissions: bool = False
    acl_rules: List[AclRule] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)  # 命中的資料一律不收（同步時就丟掉）
    groups: Dict[str, List[str]] = field(default_factory=dict)
    audit_retention_days: int = 180  # 飛書 aily 審計日誌預設 180 天
    # 員工能不能知道「有 N 筆相關資料你沒有權限看」；預設只記在操作紀錄給管理員
    reveal_denied_count_to_users: bool = False
    connectors: List[Dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    def principals_for(self, email: str) -> List[str]:
        """使用者擁有的所有主體。"""
        email = (email or "").strip().lower()
        out = [EVERYONE]
        if not email:
            return out
        out.append(email)
        _, _, domain = email.rpartition("@")
        if domain:
            out.append(f"domain:{domain}")
        for group, members in self.groups.items():
            if email in members:
                out.append(f"group:{group}")
        return out

    def is_excluded(self, source_path: str) -> bool:
        return any(fnmatch.fnmatchcase(source_path, pattern) for pattern in self.exclude)

    def resolve_acl(self, source_path: str, source_acl: Optional[Sequence[str]] = None) -> List[str]:
        """決定一份資料的 acl：沿用來源權限（有開啟且有資料時）＞第一條命中的規則＞預設值。"""
        if self.inherit_source_permissions and source_acl:
            return sorted(set(source_acl))
        for rule in self.acl_rules:
            if fnmatch.fnmatchcase(source_path, rule.match):
                return list(rule.allow)
        return list(self.default_acl)


def _normalize_principal(p: str) -> str:
    p = str(p).strip()
    if p == EVERYONE or p.startswith(("group:", "domain:")):
        return p if not p.startswith("domain:") else p.lower()
    return p.lower()


def parse_tenant(data: Dict[str, Any]) -> TenantConfig:
    """把 tenant.toml 的內容轉成 TenantConfig，並檢查欄位。"""
    try:
        stage = int(data.get("stage", 1))
    except (TypeError, ValueError) as exc:
        raise TenantConfigError("stage 必須是 1～4 的整數。") from exc
    if stage not in STAGE_NAMES:
        raise TenantConfigError(f"stage={stage} 不合法，只能是 1～4（{STAGE_NAMES}）。")

    perms = data.get("permissions", {}) or {}
    rules = []
    for i, raw in enumerate(perms.get("rules", []) or []):
        if not isinstance(raw, dict) or "match" not in raw or "allow" not in raw:
            raise TenantConfigError(f"permissions.rules 第 {i + 1} 條需要 match 與 allow 兩個欄位。")
        rules.append(AclRule(match=str(raw["match"]), allow=[_normalize_principal(p) for p in raw["allow"]]))

    groups = {
        str(name): [str(m).strip().lower() for m in members]
        for name, members in (data.get("groups", {}) or {}).items()
    }
    connectors = data.get("connectors", []) or []
    for i, c in enumerate(connectors):
        if not isinstance(c, dict) or "type" not in c:
            raise TenantConfigError(f"[[connectors]] 第 {i + 1} 個缺少 type。")

    audit = data.get("audit", {}) or {}
    return TenantConfig(
        name=str(data.get("name", "default")),
        stage=stage,
        default_acl=[_normalize_principal(p) for p in perms.get("default", [EVERYONE])],
        inherit_source_permissions=bool(perms.get("inherit_source_permissions", False)),
        acl_rules=rules,
        exclude=[str(p) for p in data.get("exclude", []) or []],
        groups=groups,
        audit_retention_days=int(audit.get("retention_days", 180)),
        reveal_denied_count_to_users=bool(perms.get("reveal_denied_count_to_users", False)),
        connectors=connectors,
    )


def tenant_path() -> Path:
    return Path(os.getenv("TENANT_CONFIG") or (config.PROJECT_ROOT / "tenant.toml"))


def load_tenant(path: Optional[Path] = None) -> TenantConfig:
    """讀取 tenant.toml；檔案不存在時回傳預設值（保持舊行為）。"""
    path = path or tenant_path()
    if not path.exists():
        return TenantConfig()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise TenantConfigError(f"{path} 不是合法的 TOML：{exc}") from exc
    return parse_tenant(data)


_cached: Optional[TenantConfig] = None


def get_tenant() -> TenantConfig:
    """全程序共用的設定（讀一次）。"""
    global _cached
    if _cached is None:
        _cached = load_tenant()
    return _cached
