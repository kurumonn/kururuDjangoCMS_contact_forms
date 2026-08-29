from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from contact_forms.models import ContactForm, ContactSubmission


class Command(BaseCommand):
    help = "フォームごとの保存期間を過ぎた問い合わせと送信結果を削除します。"

    def handle(self, **options):
        total = 0
        now = timezone.now()
        for form in ContactForm.objects.all().only("pk", "retention_days"):
            cutoff = now - timedelta(days=form.retention_days)
            deleted, _ = ContactSubmission.objects.filter(
                form=form, submitted_at__lt=cutoff
            ).delete()
            total += deleted
        self.stdout.write(self.style.SUCCESS(f"purged={total}"))
