from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    ContactField,
    ContactPluginSetting,
    ContactSubmission,
    MailDelivery,
)

MAX_BACKOFF_SECONDS = 86_400


def _plain_body(submission):
    labels = {field.key: field.label for field in submission.form.fields.all()}
    lines = []
    for key, value in submission.payload.items():
        shown = ", ".join(value) if isinstance(value, list) else str(value)
        lines.append(f"{labels.get(key, key)}: {shown}")
    return "\n".join(lines)


def _submitter_email(submission):
    email_keys = submission.form.fields.filter(
        kind=ContactField.Kind.EMAIL
    ).values_list("key", flat=True)
    for key in email_keys:
        value = submission.payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def enqueue_submission(submission: ContactSubmission, *, now=None):
    if not submission.notification_recipient:
        form = submission.form
        submitter = _submitter_email(submission)
        submission.notification_recipient = form.recipient_email
        submission.notification_subject = form.subject
        submission.notification_body = _plain_body(submission)
        submission.notification_reply_to = submitter
        if submitter and form.autoresponder_subject and form.autoresponder_body:
            submission.autoreply_recipient = submitter
            submission.autoreply_subject = form.autoresponder_subject
            submission.autoreply_body = form.autoresponder_body
        submission.save(
            update_fields=[
                "notification_recipient",
                "notification_subject",
                "notification_body",
                "notification_reply_to",
                "autoreply_recipient",
                "autoreply_subject",
                "autoreply_body",
            ]
        )
    return MailDelivery.objects.get_or_create(
        submission=submission,
        kind=MailDelivery.Kind.NOTIFICATION,
        defaults={"available_at": now or timezone.now()},
    )[0]


def _enqueue_autoreply(submission: ContactSubmission, *, now):
    if (
        not submission.autoreply_recipient
        or not submission.autoreply_subject
        or not submission.autoreply_body
    ):
        return None
    return MailDelivery.objects.get_or_create(
        submission=submission,
        kind=MailDelivery.Kind.AUTOREPLY,
        defaults={"available_at": now},
    )[0]


def _message_for(delivery: MailDelivery):
    submission = delivery.submission
    if delivery.kind == MailDelivery.Kind.NOTIFICATION:
        return EmailMessage(
            subject=submission.notification_subject,
            body=submission.notification_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[submission.notification_recipient],
            reply_to=(
                [submission.notification_reply_to]
                if submission.notification_reply_to
                else None
            ),
        )
    if (
        not submission.autoreply_recipient
        or not submission.autoreply_subject
        or not submission.autoreply_body
    ):
        raise ValueError("autoresponse is not configured")
    return EmailMessage(
        subject=submission.autoreply_subject,
        body=submission.autoreply_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[submission.autoreply_recipient],
    )


def reconcile_missing_deliveries(*, now=None) -> int:
    """旧処理や中断で配送行がない受付済み問い合わせをOutboxへ戻す。"""
    now = now or timezone.now()
    missing = (
        ContactSubmission.objects.filter(status=ContactSubmission.Status.RECEIVED)
        .exclude(deliveries__kind=MailDelivery.Kind.NOTIFICATION)
        .select_related("form")
        .prefetch_related("form__fields")
        .order_by("pk")
    )
    count = 0
    for submission in missing.iterator(chunk_size=200):
        enqueue_submission(submission, now=now)
        count += 1
    return count


def claim_next_delivery(worker_id: str, *, now=None):
    worker_id = (worker_id or "").strip()[:100]
    if not worker_id:
        raise ValueError("worker_id is required")
    now = now or timezone.now()
    plugin_setting = ContactPluginSetting.load()
    stale_before = now - timedelta(seconds=plugin_setting.mail_lock_timeout_seconds)
    candidates = MailDelivery.objects.filter(
        Q(status=MailDelivery.Status.PENDING, available_at__lte=now)
        | Q(
            status=MailDelivery.Status.PROCESSING,
            locked_at__lte=stale_before,
        )
        | Q(
            status=MailDelivery.Status.PROCESSING,
            locked_at__isnull=True,
        )
    ).order_by("available_at", "pk")

    while True:
        with transaction.atomic():
            lock_options = {}
            if connection.features.has_select_for_update_skip_locked:
                lock_options["skip_locked"] = True
            delivery = (
                candidates.select_for_update(**lock_options)
                .select_related("submission__form")
                .first()
            )
            if delivery is None:
                return None

            # A worker can die after claiming a job but before SMTP starts. Such
            # lease expirations still count toward the configured attempt cap;
            # otherwise repeated process crashes could reclaim the same row
            # forever without ever reaching _record_failure().
            if delivery.attempts >= plugin_setting.mail_max_attempts:
                delivery.status = MailDelivery.Status.FAILED
                delivery.last_error = "WorkerLeaseExpired"
                delivery.locked_at = None
                delivery.locked_by = ""
                delivery.save(
                    update_fields=[
                        "status",
                        "last_error",
                        "locked_at",
                        "locked_by",
                    ]
                )
                if delivery.kind == MailDelivery.Kind.NOTIFICATION:
                    submission = delivery.submission
                    submission.status = ContactSubmission.Status.MAIL_FAILED
                    submission.save(update_fields=["status"])
                    if plugin_setting.autorespond_after_notification_failure:
                        _enqueue_autoreply(submission, now=now)
                continue

            delivery.status = MailDelivery.Status.PROCESSING
            delivery.attempts += 1
            delivery.locked_at = now
            delivery.locked_by = worker_id
            delivery.last_attempt_at = now
            delivery.save(
                update_fields=[
                    "status",
                    "attempts",
                    "locked_at",
                    "locked_by",
                    "last_attempt_at",
                ]
            )
            return delivery.pk


def _record_success(delivery_id: int, worker_id: str, *, now):
    with transaction.atomic():
        delivery = (
            MailDelivery.objects.select_for_update()
            .select_related("submission__form")
            .get(pk=delivery_id)
        )
        if (
            delivery.status != MailDelivery.Status.PROCESSING
            or delivery.locked_by != worker_id
        ):
            return False
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
            _enqueue_autoreply(submission, now=now)
        return True


def _record_failure(delivery_id: int, worker_id: str, exc: Exception, *, now):
    with transaction.atomic():
        delivery = (
            MailDelivery.objects.select_for_update()
            .select_related("submission__form")
            .get(pk=delivery_id)
        )
        if (
            delivery.status != MailDelivery.Status.PROCESSING
            or delivery.locked_by != worker_id
        ):
            return False
        plugin_setting = ContactPluginSetting.load()
        terminal = delivery.attempts >= plugin_setting.mail_max_attempts
        delivery.status = (
            MailDelivery.Status.FAILED if terminal else MailDelivery.Status.PENDING
        )
        if not terminal:
            delay = min(
                plugin_setting.mail_retry_base_seconds
                * (2 ** max(delivery.attempts - 1, 0)),
                MAX_BACKOFF_SECONDS,
            )
            delivery.available_at = now + timedelta(seconds=delay)
        delivery.last_error = type(exc).__name__[:500]
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
        if terminal and delivery.kind == MailDelivery.Kind.NOTIFICATION:
            submission = delivery.submission
            submission.status = ContactSubmission.Status.MAIL_FAILED
            submission.save(update_fields=["status"])
            if plugin_setting.autorespond_after_notification_failure:
                _enqueue_autoreply(submission, now=now)
        return True


def process_delivery(delivery_id: int, worker_id: str, *, now=None):
    now = now or timezone.now()
    delivery = (
        MailDelivery.objects.select_related("submission__form")
        .prefetch_related("submission__form__fields")
        .get(pk=delivery_id)
    )
    if (
        delivery.status != MailDelivery.Status.PROCESSING
        or delivery.locked_by != worker_id
    ):
        return False
    try:
        _message_for(delivery).send(fail_silently=False)
    except Exception as exc:
        _record_failure(delivery.pk, worker_id, exc, now=now)
        return False
    return _record_success(delivery.pk, worker_id, now=now)


def process_next_delivery(worker_id: str, *, now=None):
    delivery_id = claim_next_delivery(worker_id, now=now)
    if delivery_id is None:
        return None
    return process_delivery(delivery_id, worker_id, now=now)
