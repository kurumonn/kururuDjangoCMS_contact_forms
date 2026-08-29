from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from contact_forms.models import ContactSubmission, MailDelivery


class Command(BaseCommand):
    help = "失敗したOutbox配送をID指定で送信待ちへ戻します。"

    def add_arguments(self, parser):
        parser.add_argument("delivery_id", type=int)

    def handle(self, **options):
        with transaction.atomic():
            delivery = MailDelivery.objects.select_for_update().filter(
                pk=options["delivery_id"],
            ).first()
            if delivery is None:
                raise CommandError("delivery not found")
            if delivery.status != MailDelivery.Status.FAILED:
                raise CommandError("only failed delivery can be retried")
            delivery.status = MailDelivery.Status.PENDING
            delivery.attempts = 0
            delivery.available_at = timezone.now()
            delivery.last_error = ""
            delivery.locked_at = None
            delivery.locked_by = ""
            delivery.save(
                update_fields=[
                    "status",
                    "attempts",
                    "available_at",
                    "last_error",
                    "locked_at",
                    "locked_by",
                ]
            )
            if delivery.kind == MailDelivery.Kind.NOTIFICATION:
                ContactSubmission.objects.filter(
                    pk=delivery.submission_id
                ).update(status=ContactSubmission.Status.RECEIVED)
        self.stdout.write(self.style.SUCCESS(f"queued={delivery.pk}"))
