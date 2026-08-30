from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from contact_forms.models import ContactSubmission, MailDelivery


class Command(BaseCommand):
    help = "配送結果不明のOutboxを、送信済み確定または明示的再送で解決します。"

    def add_arguments(self, parser):
        parser.add_argument("delivery_id", type=int)
        parser.add_argument(
            "--action",
            required=True,
            choices=("mark-sent", "retry"),
        )
        parser.add_argument(
            "--confirm-duplicate-risk",
            action="store_true",
            help="結果不明メールを再送する場合に、重複可能性を明示確認します。",
        )

    def handle(self, **options):
        action = options["action"]
        if action == "retry" and not options["confirm_duplicate_risk"]:
            raise CommandError(
                "--confirm-duplicate-risk is required when retrying unknown delivery"
            )

        now = timezone.now()
        with transaction.atomic():
            delivery = (
                MailDelivery.objects.select_for_update()
                .select_related("submission")
                .filter(pk=options["delivery_id"])
                .first()
            )
            if delivery is None:
                raise CommandError("delivery not found")
            if delivery.status != MailDelivery.Status.UNKNOWN:
                raise CommandError("only unknown delivery can be resolved")

            if action == "retry":
                delivery.status = MailDelivery.Status.PENDING
                delivery.available_at = now
                delivery.last_error = ""
                delivery.locked_at = None
                delivery.locked_by = ""
                delivery.save(
                    update_fields=[
                        "status",
                        "available_at",
                        "last_error",
                        "locked_at",
                        "locked_by",
                    ]
                )
                if delivery.kind == MailDelivery.Kind.NOTIFICATION:
                    delivery.submission.status = ContactSubmission.Status.RECEIVED
                    delivery.submission.save(update_fields=["status"])
                result = f"queued={delivery.pk}"
            else:
                delivery.status = MailDelivery.Status.SENT
                delivery.sent_at = now
                delivery.last_error = ""
                delivery.locked_at = None
                delivery.locked_by = ""
                delivery.save(
                    update_fields=[
                        "status",
                        "sent_at",
                        "last_error",
                        "locked_at",
                        "locked_by",
                    ]
                )
                if delivery.kind == MailDelivery.Kind.NOTIFICATION:
                    submission = delivery.submission
                    submission.status = ContactSubmission.Status.DELIVERED
                    submission.save(update_fields=["status"])
                    if (
                        submission.autoreply_recipient
                        and submission.autoreply_subject
                        and submission.autoreply_body
                    ):
                        MailDelivery.objects.get_or_create(
                            submission=submission,
                            kind=MailDelivery.Kind.AUTOREPLY,
                            defaults={"available_at": now},
                        )
                result = f"marked_sent={delivery.pk}"

        self.stdout.write(self.style.SUCCESS(result))
