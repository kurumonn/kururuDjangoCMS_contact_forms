import os

from django.conf import settings
from django.core.checks import Error, register


@register()
def check_security_settings(app_configs, **kwargs):
    key = getattr(settings, "KURURU_FORMS_IP_HASH_KEY", "") or os.environ.get(
        "KURURU_FORMS_IP_HASH_KEY", ""
    )
    if not isinstance(key, str) or len(key) < 32:
        return [
            Error(
                "KURURU_FORMS_IP_HASH_KEYが未設定または短すぎます。",
                hint="問い合わせIPのHMAC専用に32文字以上のランダム値を設定してください。",
                id="contact_forms.E001",
            )
        ]
    return []
