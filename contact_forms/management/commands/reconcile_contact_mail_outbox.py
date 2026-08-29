from django.core.management.base import BaseCommand

from contact_forms.mailer import reconcile_missing_deliveries


class Command(BaseCommand):
    help = "配送行がない受付済み問い合わせをDB Outboxへ復旧します。"

    def handle(self, **options):
        count = reconcile_missing_deliveries()
        self.stdout.write(self.style.SUCCESS(f"reconciled={count}"))
