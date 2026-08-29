from django.core.exceptions import ValidationError
from django.urls import reverse

from cms_plugins.registry import EditorField, PluginBlock, PluginDefinition

from . import __version__
from .forms import build_submission_form
from .models import ContactForm
from .services import make_render_token

PLUGIN_KEY = "kururu_forms"
BLOCK_NAME = "kururu_forms.contact_form"


def form_choices():
    return [
        {"value": item.pk, "label": item.name}
        for item in ContactForm.objects.filter(
            is_active=True,
            is_archived=False,
            fields__isnull=False,
        )
        .distinct()
        .order_by("name")
    ]


def validate_block(data):
    try:
        form_id = int(data.get("form_id"))
    except (TypeError, ValueError):
        raise ValidationError("問い合わせフォームを選択してください。")
    if form_id <= 0:
        raise ValidationError("問い合わせフォームを選択してください。")
    return {"form_id": form_id}


def block_context(request, data):
    if request is None:
        return {"contact_form": None}
    contact_form = (
        ContactForm.objects.filter(
            pk=data.get("form_id"),
            is_active=True,
            is_archived=False,
            fields__isnull=False,
        )
        .distinct()
        .prefetch_related("fields")
        .first()
    )
    if contact_form is None:
        return {"contact_form": None}
    return {
        "contact_form": contact_form,
        "submission_form": build_submission_form(contact_form),
        "render_token": make_render_token(contact_form.pk, request.get_full_path()),
        "submit_url": reverse("kururu_forms:submit", args=[contact_form.slug]),
    }


definition = PluginDefinition(
    key=PLUGIN_KEY,
    api_version=1,
    name="Kururu Forms",
    version=__version__,
    description="DB先行保存とスパム対策を備えた問い合わせフォーム",
    urlconf="contact_forms.urls",
    url_prefix="contact",
    blocks=(
        PluginBlock(
            name=BLOCK_NAME,
            label="問い合わせフォーム",
            validate=validate_block,
            template_name="contact_forms/block.html",
            editor_fields=(
                EditorField(
                    key="form_id",
                    label="フォーム",
                    type="select",
                    choices_provider=form_choices,
                ),
            ),
            context_provider=block_context,
        ),
    ),
)
