from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing
from django.core.exceptions import ImproperlyConfigured

SIGNING_NAMESPACE = "kururu-forms-render-v1"


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


# instance は「ページ内で何個目のフォーム配置か」。
# 入力エラーを出し直す先を特定するために持つ。同じフォームを2つ置いたとき、
# 送ったのは2つ目なのに1つ目へ入力値が戻ると、
# 利用者はどちらを直せばよいのか分からなくなる。
#
# 上限は記事のブロック数（CMS 側の上限は 300）より十分大きく取る。
# 範囲外は 0＝「配置を特定できない」にする。1 に丸めると、
# 特定できなかった入力値が無関係な1つ目のフォームに出てしまう。
MAX_INSTANCE = 1000
UNKNOWN_INSTANCE = 0


def safe_instance(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return UNKNOWN_INSTANCE
    return number if 1 <= number <= MAX_INSTANCE else UNKNOWN_INSTANCE


def make_render_token(form_id: int, return_path: str, instance: int = 1) -> str:
    return signing.dumps(
        {
            "form_id": form_id,
            "instance": safe_instance(instance),
            "idempotency_key": str(uuid.uuid4()),
            "return_path": safe_return_path(return_path),
            "shown_at": int(time.time()),
        },
        salt=SIGNING_NAMESPACE,
        compress=True,
    )


def load_render_token(token: str, form_id: int, minimum_fill_seconds: int):
    data = signing.loads(token, salt=SIGNING_NAMESPACE, max_age=86_400)
    if data.get("form_id") != form_id:
        raise signing.BadSignature("form mismatch")
    if int(time.time()) - int(data.get("shown_at", 0)) < minimum_fill_seconds:
        raise signing.BadSignature("submitted too quickly")
    try:
        data["idempotency_key"] = str(uuid.UUID(str(data.get("idempotency_key", ""))))
    except (TypeError, ValueError, AttributeError):
        raise signing.BadSignature("invalid idempotency key")
    data["return_path"] = safe_return_path(data.get("return_path", "/"))
    # instance を持たない古いトークンでも動くようにする。
    data["instance"] = safe_instance(data.get("instance", 1))
    return data
