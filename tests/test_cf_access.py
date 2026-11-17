"""G4：Cloudflare Access 身分一定要驗簽章、aud、iss、期限。"""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from src.auth import authorize
from src.cf_access import AccessTokenError, verified_email

TEAM = "corp.cloudflareaccess.com"
AUD = "aud-tag-123"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key=KEY, **over):
    now = int(time.time())
    claims = {"aud": [AUD], "iss": f"https://{TEAM}", "email": "Kai@Bell-Tech.com", "iat": now, "exp": now + 600}
    claims.update(over)
    return jwt.encode(claims, key, algorithm="RS256")


def resolver(token):
    return KEY.public_key()


def test_valid_token_gives_lowercase_email():
    assert verified_email(_token(), TEAM, AUD, resolver) == "kai@bell-tech.com"
    assert verified_email(_token(), "https://" + TEAM + "/", AUD, resolver) == "kai@bell-tech.com"


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "not-a-jwt",
        _token(key=OTHER),  # 偽造簽章
        _token(aud=["someone-else"]),
        _token(iss="https://evil.cloudflareaccess.com"),
        _token(exp=int(time.time()) - 10),
        _token(email=""),  # 服務權杖沒有 email
    ],
)
def test_rejects_bad_tokens(token):
    with pytest.raises(AccessTokenError):
        verified_email(token, TEAM, AUD, resolver)


def test_missing_config():
    with pytest.raises(AccessTokenError, match="CF_ACCESS"):
        verified_email(_token(), "", AUD, resolver)


def test_allowed_emails_without_domain():
    p = authorize("someone@gmail.com", True, "S", allowed_domains=[], admin_emails=[], allowed_emails=["someone@gmail.com"])
    assert p is not None and p.email == "someone@gmail.com"
    assert authorize("other@gmail.com", True, "O", [], [], ["someone@gmail.com"]) is None
    assert authorize("a@corp.com", True, "A", ["corp.com"], ["a@corp.com"], ["x@gmail.com"]).is_admin
