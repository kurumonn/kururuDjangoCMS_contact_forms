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
- DBへ問い合わせとOutboxを同一トランザクションで保存
- 独立ワーカーによる指数バックオフ付きメール配送
- 明示的SMTP失敗の指数再試行と、結果不明配送の自動再送禁止・手動解決
- CSRF、署名付き表示時刻、ハニーポット、64KiB上限
- IP・フォーム単位のレート制限、HMAC-SHA256によるIPハッシュ
- 問い合わせ本文の専用閲覧権限、フォームごとの保存期限削除

ファイル添付、CSV出力画面、Turnstile、Webhookは将来段階です。CSV用の独立権限だけは
先に定義しています。

## 対応するCMS

KururuCMS側にcms_plugins API v1が必要です。0.2.2のCIと修正検証では、
CMSコミット`4aa9c87c30a3adea65e66cfad83f96b79e521e61`へ固定しています。

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
    python -m pip hash dist\kururucms_contact_forms-0.2.2-py3-none-any.whl

生成したwheelをCMSのplugin_wheels/に置き、CMS側の
plugin-requirements.lockへ、表示されたSHA-256を付けて追記します。
CMS本体にない追加依存を使う場合は、その推移的依存もすべて版とhashを固定した
独立行として同じlockへ記録します。

    kururucms-contact-forms==0.2.2 --hash=sha256:<pip hashの値>

イメージを再ビルドし、次の環境変数をsecret管理下で設定します。

    KURURU_PLUGIN_PACKAGES=contact_forms
    KURURU_FORMS_IP_HASH_KEY=<32文字以上の専用ランダム値>

その後にmigrateとcheck --deployを実行します。管理画面の
「CMSプラグイン」からkururu_formsを有効にし、
「問い合わせフォーム」でフォームを作成します。

CMS統合ブランチのCompose構成では、プラグインをwheelへ固定した後に
contact-forms profileを明示して、Webとは別のOutboxワーカーと
保存期限メンテナンスを起動します。

    docker compose --profile contact-forms up -d --build

HTTPの送信処理はSMTPへ接続しません。問い合わせと管理者通知OutboxをDBへ保存して
リダイレクトし、次の常駐コマンドだけがSMTPへ接続します。

    python manage.py process_contact_mail_outbox --poll-seconds 5

管理者通知が成功した後にだけ自動返信Outboxを作ります。
管理者通知が最大試行回数まで失敗した場合、自動返信は既定では送信しません。
宛先、件名、本文、Reply-Toは受付時に問い合わせ行へスナップショットします。
送信待ちの間にフォーム設定を変更しても、既に受け付けたメールの配送内容は変わりません。

## 保存期限と監視

次の常駐プロセスは起動直後と以後24時間ごとに期限切れ問い合わせを削除し、
実行結果、削除件数、例外の型をContactMaintenanceRunへ記録します。

    python manage.py run_contact_forms_maintenance --interval-seconds 86400

監視では次のコマンドを実行します。失敗・結果不明配送、30分以上滞留したOutbox、
36時間以内に成功した削除実行がない場合は終了コード1になります。

    python manage.py check_contact_forms_health

監視基盤はこの終了コードとDocker health statusを通知対象にしてください。
一時的なSMTP障害を復旧した後は、管理画面で配送IDを確認し、次のように
失敗した1件だけを送信待ちへ戻します。

    python manage.py retry_contact_mail_delivery <delivery_id>

DB保存直後にプロセスが停止し、配送行だけが作られなかった旧状態は次で再構築できます。

    python manage.py reconcile_contact_mail_outbox

削除処理の失敗は原因を解消してから手動実行できます。成功すれば新しい監査記録が残ります。

    python manage.py purge_contact_submissions

SMTPには「送信」とDBの「送信済み更新」を同一トランザクションにする仕組みがありません。
そのため、SMTPが受理した直後かつDB更新前にOSごと停止した配送は`unknown`へ隔離し、
自動再送しません。配送事業者のログと固定Message-IDを照合し、受理済みなら送信済み確定、
未受理と判断して再送する場合だけ重複リスクを明示確認します。

    python manage.py resolve_contact_mail_delivery <delivery_id> --action mark-sent
    python manage.py resolve_contact_mail_delivery <delivery_id> --action retry --confirm-duplicate-risk

HTTPの重複POSTは署名トークン内のUUIDとDB一意制約で1件へ収束します。

## Python/Django実装の要点

pyproject.tomlのentry pointがDjangoのAppConfigを公開します。
CMSはデプロイ時の許可リストと一致したentry pointだけを
INSTALLED_APPSへ加えます。ContactFormsConfig.ready()はAPI v1の
PluginDefinitionを登録し、URLと記事ブロックをCMSへ通知します。

フォーム項目はContactFieldからDjango Formをサーバー側で動的生成します。
ブラウザの入力制約だけに依存せず、型、必須、選択肢、文字数をPOST時に再検証します。
送信内容と管理者通知MailDeliveryは同一DBトランザクションで保存します。
ワーカーは送信可能時刻を確認し、明示的なSMTP例外だけを指数バックオフで再試行します。
失効した処理中leaseは配送結果不明として隔離し、運用者の判断なしには再送しません。
SMTP例外には宛先が含まれることがあるため、保存するエラーは例外クラス名だけです。

## テスト

CMSリポジトリをPython pathへ含めて実行します。

    $env:PYTHONPATH = "C:\path\to\kururuDjangoCMS_contact_forms;C:\path\to\DjangoCMS"
    python C:\path\to\DjangoCMS\manage.py test tests.test_contact_forms --settings=tests.settings

CIは上記テストに加え、マイグレーション差分、check --deploy、Bandit、
pip-audit、detect-secrets、wheelビルド、隔離venvへのインストール、
kururucms.plugins entry pointと同梱ファイルを検証します。
