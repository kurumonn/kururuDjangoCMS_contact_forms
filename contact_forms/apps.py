from django.apps import AppConfig
from django.db.models.signals import post_migrate


class ContactFormsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "contact_forms"
    verbose_name = "問い合わせフォーム"

    def ready(self):
        from . import checks  # noqa: F401
        from .plugin import definition

        from cms_plugins.registry import register_plugin

        register_plugin(definition)

        def ensure_settings(**kwargs):
            from .models import ContactPluginSetting

            ContactPluginSetting.objects.get_or_create(pk=1)

        post_migrate.connect(
            ensure_settings,
            dispatch_uid="contact_forms.ensure_settings",
            weak=False,
        )
