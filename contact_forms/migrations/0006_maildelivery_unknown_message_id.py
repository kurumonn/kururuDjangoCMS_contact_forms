import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("contact_forms", "0005_delivery_snapshots_retention_constraints"),
    ]

    operations = [
        migrations.AddField(
            model_name="maildelivery",
            name="message_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="maildelivery",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "送信待ち"),
                    ("processing", "送信処理中"),
                    ("unknown", "配送結果不明"),
                    ("sent", "送信済み"),
                    ("failed", "失敗"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
    ]
