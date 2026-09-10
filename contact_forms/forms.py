from __future__ import annotations

from django import forms
from django.core.validators import RegexValidator

from .models import ContactField

PHONE = RegexValidator(r"^[0-9+()\- .]{5,40}$", "電話番号の形式が正しくありません。")


def build_submission_form(contact_form, data=None, auto_id=None):
    """フォーム1つ分の Django フォームを組み立てる。

    `auto_id` は HTML の id の付け方。既定のままだと項目名から
    `id_email` のように決まるため、同じページに問い合わせフォームを
    2つ置くと id が衝突し、ラベルをクリックしても片方の入力欄しか
    フォーカスされない。描画側が配置ごとに固有の `auto_id` を渡す。

    送信項目名（name 属性）は分けない。フォームごとに送信先URLが違い、
    送られてくるのは常に1フォーム分なので、name が同じでも取り違えない。
    HTML の id だけがページ内で一意である必要がある。
    """
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
    extra = {"auto_id": auto_id} if auto_id else {}
    instance = dynamic(data=data, **extra)
    instance.contact_fields = source_fields
    return instance


def mark_invalid_fields(form) -> None:
    """エラーになった項目を支援技術へも伝える。

    画面上は赤字のエラー文が入力欄の下に出るが、それだけだと
    スクリーンリーダーの利用者には「どの入力欄の話か」が結び付かない。
    aria-invalid と aria-describedby で入力欄とエラー文を紐付ける。
    """
    for name in form.errors:
        if name not in form.fields:
            continue
        widget = form.fields[name].widget
        widget.attrs["aria-invalid"] = "true"
        error_id = f"{form[name].auto_id}-errors"
        described_by = widget.attrs.get("aria-describedby")
        widget.attrs["aria-describedby"] = (
            f"{described_by} {error_id}" if described_by else error_id
        )


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
