# アプリ側の認証（発展問題・パッチ 0014）

当番ボードに、「パスワードだけでログインする画面」を足します。
**設定しなければ、今までと何も変わりません。** 使いたいときだけ、環境変数で有効にします。

入口のプロキシで守る BASIC認証との違いと、気をつける点は、提出資料「アプリ側の認証と BASIC認証」にまとめています。
ここには、動かし方と、コードのどこで何を守っているかを書きます。

## 有効にする手順（Raspberry Pi）

1. パスワードのハッシュと、署名用の鍵を作ります。パスワードは画面に出ません。
   ```
   cd /opt/toban
   ./venv/bin/python -m toban.auth
   ```
2. 出た2行を、`/etc/toban/auth.env` に貼ります（持ち主 root、権限 600。GitHub には載せません）。
   ```
   sudo install -m 600 -o root -g root /dev/null /etc/toban/auth.env
   sudoedit /etc/toban/auth.env
   ```
3. 必要なら、同じファイルに次の行も足します。
   ```
   TOBAN_TRUSTED_PROXIES=192.168.2.220        # 信用するプロキシ（X-Forwarded-For を読んでよい相手）
   TOBAN_TRUSTED_NETS=192.168.1.0/24          # ログインなしで入れる範囲（校内の職員用ネットワーク）
   ```
4. サービスの設定を入れ直して、再起動します（このパッチで `EnvironmentFile` の行が増えています）。
   ```
   cd /opt/toban && sudo git pull
   sudo cp deploy/toban.service /etc/systemd/system/toban.service
   sudo systemctl daemon-reload && sudo systemctl restart toban
   ```

元に戻すときは、`/etc/toban/auth.env` を消して、`sudo systemctl restart toban` します。

## 環境変数

| 名前 | 意味 | 無いとき |
|---|---|---|
| `TOBAN_PASSWORD_HASH` | パスワードのハッシュ。これがあると認証が有効になる | 認証なし（今までどおり） |
| `TOBAN_SECRET_KEY` | ログインの印の署名に使う鍵（32文字以上） | 起動を止める |
| `TOBAN_TRUSTED_NETS` | ログインなしで入れる範囲（カンマ区切り） | 全員ログインが必要 |
| `TOBAN_TRUSTED_PROXIES` | 信用するプロキシ。ここから来たときだけ X-Forwarded-For を読む | ヘッダーは読まない |
| `TOBAN_COOKIE_SECURE` | `0` で、手元の http でも Cookie を送る（初期値は https のみ） | https のみ |

設定が危ないとき（鍵が無い・短い、範囲の書き方が読めない、ハッシュではなくパスワードをそのまま書いた、
認証が無いのに範囲だけある）は、黙って動かさずに、**起動を止めます**。

## 何を守っているか

| # | 守り | コード | テスト |
|---|---|---|---|
| ① | 入力の大きさに上限（送信 64KB、パスワード 128文字） | `MAX_BODY_BYTES` `MAX_PASSWORD_LEN` | `test_overlong_password_is_refused_without_hashing` `test_huge_requests_are_rejected_early` |
| ② | 総当たりを止める（5回失敗で5分ロック、接続元ごと、表の大きさにも上限） | `Throttle` | `test_lockout_*` `test_failure_table_stays_small` |
| ③ | 重いパスワード照合を、同時に2つまで | `gate` | `test_password_check_is_limited_to_a_few_at_once` |
| ④ | X-Forwarded-For を、信用するプロキシからのときだけ、右から読む | `Auth.client_ip` | `test_forwarded_header_*` |
| ⑤ | 設定が危ないときは起動しない | `Auth.__init__` `init_auth` | `test_unsafe_settings_stop_the_app` |
| ⑥ | Cookie は HttpOnly・SameSite=Lax・Secure、12時間で切れる | `init_auth` | `test_cookie_has_protective_flags` `test_login_expires_*` `test_forged_cookie_is_refused` |
| ⑦ | 他のサイトからの送信を断る（X-Requested-With） | `_guard` | `test_changes_need_the_csrf_header_*` |
| ⑧ | ログイン後の戻り先は、自サイトの中だけ | `safe_next` | `test_login_never_redirects_outside_the_site` |
| ⑨ | パスワードもハッシュも、ログに書かない | `login` | `test_secrets_are_never_logged` |
| ⑩ | 応答ヘッダー（nosniff・DENY・no-store・CSP） | `_headers` | `test_response_headers` |

## この教材で扱わないこと（さらに進めたい人へ）

- ログアウトの画面と、ログイン済みの人の一覧
- 人ごとのIDと権限（今は全員が同じパスワード）
- 二要素認証、パスワード変更の画面
- `/healthz` は認証なしで開けます。`error` に内部の文言が出る場合があるので、認証なしでは `ok` だけを返す形にするのが次の課題です
- gunicorn の worker を増やす場合、失敗の記録（`Throttle`）は worker ごとに別になります。共有できる置き場所が要ります

## テスト

```
python -m pytest tests/test_auth.py        # 画面なし（42件）
python -m pytest tests/test_auth_ui.py     # ブラウザでログインから交代まで（1件）
```
