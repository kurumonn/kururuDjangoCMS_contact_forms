import uuid

from django.db import migrations, models


def populate_idempotency_keys(apps, schema_editor):
    ContactSubmission = apps.get_model("contact_forms", "ContactSubmission")
    for submission in ContactSubmission.objects.filter(
        idempotency_key__isnull=True
    ).iterator():
        submission.idempotency_key = uuid.uuid4()
        submission.save(update_fields=["idempotency_key"])


class Migration(migrations.Migration):
    dependencies = [
        ("contact_forms", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="contactsubmission",
            name="idempotency_key",
            field=models.UUIDField(editable=False, null=True, verbose_name="冪等キー"),
        ),
        migrations.RunPython(
            populate_idempotency_keys,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="contactsubmission",
            name="idempotency_key",
            field=models.UUIDField(editable=False, unique=True, verbose_name="冪等キー"),
        ),
    ]
