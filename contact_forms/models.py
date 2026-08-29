from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

FIELD_KEY = re.compile(r"^[a-z][a-z0-9_]{0,49}$")


class ContactForm(models.Model):
    name = models.CharField("フォーム名", max_length=120)
    slug = models.SlugField("slug", max_length=100, unique=True)
    recipient_email = models.EmailField("通知先")
    subject = models.CharField("通知件名", max_length=180, default="Webサイトからのお問い合わせ")
    autoresponder_subject = models.CharField("自動返信件名", max_length=180, blank=True)
    autoresponder_body = models.TextField("自動返信本文", blank=True)
    success_message = models.CharField("成功メッセージ", max_length=300, default="お問い合わせを受け付けました。")
    error_message = models.CharField("失敗メッセージ", max_length=300, default="送信できませんでした。入力内容をご確認ください。")
    retention_days = models.PositiveSmallIntegerField(
        "保存日数",
        blank=True,
        null=True,
        validators=[MinValueValidator(1), MaxValueValidator(3650)],
    )
    is_active = models.BooleanField("有効", default=False)
    is_archived = models.BooleanField("アーカイブ", default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "問い合わせフォーム"
        verbose_name_plural = "問い合わせフォーム"
        ordering = ("name",)
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(retention_days__isnull=True)
                    | models.Q(retention_days__gte=1, retention_days__lte=3650)
                ),
                name="contact_form_retention_days_range",
            )
        ]

    def __str__(self):
        return self.name

    def clean(self):
        if self.retention_days is not None and not 1 <= self.retention_days <= 3650:
            raise ValidationError({"retention_days": "保存日数は1〜3650日で指定してください。"})
        if self.is_active and self.is_archived:
            raise ValidationError("アーカイブ済みフォームは有効化できません。")

    def save(self, *args, **kwargs):
        if self.retention_days is None:
            self.retention_days = ContactPluginSetting.load().default_retention_days
        if not 1 <= self.retention_days <= 3650:
            raise ValidationError(
                {"retention_days": "保存日数は1〜3650日で指定してください。"}
            )
        if self.is_active and (
            self.pk is None
            or not ContactField.objects.filter(form_id=self.pk).exists()
        ):
            raise ValidationError(
                {"is_active": "有効化するフォームには1項目以上が必要です。"}
            )
        return super().save(*args, **kwargs)


class ContactField(models.Model):
    class Kind(models.TextChoices):
        TEXT = "text", "1行テキスト"
        EMAIL = "email", "メール"
        TEL = "tel", "電話番号"
        TEXTAREA = "textarea", "複数行テキスト"
        NUMBER = "number", "数値"
        DATE = "date", "日付"
        SELECT = "select", "セレクトボックス"
        RADIO = "radio", "ラジオボタン"
        CHECKBOX = "checkbox", "チェックボックス"
        CONSENT = "consent", "個人情報取扱いへの同意"

    form = models.ForeignKey(ContactForm, related_name="fields", on_delete=models.CASCADE)
    key = models.CharField("項目キー", max_length=50)
    label = models.CharField("表示名", max_length=120)
    kind = models.CharField("種類", max_length=20, choices=Kind.choices)
    required = models.BooleanField("必須", default=False)
    options = models.JSONField("選択肢", default=list, blank=True)
    max_length = models.PositiveIntegerField("最大文字数", default=200)
    order = models.PositiveSmallIntegerField("並び順", default=0)

    class Meta:
        ordering = ("order", "pk")
        unique_together = (("form", "key"),)
        verbose_name = "入力項目"
        verbose_name_plural = "入力項目"

    def __str__(self):
        return f"{self.form}: {self.label}"

    def clean(self):
        if not FIELD_KEY.fullmatch(self.key or ""):
            raise ValidationError({"key": "英小文字で始まる英小文字・数字・_のみ使用できます。"})
        if self.max_length > 20_000:
            raise ValidationError({"max_length": "最大文字数は20000以下にしてください。"})
        if self.kind in {self.Kind.SELECT, self.Kind.RADIO, self.Kind.CHECKBOX}:
            if not isinstance(self.options, list) or not self.options or len(self.options) > 100:
                raise ValidationError({"options": "1〜100件の文字列リストを指定してください。"})
            if not all(isinstance(item, str) and 0 < len(item) <= 200 for item in self.options):
                raise ValidationError({"options": "選択肢は200文字以内の文字列にしてください。"})


class ContactSubmission(models.Model):
    class Status(models.TextChoices):
        RECEIVED = "received", "受付済み"
        DELIVERED = "delivered", "通知済み"
        MAIL_FAILED = "mail_failed", "メール失敗"

    form = models.ForeignKey(ContactForm, related_name="submissions", on_delete=models.PROTECT)
    idempotency_key = models.UUIDField("冪等キー", unique=True, editable=False)
    payload = models.JSONField("送信内容")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RECEIVED)
    ip_hash = models.CharField("IPハッシュ", max_length=64)
    user_agent = models.CharField("User-Agent", max_length=200, blank=True)
    page_path = models.CharField("送信元ページ", max_length=500, blank=True)
    notification_recipient = models.EmailField("通知先スナップショット", blank=True)
    notification_subject = models.CharField(
        "通知件名スナップショット", max_length=180, blank=True
    )
    notification_body = models.TextField("通知本文スナップショット", blank=True)
    notification_reply_to = models.EmailField("Reply-Toスナップショット", blank=True)
    autoreply_recipient = models.EmailField("自動返信先スナップショット", blank=True)
    autoreply_subject = models.CharField(
        "自動返信件名スナップショット", max_length=180, blank=True
    )
    autoreply_body = models.TextField("自動返信本文スナップショット", blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-submitted_at",)
        permissions = [
            ("view_contact_content", "問い合わせ本文を閲覧できる"),
            ("export_contact_submission", "問い合わせをCSV出力できる"),
        ]
        verbose_name = "問い合わせ履歴"
        verbose_name_plural = "問い合わせ履歴"


class MailDelivery(models.Model):
    class Kind(models.TextChoices):
        NOTIFICATION = "notification", "管理者通知"
        AUTOREPLY = "autoreply", "自動返信"

    class Status(models.TextChoices):
        PENDING = "pending", "送信待ち"
        PROCESSING = "processing", "送信処理中"
        SENT = "sent", "送信済み"
        FAILED = "failed", "失敗"

    submission = models.ForeignKey(ContactSubmission, related_name="deliveries", on_delete=models.CASCADE)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True)
    available_at = models.DateTimeField(default=timezone.now, db_index=True)
    locked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    locked_by = models.CharField(max_length=100, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = (("submission", "kind"),)
        verbose_name = "メール送信結果"
        verbose_name_plural = "メール送信結果"


class ContactPluginSetting(models.Model):
    max_post_bytes = models.PositiveIntegerField("POST上限", default=65_536)
    rate_limit = models.PositiveSmallIntegerField("IP・フォーム別上限", default=5)
    rate_window_seconds = models.PositiveIntegerField("制限時間（秒）", default=600)
    minimum_fill_seconds = models.PositiveSmallIntegerField("最短入力時間", default=2)
    default_retention_days = models.PositiveSmallIntegerField(
        "既定保存日数",
        default=90,
        validators=[MinValueValidator(1), MaxValueValidator(3650)],
    )
    mail_max_attempts = models.PositiveSmallIntegerField("メール最大試行回数", default=5)
    mail_retry_base_seconds = models.PositiveIntegerField("メール再試行基準秒", default=60)
    mail_lock_timeout_seconds = models.PositiveIntegerField("メールロック失効秒", default=900)
    autorespond_after_notification_failure = models.BooleanField(
        "管理者通知失敗後も自動返信する",
        default=False,
    )

    class Meta:
        verbose_name = "問い合わせフォーム設定"
        verbose_name_plural = "問い合わせフォーム設定"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    default_retention_days__gte=1,
                    default_retention_days__lte=3650,
                ),
                name="contact_default_retention_days_range",
            )
        ]

    def clean(self):
        if not 1 <= self.default_retention_days <= 3650:
            raise ValidationError(
                {"default_retention_days": "既定保存日数は1〜3650日で指定してください。"}
            )
        if not 1 <= self.max_post_bytes <= 65_536:
            raise ValidationError({"max_post_bytes": "POST上限は1〜65536バイトです。"})
        if not 1 <= self.rate_limit <= 100:
            raise ValidationError({"rate_limit": "上限は1〜100回です。"})
        if not 1 <= self.rate_window_seconds <= 86_400:
            raise ValidationError({"rate_window_seconds": "制限時間は1〜86400秒です。"})
        if not 1 <= self.mail_max_attempts <= 20:
            raise ValidationError({"mail_max_attempts": "最大試行回数は1〜20回です。"})
        if not 1 <= self.mail_retry_base_seconds <= 3_600:
            raise ValidationError({"mail_retry_base_seconds": "再試行基準秒は1〜3600秒です。"})
        if not 30 <= self.mail_lock_timeout_seconds <= 86_400:
            raise ValidationError({"mail_lock_timeout_seconds": "ロック失効秒は30〜86400秒です。"})

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]


class ContactMaintenanceRun(models.Model):
    class Kind(models.TextChoices):
        PURGE = "purge", "保存期限削除"

    class Status(models.TextChoices):
        RUNNING = "running", "実行中"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失敗"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.RUNNING,
    )
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    deleted_count = models.PositiveIntegerField(default=0)
    error_type = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ("-started_at",)
        verbose_name = "問い合わせ保守実行"
        verbose_name_plural = "問い合わせ保守実行"
