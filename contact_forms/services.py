from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing
from django.core.exceptions import ImproperlyConfigured

TOKEN_SALT = "kururu-forms-render-v1"


def ip_hash(ip: str) -> str:
    key = getattr(settings, "KURURU_FORMS_IP_HASH_KEY", "") or os.environ.get(
        "KURURU_FORMS_IP_HASH_KEY", ""
    )
    if not isinstance(key, str) or len(key) < 32:
        raise ImproperlyConfigured("KURURU_FORMS_IP_HASH_KEYは32文字以上で設定してください。")
    return hmac.new(key.encode(), ip.encode(), hashlib.sha256).hexdigest()


def safe_return_path(value: str) -> str:
    parsed = urlsplit(value or "")
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return "/"
    result = parsed.path
    if parsed.query:
        result += "?" + parsed.query
    return result[:500]


def make_render_token(form_id: int, return_path: str) -> str:
    return signing.dumps(
        {
            "form_id": form_id,
            "return_path": safe_return_path(return_path),
            "shown_at": int(time.time()),
        },
        salt=TOKEN_SALT,
        compress=True,
    )


def load_render_token(token: str, form_id: int, minimum_fill_seconds: int):
    data = signing.loads(token, salt=TOKEN_SALT, max_age=86_400)
    if data.get("form_id") != form_id:
        raise signing.BadSignature("form mismatch")
    if int(time.time()) - int(data.get("shown_at", 0)) < minimum_fill_seconds:
        raise signing.BadSignature("submitted too quickly")
    data["return_path"] = safe_return_path(data.get("return_path", "/"))
    return data
