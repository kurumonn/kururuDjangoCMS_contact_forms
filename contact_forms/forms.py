from __future__ import annotations

from django import forms
from django.core.validators import RegexValidator

from .models import ContactField

PHONE = RegexValidator(r"^[0-9+()\- .]{5,40}$", "電話番号の形式が正しくありません。")


def build_submission_form(contact_form, data=None):
    fields = {}
    source_fields = list(contact_form.fields.all())
    for item in source_fields:
        kwargs = {"label": item.label, "required": item.required}
        if item.kind == ContactField.Kind.EMAIL:
            field = forms.EmailField(max_length=min(item.max_length, 320), **kwargs)
        elif item.kind == ContactField.Kind.TEL:
            field = forms.CharField(max_length=item.max_length, validators=[PHONE], **kwargs)
        elif item.kind == ContactField.Kind.TEXTAREA:
            field = forms.CharField(max_length=item.max_length, widget=forms.Textarea, **kwargs)
        elif item.kind == ContactField.Kind.NUMBER:
            field = forms.DecimalField(max_digits=18, decimal_places=4, **kwargs)
        elif item.kind == ContactField.Kind.DATE:
            field = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **kwargs)
        elif item.kind == ContactField.Kind.SELECT:
            field = forms.ChoiceField(choices=[(x, x) for x in item.options], **kwargs)
        elif item.kind == ContactField.Kind.RADIO:
            field = forms.ChoiceField(
                choices=[(x, x) for x in item.options], widget=forms.RadioSelect, **kwargs
            )
        elif item.kind == ContactField.Kind.CHECKBOX:
            field = forms.MultipleChoiceField(
                choices=[(x, x) for x in item.options],
                widget=forms.CheckboxSelectMultiple,
                **kwargs,
            )
        elif item.kind == ContactField.Kind.CONSENT:
            field = forms.BooleanField(**kwargs)
        else:
            field = forms.CharField(max_length=item.max_length, **kwargs)
        fields[item.key] = field

    dynamic = type("KururuContactForm", (forms.Form,), fields)
    instance = dynamic(data=data)
    instance.contact_fields = source_fields
    return instance


def serializable_payload(form):
    result = {}
    for key, value in form.cleaned_data.items():
        if isinstance(value, list):
            result[key] = [str(item) for item in value]
        elif isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        else:
            result[key] = str(value)
    return result
