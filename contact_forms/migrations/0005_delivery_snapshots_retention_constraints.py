import django.core.validators
from django.db import migrations, models


def snapshot_existing_submissions(apps, schema_editor):
    ContactField = apps.get_model("contact_forms", "ContactField")
    ContactSubmission = apps.get_model("contact_forms", "ContactSubmission")

    labels = {}
    email_keys = {}
    for field in ContactField.objects.order_by("form_id", "order", "pk").iterator():
        labels.setdefault(field.form_id, {})[field.key] = field.label
        if field.kind == "email":
            email_keys.setdefault(field.form_id, []).append(field.key)

    pending = []
    queryset = ContactSubmission.objects.select_related("form").order_by("pk")
    for submission in queryset.iterator(chunk_size=200):
        payload = submission.payload or {}
        body_lines = []
        for key, value in payload.items():
            shown = ", ".join(value) if isinstance(value, list) else str(value)
            body_lines.append(f"{labels.get(submission.form_id, {}).get(key, key)}: {shown}")
        submitter = ""
        for key in email_keys.get(submission.form_id, []):
            value = payload.get(key)
            if isinstance(value, str) and value:
                submitter = value
                break

        form = submission.form
        submission.notification_recipient = form.recipient_email
        submission.notification_subject = form.subject
        submission.notification_body = "\n".join(body_lines)
        submission.notification_reply_to = submitter
        if submitter and form.autoresponder_subject and form.autoresponder_body:
            submission.autoreply_recipient = submitter
            submission.autoreply_subject = form.autoresponder_subject
            submission.autoreply_body = form.autoresponder_body
        pending.append(submission)
        if len(pending) >= 200:
            ContactSubmission.objects.bulk_update(
                pending,
                [
                    "notification_recipient",
                    "notification_subject",
                    "notification_body",
                    "notification_reply_to",
                    "autoreply_recipient",
                    "autoreply_subject",
                    "autoreply_body",
                ],
            )
            pending.clear()
    if pending:
        ContactSubmission.objects.bulk_update(
            pending,
            [
                "notification_recipient",
                "notification_subject",
                "notification_body",
                "notification_reply_to",
                "autoreply_recipient",
                "autoreply_subject",
                "autoreply_body",
            ],
        )


class Migration(migrations.Migration):
    dependencies = [
        ("contact_forms", "0004_form_constraints_and_maintenance"),
    ]

    operations = [
        migrations.AddField(
            model_name="contactsubmission",
            name="autoreply_body",
            field=models.TextField(blank=True, verbose_name="自動返信本文スナップショット"),
        ),
        migrations.AddField(
            model_name="contactsubmission",
            name="autoreply_recipient",
            field=models.EmailField(blank=True, max_length=254, verbose_name="自動返信先スナップショット"),
        ),
        migrations.AddField(
            model_name="contactsubmission",
            name="autoreply_subject",
            field=models.CharField(blank=True, max_length=180, verbose_name="自動返信件名スナップショット"),
        ),
        migrations.AddField(
            model_name="contactsubmission",
            name="notification_body",
            field=models.TextField(blank=True, verbose_name="通知本文スナップショット"),
        ),
        migrations.AddField(
            model_name="contactsubmission",
            name="notification_recipient",
            field=models.EmailField(blank=True, max_length=254, verbose_name="通知先スナップショット"),
        ),
        migrations.AddField(
            model_name="contactsubmission",
            name="notification_reply_to",
            field=models.EmailField(blank=True, max_length=254, verbose_name="Reply-Toスナップショット"),
        ),
        migrations.AddField(
            model_name="contactsubmission",
            name="notification_subject",
            field=models.CharField(blank=True, max_length=180, verbose_name="通知件名スナップショット"),
        ),
        migrations.AlterField(
            model_name="contactform",
            name="retention_days",
            field=models.PositiveSmallIntegerField(
                blank=True,
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(3650),
                ],
                verbose_name="保存日数",
            ),
        ),
        migrations.AlterField(
            model_name="contactpluginsetting",
            name="default_retention_days",
            field=models.PositiveSmallIntegerField(
                default=90,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(3650),
                ],
                verbose_name="既定保存日数",
            ),
        ),
        migrations.AddConstraint(
            model_name="contactform",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("retention_days__isnull", True))
                    | models.Q(("retention_days__gte", 1), ("retention_days__lte", 3650))
                ),
                name="contact_form_retention_days_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="contactpluginsetting",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("default_retention_days__gte", 1),
                    ("default_retention_days__lte", 3650),
                ),
                name="contact_default_retention_days_range",
            ),
        ),
        migrations.RunPython(snapshot_existing_submissions, migrations.RunPython.noop),
    ]
