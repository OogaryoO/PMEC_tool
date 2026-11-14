from __future__ import annotations

import pytest

from src.actions import ActionNotAllowed, check_action
from src.tenant import TenantConfig, TenantConfigError, load_tenant, parse_tenant

EXAMPLE = {
    "name": "示範公司",
    "stage": 2,
    "exclude": ["fileshare/nas/人資/薪資/*", "*.key"],
    "groups": {"業務部": ["Amy@corp.com", "bob@corp.com"], "人資": ["hr@corp.com"]},
    "permissions": {
        "default": ["domain:CORP.com"],
        "inherit_source_permissions": True,
        "rules": [
            {"match": "fileshare/nas/人資/*", "allow": ["group:人資"]},
            {"match": "fileshare/nas/業務/*", "allow": ["group:業務部", "boss@corp.com"]},
        ],
    },
    "audit": {"retention_days": 90},
    "connectors": [{"type": "fileshare", "name": "nas", "root": "/mnt/nas"}],
}


def test_defaults_keep_old_behaviour(tmp_path):
    tenant = load_tenant(tmp_path / "missing.toml")
    assert tenant.stage == 1
    assert tenant.resolve_acl("gdrive/任何檔案") == ["*"]
    assert "*" in tenant.principals_for("someone@corp.com")
    assert tenant.connectors == []


def test_principals_include_email_domain_and_groups():
    tenant = parse_tenant(EXAMPLE)
    assert set(tenant.principals_for("AMY@corp.com")) == {"*", "amy@corp.com", "domain:corp.com", "group:業務部"}
    assert "group:人資" not in tenant.principals_for("amy@corp.com")


def test_resolve_acl_order_source_then_rule_then_default():
    tenant = parse_tenant(EXAMPLE)
    assert tenant.resolve_acl("gdrive/x.docx", ["a@corp.com"]) == ["a@corp.com"]
    assert tenant.resolve_acl("fileshare/nas/人資/規章.docx") == ["group:人資"]
    assert tenant.resolve_acl("fileshare/nas/業務/報價.xlsx") == ["group:業務部", "boss@corp.com"]
    assert tenant.resolve_acl("fileshare/nas/公告/週會.md") == ["domain:corp.com"]


def test_source_permissions_ignored_when_inherit_off():
    tenant = parse_tenant({**EXAMPLE, "permissions": {**EXAMPLE["permissions"], "inherit_source_permissions": False}})
    assert tenant.resolve_acl("gdrive/x.docx", ["a@corp.com"]) == ["domain:corp.com"]


def test_exclude_patterns():
    tenant = parse_tenant(EXAMPLE)
    assert tenant.is_excluded("fileshare/nas/人資/薪資/2026.xlsx")
    assert tenant.is_excluded("gdrive/秘密/server.key")
    assert not tenant.is_excluded("fileshare/nas/人資/規章.docx")


@pytest.mark.parametrize(
    "bad",
    [
        {"stage": 5},
        {"permissions": {"rules": [{"match": "x"}]}},
        {"connectors": [{"name": "沒有 type"}]},
    ],
)
def test_invalid_config_rejected(bad):
    with pytest.raises(TenantConfigError):
        parse_tenant(bad)


def test_example_file_parses():
    from pathlib import Path

    tenant = load_tenant(Path(__file__).resolve().parent.parent / "tenant.example.toml")
    assert tenant.stage == 1 and tenant.connectors


def test_action_stage_gate():
    check_action(TenantConfig(stage=1), "answer")
    with pytest.raises(ActionNotAllowed, match="第 2 階段"):
        check_action(TenantConfig(stage=1), "draft")
    check_action(TenantConfig(stage=2), "draft")
    with pytest.raises(ActionNotAllowed, match="確認"):
        check_action(TenantConfig(stage=4), "send_email")
    check_action(TenantConfig(stage=4), "send_email", confirmed=True)
    with pytest.raises(ActionNotAllowed):
        check_action(TenantConfig(stage=3), "send_email", confirmed=True)


def test_no_source_answer_detection():
    from src.rag_engine import is_no_source_answer

    assert is_no_source_answer("在你有權限查看的資料中找不到相關紀錄。 [1] [2]")
    assert is_no_source_answer("沒有引用的回答")
    assert not is_no_source_answer("單價 12,000 元 [1]")
