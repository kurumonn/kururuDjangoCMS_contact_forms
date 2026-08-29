from functools import wraps

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from cms_plugins.security import require_plugin_enabled
from core.ratelimit import check_rate_limit, client_ip

from .forms import build_submission_form, serializable_payload
from .mailer import enqueue_submission
from .models import ContactForm, ContactPluginSetting, ContactSubmission
from .plugin import PLUGIN_KEY
from .services import ip_hash, load_render_token


@staff_member_required
def manage(request):
    """CMSプラグイン一覧から、権限確認後にDjango管理画面へ案内する。"""
    if not request.user.has_perm("contact_forms.view_contactform"):
        raise PermissionDenied
    return redirect("admin:contact_forms_contactform_changelist")


def post_size_limit(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        plugin_setting = ContactPluginSetting.load()
        try:
            content_length = int(request.META.get("CONTENT_LENGTH", ""))
        except (TypeError, ValueError):
            return HttpResponseBadRequest("content length required")
        if content_length > plugin_setting.max_post_bytes:
            return HttpResponseBadRequest("request too large")
        return view(request, *args, **kwargs)
    return wrapped


@require_POST
@post_size_limit
@csrf_protect
@require_plugin_enabled(PLUGIN_KEY)
def submit(request, slug):
    contact_form = get_object_or_404(
        ContactForm.objects.filter(fields__isnull=False)
        .distinct()
        .prefetch_related("fields"),
        slug=slug,
        is_active=True,
        is_archived=False,
    )
    plugin_setting = ContactPluginSetting.load()

    if request.POST.get("_company"):
        return HttpResponseBadRequest("invalid request")
    try:
        token_data = load_render_token(
            request.POST.get("_render_token", ""),
            contact_form.pk,
            plugin_setting.minimum_fill_seconds,
        )
    except signing.BadSignature:
        return HttpResponseBadRequest("invalid form token")

    hashed_ip = ip_hash(client_ip(request))
    limit = check_rate_limit(
        f"kururu-forms:ip:{hashed_ip}",
        limit=plugin_setting.rate_limit,
        window_seconds=plugin_setting.rate_window_seconds,
    )
    form_limit = check_rate_limit(
        f"kururu-forms:form:{contact_form.pk}:{hashed_ip}",
        limit=plugin_setting.rate_limit,
        window_seconds=plugin_setting.rate_window_seconds,
    )
    if not limit.allowed or not form_limit.allowed:
        response = HttpResponseBadRequest("rate limit exceeded")
        response["Retry-After"] = str(max(limit.retry_after, form_limit.retry_after))
        return response

    submitted = build_submission_form(contact_form, request.POST)
    if not submitted.is_valid():
        messages.error(request, contact_form.error_message)
        return HttpResponseRedirect(token_data["return_path"])

    with transaction.atomic():
        submission, created = ContactSubmission.objects.get_or_create(
            idempotency_key=token_data["idempotency_key"],
            defaults={
                "form": contact_form,
                "payload": serializable_payload(submitted),
                "ip_hash": hashed_ip,
                "user_agent": request.META.get("HTTP_USER_AGENT", "")[:200],
                "page_path": token_data["return_path"],
            },
        )
        if created:
            enqueue_submission(submission)
    messages.success(request, contact_form.success_message)
    return HttpResponseRedirect(token_data["return_path"])
