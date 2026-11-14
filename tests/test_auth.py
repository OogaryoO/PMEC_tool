from __future__ import annotations

import pytest

from src.auth import authorize

DOMAINS = ["belltech.com"]
ADMINS = ["Boss@BellTech.com"]


@pytest.mark.parametrize(
    "email,verified",
    [
        ("someone@gmail.com", True),  # 網域不在允許清單
        ("someone@belltech.com.evil.io", True),  # 網域前綴相同但不是公司網域
        ("someone@evil.belltech.com", True),  # 子網域不視為公司網域
        ("someone@belltech.com", False),  # email 未經驗證
        ("someone@belltech.com", None),
        ("@belltech.com", True),
        ("", True),
    ],
)
def test_rejects_non_company_or_unverified(email, verified):
    assert authorize(email, verified, None, DOMAINS, ADMINS) is None


def test_no_allowed_domains_configured_rejects_everyone():
    assert authorize("someone@belltech.com", True, None, [], ADMINS) is None


def test_company_account_admin_flag_case_insensitive():
    user = authorize("Someone@BellTech.com", True, "某人", DOMAINS, ADMINS)
    assert user is not None and user.email == "someone@belltech.com" and not user.is_admin
    assert authorize("boss@belltech.com", True, None, DOMAINS, ADMINS).is_admin
