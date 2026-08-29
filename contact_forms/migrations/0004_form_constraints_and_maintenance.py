import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("contact_forms", "0003_maildelivery_outbox"),
    ]

    operations = [
        migrations.AlterField(
            model_name="contactform",
            name="retention_days",
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                verbose_name="保存日数",
            ),
        ),
        migrations.CreateModel(
            name="ContactMaintenanceRun",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("purge", "保存期限削除")],
                        max_length=20,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("running", "実行中"),
                            ("succeeded", "成功"),
                            ("failed", "失敗"),
                        ],
                        default="running",
                        max_length=20,
                    ),
                ),
                (
                    "started_at",
                    models.DateTimeField(
                        db_index=True,
                        default=django.utils.timezone.now,
                    ),
                ),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("deleted_count", models.PositiveIntegerField(default=0)),
                ("error_type", models.CharField(blank=True, max_length=200)),
            ],
            options={
                "verbose_name": "問い合わせ保守実行",
                "verbose_name_plural": "問い合わせ保守実行",
                "ordering": ("-started_at",),
            },
        ),
    ]
