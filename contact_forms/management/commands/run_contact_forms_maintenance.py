import time

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "保存期限削除を定期実行する常駐メンテナンスプロセスです。"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument(
            "--interval-seconds",
            type=int,
            default=86_400,
        )

    def handle(self, **options):
        if options["interval_seconds"] < 60:
            raise CommandError("--interval-seconds must be at least 60")
        while True:
            call_command("purge_contact_submissions")
            if options["once"]:
                return
            time.sleep(options["interval_seconds"])
