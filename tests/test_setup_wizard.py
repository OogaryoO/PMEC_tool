"""G5：安裝精靈產生的設定檔，程式讀得進去，而且不弄丟原本的設定。"""
from __future__ import annotations

import os
import stat
import tomllib

import pytest

from src import setup_wizard as w
from src.llm import load_settings, missing_settings
from src.tenant import parse_tenant

BASE = {
    "package": "trial",
    "auth_mode": "cloudflare",
    "llm_provider": "rap",
    "llm_model": "Llama-3.3-70B-Instruct",
    "llm_api_key": "rap-key",
    "company": "BellD",
    "admin_emails": "gary@bell-tech.com",
    "allowed_domains": "bell-tech.com",
    "allowed_emails": "Jinny@Gmail.com",
    "github_repo": "BellTech-organization/BellD-Web-V2",
    "github_token": "gh-token",
    "gdrive_folder_ids": "abc,def",
    "fileshare_paths": "/mnt/nas/文件",
    "exclude": "*.env,*.key,fileshare/share/人資/*",
    "cf_team_domain": "bell.cloudflareaccess.com",
    "cf_aud": "aud123",
    "tunnel_token": "tt",
}


def test_generate_trial(tmp_path):
    (tmp_path / ".env").write_text("# 我的註解\nGITHUB_TOKEN=old\nCUSTOM_KEEP=1\nRAP_API_KEY=legacy\n", encoding="utf-8")
    w.generate(dict(BASE), tmp_path)

    tenant = parse_tenant(tomllib.loads((tmp_path / "tenant.toml").read_text(encoding="utf-8")))
    assert tenant.name == "BellD" and tenant.stage == 1
    assert [c["type"] for c in tenant.connectors] == ["github", "gdrive", "fileshare"]
    assert tenant.connectors[1]["folder_ids"] == ["abc", "def"]
    assert set(tenant.default_acl) == {"domain:bell-tech.com", "jinny@gmail.com"}
    assert "fileshare/share/人資/*" in tenant.exclude

    env = w.read_env(tmp_path / ".env")
    assert env["CUSTOM_KEEP"] == "1" and env["GITHUB_TOKEN"] == "gh-token"
    assert env["RAP_API_KEY"] == ""  # 舊設定清空，改用 LLM_*
    assert env["AUTH_MODE"] == "cloudflare" and env["AUTH_DISABLED"] == "false"
    assert env["SYNC_MODE"] == "queue" and len(env["DB_APP_PASSWORD"]) > 20
    assert "# 我的註解" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert missing_settings(load_settings(env)) == []
    assert stat.S_IMODE(os.stat(tmp_path / ".env").st_mode) == 0o600
    assert any(".env.bak-" in p for p in os.listdir(tmp_path))
    assert not (tmp_path / "deploy" / "deploy.env").exists()

    # 再跑一次：資料庫密碼不變（否則已建立的資料庫會連不上）
    pw = env["DB_APP_PASSWORD"]
    w.generate(dict(BASE), tmp_path)
    assert w.read_env(tmp_path / ".env")["DB_APP_PASSWORD"] == pw


def test_generate_cloud_google(tmp_path):
    a = dict(BASE, package="cloud", auth_mode="google", llm_provider="anthropic", llm_model="claude-opus-5-5",
             oauth_client_id="cid", oauth_client_secret="cs", public_url="https://brain.corp.com/", gcp_project="p1")
    w.generate(a, tmp_path)
    secrets_toml = tomllib.loads((tmp_path / ".streamlit" / "secrets.toml").read_text(encoding="utf-8"))
    assert secrets_toml["auth"]["redirect_uri"] == "https://brain.corp.com/oauth2callback"
    assert len(secrets_toml["auth"]["cookie_secret"]) > 40
    deploy = w.read_env(tmp_path / "deploy" / "deploy.env")
    assert deploy["DB_MODE"] == "external" and deploy["LLM_PROVIDER"] == "anthropic"


def test_generate_onprem_ollama_and_m365(tmp_path):
    a = dict(BASE, package="onprem", llm_provider="ollama", llm_api_key="", llm_model="llama3.3",
             m365_drive_id="b!x", m365_tenant_id="t", m365_client_id="c", m365_client_secret="s",
             m365_folder_path="/共用/業務")
    w.generate(a, tmp_path)
    env = w.read_env(tmp_path / ".env")
    assert env["LLM_BASE_URL"] == "http://ollama:11434/v1" and env["M365_CLIENT_SECRET"] == "s"
    tenant = tomllib.loads((tmp_path / "tenant.toml").read_text(encoding="utf-8"))
    m365 = [c for c in tenant["connectors"] if c["type"] == "m365"][0]
    assert m365["folder_path"] == "/共用/業務" and "client_secret" not in m365


@pytest.mark.parametrize(
    "change,needle",
    [
        ({"allowed_domains": "", "allowed_emails": ""}, "沒有人能登入"),
        ({"llm_api_key": ""}, "API key"),
        ({"cf_aud": ""}, "cf_aud"),
        ({"github_repo": "", "gdrive_folder_ids": "", "fileshare_paths": ""}, "資料來源"),
        ({"auth_mode": "microsoft", "oauth_client_id": "c", "oauth_client_secret": "s",
          "public_url": "https://x", "entra_tenant_id": "organizations"}, "租用戶"),
    ],
)
def test_validation(tmp_path, change, needle):
    with pytest.raises(ValueError, match=needle):
        w.generate(dict(BASE, **change), tmp_path)


def test_answers_file_cli(tmp_path):
    import json

    f = tmp_path / "a.json"
    f.write_text(json.dumps(BASE, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    assert w.main(["--answers", str(f), "--root", str(out)]) == 0
    assert (out / "tenant.toml").exists()
