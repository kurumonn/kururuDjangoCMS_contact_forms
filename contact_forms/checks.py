import os

from django.conf import settings
from django.core.checks import Error, register
from django.db import connection
from django.db.utils import OperationalError, ProgrammingError


@register()
def check_security_settings(app_configs, **kwargs):
    errors = []
    key = getattr(settings, "KURURU_FORMS_IP_HASH_KEY", "") or os.environ.get(
        "KURURU_FORMS_IP_HASH_KEY", ""
    )
    if not isinstance(key, str) or len(key) < 32:
        errors.append(
            Error(
                "KURURU_FORMS_IP_HASH_KEYが未設定または短すぎます。",
                hint="問い合わせIPのHMAC専用に32文字以上のランダム値を設定してください。",
                id="contact_forms.E001",
            )
        )
    try:
        from .models import ContactForm

        table_name = ContactForm._meta.db_table
        if (
            table_name in connection.introspection.table_names()
            and ContactForm.objects.filter(
                is_active=True,
                fields__isnull=True,
            ).exists()
        ):
            errors.append(
                Error(
                    "入力項目のない有効な問い合わせフォームがあります。",
                    hint="フォームを無効化するか、1項目以上追加してください。",
                    id="contact_forms.E002",
                )
            )
    except (OperationalError, ProgrammingError):
        pass
    return errors
