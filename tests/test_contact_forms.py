from unittest.mock import patch

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from blog.blocks import block_editor_catalog, validate_blocks
from blog.templatetags.block_tags import render_blocks
from cms_plugins.models import PluginActivation

from contact_forms.forms import build_submission_form
from contact_forms.admin import ContactFormAdmin, ContactSubmissionAdmin
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
    def test_submission_is_stored_before_notification_and_uses_fixed_from(self):
        response = self.client.post(self.url, self.payload(), REMOTE_ADDR="198.51.100.10")
        self.assertRedirects(response, "/articles/example/", fetch_redirect_response=False)
        submission = ContactSubmission.objects.get()
        self.assertEqual(submission.payload["email"], "reader@example.test")
        self.assertNotEqual(submission.ip_hash, "198.51.100.10")
        self.assertEqual(submission.status, ContactSubmission.Status.DELIVERED)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[0].from_email, "forms@example.test")
        self.assertEqual(mail.outbox[0].reply_to, ["reader@example.test"])

    def test_mail_failure_does_not_lose_submission_or_store_exception_text(self):
        with patch("contact_forms.mailer.EmailMessage.send", side_effect=RuntimeError("reader@example.test")):
            response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 302)
        submission = ContactSubmission.objects.get()
        self.assertEqual(submission.status, ContactSubmission.Status.MAIL_FAILED)
        delivery = MailDelivery.objects.get(kind=MailDelivery.Kind.NOTIFICATION)
        self.assertEqual(delivery.last_error, "RuntimeError")
        self.assertNotIn("reader@", delivery.last_error)

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
        with patch.object(model_admin, "message_user"):
            model_admin.duplicate_forms(
                RequestFactory().post("/admin/"),
                ContactForm.objects.filter(pk=self.form.pk),
            )
        clone = ContactForm.objects.exclude(pk=self.form.pk).get()
        self.assertFalse(clone.is_active)
        self.assertEqual(clone.fields.count(), self.form.fields.count())
