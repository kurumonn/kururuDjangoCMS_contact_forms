import re
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.checks import run_checks
from django.core import mail, signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command, CommandError
from django.forms.models import inlineformset_factory
from django.http import QueryDict
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from blog.blocks import block_editor_catalog, validate_blocks
from blog.templatetags.block_tags import render_blocks
from blog.tests.factories import create_article
from cms_plugins.models import PluginActivation

from contact_forms.forms import build_submission_form
from contact_forms.pending import (
    MAX_AGE_SECONDS,
    MAX_TOTAL_CHARS,
    SESSION_KEY,
    pop_invalid_submission,
    remember_invalid_submission,
)
from contact_forms.admin import (
    ContactFieldInlineFormSet,
    ContactFormAdmin,
    ContactFormAdminForm,
    ContactPluginSettingAdmin,
    ContactSubmissionAdmin,
)
from contact_forms.mailer import _message_for, process_next_delivery
from contact_forms.models import (
    ContactField,
    ContactForm,
    ContactMaintenanceRun,
    ContactPluginSetting,
    ContactSubmission,
    MailDelivery,
)
from contact_forms.plugin import BLOCK_NAME, PLUGIN_KEY
from contact_forms.services import (
    SIGNING_NAMESPACE,
    UNKNOWN_INSTANCE,
    ip_hash,
    load_render_token,
    make_render_token,
    safe_instance,
    safe_return_path,
)
from contact_forms.views import manage


class KururuFormsTestCase(TestCase):
    def setUp(self):
        # レート制限のカウンタはキャッシュに残り、テストをまたいで積み上がる。
        # 捨てないと「テストを増やしたら別のテストが落ちる」ようになる。
        cache.clear()
        PluginActivation.objects.update_or_create(
            key=PLUGIN_KEY, defaults={"enabled": True}
        )
        ContactPluginSetting.objects.update_or_create(
            pk=1,
            defaults={
                "minimum_fill_seconds": 0,
                "rate_limit": 10,
                "rate_window_seconds": 600,
                "max_post_bytes": 65_536,
            },
        )
        self.form = ContactForm.objects.create(
            name="標準お問い合わせ",
            slug="general",
            recipient_email="owner@example.test",
            subject="問い合わせ",
            autoresponder_subject="受付完了",
            autoresponder_body="お問い合わせを受け付けました。",
            is_active=False,
        )
        ContactField.objects.create(
            form=self.form, key="name", label="お名前",
            kind=ContactField.Kind.TEXT, required=True, order=1,
        )
        ContactField.objects.create(
            form=self.form, key="email", label="メール",
            kind=ContactField.Kind.EMAIL, required=True, order=2,
        )
        ContactField.objects.create(
            form=self.form, key="message", label="内容",
            kind=ContactField.Kind.TEXTAREA, required=True,
            max_length=1000, order=3,
        )
        self.form.is_active = True
        self.form.save(update_fields=["is_active"])
        self.url = reverse("kururu_forms:submit", args=[self.form.slug])

    def payload(self, **overrides):
        result = {
            "_render_token": make_render_token(self.form.pk, "/articles/example/"),
            "_company": "",
            "name": "山田",
            "email": "reader@example.test",
            "message": "資料をお願いします。",
        }
        result.update(overrides)
        return result


class SubmissionTests(KururuFormsTestCase):
    def drain_outbox(self):
        while process_next_delivery("test-worker") is not None:
            pass

    def test_submission_is_stored_before_notification_and_uses_fixed_from(self):
        with patch("contact_forms.mailer.EmailMessage.send", wraps=None) as send:
            response = self.client.post(
                self.url,
                self.payload(),
                REMOTE_ADDR="198.51.100.10",
            )
            send.assert_not_called()
        self.assertRedirects(response, "/articles/example/", fetch_redirect_response=False)
        submission = ContactSubmission.objects.get()
        self.assertEqual(submission.payload["email"], "reader@example.test")
        self.assertNotEqual(submission.ip_hash, "198.51.100.10")
        self.assertEqual(submission.status, ContactSubmission.Status.RECEIVED)
        self.assertEqual(MailDelivery.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 0)

        self.drain_outbox()
        submission.refresh_from_db()
        self.assertEqual(submission.status, ContactSubmission.Status.DELIVERED)
        self.assertEqual(MailDelivery.objects.count(), 2)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[0].from_email, "forms@example.test")
        self.assertEqual(mail.outbox[0].reply_to, ["reader@example.test"])

    def test_pending_delivery_uses_submission_time_recipient_and_content(self):
        self.client.post(self.url, self.payload())
        submission = ContactSubmission.objects.get()
        original_body = submission.notification_body

        self.form.recipient_email = "changed@example.test"
        self.form.subject = "変更後の件名"
        self.form.autoresponder_body = "変更後の自動返信"
        self.form.save()

        self.drain_outbox()

        self.assertEqual(mail.outbox[0].to, ["owner@example.test"])
        self.assertEqual(mail.outbox[0].subject, "問い合わせ")
        self.assertEqual(mail.outbox[0].body, original_body)
        self.assertEqual(
            mail.outbox[1].body,
            "お問い合わせを受け付けました。",
        )

    def test_mail_failure_does_not_lose_submission_or_store_exception_text(self):
        setting = ContactPluginSetting.load()
        setting.mail_max_attempts = 1
        setting.save(update_fields=["mail_max_attempts"])
        with patch("contact_forms.mailer.EmailMessage.send", side_effect=RuntimeError("reader@example.test")):
            response = self.client.post(self.url, self.payload())
            self.assertFalse(process_next_delivery("failing-worker"))
        self.assertEqual(response.status_code, 302)
        submission = ContactSubmission.objects.get()
        self.assertEqual(submission.status, ContactSubmission.Status.MAIL_FAILED)
        delivery = MailDelivery.objects.get(kind=MailDelivery.Kind.NOTIFICATION)
        self.assertEqual(delivery.status, MailDelivery.Status.FAILED)
        self.assertEqual(delivery.attempts, 1)
        self.assertEqual(delivery.last_error, "RuntimeError")
        self.assertNotIn("reader@", delivery.last_error)
        self.assertFalse(
            MailDelivery.objects.filter(kind=MailDelivery.Kind.AUTOREPLY).exists()
        )

    def test_invalid_email_is_not_stored(self):
        response = self.client.post(self.url, self.payload(email="not-an-email"))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ContactSubmission.objects.exists())

    def test_disabled_plugin_and_disabled_form_fail_closed(self):
        PluginActivation.objects.filter(key=PLUGIN_KEY).update(enabled=False)
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 404)
        PluginActivation.objects.filter(key=PLUGIN_KEY).update(enabled=True)
        self.form.is_active = False
        self.form.save(update_fields=["is_active"])
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 404)

    def test_honeypot_and_tampered_token_are_rejected(self):
        self.assertEqual(
            self.client.post(self.url, self.payload(_company="https://spam.test")).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(self.url, self.payload(_render_token="tampered")).status_code,
            400,
        )
        self.assertFalse(ContactSubmission.objects.exists())

    def test_post_size_limit_is_checked_before_parsing(self):
        response = self.client.post(self.url, self.payload(), CONTENT_LENGTH="65537")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ContactSubmission.objects.exists())

    def test_csrf_is_required(self):
        response = Client(enforce_csrf_checks=True).post(self.url, self.payload())
        self.assertEqual(response.status_code, 403)

    def test_rate_limit_is_per_ip_and_form(self):
        setting = ContactPluginSetting.load()
        setting.rate_limit = 1
        setting.save(update_fields=["rate_limit"])
        first = self.client.post(self.url, self.payload(), REMOTE_ADDR="198.51.100.1")
        second = self.client.post(self.url, self.payload(), REMOTE_ADDR="198.51.100.1")
        other = self.client.post(self.url, self.payload(), REMOTE_ADDR="198.51.100.2")
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(other.status_code, 302)

    def test_replayed_render_token_creates_one_submission_and_one_delivery_set(self):
        payload = self.payload()

        first = self.client.post(self.url, payload, REMOTE_ADDR="198.51.100.20")
        second = self.client.post(self.url, payload, REMOTE_ADDR="198.51.100.20")

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(ContactSubmission.objects.count(), 1)
        self.assertEqual(MailDelivery.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 0)

        self.drain_outbox()
        self.assertEqual(MailDelivery.objects.count(), 2)
        self.assertEqual(len(mail.outbox), 2)

    def test_delivery_uses_stable_message_id(self):
        self.client.post(self.url, self.payload())
        delivery = MailDelivery.objects.get()

        first = _message_for(delivery).extra_headers["Message-ID"]
        second = _message_for(delivery).extra_headers["Message-ID"]

        self.assertEqual(first, second)
        self.assertEqual(
            first,
            f"<kururu-forms-{delivery.message_id.hex}@example.test>",
        )

    def test_stale_processing_delivery_is_quarantined_before_explicit_retry(self):
        self.client.post(self.url, self.payload())
        delivery = MailDelivery.objects.get()
        now = timezone.now()
        delivery.status = MailDelivery.Status.PROCESSING
        delivery.attempts = 1
        delivery.locked_at = now - timedelta(seconds=901)
        delivery.locked_by = "stopped-worker"
        delivery.save(
            update_fields=["status", "attempts", "locked_at", "locked_by"]
        )

        with patch("contact_forms.mailer.EmailMessage.send") as send:
            self.assertIsNone(process_next_delivery("replacement-worker", now=now))
        send.assert_not_called()

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, MailDelivery.Status.UNKNOWN)
        self.assertEqual(delivery.last_error, "DeliveryOutcomeUnknown")
        self.assertEqual(
            ContactSubmission.objects.get().status,
            ContactSubmission.Status.MAIL_FAILED,
        )

        with self.assertRaises(CommandError):
            call_command(
                "resolve_contact_mail_delivery",
                delivery.pk,
                action="retry",
            )

        call_command(
            "resolve_contact_mail_delivery",
            delivery.pk,
            action="retry",
            confirm_duplicate_risk=True,
        )
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, MailDelivery.Status.PENDING)
        self.assertTrue(process_next_delivery("replacement-worker"))
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, MailDelivery.Status.SENT)
        self.assertEqual(delivery.attempts, 2)

    def test_unknown_delivery_can_be_marked_sent_without_resending(self):
        self.client.post(self.url, self.payload())
        delivery = MailDelivery.objects.get()
        now = timezone.now()
        delivery.status = MailDelivery.Status.PROCESSING
        delivery.attempts = 1
        delivery.locked_at = now - timedelta(seconds=901)
        delivery.locked_by = "crashed-worker"
        delivery.save(
            update_fields=["status", "attempts", "locked_at", "locked_by"]
        )

        with patch("contact_forms.mailer.EmailMessage.send") as send:
            self.assertIsNone(process_next_delivery("replacement-worker", now=now))
        send.assert_not_called()

        call_command(
            "resolve_contact_mail_delivery",
            delivery.pk,
            action="mark-sent",
        )

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, MailDelivery.Status.SENT)
        self.assertEqual(
            ContactSubmission.objects.get().status,
            ContactSubmission.Status.DELIVERED,
        )
        self.assertTrue(
            MailDelivery.objects.filter(
                kind=MailDelivery.Kind.AUTOREPLY,
                status=MailDelivery.Status.PENDING,
            ).exists()
        )

    def test_management_command_processes_outbox_without_web_request(self):
        self.client.post(self.url, self.payload())
        self.assertEqual(len(mail.outbox), 0)

        call_command(
            "process_contact_mail_outbox",
            once=True,
            worker_id="command-worker",
        )
        call_command(
            "process_contact_mail_outbox",
            once=True,
            worker_id="command-worker",
        )

        self.assertEqual(len(mail.outbox), 2)
        self.assertFalse(
            MailDelivery.objects.exclude(status=MailDelivery.Status.SENT).exists()
        )

    def test_outbox_uses_exponential_backoff_and_stops_at_max_attempts(self):
        setting = ContactPluginSetting.load()
        setting.mail_max_attempts = 3
        setting.mail_retry_base_seconds = 10
        setting.save(
            update_fields=["mail_max_attempts", "mail_retry_base_seconds"]
        )
        self.client.post(self.url, self.payload())
        now = timezone.now()

        with patch(
            "contact_forms.mailer.EmailMessage.send",
            side_effect=RuntimeError("SMTP unavailable"),
        ) as send:
            self.assertFalse(process_next_delivery("worker", now=now))
            delivery = MailDelivery.objects.get()
            self.assertEqual(delivery.status, MailDelivery.Status.PENDING)
            self.assertEqual(delivery.available_at, now + timedelta(seconds=10))

            self.assertIsNone(
                process_next_delivery("worker", now=now + timedelta(seconds=9))
            )
            self.assertFalse(
                process_next_delivery("worker", now=now + timedelta(seconds=10))
            )
            delivery.refresh_from_db()
            self.assertEqual(delivery.available_at, now + timedelta(seconds=30))

            self.assertFalse(
                process_next_delivery("worker", now=now + timedelta(seconds=30))
            )

        delivery.refresh_from_db()
        self.assertEqual(send.call_count, 3)
        self.assertEqual(delivery.attempts, 3)
        self.assertEqual(delivery.status, MailDelivery.Status.FAILED)
        self.assertEqual(ContactSubmission.objects.get().status, ContactSubmission.Status.MAIL_FAILED)
        self.assertFalse(
            MailDelivery.objects.filter(kind=MailDelivery.Kind.AUTOREPLY).exists()
        )


class FormAndPluginTests(KururuFormsTestCase):
    def test_server_side_field_validation_and_normalization(self):
        form = build_submission_form(
            self.form,
            {"name": "山田", "email": "reader@example.test", "message": "x" * 1001},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("message", form.errors)

    def test_block_is_validated_and_only_enabled_plugin_is_in_editor_catalog(self):
        normalized = validate_blocks(
            [{"type": BLOCK_NAME, "data": {"form_id": str(self.form.pk)}}]
        )
        self.assertEqual(normalized[0]["data"]["form_id"], self.form.pk)
        options = block_editor_catalog()[BLOCK_NAME]["fields"][0]["options"]
        self.assertEqual(options, [{"value": self.form.pk, "label": self.form.name}])
        PluginActivation.objects.filter(key=PLUGIN_KEY).update(enabled=False)
        self.assertNotIn(BLOCK_NAME, block_editor_catalog())

    def test_ip_hash_is_stable(self):
        self.assertEqual(ip_hash("203.0.113.4"), ip_hash("203.0.113.4"))
        self.assertNotIn("203.0.113.4", ip_hash("203.0.113.4"))

    def test_signed_return_path_cannot_be_an_open_redirect(self):
        self.assertEqual(safe_return_path("https://evil.test/steal"), "/")
        self.assertEqual(safe_return_path("//evil.test/steal"), "/")
        self.assertEqual(safe_return_path("/article/?page=2"), "/article/?page=2")

    def test_enabled_block_renders_form_and_disabled_plugin_renders_nothing(self):
        request = RequestFactory().get("/articles/example/")
        blocks = [{"type": BLOCK_NAME, "data": {"form_id": self.form.pk}}]
        html = str(render_blocks({"request": request}, blocks))
        self.assertIn(self.form.name, html)
        self.assertIn(self.url, html)
        self.assertNotIn("reader@example.test", html)
        PluginActivation.objects.filter(key=PLUGIN_KEY).update(enabled=False)
        self.assertEqual(str(render_blocks({"request": request}, blocks)), "")

    def test_empty_form_cannot_be_activated_or_reached_if_database_is_inconsistent(self):
        empty = ContactForm.objects.create(
            name="空フォーム",
            slug="empty",
            recipient_email="owner@example.test",
        )
        empty.is_active = True
        with self.assertRaises(ValidationError):
            empty.save(update_fields=["is_active"])

        ContactForm.objects.filter(pk=empty.pk).update(is_active=True)
        self.assertIn(
            "contact_forms.E002",
            {error.id for error in run_checks()},
        )
        url = reverse("kururu_forms:submit", args=[empty.slug])
        response = self.client.post(
            url,
            {
                "_render_token": make_render_token(empty.pk, "/"),
                "_company": "",
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_admin_inline_rejects_active_empty_form(self):
        empty = ContactForm(
            name="空フォーム",
            slug="empty-admin",
            recipient_email="owner@example.test",
            is_active=True,
        )
        formset_class = inlineformset_factory(
            ContactForm,
            ContactField,
            formset=ContactFieldInlineFormSet,
            fields=(
                "key",
                "label",
                "kind",
                "required",
                "options",
                "max_length",
                "order",
            ),
            extra=0,
        )
        formset = formset_class(
            data={
                "fields-TOTAL_FORMS": "0",
                "fields-INITIAL_FORMS": "0",
                "fields-MIN_NUM_FORMS": "0",
                "fields-MAX_NUM_FORMS": "1000",
            },
            instance=empty,
            prefix="fields",
        )
        self.assertFalse(formset.is_valid())
        self.assertIn("1項目以上", str(formset.non_form_errors()))

    def test_new_form_uses_configured_default_retention(self):
        setting = ContactPluginSetting.load()
        setting.default_retention_days = 30
        setting.save(update_fields=["default_retention_days"])

        created = ContactForm.objects.create(
            name="保存期限テスト",
            slug="retention-default",
            recipient_email="owner@example.test",
        )

        self.assertEqual(created.retention_days, 30)
        self.assertEqual(
            ContactFormAdminForm().fields["retention_days"].initial,
            30,
        )

    def test_default_retention_rejects_zero_and_values_above_limit(self):
        setting = ContactPluginSetting.load()
        for value in (0, 3651):
            with self.subTest(value=value):
                setting.default_retention_days = value
                with self.assertRaises(ValidationError):
                    setting.full_clean()

    def test_blank_form_retention_cannot_copy_invalid_orm_value(self):
        setting = ContactPluginSetting.load()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContactPluginSetting.objects.filter(pk=setting.pk).update(
                default_retention_days=0
            )

        setting.default_retention_days = 0
        with patch.object(ContactPluginSetting, "load", return_value=setting):
            with self.assertRaises(ValidationError):
                ContactForm.objects.create(
                    name="不正な保存期限",
                    slug="invalid-retention",
                    recipient_email="owner@example.test",
                )


class MaintenanceTests(KururuFormsTestCase):
    def test_reconcile_restores_legacy_submission_without_delivery(self):
        submission = ContactSubmission.objects.create(
            form=self.form,
            idempotency_key=uuid.uuid4(),
            payload={
                "name": "旧受付",
                "email": "legacy@example.test",
                "message": "復旧対象",
            },
            ip_hash="0" * 64,
        )

        call_command("reconcile_contact_mail_outbox")

        delivery = MailDelivery.objects.get(submission=submission)
        submission.refresh_from_db()
        self.assertEqual(delivery.kind, MailDelivery.Kind.NOTIFICATION)
        self.assertEqual(submission.notification_recipient, "owner@example.test")
        self.assertEqual(submission.notification_reply_to, "legacy@example.test")

    def test_purge_skips_submission_while_smtp_worker_holds_lease(self):
        self.form.retention_days = 1
        self.form.save(update_fields=["retention_days"])
        submission = ContactSubmission.objects.create(
            form=self.form,
            idempotency_key=uuid.uuid4(),
            payload={"name": "processing"},
            ip_hash="0" * 64,
        )
        delivery = MailDelivery.objects.create(
            submission=submission,
            kind=MailDelivery.Kind.NOTIFICATION,
            status=MailDelivery.Status.PROCESSING,
            locked_at=timezone.now(),
            locked_by="worker",
        )
        ContactSubmission.objects.filter(pk=submission.pk).update(
            submitted_at=timezone.now() - timedelta(days=2)
        )

        call_command("purge_contact_submissions")

        self.assertTrue(ContactSubmission.objects.filter(pk=submission.pk).exists())
        self.assertTrue(MailDelivery.objects.filter(pk=delivery.pk).exists())

    def test_purge_skips_submission_with_unknown_delivery(self):
        self.form.retention_days = 1
        self.form.save(update_fields=["retention_days"])
        submission = ContactSubmission.objects.create(
            form=self.form,
            idempotency_key=uuid.uuid4(),
            payload={"name": "unknown"},
            ip_hash="0" * 64,
        )
        delivery = MailDelivery.objects.create(
            submission=submission,
            kind=MailDelivery.Kind.NOTIFICATION,
            status=MailDelivery.Status.UNKNOWN,
        )
        ContactSubmission.objects.filter(pk=submission.pk).update(
            submitted_at=timezone.now() - timedelta(days=2)
        )

        call_command("purge_contact_submissions")

        self.assertTrue(ContactSubmission.objects.filter(pk=submission.pk).exists())
        self.assertTrue(MailDelivery.objects.filter(pk=delivery.pk).exists())

    def test_health_check_reports_unknown_delivery(self):
        call_command(
            "run_contact_forms_maintenance",
            once=True,
            interval_seconds=86_400,
        )
        submission = ContactSubmission.objects.create(
            form=self.form,
            idempotency_key=uuid.uuid4(),
            payload={"name": "unknown"},
            ip_hash="0" * 64,
        )
        MailDelivery.objects.create(
            submission=submission,
            kind=MailDelivery.Kind.NOTIFICATION,
            status=MailDelivery.Status.UNKNOWN,
        )

        with self.assertRaises(CommandError) as caught:
            call_command("check_contact_forms_health")

        self.assertIn("unknown_deliveries=1", str(caught.exception))

    def test_periodic_purge_records_audit_and_health_check_passes(self):
        self.form.retention_days = 1
        self.form.save(update_fields=["retention_days"])
        ContactSubmission.objects.create(
            form=self.form,
            idempotency_key=uuid.uuid4(),
            payload={"name": "expired"},
            ip_hash="0" * 64,
        )
        ContactSubmission.objects.update(
            submitted_at=timezone.now() - timedelta(days=2)
        )

        call_command(
            "run_contact_forms_maintenance",
            once=True,
            interval_seconds=86_400,
        )

        self.assertFalse(ContactSubmission.objects.exists())
        run = ContactMaintenanceRun.objects.get()
        self.assertEqual(run.status, ContactMaintenanceRun.Status.SUCCEEDED)
        self.assertEqual(run.deleted_count, 1)
        call_command("check_contact_forms_health")

    def test_health_check_detects_missing_purge_and_failed_delivery_can_be_requeued(self):
        with self.assertRaises(CommandError):
            call_command("check_contact_forms_health")

        setting = ContactPluginSetting.load()
        setting.mail_max_attempts = 1
        setting.save(update_fields=["mail_max_attempts"])
        self.client.post(self.url, self.payload())
        with patch(
            "contact_forms.mailer.EmailMessage.send",
            side_effect=RuntimeError("SMTP unavailable"),
        ):
            self.assertFalse(process_next_delivery("worker"))
        delivery = MailDelivery.objects.get()
        self.assertEqual(delivery.status, MailDelivery.Status.FAILED)

        call_command("retry_contact_mail_delivery", delivery.pk)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, MailDelivery.Status.PENDING)
        self.assertEqual(delivery.attempts, 0)
        self.assertEqual(
            ContactSubmission.objects.get().status,
            ContactSubmission.Status.RECEIVED,
        )


class AdminTests(KururuFormsTestCase):
    def staff_with_permissions(self, username, *codenames):
        user = get_user_model().objects.create_user(
            username=username,
            email=f"{username}@example.test",
            password="password",  # pragma: allowlist secret
            is_staff=True,
        )
        user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="contact_forms",
                codename__in=codenames,
            )
        )
        return user

    def test_submission_content_requires_dedicated_permission(self):
        user = get_user_model().objects.create_user(
            username="staff",
            email="staff@example.test",
            password="password",  # pragma: allowlist secret
            is_staff=True,
        )
        request = RequestFactory().get("/admin/")
        request.user = user
        model_admin = ContactSubmissionAdmin(ContactSubmission, admin.site)
        self.assertFalse(model_admin.has_module_permission(request))

    def test_missing_singleton_still_requires_add_permission(self):
        ContactPluginSetting.objects.all().delete()
        user = get_user_model().objects.create_user(
            username="settings-viewer",
            password="password",  # pragma: allowlist secret
            is_staff=True,
        )
        request = RequestFactory().get("/admin/")
        request.user = user
        model_admin = ContactPluginSettingAdmin(ContactPluginSetting, admin.site)

        self.assertFalse(model_admin.has_add_permission(request))

    def test_plugin_management_link_requires_view_permission(self):
        permitted = self.staff_with_permissions("manager", "view_contactform")
        request = RequestFactory().get("/contact/manage/")
        request.user = permitted
        response = manage(request)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse("admin:contact_forms_contactform_changelist"),
        )

        denied = self.staff_with_permissions("no-form-access")
        request.user = denied
        with self.assertRaises(PermissionDenied):
            manage(request)

    def test_duplicate_is_inactive_and_copies_fields(self):
        model_admin = ContactFormAdmin(ContactForm, admin.site)
        request = RequestFactory().post("/admin/")
        request.user = self.staff_with_permissions(
            "duplicator", "view_contactform", "add_contactform"
        )
        with patch.object(model_admin, "message_user"):
            model_admin.duplicate_forms(
                request,
                ContactForm.objects.filter(pk=self.form.pk),
            )
        clone = ContactForm.objects.exclude(pk=self.form.pk).get()
        self.assertFalse(clone.is_active)
        self.assertEqual(clone.fields.count(), self.form.fields.count())

    def test_view_only_staff_cannot_see_or_execute_mutating_actions(self):
        user = self.staff_with_permissions("viewer", "view_contactform")
        request = RequestFactory().post("/admin/")
        request.user = user
        model_admin = ContactFormAdmin(ContactForm, admin.site)

        actions = model_admin.get_actions(request)
        self.assertNotIn("duplicate_forms", actions)
        self.assertNotIn("archive_forms", actions)

        queryset = ContactForm.objects.filter(pk=self.form.pk)
        with self.assertRaises(PermissionDenied):
            model_admin.duplicate_forms(request, queryset)
        with self.assertRaises(PermissionDenied):
            model_admin.archive_forms(request, queryset)

        self.form.refresh_from_db()
        self.assertTrue(self.form.is_active)
        self.assertFalse(self.form.is_archived)
        self.assertEqual(ContactForm.objects.count(), 1)

    def test_action_visibility_matches_required_permissions(self):
        model_admin = ContactFormAdmin(ContactForm, admin.site)

        duplicate_request = RequestFactory().get("/admin/")
        duplicate_request.user = self.staff_with_permissions(
            "adder", "view_contactform", "add_contactform"
        )
        self.assertIn("duplicate_forms", model_admin.get_actions(duplicate_request))
        self.assertNotIn("archive_forms", model_admin.get_actions(duplicate_request))

        archive_request = RequestFactory().get("/admin/")
        archive_request.user = self.staff_with_permissions(
            "changer", "change_contactform"
        )
        self.assertNotIn("duplicate_forms", model_admin.get_actions(archive_request))
        self.assertIn("archive_forms", model_admin.get_actions(archive_request))


class InvalidSubmissionRedisplayTests(KururuFormsTestCase):
    """入力エラーのとき、元のページで入力値と項目別エラーを出し直せるか。

    以前は共通の失敗メッセージを1つ出してリダイレクトするだけだったので、
    利用者は「どの項目がなぜ駄目か」も「さっき書いた本文」も失っていた。
    長い問い合わせ文を書き直させるのは、送信をあきらめさせるのとほぼ同じ。
    """

    def setUp(self):
        super().setUp()
        self.article = create_article(
            title="問い合わせフォーム付きの記事",
            blocks=[{"type": BLOCK_NAME, "data": {"form_id": self.form.pk}}],
        )
        self.page_path = self.article.get_absolute_url()

    def submit_invalid(self, **overrides):
        data = self.payload(
            _render_token=make_render_token(self.form.pk, self.page_path),
            email="not-an-email",
        )
        data.update(overrides)
        return self.client.post(self.url, data)

    def test_entered_values_and_field_errors_come_back_on_the_page(self):
        response = self.submit_invalid(name="山田太郎")
        self.assertRedirects(response, self.page_path, fetch_redirect_response=False)

        html = self.client.get(self.page_path).content.decode()
        prefix = f"kururu-form-{self.form.pk}-1"

        # 入力値が残っている（書き直させない）。
        self.assertIn('value="山田太郎"', html)
        self.assertIn('value="not-an-email"', html)
        # どの項目が駄目かが分かる。
        self.assertIn(f'id="{prefix}-email-errors"', html)
        self.assertIn('class="errorlist"', html)
        # 支援技術にも入力欄とエラー文の対応が伝わる。
        self.assertIn('aria-invalid="true"', html)
        self.assertIn(f'aria-describedby="{prefix}-email-errors"', html)
        # 問題のない項目にはエラー印を付けない。
        self.assertNotIn(f'id="{prefix}-name-errors"', html)

    def test_values_are_shown_once_and_then_cleared(self):
        self.submit_invalid()
        self.assertIn("not-an-email", self.client.get(self.page_path).content.decode())
        # 2回目の表示には残さない。リロードで古い入力が復活すると混乱する。
        self.assertNotIn(
            "not-an-email", self.client.get(self.page_path).content.decode()
        )

    def test_secrets_are_not_carried_over(self):
        submitted_token = make_render_token(self.form.pk, self.page_path)
        self.client.post(
            self.url,
            self.payload(_render_token=submitted_token, email="not-an-email"),
        )
        html = self.client.get(self.page_path).content.decode()
        # 使い終わった署名トークンは持ち越さず、描画のたびに作り直す。
        self.assertNotIn(submitted_token, html)
        # ハニーポットに値を書き戻すと、正規の利用者がボット判定される。
        self.assertIn('name="_company" tabindex="-1"', html)

    def test_values_do_not_leak_into_a_different_form(self):
        other = ContactForm.objects.create(
            name="別のフォーム",
            slug="other",
            recipient_email="owner@example.test",
        )
        ContactField.objects.create(
            form=other, key="name", label="お名前",
            kind=ContactField.Kind.TEXT, required=True, order=1,
        )
        other.is_active = True
        other.save(update_fields=["is_active"])
        other_article = create_article(
            title="別フォームの記事",
            blocks=[{"type": BLOCK_NAME, "data": {"form_id": other.pk}}],
        )

        self.submit_invalid(name="山田太郎")
        html = self.client.get(other_article.get_absolute_url()).content.decode()
        self.assertNotIn("山田太郎", html)
        # 預けたままのものは、本来のフォームの側でちゃんと使える。
        self.assertIn("山田太郎", self.client.get(self.page_path).content.decode())

    def test_oversized_input_falls_back_to_the_shared_message(self):
        """セッションを太らせない。上限を超えたら預けず、従来どおりの案内にする。"""
        self.form.fields.filter(key="message").update(max_length=MAX_TOTAL_CHARS * 2)
        self.submit_invalid(message="あ" * (MAX_TOTAL_CHARS + 1))
        html = self.client.get(self.page_path).content.decode()
        self.assertNotIn("not-an-email", html)
        self.assertIn(self.form.error_message, html)

    def test_values_come_back_to_the_form_that_was_submitted(self):
        """同じフォームを2つ置いた記事で、送った側にだけ入力値が戻ること。

        戻り先を「フォームのID」だけで決めると、2つ目を送っても
        1つ目に入力値とエラーが出る。利用者はどちらを直せばよいのか分からない。
        """
        article = create_article(
            title="同じフォームを2つ置いた記事",
            blocks=[
                {"type": BLOCK_NAME, "data": {"form_id": self.form.pk}},
                {"type": BLOCK_NAME, "data": {"form_id": self.form.pk}},
            ],
        )
        path = article.get_absolute_url()
        self.client.post(
            self.url,
            self.payload(
                _render_token=make_render_token(self.form.pk, path, 2),
                name="2つ目から送った",
                email="not-an-email",
            ),
        )

        sections = re.findall(
            r'<section class="kururu-form".*?</section>',
            self.client.get(path).content.decode(),
            re.S,
        )
        self.assertEqual(len(sections), 2)
        self.assertNotIn("2つ目から送った", sections[0])
        self.assertIn("2つ目から送った", sections[1])
        self.assertNotIn("aria-invalid", sections[0])
        self.assertIn('aria-invalid="true"', sections[1])

    def test_values_wait_for_their_own_placement(self):
        """送信元の配置が無いページでは取り出さず、戻ってきたときに渡す。"""
        two_forms = create_article(
            title="2つ置いた記事",
            blocks=[
                {"type": BLOCK_NAME, "data": {"form_id": self.form.pk}},
                {"type": BLOCK_NAME, "data": {"form_id": self.form.pk}},
            ],
        )
        two_path = two_forms.get_absolute_url()
        self.client.post(
            self.url,
            self.payload(
                _render_token=make_render_token(self.form.pk, two_path, 2),
                name="2つ目から送った",
                email="not-an-email",
            ),
        )

        # 1つしか置いていないページでは、2つ目の配置が無いので出さない。
        self.assertNotIn(
            "2つ目から送った", self.client.get(self.page_path).content.decode()
        )
        # 元のページへ戻れば、2つ目にちゃんと出る。
        self.assertIn("2つ目から送った", self.client.get(two_path).content.decode())

    def test_stale_values_are_dropped_instead_of_reappearing_later(self):
        """時間が経った入力値は復活させない。"""
        request = RequestFactory().post("/")
        request.session = {}
        data = QueryDict(mutable=True)
        data["name"] = "山田"
        self.assertTrue(remember_invalid_submission(request, self.form, data))

        request.session[SESSION_KEY]["at"] -= MAX_AGE_SECONDS + 1
        self.assertIsNone(pop_invalid_submission(request, self.form.pk))
        self.assertNotIn(SESSION_KEY, request.session)

    def test_render_token_instance_is_bounded(self):
        """署名済みでも、instance の値はそのまま信用しない。"""
        for value in (0, -3, 10_000, "x", None):
            with self.subTest(value=value):
                # 0 は「配置を特定できない」。1 に丸めると、
                # 無関係な1つ目のフォームに入力値が出てしまう。
                self.assertEqual(safe_instance(value), 0)
        self.assertEqual(safe_instance(2), 2)

    def test_unidentified_placement_never_lands_on_another_form(self):
        """配置が特定できない入力値は、どのフォームにも出さない。"""
        request = RequestFactory().post("/")
        request.session = {}
        data = QueryDict(mutable=True)
        data["name"] = "配置不明の入力"
        self.assertTrue(
            remember_invalid_submission(
                request, self.form, data, UNKNOWN_INSTANCE
            )
        )
        for instance in (1, 2, 3):
            with self.subTest(instance=instance):
                self.assertIsNone(
                    pop_invalid_submission(request, self.form.pk, instance)
                )

    def test_render_token_instance_is_clamped_when_loaded(self):
        """署名済みのトークンでも、範囲外の instance はそのまま使わない。

        署名があるので外部から改ざんはできないが、値の妥当性と
        署名の正しさは別の話。範囲を外れたものは既定値へ丸める。
        """
        token = signing.dumps(
            {
                "form_id": self.form.pk,
                "instance": 10_000,
                "idempotency_key": str(uuid.uuid4()),
                "return_path": "/articles/example/",
                "shown_at": int(timezone.now().timestamp()),
            },
            salt=SIGNING_NAMESPACE,
            compress=True,
        )
        self.assertEqual(load_render_token(token, self.form.pk, 0)["instance"], 0)

    def test_multiple_choice_values_survive_the_round_trip(self):
        """チェックボックスの複数選択が1つに潰れないこと。"""
        ContactField.objects.create(
            form=self.form, key="topics", label="ご興味",
            kind=ContactField.Kind.CHECKBOX, options=["料金", "導入支援"], order=4,
        )
        request = RequestFactory().post("/")
        request.session = {}
        data = QueryDict(mutable=True)
        data["name"] = "山田"
        data.setlist("topics", ["料金", "導入支援"])

        self.assertTrue(remember_invalid_submission(request, self.form, data))
        restored = pop_invalid_submission(request, self.form.pk)
        self.assertEqual(restored.getlist("topics"), ["料金", "導入支援"])
        # 取り出したら消す（2回目は None）。
        self.assertIsNone(pop_invalid_submission(request, self.form.pk))


class FormIdentityTests(KururuFormsTestCase):
    """1ページに複数のフォームを置いても HTML の id が衝突しないか。

    Django の既定では項目名から id が決まるため、`email` を持つフォームを
    2つ置くと両方が id_email になる。ラベルをクリックしても
    1つ目の入力欄にフォーカスが移り、支援技術も対応を取り違える。
    """

    def render_page(self, *form_ids):
        article = create_article(
            title="フォームを並べた記事",
            blocks=[
                {"type": BLOCK_NAME, "data": {"form_id": form_id}}
                for form_id in form_ids
            ],
        )
        return self.client.get(article.get_absolute_url()).content.decode()

    def form_ids(self, html):
        return [
            value
            for value in re.findall(r'\sid="([^"]+)"', html)
            if value.startswith("kururu-form")
        ]

    def test_two_placements_of_the_same_form_get_unique_ids(self):
        html = self.render_page(self.form.pk, self.form.pk)

        ids = self.form_ids(html)
        expected = 2 * (1 + self.form.fields.count())  # 見出し + 各入力欄
        self.assertEqual(len(ids), expected)
        self.assertEqual(len(ids), len(set(ids)), f"id が重複しています: {ids}")

    def test_every_label_points_at_an_existing_input(self):
        html = self.render_page(self.form.pk, self.form.pk)

        ids = set(self.form_ids(html))
        targets = [
            value
            for value in re.findall(r'\sfor="([^"]+)"', html)
            if value.startswith("kururu-form")
        ]
        self.assertEqual(len(targets), 2 * self.form.fields.count())
        for target in targets:
            self.assertIn(target, ids)
        # 参照先が全部ばらばら＝それぞれ自分の入力欄を指している。
        self.assertEqual(len(targets), len(set(targets)))

    def test_section_headings_are_addressable_individually(self):
        html = self.render_page(self.form.pk, self.form.pk)
        labelled_by = re.findall(r'aria-labelledby="([^"]+)"', html)
        self.assertEqual(len(labelled_by), 2)
        self.assertEqual(len(set(labelled_by)), 2)
        for value in labelled_by:
            self.assertIn(f'id="{value}"', html)

    def test_submitted_field_names_stay_the_same(self):
        """name は分けない。送信先URLがフォームごとに違うので取り違えない。"""
        html = self.render_page(self.form.pk, self.form.pk)
        sections = re.findall(
            r'<section class="kururu-form".*?</section>', html, re.S
        )
        self.assertEqual(len(sections), 2)
        for section in sections:
            self.assertEqual(section.count('name="email"'), 1)
