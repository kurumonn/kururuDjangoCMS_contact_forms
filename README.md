# Kururu Forms

Kururu Formsは、KururuCMS向けの再利用可能なDjango問い合わせフォームプラグインです。
プラグインコードはCMS本体と分離し、Python wheelとしてデプロイします。管理画面から
行うのは、インストール済みプラグインの有効化、フォーム作成、記事への配置だけです。

管理画面からpip、GitHub URL、任意Python、任意HTMLを実行する機能はありません。
管理画面の侵害を任意コード実行へ直結させないための意図的な境界です。

## MVPでできること

- 複数フォーム、標準・資料請求・採用応募・空フォームのプリセット
- フォーム複製、有効化、アーカイブ
- 記事ブロックのプルダウンからフォームを選択
- DBへ保存してから管理者通知と自動返信を送信
- メール失敗履歴と再送回数の記録
- CSRF、署名付き表示時刻、ハニーポット、64KiB上限
- IP・フォーム単位のレート制限、HMAC-SHA256によるIPハッシュ
- 問い合わせ本文の専用閲覧権限、フォームごとの保存期限削除

ファイル添付、CSV出力画面、Turnstile、Webhookは将来段階です。CSV用の独立権限だけは
先に定義しています。

## 対応するCMS

KururuCMS側にcms_plugins API v1が必要です。現時点ではDjangoCMSの
codex/plugin-framework-20260830ブランチと組み合わせます。

## 開発環境への導入

CMSとこのリポジトリを別々にcloneします。

    python -m pip install --no-deps --editable C:\path\to\kururuDjangoCMS_contact_forms
    $env:KURURU_PLUGIN_PACKAGES = "contact_forms"
    $env:KURURU_FORMS_IP_HASH_KEY = python -c "import secrets; print(secrets.token_urlsafe(48))"
    python manage.py migrate
    python manage.py check
    python manage.py runserver

KURURU_PLUGIN_PACKAGESはPythonパッケージ名ではなく、
kururucms.plugins entry point名です。ここに無いパッケージは、環境へ
インストールされていてもCMSからimportされません。

メールはCMS側のDEFAULT_FROM_EMAILとメールバックエンドを使います。
送信者のメールアドレスはFromではなくReply-Toへ設定されます。

## 本番イメージへの導入

実行中コンテナでpipを実行せず、wheelを作ってイメージへ固定します。

    python -m build --wheel
    python -m pip hash dist\kururucms_contact_forms-0.1.0-py3-none-any.whl

生成したwheelをCMSのplugin_wheels/に置き、CMS側の
plugin-requirements.lockへ、表示されたSHA-256を付けて追記します。
CMS本体にない追加依存を使う場合は、その推移的依存もすべて版とhashを固定した
独立行として同じlockへ記録します。

    kururucms-contact-forms==0.1.0 --hash=sha256:<pip hashの値>

イメージを再ビルドし、次の環境変数をsecret管理下で設定します。

    KURURU_PLUGIN_PACKAGES=contact_forms
    KURURU_FORMS_IP_HASH_KEY=<32文字以上の専用ランダム値>

その後にmigrateとcheck --deployを実行します。管理画面の
「CMSプラグイン」からkururu_formsを有効にし、
「問い合わせフォーム」でフォームを作成します。

## Python/Django実装の要点

pyproject.tomlのentry pointがDjangoのAppConfigを公開します。
CMSはデプロイ時の許可リストと一致したentry pointだけを
INSTALLED_APPSへ加えます。ContactFormsConfig.ready()はAPI v1の
PluginDefinitionを登録し、URLと記事ブロックをCMSへ通知します。

フォーム項目はContactFieldからDjango Formをサーバー側で動的生成します。
ブラウザの入力制約だけに依存せず、型、必須、選択肢、文字数をPOST時に再検証します。
送信内容はContactSubmissionへatomicに保存した後、MailDeliveryを作って
メールを送信します。SMTP例外には宛先が含まれることがあるため、保存するエラーは
例外クラス名だけです。

## テスト

CMSリポジトリをPython pathへ含めて実行します。

    $env:PYTHONPATH = "C:\path\to\kururuDjangoCMS_contact_forms;C:\path\to\DjangoCMS"
    python C:\path\to\DjangoCMS\manage.py test tests.test_contact_forms --settings=tests.settings
