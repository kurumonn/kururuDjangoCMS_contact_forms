from __future__ import annotations

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone

from .models import ContactField, ContactSubmission, MailDelivery


def _plain_body(submission):
    labels = {field.key: field.label for field in submission.form.fields.all()}
    lines = []
    for key, value in submission.payload.items():
        shown = ", ".join(value) if isinstance(value, list) else str(value)
        lines.append(f"{labels.get(key, key)}: {shown}")
    return "\n".join(lines)


def _submitter_email(submission):
    email_keys = submission.form.fields.filter(kind=ContactField.Kind.EMAIL).values_list("key", flat=True)
    for key in email_keys:
        value = submission.payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _send(delivery, message):
    delivery.attempts += 1
    try:
        message.send(fail_silently=False)
    except Exception as exc:
        delivery.status = MailDelivery.Status.FAILED
        # 例外本文には宛先などが混ざることがあるため、型名だけを保存する。
        delivery.last_error = type(exc).__name__[:500]
        delivery.save(update_fields=["attempts", "status", "last_error"])
        return False
    delivery.status = MailDelivery.Status.SENT
    delivery.sent_at = timezone.now()
    delivery.last_error = ""
    delivery.save(update_fields=["attempts", "status", "sent_at", "last_error"])
    return True


def deliver_submission(submission: ContactSubmission):
    form = submission.form
    reply_to = _submitter_email(submission)
    notification, _ = MailDelivery.objects.get_or_create(
        submission=submission, kind=MailDelivery.Kind.NOTIFICATION
    )
    message = EmailMessage(
        subject=form.subject,
        body=_plain_body(submission),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[form.recipient_email],
        reply_to=[reply_to] if reply_to else None,
    )
    notified = _send(notification, message)

    if reply_to and form.autoresponder_subject and form.autoresponder_body:
        autoresponse, _ = MailDelivery.objects.get_or_create(
            submission=submission, kind=MailDelivery.Kind.AUTOREPLY
        )
        _send(
            autoresponse,
            EmailMessage(
                subject=form.autoresponder_subject,
                body=form.autoresponder_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[reply_to],
            ),
        )

    submission.status = (
        ContactSubmission.Status.DELIVERED
        if notified
        else ContactSubmission.Status.MAIL_FAILED
    )
    submission.save(update_fields=["status"])
    return notified
