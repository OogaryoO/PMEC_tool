"""
安裝精靈：回答幾個問題，產生這家公司的設定檔，不用自己改程式或讀設定說明。

    python -m src.setup_wizard                     # 互動問答
    python -m src.setup_wizard --answers a.json    # 用事先填好的答案（BellD 代填、測試用）
    python -m src.setup_wizard --root /path        # 寫到別的目錄（預設專案根目錄）

產生（已存在的檔案會先備份成 *.bak-<時間>）：
    tenant.toml                 資料來源、誰能看、排除規則、保存天數
    .env                        AI 模型、登入、資料庫密碼（權限 600；保留原本其他設定）
    .streamlit/secrets.toml     Google／Microsoft 登入設定（權限 600）
    deploy/deploy.env           雲端版／企業雲版才產生

答案的欄位（--answers 的 JSON 也用同樣名稱）見 QUESTIONS。
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from src.llm import PROVIDERS

PACKAGES = {
    "trial": "試用版：裝在公司一台常開的電腦或 NAS（Docker）",
    "cloud": "雲端版：Google Cloud Run＋免費雲端資料庫（Neon）",
    "enterprise": "企業雲版：Google Cloud Run＋Cloud SQL",
    "onprem": "地端版：公司內部主機＋地端 AI 模型（資料不出公司）",
}
AUTH_MODES = {
    "cloudflare": "Cloudflare Access：輸入 email 收驗證碼（員工不需要 Google／Microsoft 帳號）",
    "google": "Google 帳號登入",
    "microsoft": "Microsoft（Entra ID）帳號登入",
}

# (欄位, 問題, 是否機密, 預設值)；選單類欄位另外處理
QUESTIONS: List[Tuple[str, str, bool, str]] = [
    ("company", "公司名稱", False, ""),
    ("admin_emails", "管理員 email（可多個，逗號分隔）", False, ""),
    ("allowed_domains", "允許登入的公司網域（逗號分隔，例如 corp.com；沒有就留空）", False, ""),
    ("allowed_emails", "另外允許的個別 email（逗號分隔，例如外部顧問的 Gmail；沒有就留空）", False, ""),
    ("public_url", "員工打開的網址（例如 https://brain.corp.com；只在公司內網用就留空）", False, ""),
    ("github_repo", "GitHub repo（owner/repo；沒有就留空）", False, ""),
    ("github_token", "GitHub 唯讀 token", True, ""),
    ("gdrive_folder_ids", "Google Drive 資料夾 ID（逗號分隔；沒有就留空）", False, ""),
    ("fileshare_paths", "公司共用資料夾／NAS 掛載路徑（逗號分隔；沒有就留空）", False, ""),
    ("m365_drive_id", "Microsoft 365 文件庫的 drive ID（SharePoint／OneDrive；沒有就留空）", False, ""),
    ("exclude", "絕對不能讓 AI 看的路徑（逗號分隔，可用 *，例如 fileshare/nas/人資/*）", False, "*.env,*.key,*secret*,*credential*,*密碼*"),
    ("llm_model", "AI 模型名稱（例如 gpt-5.1、claude-opus-5-5、Llama-3.3-70B-Instruct）", False, ""),
    ("llm_api_key", "AI 服務的 API key（只存在這台機器的 .env）", True, ""),
    ("llm_base_url", "AI 端點網址（選填；custom、Azure、自架才需要）", False, ""),
]


def _csv(value: str) -> List[str]:
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def _choose(title: str, options: Dict[str, str], default: str) -> str:
    print(f"\n{title}")
    keys = list(options)
    for i, k in enumerate(keys, 1):
        print(f"  {i}. {options[k]}" + ("（預設）" if k == default else ""))
    while True:
        raw = input("請輸入編號：").strip()
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        print("請輸入清單中的編號。")


def ask_interactively() -> Dict[str, str]:
    a: Dict[str, str] = {}
    a["package"] = _choose("1) 安裝方式", PACKAGES, "trial")
    a["auth_mode"] = _choose("2) 員工怎麼登入", AUTH_MODES, "cloudflare")
    a["llm_provider"] = _choose(
        "3) 用哪一家 AI 模型", {k: p.label for k, p in PROVIDERS.items()},
        "ollama" if a["package"] == "onprem" else "openai",
    )
    print("\n4) 其他問題（直接按 Enter 用預設或留空）")
    for key, question, secret, default in QUESTIONS:
        if key == "github_token" and not a.get("github_repo"):
            continue
        if key == "llm_api_key" and not PROVIDERS[a["llm_provider"]].needs_key:
            continue
        prompt = f"{question}" + (f" [{default}]" if default else "") + "："
        value = getpass.getpass(prompt) if secret else input(prompt)
        a[key] = value.strip() or default
    if a["auth_mode"] in ("google", "microsoft"):
        print("\n5) 登入用的 OAuth 用戶端（Google Cloud Console 或 Microsoft Entra 建立）")
        a["oauth_client_id"] = input("client_id：").strip()
        a["oauth_client_secret"] = getpass.getpass("client_secret：").strip()
        if a["auth_mode"] == "microsoft":
            a["entra_tenant_id"] = input("Entra 租用戶 ID（tenant ID）：").strip()
    if a["auth_mode"] == "cloudflare":
        print("\n5) Cloudflare Access（Zero Trust → Access → Applications 裡看得到）")
        a["cf_team_domain"] = input("團隊網域（例如 corp.cloudflareaccess.com）：").strip()
        a["cf_aud"] = input("應用程式的 Application Audience (AUD) Tag：").strip()
        a["tunnel_token"] = getpass.getpass("Tunnel token（選填，之後也可以補在 .env）：").strip()
    if a.get("m365_drive_id"):
        print("\n6) Microsoft 365 應用程式（Graph 應用程式權限，建議 Sites.Selected）")
        a["m365_tenant_id"] = input("tenant ID：").strip()
        a["m365_client_id"] = input("client ID：").strip()
        a["m365_client_secret"] = getpass.getpass("client secret：").strip()
        a["m365_folder_path"] = input("只讀這個資料夾（例如 /共用文件/業務；整個文件庫就留空）：").strip()
    if a["package"] in ("cloud", "enterprise"):
        a["gcp_project"] = input("\nGoogle Cloud 專案 ID：").strip()
    return a


# ---------------------------------------------------------------------------
def validate(a: Dict[str, str]) -> List[str]:
    problems = []
    if a.get("package") not in PACKAGES:
        problems.append(f"package 只能是 {', '.join(PACKAGES)}。")
    if a.get("auth_mode") not in AUTH_MODES:
        problems.append(f"auth_mode 只能是 {', '.join(AUTH_MODES)}。")
    if a.get("llm_provider") not in PROVIDERS:
        problems.append(f"llm_provider 只能是 {', '.join(PROVIDERS)}。")
    if not a.get("company"):
        problems.append("請填公司名稱（company）。")
    if not _csv(a.get("admin_emails", "")):
        problems.append("至少要有一位管理員（admin_emails）。")
    if not _csv(a.get("allowed_domains", "")) and not _csv(a.get("allowed_emails", "")):
        problems.append("allowed_domains 和 allowed_emails 至少要填一個，否則沒有人能登入。")
    if not a.get("llm_model"):
        problems.append("請填 AI 模型名稱（llm_model）。")
    if a.get("llm_provider") in PROVIDERS and PROVIDERS[a["llm_provider"]].needs_key and not a.get("llm_api_key"):
        problems.append("這家 AI 服務需要 API key（llm_api_key）。")
    if a.get("llm_provider") == "custom" and not a.get("llm_base_url"):
        problems.append("custom 需要 llm_base_url。")
    if not any(a.get(k) for k in ("github_repo", "gdrive_folder_ids", "fileshare_paths", "m365_drive_id")):
        problems.append("至少要有一個資料來源（GitHub、Google Drive、共用資料夾、Microsoft 365）。")
    if a.get("auth_mode") == "cloudflare" and not (a.get("cf_team_domain") and a.get("cf_aud")):
        problems.append("Cloudflare 登入需要 cf_team_domain 與 cf_aud。")
    if a.get("auth_mode") in ("google", "microsoft") and not (a.get("oauth_client_id") and a.get("oauth_client_secret")):
        problems.append("Google／Microsoft 登入需要 oauth_client_id 與 oauth_client_secret。")
    if a.get("auth_mode") in ("google", "microsoft") and not a.get("public_url"):
        problems.append("Google／Microsoft 登入需要 public_url（OAuth 的 redirect URI 用）。")
    if a.get("auth_mode") == "microsoft" and a.get("entra_tenant_id", "").lower() in ("", "common", "organizations", "consumers"):
        problems.append("Microsoft 登入需要公司自己的 Entra 租用戶 ID（entra_tenant_id），不能用 common／organizations。")
    if a.get("m365_drive_id") and not all(a.get(k) for k in ("m365_tenant_id", "m365_client_id", "m365_client_secret")):
        problems.append("Microsoft 365 資料來源需要 m365_tenant_id、m365_client_id、m365_client_secret。")
    if a.get("package") in ("cloud", "enterprise") and not a.get("gcp_project"):
        problems.append("雲端版／企業雲版需要 gcp_project。")
    return problems


def _q(value: str) -> str:
    """TOML 基本字串（JSON 的跳脫規則相容）。"""
    return json.dumps(str(value), ensure_ascii=False)


def _qlist(values: List[str]) -> str:
    return "[" + ", ".join(_q(v) for v in values) + "]"


def render_tenant(a: Dict[str, str]) -> str:
    lines = [
        "# 由 python -m src.setup_wizard 產生。可以手動修改；改完執行 python -m src.worker check-tenant 檢查。",
        f"name = {_q(a['company'])}",
        "stage = 1   # 1 只讀｜2 擬稿｜3 改檔（逐次確認）｜4 寄信（逐次確認）",
        f"exclude = {_qlist(_csv(a.get('exclude', '')))}",
        "",
    ]
    if a.get("github_repo"):
        lines += ["[[connectors]]", 'type = "github"', f"repo = {_q(a['github_repo'])}", ""]
    if a.get("gdrive_folder_ids"):
        lines += ["[[connectors]]", 'type = "gdrive"', f"folder_ids = {_qlist(_csv(a['gdrive_folder_ids']))}", ""]
    for i, path in enumerate(_csv(a.get("fileshare_paths", ""))):
        lines += ["[[connectors]]", 'type = "fileshare"', f"name = {_q('share' if i == 0 else f'share{i + 1}')}",
                  f"root = {_q(path)}", ""]
    if a.get("m365_drive_id"):
        lines += ["[[connectors]]", 'type = "m365"', f"tenant_id = {_q(a['m365_tenant_id'])}",
                  f"client_id = {_q(a['m365_client_id'])}", "# client secret 在 .env 的 M365_CLIENT_SECRET",
                  f"drive_id = {_q(a['m365_drive_id'])}"]
        if a.get("m365_folder_path"):
            lines.append(f"folder_path = {_q(a['m365_folder_path'])}")
        lines.append("")
    default = [f"domain:{d.lower()}" for d in _csv(a.get("allowed_domains", ""))]
    default += [e.lower() for e in _csv(a.get("allowed_emails", ""))]
    lines += [
        "[permissions]",
        "# 沒有規則命中時誰能看：預設是所有能登入的人。要依部門區分，加 [[permissions.rules]]（見 tenant.example.toml）",
        f"default = {_qlist(default)}",
        "inherit_source_permissions = false",
        "reveal_denied_count_to_users = false",
        "",
        "[groups]",
        "",
        "[audit]",
        "retention_days = 180",
        "",
    ]
    return "\n".join(lines)


def env_updates(a: Dict[str, str], existing: Dict[str, str]) -> Dict[str, str]:
    u = {
        "LLM_PROVIDER": a["llm_provider"],
        "LLM_MODEL": a["llm_model"],
        "LLM_API_KEY": a.get("llm_api_key", ""),
        "LLM_BASE_URL": a.get("llm_base_url", "")
        or ("http://ollama:11434/v1" if a["llm_provider"] == "ollama" and a["package"] == "onprem" else ""),
        "ALLOWED_EMAIL_DOMAINS": ",".join(d.lower() for d in _csv(a.get("allowed_domains", ""))),
        "ALLOWED_EMAILS": ",".join(e.lower() for e in _csv(a.get("allowed_emails", ""))),
        "ADMIN_EMAILS": ",".join(e.lower() for e in _csv(a["admin_emails"])),
        "AUTH_MODE": a["auth_mode"],
        "AUTH_DISABLED": "false",
        "GITHUB_REPO": a.get("github_repo", ""),
    }
    if a.get("github_token"):
        u["GITHUB_TOKEN"] = a["github_token"]
    if a["auth_mode"] == "microsoft":
        u["ENTRA_TENANT_ID"] = a.get("entra_tenant_id", "")
    if a["auth_mode"] == "cloudflare":
        u["CF_ACCESS_TEAM_DOMAIN"] = a["cf_team_domain"]
        u["CF_ACCESS_AUD"] = a["cf_aud"]
    if a.get("tunnel_token"):
        u["TUNNEL_TOKEN"] = a["tunnel_token"]
    if a.get("m365_client_secret"):
        u["M365_CLIENT_SECRET"] = a["m365_client_secret"]
    if a["package"] in ("trial", "onprem"):
        u["SYNC_MODE"] = "queue"
        for k in ("DB_WORKER_PASSWORD", "DB_APP_PASSWORD"):
            u[k] = existing.get(k) or secrets.token_urlsafe(24)
    # 舊設定 RAP_* 會被 LLM_* 取代；留著不影響，但避免混淆，標記為空
    for k in ("RAP_API_KEY", "RAP_BASE_URL", "RAP_MODEL_NAME"):
        if k in existing:
            u[k] = ""
    return u


def read_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def merge_env(path: Path, updates: Dict[str, str]) -> str:
    """改掉已有的鍵、保留其他行與註解，新鍵加在最後。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen = set()
    out = []
    for line in lines:
        key = line.partition("=")[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    new = [k for k in updates if k not in seen]
    if new:
        out += ["", f"# --- 安裝精靈 {dt.date.today().isoformat()} ---"] + [f"{k}={updates[k]}" for k in new]
    return "\n".join(out) + "\n"


def render_streamlit_secrets(a: Dict[str, str]) -> str:
    if a["auth_mode"] == "cloudflare":
        return "# 登入由 Cloudflare Access 負責（AUTH_MODE=cloudflare），這裡不需要設定。\n"
    redirect = a["public_url"].rstrip("/") + "/oauth2callback"
    if a["auth_mode"] == "google":
        meta = "https://accounts.google.com/.well-known/openid-configuration"
    else:
        meta = f"https://login.microsoftonline.com/{a.get('entra_tenant_id') or 'organizations'}/v2.0/.well-known/openid-configuration"
    return (
        "# 由安裝精靈產生；OAuth 用戶端的 redirect URI 要設成下面的 redirect_uri。\n"
        "[auth]\n"
        f"redirect_uri = {_q(redirect)}\n"
        f"cookie_secret = {_q(secrets.token_urlsafe(48))}\n"
        f"client_id = {_q(a['oauth_client_id'])}\n"
        f"client_secret = {_q(a['oauth_client_secret'])}\n"
        f"server_metadata_url = {_q(meta)}\n"
    )


def render_deploy_env(a: Dict[str, str]) -> str:
    return "\n".join([
        "# 由安裝精靈產生（非機密）；密碼與 key 由 deploy/gcp_setup.sh 提示輸入，存進 Secret Manager。",
        f"GCP_PROJECT={a['gcp_project']}",
        "GCP_REGION=asia-east1",
        f"DB_MODE={'external' if a['package'] == 'cloud' else 'cloudsql'}",
        "SQL_INSTANCE=pmec-db",
        "SQL_TIER=db-g1-small",
        "DB_NAME=pmec",
        f"LLM_PROVIDER={a['llm_provider']}",
        f"LLM_MODEL={a['llm_model']}",
        f"LLM_BASE_URL={a.get('llm_base_url', '')}",
        f"GITHUB_REPO={a.get('github_repo', '')}",
        f"GDRIVE_FOLDER_IDS={a.get('gdrive_folder_ids', '')}",
        "GDRIVE_MAX_FILES=100",
        f"ALLOWED_EMAIL_DOMAINS={','.join(_csv(a.get('allowed_domains', '')))}",
        f"ALLOWED_EMAILS={','.join(_csv(a.get('allowed_emails', '')))}",
        f"ADMIN_EMAILS={','.join(_csv(a['admin_emails']))}",
        'SYNC_SCHEDULE="0 3 * * *"',
        "",
    ])


def _write(path: Path, content: str, private: bool, written: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak-" + dt.datetime.now().strftime("%Y%m%d%H%M%S"))
        backup.write_bytes(path.read_bytes())
        if private:
            os.chmod(backup, 0o600)
    path.write_text(content, encoding="utf-8")
    if private:
        os.chmod(path, 0o600)
    written.append(str(path))


def generate(a: Dict[str, str], root: Path) -> List[str]:
    problems = validate(a)
    if problems:
        raise ValueError("答案不完整：\n- " + "\n- ".join(problems))
    written: List[str] = []
    _write(root / "tenant.toml", render_tenant(a), False, written)
    env_path = root / ".env"
    _write(env_path, merge_env(env_path, env_updates(a, read_env(env_path))), True, written)
    _write(root / ".streamlit" / "secrets.toml", render_streamlit_secrets(a), True, written)
    (root / "secrets").mkdir(exist_ok=True)
    if a["package"] in ("cloud", "enterprise"):
        _write(root / "deploy" / "deploy.env", render_deploy_env(a), False, written)
    return written


NEXT_STEPS = {
    "trial": [
        "docker compose up -d --build          # 沒有 Docker 的 Linux／WSL：bash scripts/local_stack.sh",
        "docker compose --profile tunnel up -d  # 有 TUNNEL_TOKEN 時：接上 Cloudflare Tunnel（服務填 http://web:8080）",
        "打開網址，管理員按「同步並更新知識庫」，再依導入手冊第 6 節驗收",
    ],
    "onprem": [
        "docker compose --profile ollama up -d --build",
        f"docker compose exec ollama ollama pull <模型名稱>",
        "打開 http://127.0.0.1:8501，管理員按同步，再依導入手冊第 6 節驗收",
    ],
    "cloud": [
        "bash deploy/gcp_setup.sh   # 會請你貼上 Neon 連線字串與 API key",
        "bash deploy/deploy.sh",
        "Google Cloud 設預算警示；依導入手冊第 6 節驗收",
    ],
    "enterprise": ["bash deploy/gcp_setup.sh", "bash deploy/deploy.sh", "依導入手冊第 6 節驗收"],
}


def main(argv: Optional[List[str]] = None) -> int:
    from src import config

    parser = argparse.ArgumentParser(prog="python -m src.setup_wizard")
    parser.add_argument("--answers", help="事先填好的答案 JSON 檔")
    parser.add_argument("--root", default=str(config.PROJECT_ROOT), help="設定檔寫到哪個目錄")
    args = parser.parse_args(argv)

    if args.answers:
        answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    else:
        print("公司大腦 安裝精靈：回答下面的問題，產生這家公司的設定檔。")
        answers = ask_interactively()
    answers = {k: (str(v) if not isinstance(v, list) else ",".join(map(str, v))) for k, v in answers.items()}
    try:
        written = generate(answers, Path(args.root))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("\n已寫入：\n  " + "\n  ".join(written))
    print("\n下一步：\n  " + "\n  ".join(NEXT_STEPS[answers["package"]]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
