from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.forms.models import BaseInlineFormSet
from django.utils.text import slugify

from .models import (
    ContactField,
    ContactForm,
    ContactMaintenanceRun,
    ContactPluginSetting,
    ContactSubmission,
    MailDelivery,
)

PRESETS = {
    "standard": [
        ("name", "お名前", ContactField.Kind.TEXT, True),
        ("email", "メールアドレス", ContactField.Kind.EMAIL, True),
        ("subject", "件名", ContactField.Kind.TEXT, True),
        ("message", "お問い合わせ内容", ContactField.Kind.TEXTAREA, True),
        ("privacy", "個人情報の取扱いに同意する", ContactField.Kind.CONSENT, True),
    ],
    "brochure": [
        ("name", "お名前", ContactField.Kind.TEXT, True),
        ("email", "メールアドレス", ContactField.Kind.EMAIL, True),
        ("company", "会社名", ContactField.Kind.TEXT, False),
        ("document", "希望資料", ContactField.Kind.SELECT, True),
        ("privacy", "個人情報の取扱いに同意する", ContactField.Kind.CONSENT, True),
    ],
    "recruit": [
        ("name", "お名前", ContactField.Kind.TEXT, True),
        ("email", "メールアドレス", ContactField.Kind.EMAIL, True),
        ("position", "希望職種", ContactField.Kind.TEXT, True),
        ("message", "自己紹介", ContactField.Kind.TEXTAREA, True),
        ("privacy", "個人情報の取扱いに同意する", ContactField.Kind.CONSENT, True),
    ],
    "empty": [],
}


class ContactFormAdminForm(forms.ModelForm):
    preset = forms.ChoiceField(
        label="プリセット",
        required=False,
        choices=[
            ("standard", "標準お問い合わせ"),
            ("brochure", "資料請求"),
            ("recruit", "採用応募"),
            ("empty", "空のフォーム"),
        ],
        initial="standard",
        help_text="新規作成時だけ使用します。",
    )

    class Meta:
        model = ContactForm
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and self.instance.pk is None:
            self.fields["retention_days"].initial = (
                ContactPluginSetting.load().default_retention_days
            )

    def clean(self):
        cleaned_data = super().clean()
        self.instance._kururu_selected_preset = cleaned_data.get("preset") or "standard"
        return cleaned_data


class ContactFieldInlineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors) or not self.instance.is_active:
            return
        remaining = sum(
            1
            for form in self.forms
            if form.cleaned_data and not form.cleaned_data.get("DELETE", False)
        )
        preset = getattr(self.instance, "_kururu_selected_preset", "")
        preset_will_populate = self.instance.pk is None and bool(PRESETS.get(preset))
        if remaining == 0 and not preset_will_populate:
            raise forms.ValidationError("有効化するフォームには1項目以上が必要です。")


class ContactFieldInline(admin.TabularInline):
    model = ContactField
    formset = ContactFieldInlineFormSet
    extra = 0
    ordering = ("order",)


@admin.register(ContactForm)
class ContactFormAdmin(admin.ModelAdmin):
    form = ContactFormAdminForm
    inlines = (ContactFieldInline,)
    list_display = ("name", "slug", "is_active", "is_archived", "updated_at")
    list_filter = ("is_active", "is_archived")
    prepopulated_fields = {"slug": ("name",)}
    actions = ("duplicate_forms", "archive_forms")

    def has_delete_permission(self, request, obj=None):
        return False

    def has_duplicate_permission(self, request, obj=None):
        return self.has_view_permission(request, obj) and self.has_add_permission(request)

    def save_model(self, request, obj, form, change):
        obj._kururu_created_now = not change
        obj._kururu_requested_active = bool(obj.is_active)
        if obj._kururu_requested_active and (
            obj.pk is None or not obj.fields.exists()
        ):
            obj.is_active = False
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        if getattr(obj, "_kururu_created_now", False) and not obj.fields.exists():
            for order, (key, label, kind, required) in enumerate(
                PRESETS.get(form.cleaned_data.get("preset") or "standard", [])
            ):
                options = ["会社案内", "製品資料", "料金表"] if key == "document" else []
                ContactField.objects.create(
                    form=obj,
                    key=key,
                    label=label,
                    kind=kind,
                    required=required,
                    options=options,
                    max_length=5000 if kind == ContactField.Kind.TEXTAREA else 200,
                    order=order,
                )
        if getattr(obj, "_kururu_requested_active", False) and not obj.is_active:
            if not obj.fields.exists():
                raise forms.ValidationError(
                    "有効化するフォームには1項目以上が必要です。"
                )
            obj.is_active = True
            obj.save(update_fields=["is_active"])

    @admin.action(description="選択したフォームを複製", permissions=["duplicate"])
    def duplicate_forms(self, request, queryset):
        if not self.has_duplicate_permission(request):
            raise PermissionDenied
        count = 0
        for source in queryset.prefetch_related("fields"):
            if not self.has_duplicate_permission(request, source):
                raise PermissionDenied
            base = slugify(source.slug + "-copy")[:90] or "form-copy"
            slug = base
            number = 2
            while ContactForm.objects.filter(slug=slug).exists():
                slug = f"{base}-{number}"[:100]
                number += 1
            clone = ContactForm.objects.create(
                name=f"{source.name}（複製）",
                slug=slug,
                recipient_email=source.recipient_email,
                subject=source.subject,
                autoresponder_subject=source.autoresponder_subject,
                autoresponder_body=source.autoresponder_body,
                success_message=source.success_message,
                error_message=source.error_message,
                retention_days=source.retention_days,
                is_active=False,
            )
            ContactField.objects.bulk_create(
                [
                    ContactField(
                        form=clone,
                        key=item.key,
                        label=item.label,
                        kind=item.kind,
                        required=item.required,
                        options=item.options,
                        max_length=item.max_length,
                        order=item.order,
                    )
                    for item in source.fields.all()
                ]
            )
            count += 1
        self.message_user(request, f"{count}件を無効状態で複製しました。", messages.SUCCESS)

    @admin.action(description="選択したフォームをアーカイブ", permissions=["change"])
    def archive_forms(self, request, queryset):
        if not self.has_change_permission(request):
            raise PermissionDenied
        for contact_form in queryset.only("pk"):
            if not self.has_change_permission(request, contact_form):
                raise PermissionDenied
        count = queryset.update(is_active=False, is_archived=True)
        self.message_user(request, f"{count}件をアーカイブしました。", messages.SUCCESS)


@admin.register(ContactSubmission)
class ContactSubmissionAdmin(admin.ModelAdmin):
    list_display = ("form", "status", "submitted_at")
    list_filter = ("form", "status")
    readonly_fields = (
        "form", "payload", "status", "ip_hash", "user_agent", "page_path",
        "notification_recipient", "notification_subject", "notification_body",
        "notification_reply_to", "autoreply_recipient", "autoreply_subject",
        "autoreply_body", "submitted_at"
    )
    date_hierarchy = "submitted_at"

    def _can_view_content(self, request):
        return request.user.is_superuser or request.user.has_perm(
            "contact_forms.view_contact_content"
        )

    def has_module_permission(self, request):
        return self._can_view_content(request)

    def has_view_permission(self, request, obj=None):
        return self._can_view_content(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        if not self._can_view_content(request):
            raise PermissionDenied
        return super().get_queryset(request)


@admin.register(MailDelivery)
class MailDeliveryAdmin(admin.ModelAdmin):
    list_display = (
        "submission",
        "kind",
        "status",
        "attempts",
        "available_at",
        "sent_at",
    )
    readonly_fields = (
        "submission",
        "kind",
        "status",
        "attempts",
        "last_error",
        "available_at",
        "locked_at",
        "locked_by",
        "last_attempt_at",
        "sent_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ContactPluginSetting)
class ContactPluginSettingAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return (
            super().has_add_permission(request)
            and not ContactPluginSetting.objects.exists()
        )

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ContactMaintenanceRun)
class ContactMaintenanceRunAdmin(admin.ModelAdmin):
    list_display = (
        "kind",
        "status",
        "started_at",
        "finished_at",
        "deleted_count",
        "error_type",
    )
    readonly_fields = (
        "kind",
        "status",
        "started_at",
        "finished_at",
        "deleted_count",
        "error_type",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
