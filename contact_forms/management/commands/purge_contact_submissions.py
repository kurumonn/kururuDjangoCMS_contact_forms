from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from contact_forms.models import (
    ContactForm,
    ContactMaintenanceRun,
    ContactSubmission,
)


class Command(BaseCommand):
    help = "フォームごとの保存期間を過ぎた問い合わせと送信結果を削除します。"

    def handle(self, **options):
        run = ContactMaintenanceRun.objects.create(
            kind=ContactMaintenanceRun.Kind.PURGE,
        )
        total = 0
        try:
            now = timezone.now()
            with transaction.atomic():
                for form in ContactForm.objects.exclude(
                    retention_days__isnull=True
                ).only("pk", "retention_days"):
                    cutoff = now - timedelta(days=form.retention_days)
                    expired = ContactSubmission.objects.filter(
                        form=form,
                        submitted_at__lt=cutoff,
                    )
                    total += expired.count()
                    expired.delete()
        except Exception as exc:
            run.status = ContactMaintenanceRun.Status.FAILED
            run.finished_at = timezone.now()
            run.error_type = type(exc).__name__[:200]
            run.save(update_fields=["status", "finished_at", "error_type"])
            raise
        run.status = ContactMaintenanceRun.Status.SUCCEEDED
        run.finished_at = timezone.now()
        run.deleted_count = total
        run.save(
            update_fields=["status", "finished_at", "deleted_count"]
        )
        self.stdout.write(self.style.SUCCESS(f"purged={total}"))
