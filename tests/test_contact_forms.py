from datetime import timedelta
from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from blog.blocks import block_editor_catalog, validate_blocks
from blog.templatetags.block_tags import render_blocks
from cms_plugins.models import PluginActivation

from contact_forms.forms import build_submission_form
from contact_forms.admin import ContactFormAdmin, ContactSubmissionAdmin
from contact_forms.mailer import process_next_delivery
from contact_forms.models import (
    ContactField,
    ContactForm,
    ContactPluginSetting,
    ContactSubmission,
    MailDelivery,
)
from contact_forms.plugin import BLOCK_NAME, PLUGIN_KEY
from contact_forms.services import ip_hash, make_render_token, safe_return_path


class KururuFormsTestCase(TestCase):
    def setUp(self):
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
            is_active=True,
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

    def test_stale_processing_delivery_is_reclaimed_after_worker_stops(self):
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

        self.assertTrue(process_next_delivery("replacement-worker", now=now))

        delivery.refresh_from_db()
        self.assertEqual(delivery.status, MailDelivery.Status.SENT)
        self.assertEqual(delivery.attempts, 2)
        self.assertEqual(len(mail.outbox), 1)

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


class AdminTests(KururuFormsTestCase):
    def staff_with_permissions(self, username, *codenames):
        user = get_user_model().objects.create_user(
            username=username,
            email=f"{username}@example.test",
            password="password",
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
            username="staff", email="staff@example.test", password="password",
            is_staff=True,
        )
        request = RequestFactory().get("/admin/")
        request.user = user
        model_admin = ContactSubmissionAdmin(ContactSubmission, admin.site)
        self.assertFalse(model_admin.has_module_permission(request))

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
