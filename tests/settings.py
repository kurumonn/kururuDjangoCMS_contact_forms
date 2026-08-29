from config.settings.test import *  # noqa: F401,F403

if "contact_forms.apps.ContactFormsConfig" not in INSTALLED_APPS:
    INSTALLED_APPS = [*INSTALLED_APPS, "contact_forms.apps.ContactFormsConfig"]

ROOT_URLCONF = "tests.urls"
KURURU_FORMS_IP_HASH_KEY = "test-only-32-byte-minimum-hmac-key-123456789"
DEFAULT_FROM_EMAIL = "forms@example.test"
