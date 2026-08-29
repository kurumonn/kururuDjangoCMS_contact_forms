from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from contact_forms.models import ContactMaintenanceRun, MailDelivery


class Command(BaseCommand):
    help = "Outbox滞留・失敗と保存期限削除の停止を監視します。"

    def add_arguments(self, parser):
        parser.add_argument("--max-outbox-age-minutes", type=int, default=30)
        parser.add_argument("--max-purge-age-hours", type=int, default=36)

    def handle(self, **options):
        if options["max_outbox_age_minutes"] < 1:
            raise CommandError("--max-outbox-age-minutes must be positive")
        if options["max_purge_age_hours"] < 1:
            raise CommandError("--max-purge-age-hours must be positive")

        now = timezone.now()
        outbox_cutoff = now - timedelta(
            minutes=options["max_outbox_age_minutes"]
        )
        purge_cutoff = now - timedelta(hours=options["max_purge_age_hours"])
        issues = []

        failed = MailDelivery.objects.filter(
            status=MailDelivery.Status.FAILED
        ).count()
        if failed:
            issues.append(f"failed_deliveries={failed}")

        stale = MailDelivery.objects.filter(
            Q(
                status=MailDelivery.Status.PENDING,
                available_at__lt=outbox_cutoff,
            )
            | Q(
                status=MailDelivery.Status.PROCESSING,
                locked_at__lt=outbox_cutoff,
            )
            | Q(
                status=MailDelivery.Status.PROCESSING,
                locked_at__isnull=True,
            )
        ).count()
        if stale:
            issues.append(f"stale_deliveries={stale}")

        latest_purge = ContactMaintenanceRun.objects.filter(
            kind=ContactMaintenanceRun.Kind.PURGE,
            status=ContactMaintenanceRun.Status.SUCCEEDED,
        ).first()
        if latest_purge is None:
            issues.append("last_purge=missing")
        elif latest_purge.finished_at is None or latest_purge.finished_at < purge_cutoff:
            issues.append("last_purge=stale")

        if issues:
            raise CommandError("; ".join(issues))
        self.stdout.write(self.style.SUCCESS("contact_forms_health=ok"))
