"""入力エラーのとき、元のページで入力値とエラーを出し直すための受け渡し。

送信は POST → リダイレクト → GET（PRG）にしている。二重送信を避けるためで、
これ自体は変えたくない。ただ素直にリダイレクトすると、入力値もエラーも
リクエストの終わりに消えるので、利用者には「送信できませんでした」という
一文だけが残る。どの項目が悪いのか分からない。

かといって入力内容を URL のクエリに載せて戻すのは避ける。問い合わせ本文は
個人情報を含みうるのに、URL はブラウザ履歴・リファラ・アクセスログ・
共有リンクへそのまま残ってしまう。

そこでサーバー側のセッションへ1回だけ預ける。取り出したら必ず捨てる。
"""

from __future__ import annotations

import time

SESSION_KEY = "kururu_forms:pending"

# セッションへ載せる入力値の上限。
# 問い合わせ本文は長くなりうるが、セッションは全ページのリクエストで
# 読み書きされるので、際限なく太らせない。超えたら預けない
# （利用者にはこれまで通り共通のエラーメッセージだけが出る）。
MAX_TOTAL_CHARS = 20_000

# 預けた入力値の寿命。
# 送信元の配置が見つからないと預けたまま残るので、時間で必ず切る。
# 何時間も経ってから前の入力が復活すると、利用者には理由が分からない。
MAX_AGE_SECONDS = 30 * 60


def _field_keys(contact_form) -> list[str]:
    return [field.key for field in contact_form.fields.all()]


def remember_invalid_submission(request, contact_form, post_data, instance=1) -> bool:
    """入力値をセッションへ1回分だけ預ける。預けられたら True。

    フォームの項目として定義されているキーだけを拾う。
    ハニーポットや署名トークン、CSRF トークンは持ち越さない
    （トークンは描画時に必ず作り直す）。
    """
    session = getattr(request, "session", None)
    if session is None:
        return False

    values = {
        key: [str(value)[:MAX_TOTAL_CHARS] for value in post_data.getlist(key)]
        for key in _field_keys(contact_form)
        if key in post_data
    }
    total = sum(len(value) for entries in values.values() for value in entries)
    if not values or total > MAX_TOTAL_CHARS:
        return False

    session[SESSION_KEY] = {
        "form_id": contact_form.pk,
        "instance": int(instance),
        "values": values,
        "at": int(time.time()),
    }
    return True


def pop_invalid_submission(request, form_id: int, instance: int = 1):
    """預けた入力値を取り出す。取り出したら消す。無ければ None。

    `instance` は「ページ内で何個目の配置か」。送信元と同じ配置のときだけ
    取り出す。違う配置なら消さずに残し、本来の配置が描画されたときに渡す。
    """
    session = getattr(request, "session", None)
    if session is None:
        return None
    pending = session.get(SESSION_KEY)
    if not isinstance(pending, dict):
        return None

    if int(time.time()) - int(pending.get("at", 0)) > MAX_AGE_SECONDS:
        del session[SESSION_KEY]
        return None

    if pending.get("form_id") != form_id:
        return None
    if int(pending.get("instance", 1)) != int(instance):
        # 送信元ではない配置。ここで消すと、本来の配置に何も残らない。
        return None

    del session[SESSION_KEY]
    values = pending.get("values")
    if not isinstance(values, dict):
        return None

    # QueryDict と同じく「1キーに複数値」を扱える形へ戻す。
    # チェックボックスの複数選択がここで潰れると、
    # エラーを直した利用者の選択が勝手に消える。
    from django.http import QueryDict

    restored = QueryDict(mutable=True)
    for key, entries in values.items():
        if isinstance(entries, list):
            restored.setlist(key, [str(entry) for entry in entries])
    return restored
