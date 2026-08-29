import os
import socket
import time
import uuid

from django.core.management.base import BaseCommand, CommandError

from contact_forms.mailer import process_next_delivery
from contact_forms.mailer import reconcile_missing_deliveries


class Command(BaseCommand):
    help = "DB Outboxの問い合わせメールを送信します。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="送信可能なジョブを1件処理して終了します。",
        )
        parser.add_argument(
            "--poll-seconds",
            type=float,
            default=5.0,
            help="待機中のポーリング間隔です。",
        )
        parser.add_argument(
            "--max-jobs",
            type=int,
            default=0,
            help="0は無制限、正数は処理件数到達後に終了します。",
        )
        parser.add_argument("--worker-id", default="")

    def handle(self, **options):
        if options["poll_seconds"] <= 0:
            raise CommandError("--poll-seconds must be greater than zero")
        if options["max_jobs"] < 0:
            raise CommandError("--max-jobs must be zero or greater")
        worker_id = options["worker_id"] or (
            f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        )
        reconcile_missing_deliveries()
        processed = 0
        while True:
            result = process_next_delivery(worker_id)
            if result is not None:
                processed += 1
                if options["once"] or (
                    options["max_jobs"] and processed >= options["max_jobs"]
                ):
                    break
                continue
            if options["once"]:
                break
            time.sleep(options["poll_seconds"])
        self.stdout.write(f"processed={processed}")
