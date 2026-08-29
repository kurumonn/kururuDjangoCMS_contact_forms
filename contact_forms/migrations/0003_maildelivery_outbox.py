import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("contact_forms", "0002_contactsubmission_idempotency_key"),
    ]

    operations = [
        migrations.AddField(
            model_name="contactpluginsetting",
            name="autorespond_after_notification_failure",
            field=models.BooleanField(default=False, verbose_name="管理者通知失敗後も自動返信する"),
        ),
        migrations.AddField(
            model_name="contactpluginsetting",
            name="mail_lock_timeout_seconds",
            field=models.PositiveIntegerField(default=900, verbose_name="メールロック失効秒"),
        ),
        migrations.AddField(
            model_name="contactpluginsetting",
            name="mail_max_attempts",
            field=models.PositiveSmallIntegerField(default=5, verbose_name="メール最大試行回数"),
        ),
        migrations.AddField(
            model_name="contactpluginsetting",
            name="mail_retry_base_seconds",
            field=models.PositiveIntegerField(default=60, verbose_name="メール再試行基準秒"),
        ),
        migrations.AddField(
            model_name="maildelivery",
            name="available_at",
            field=models.DateTimeField(
                db_index=True,
                default=django.utils.timezone.now,
            ),
        ),
        migrations.AddField(
            model_name="maildelivery",
            name="last_attempt_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="maildelivery",
            name="locked_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="maildelivery",
            name="locked_by",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AlterField(
            model_name="maildelivery",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "送信待ち"),
                    ("processing", "送信処理中"),
                    ("sent", "送信済み"),
                    ("failed", "失敗"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
