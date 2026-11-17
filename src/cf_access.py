"""
信任 Cloudflare Access 的登入身分（AUTH_MODE=cloudflare）。

Cloudflare Access 在員工通過 email 驗證碼後，每個請求都帶一個簽章過的 JWT
（標頭 Cf-Access-Jwt-Assertion）。這裡驗證：
  - 簽章：用團隊的公開金鑰 https://<團隊網域>/cdn-cgi/access/certs
  - aud：等於這個 Access 應用程式的 AUD tag（CF_ACCESS_AUD）
  - iss：https://<團隊網域>（CF_ACCESS_TEAM_DOMAIN）
  - 有效期限
驗證通過才取出 email。只看標頭、不驗簽章的話，任何人都能偽造身分，所以一定要驗。
"""
from __future__ import annotations

from typing import Callable, Optional

import jwt

HEADER = "Cf-Access-Jwt-Assertion"

_jwk_clients: dict = {}


class AccessTokenError(Exception):
    """JWT 不存在或驗證失敗；訊息可給管理員看。"""


def _default_key_resolver(team_domain: str) -> Callable[[str], object]:
    if team_domain not in _jwk_clients:
        _jwk_clients[team_domain] = jwt.PyJWKClient(
            f"https://{team_domain}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600
        )
    client = _jwk_clients[team_domain]
    return lambda token: client.get_signing_key_from_jwt(token).key


def verified_email(
    token: Optional[str],
    team_domain: str,
    aud: str,
    key_resolver: Optional[Callable[[str], object]] = None,
) -> str:
    if not team_domain or not aud:
        raise AccessTokenError("AUTH_MODE=cloudflare 需要設定 CF_ACCESS_TEAM_DOMAIN 與 CF_ACCESS_AUD。")
    if not token:
        raise AccessTokenError("請求沒有 Cloudflare Access 的登入憑證：請從公司網址進入，不要直接連主機。")
    team_domain = team_domain.removeprefix("https://").rstrip("/")
    try:
        key = (key_resolver or _default_key_resolver(team_domain))(token)
        claims = jwt.decode(
            token, key, algorithms=["RS256"], audience=aud, issuer=f"https://{team_domain}",
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise AccessTokenError(f"Cloudflare Access 登入憑證驗證失敗：{exc}") from exc
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise AccessTokenError("Cloudflare Access 登入憑證裡沒有 email（服務權杖不能用來登入網頁）。")
    return email
