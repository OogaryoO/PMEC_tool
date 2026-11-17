"""
登入後的存取判斷：只允許公司網域（或個別列出的 email）、且 email 已由身分提供者驗證的帳號；
管理員名單（ADMIN_EMAILS）可使用同步等管理功能。

P1 只有「能否進入」與「是否為管理員」兩種區分；分級存取在後續階段加入。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class Principal:
    email: str
    name: str
    is_admin: bool


def authorize(
    email: Optional[str],
    email_verified: Optional[bool],
    name: Optional[str],
    allowed_domains: Iterable[str],
    admin_emails: Iterable[str],
    allowed_emails: Iterable[str] = (),
) -> Optional[Principal]:
    """回傳可存取的使用者；不符合條件（未驗證、不在允許的網域或 email 清單、兩者都沒設定）回傳 None。"""
    domains = {d.lower() for d in allowed_domains}
    emails = {e.strip().lower() for e in allowed_emails}
    if not email or email_verified is not True or not (domains or emails):
        return None
    email = email.strip().lower()
    local, sep, domain = email.rpartition("@")
    if not sep or not local or (domain not in domains and email not in emails):
        return None
    return Principal(email=email, name=name or email, is_admin=email in {e.lower() for e in admin_emails})
