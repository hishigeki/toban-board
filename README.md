# 当番ボード (TobanBoard)

鍵当番（1名）・ごみ当番（正副2名）を、1号館・2号館それぞれに自動で割り振り、ブラウザで見せるシステムです。
Raspberry Pi（旧 kyujin-proxy）の上で Flask が動き、**データはすべて Google スプレッドシート**にあります。
Pi の SD カードには何も書き込みません。

- 画面: `http://<Piのアドレス>/1`（1号館） / `/2`（2号館）。ログインなし・学内LAN専用
- 構成: Python 3.11+ / Flask / gunicorn（worker 1つ）/ gspread

## 1. スプレッドシートの準備

1つのスプレッドシートに次のシートを用意します（`当番表`・`交代ログ`・`休校日` は無ければ自動作成）。

| シート | 内容 |
|---|---|
| **職員名簿**（必須・手入力） | 1行目が見出し。`氏名` / `鍵当番` / `ごみ当番` の列が必須。担当できる人は `○`（`TRUE`・チェックボックスも可）、できない人は空欄。`館` 列（任意）に `1号館` か `2号館` を入れると、その館専用になる（空欄は両館可） |
| 休校日（任意・手入力） | `日付` `理由` の2列。ここに書いた日は当番日から外れる。土日・祝日・12/29〜1/3 は自動で除外 |
| 当番表（自動） | システムが書き込む。手で書き換えない |
| 交代ログ（自動） | 交代・再割り振りの記録 |

名簿の列の並び・追加列は自由です（見出しの文字で探します）。

## 2. Google 側（サービスアカウント）

1. Google Cloud コンソールでプロジェクトを作り、**Google Sheets API** を有効化
2. サービスアカウントを作成し、**JSONキー**をダウンロード
   - Workspace の組織ポリシーでキー作成が禁止されている場合は、管理者権限でポリシー `iam.disableServiceAccountKeyCreation` を一時的に解除する必要があります
3. スプレッドシートの「共有」に、サービスアカウントのメールアドレス（`xxx@xxx.iam.gserviceaccount.com`）を**編集者**として追加
4. スプレッドシートID（URL の `/d/` と `/edit` の間）を控える

## 3. Pi への導入

1. Raspberry Pi OS Lite（Bookworm）を SD カードに書き込み、SSH とネットワーク（固定IP推奨）を設定
2. このフォルダ一式を `/opt/toban` にコピー
3. `sudo bash /opt/toban/deploy/setup-pi.sh`
   （venv 作成、パッケージ導入、systemd 登録、**ディスク書き込み削減設定**まで行います）
4. 設定ファイルと鍵を置く
   ```
   sudo nano /etc/toban/config.toml              # config.example.toml を参考に spreadsheet_id を記入
   sudo cp service-account.json /etc/toban/      # 鍵ファイル
   sudo chown root:toban /etc/toban/*; sudo chmod 640 /etc/toban/*
   sudo reboot                                    # tmpfs・journald 設定を反映
   sudo systemctl status toban
   ```
5. ブラウザで `http://<Piのアドレス>/1` を開き、「管理」タブ →「今年度の今日以降を再割り振り」で最初の当番表を作成

### SDカードへの書き込みを極力なくす（任意だが推奨）

`setup-pi.sh` で次を設定済みです: ジャーナルをメモリのみ / スワップ無効 / `/tmp` `/var/log` を tmpfs / `noatime` / `.pyc` を作らない。
さらに、動作確認が済んだら**ルートを読み取り専用（OverlayFS）**にすると、SD への書き込みがゼロになります。
状態はスプレッドシートにあるので、再起動しても当番表は失われません。

```
sudo raspi-config nonint enable_overlayfs
sudo raspi-config nonint enable_bootro
sudo reboot
```

> 読み取り専用にした後でコードや設定を変えるときは `sudo raspi-config nonint disable_bootro` と
> `disable_overlayfs` → 再起動 → 変更 → 再度 enable、の手順になります。
> 設定ファイル（`/etc/toban/config.toml`）の変更も同様です。

## 4. 使い方

- **今日・明日の当番**が画面の一番上に出ます。その下の「当番表」は、今日を先頭に月ごとに並び、上へスクロールで過去、下へスクロールで来月以降が見られます。「今日へ」で戻ります
- **交代**: 「変更」を押す → 入れ替えたい2名をタップ（鍵どうし、ごみどうし。正↔副も可）→「交代」→ 確認 OK。過去日は不可。何度でも交代できます。交代した枠には ↔ が付きます
- **変更になったこと**タブ: 交代の履歴（日時・種別・2名・変更前後の日付と役割）。操作者は記録しません
- **担当回数**タブ: 年度の予定回数（鍵／ごみ正／ごみ副）、残り回数、次回の日付
- **名簿の異動**（退職・新採用・担当可否の変更）: 職員名簿シートを直し →「管理」タブ →「再読込して確認」→ 画面上部に差分が出る →「今日以降を再割り振り」。過去分と交代済みの枠は変わりません
- **次年度**: 3月ごろに「次年度の当番表を作成」
- スプレッドシートの当番表は10分ごとに読み直します（手で直した場合も反映）。Pi が一時的にネットに出られなくても、最後に取得した内容を表示し続けます

## 5. 割り振りルール

1. 鍵・ごみそれぞれ、担当できる人の間で回数をできるだけ均等に
2. 連日（前後の当番日）の担当を避ける（人数不足のときだけ許容）
3. 同じ日に鍵とごみの両方、および1号館と2号館の両方に入らない
4. ごみの正副の組み合わせが偏らないように、正・副の回数もならす
5. 再割り振り時は、過去分・交代済みの回数も数えたうえで割り振るので、新採用の人は少しずつ追いつきます

## 6. 別のPCで動かす（開発・動作確認）

スプレッドシートがなくても、デモデータ（メモリ上）で画面と交代操作を試せます。必要なもの: **Python 3.11 以上**、Git。

**Windows（PowerShell）**
```
git clone <このリポジトリのURL> toban-board
cd toban-board
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:TOBAN_DEMO = "1"; $env:PORT = "8080"
python -m toban
```
**macOS / Linux**
```
git clone <このリポジトリのURL> toban-board && cd toban-board
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
TOBAN_DEMO=1 PORT=8080 python -m toban
```
ブラウザで `http://localhost:8080/1`（1号館）と `/2`（2号館）を開きます。デモ起動時は、起動した日から年度末までの当番表が自動で作られます（再起動すると消えます）。

### テスト
```
pip install -r requirements-dev.txt
python -m pytest tests/test_toban.py          # 画面なしのテスト（23件）
playwright install chromium                   # 初回のみ
python -m pytest tests                        # 画面テストを含む全件（34件）
```

### フォルダ構成
```
toban/allocator.py      当番の自動割り振り（均等・連日回避・正副ペア分散）
toban/calendar_util.py  年度・当番日（祝日・休校日）の計算
toban/backends.py       データの置き場所（Googleスプレッドシート / デモ用メモリ）
toban/store.py          状態管理（読み込み・交代・割り振り・集計・ログ）
toban/app.py            Flask アプリ（画面とAPI）
toban/templates, static 画面（HTML / CSS / JavaScript）
tests/                  自動テスト
deploy/                 Raspberry Pi 用（systemd・セットアップスクリプト）
```
