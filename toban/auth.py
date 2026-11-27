"""アプリ側の認証（発展問題・パッチ 0014）。

環境変数 TOBAN_PASSWORD_HASH を設定したときだけ働きます。未設定なら、今までと何も変わりません。
使い方と考え方は docs/発展-アプリ側の認証.md を読んでください。

この認証は「パスワードだけでログインする」画面を、アプリの中に持たせたものです。
入口のプロキシで守る BASIC認証と違い、守りを全部自分で書くので、次の項目を落とさないことが大切です。

  ① 入力の大きさに上限 ........ 送信は64KBまで、パスワードは128文字まで
  ② 総当たりを止める ........... 接続元ごとに5回失敗で5分ロック（sleep では待たせない）
  ③ 重い計算を同時にさせない ... パスワードの照合は同時に2つまで
  ④ ヘッダーを信用しすぎない ... X-Forwarded-For は、信用するプロキシから来たときだけ、右から読む
  ⑤ 設定が危ないときは起動しない（fail closed）
  ⑥ Cookie の属性と期限 ........ HttpOnly / SameSite=Lax / Secure、12時間
  ⑦ CSRF ......................... 変更系の送信は X-Requested-With が無ければ断る
  ⑧ 戻り先（next）の検査 ........ 自サイト内の「/」で始まるものだけ
  ⑨ 秘密をログに書かない ....... パスワードもハッシュも出さない
  ⑩ 応答ヘッダー ............... nosniff / DENY / no-store / CSP（ログイン画面）

Python は C 言語のような「バッファ溢れ」が起きにくい言語です（範囲の外を読み書きせず、例外になる）。
それでも、①のように入力の大きさを先に決めておく考え方は同じです。一方、Python 本体（CPython）、
OS の部品（libc）、入口の nginx と OpenSSL は C で書かれています。そこは自分では直せないので、
OS とパッケージを更新し続けることが、守りの一部になります。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import secrets
import sys
import threading
import time
from datetime import timedelta
from urllib.parse import quote

from flask import Flask, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

log = logging.getLogger("toban.auth")

MAX_BODY_BYTES = 64 * 1024     # ① 1回の送信の大きさの上限（当番の交代などは、ごく小さい）
MAX_PASSWORD_LEN = 128         # ① パスワードの長さの上限
MIN_PASSWORD_LEN = 10          # 新しく作るときの下限（長いほうが強い）
SESSION_HOURS = 12             # ⑥ ログインが続く時間
MAX_FAILS = 5                  # ② 失敗できる回数
FAIL_WINDOW_SEC = 600          # ② 失敗を数える期間
LOCK_SEC = 300                 # ② ロックする時間
MAX_TRACKED = 1000             # ② 失敗を覚えておく接続元の数の上限（メモリを使い切らせない）
MAX_HASH_AT_ONCE = 2           # ③ パスワード照合の同時実行数
XFF_MAX_HOPS = 10              # ④ X-Forwarded-For は右から10個までしか読まない
HASH_METHOD = "pbkdf2:sha256:600000"
OPEN_ENDPOINTS = {"static", "login", "healthz"}   # ログインなしで開ける場所
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _ip(text):
    """文字列を IP アドレスにする。読めなければ None（偽装や壊れた値を通さない）。"""
    text = (text or "").strip()
    if not text or len(text) > 45:
        return None
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    return getattr(ip, "ipv4_mapped", None) or ip      # ::ffff:192.168.1.5 は 192.168.1.5 として扱う


def _parse_nets(text, name):
    nets = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError as e:     # ⑤ 読めない設定は、黙って無視せず起動を止める
            raise ValueError(f"{name} の書き方が正しくありません: {part!r}（例: 192.168.1.0/24）") from e
    return nets


def _within(ip, nets) -> bool:
    return any(ip in n for n in nets)


def safe_next(value: str) -> str:
    """⑧ ログイン後の戻り先。自サイトの中（/ で始まる）だけを通す。"""
    if not value or len(value) > 200:
        return "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return "/"
    return value


class Throttle:
    """② 接続元ごとの失敗回数。メモリ上の小さな表で、増えすぎないよう上限がある。

    gunicorn の worker は1つ（deploy/toban.service）なので、表は1つで足りる。
    worker を増やすときは、共有できる置き場所（Redis など）が要る。
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._rec: dict[str, list] = {}     # 接続元 -> [失敗回数, 最初の失敗の時刻, ロック解除の時刻]

    def blocked(self, key: str) -> bool:
        with self._lock:
            r = self._rec.get(key)
            return bool(r and r[2] > self.clock())

    def fail(self, key: str) -> int:
        now = self.clock()
        with self._lock:
            r = self._rec.get(key)
            if r is None or now - r[1] > FAIL_WINDOW_SEC:
                r = self._rec[key] = [0, now, 0.0]
            r[0] += 1
            if r[0] >= MAX_FAILS:
                r[2] = now + LOCK_SEC
            self._trim(now)
            return r[0]

    def ok(self, key: str) -> None:
        with self._lock:
            self._rec.pop(key, None)

    def size(self) -> int:
        with self._lock:
            return len(self._rec)

    def _trim(self, now: float) -> None:
        if len(self._rec) <= MAX_TRACKED:
            return
        for k in [k for k, r in self._rec.items() if r[2] <= now and now - r[1] > FAIL_WINDOW_SEC]:
            del self._rec[k]
        while len(self._rec) > MAX_TRACKED:
            del self._rec[min(self._rec, key=lambda k: self._rec[k][1])]


class Auth:
    def __init__(self, env):
        self.pw_hash = env["TOBAN_PASSWORD_HASH"].strip()
        if "$" not in self.pw_hash:
            raise ValueError("TOBAN_PASSWORD_HASH がハッシュの形ではありません。"
                             "パスワードをそのまま書かず、python -m toban.auth で作ってください")
        self.secret = env.get("TOBAN_SECRET_KEY", "")
        if len(self.secret) < 32:           # ⑤ 署名の鍵が無い・短いときは動かさない
            raise RuntimeError("TOBAN_SECRET_KEY（32文字以上）が必要です。python -m toban.auth で作れます")
        self.staff = _parse_nets(env.get("TOBAN_TRUSTED_NETS"), "TOBAN_TRUSTED_NETS")
        self.proxies = _parse_nets(env.get("TOBAN_TRUSTED_PROXIES"), "TOBAN_TRUSTED_PROXIES")
        self.cookie_secure = env.get("TOBAN_COOKIE_SECURE", "1") != "0"
        self.throttle = Throttle()
        self.gate = threading.BoundedSemaphore(MAX_HASH_AT_ONCE)
        self.now = time.time

    def client_ip(self):
        """④ 本当の接続元。直接つながった相手が信用するプロキシのときだけ X-Forwarded-For を読む。

        ヘッダーの左側は、送ってきた人が自由に書ける。右から読んで、信用するプロキシを
        飛ばした最初のアドレスが、プロキシ（自分たちの機器）が見た本当の相手になる。
        """
        peer = _ip(request.remote_addr)
        if peer is None or not _within(peer, self.proxies):
            return peer
        for item in reversed(request.headers.get("X-Forwarded-For", "").split(",")[-XFF_MAX_HOPS:]):
            ip = _ip(item)
            if ip is None:
                return peer                 # 読めない値が混じったら、ヘッダーごと信用しない
            if not _within(ip, self.proxies):
                return ip
        return peer

    def is_staff(self) -> bool:
        ip = self.client_ip()
        return ip is not None and _within(ip, self.staff)

    def session_ok(self) -> bool:
        if not session.get("ok"):
            return False
        t = session.get("t")
        return isinstance(t, (int, float)) and 0 <= self.now() - t < SESSION_HOURS * 3600


def init_auth(app: Flask, env=None):
    env = os.environ if env is None else env
    if not env.get("TOBAN_PASSWORD_HASH"):
        if env.get("TOBAN_TRUSTED_NETS") or env.get("TOBAN_TRUSTED_PROXIES"):
            raise ValueError("TOBAN_TRUSTED_NETS / TOBAN_TRUSTED_PROXIES があるのに TOBAN_PASSWORD_HASH がありません。"
                             "認証が効かないまま動かさないよう、起動を止めました")
        return None                         # 設定が無ければ、今までどおり（何も変えない）

    auth = Auth(env)
    app.config["AUTH"] = auth
    app.secret_key = auth.secret
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_BODY_BYTES,          # ① 大きすぎる送信は、中身を読む前に 413 で断る
        MAX_FORM_MEMORY_SIZE=MAX_BODY_BYTES,
        SESSION_COOKIE_HTTPONLY=True,               # ⑥ JavaScript から Cookie を読ませない
        SESSION_COOKIE_SAMESITE="Lax",              # ⑥ 他のサイトからの送信に Cookie を付けない
        SESSION_COOKIE_SECURE=auth.cookie_secure,   # ⑥ https のときだけ送る（手元の http 確認は TOBAN_COOKIE_SECURE=0）
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
    )

    @app.before_request
    def _guard():
        if request.endpoint is None or request.endpoint in OPEN_ENDPOINTS:
            return None
        if not (auth.is_staff() or auth.session_ok()):
            if request.path.startswith("/api/"):
                return jsonify(error="ログインが必要です"), 401
            return redirect("/login?next=" + quote(request.full_path.rstrip("?"), safe=""))
        # ⑦ 校内（IPで通る）人にも、ログイン済みの人にも同じ。他のサイトの画面から勝手に送らせない。
        # 独自のヘッダーは、他のサイトからは付けられない（付けるには CORS の許可が要り、出していない）
        if request.method not in SAFE_METHODS and request.headers.get("X-Requested-With") != "toban":
            return jsonify(error="この送信は受け付けられません"), 400
        return None

    @app.after_request
    def _headers(resp):
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")      # ⑩ 種類を勝手に推測させない
        h.setdefault("X-Frame-Options", "DENY")                # ⑩ 他のサイトの枠の中に表示させない
        h.setdefault("Referrer-Policy", "same-origin")
        if request.endpoint != "static":
            h["Cache-Control"] = "no-store"                    # ⑩ 共有のスマホに画面を残さない
        if request.endpoint == "login":
            h["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'; form-action 'self'"
        return resp

    def page(nxt, error=None, status=200):
        return render_template("login.html", next=nxt, error=error), status

    @app.route("/login", methods=["GET", "POST"])
    def login():
        nxt = safe_next(request.values.get("next", ""))
        if request.method == "GET":
            return page(nxt)
        key = str(auth.client_ip() or "unknown")
        if auth.throttle.blocked(key):
            return page(nxt, "失敗が続いたため、しばらく使えません。時間をおいてやり直してください。", 429)
        pw = request.form.get("password", "")
        if len(pw) > MAX_PASSWORD_LEN:      # ① 長すぎるものは、重い計算をせずに断る（失敗には数える）
            auth.throttle.fail(key)
            return page(nxt, "パスワードが違います。", 401)
        if not auth.gate.acquire(blocking=False):      # ③ 同時に照合できる数を超えたら、待たせず断る
            return page(nxt, "混み合っています。少し待ってやり直してください。", 429)
        try:
            ok = check_password_hash(auth.pw_hash, pw)  # 比較は一定時間で行われる（タイミングで当てさせない）
        finally:
            auth.gate.release()
        if not ok:
            n = auth.throttle.fail(key)
            log.warning("ログイン失敗 ip=%s 回数=%d", key, n)    # ⑨ 入力されたパスワードは書かない
            return page(nxt, "パスワードが違います。", 401)        # 理由は細かく言わない
        auth.throttle.ok(key)
        session.clear()                                  # ⑥ 古い内容を引き継がない
        session["ok"] = 1
        session["t"] = int(auth.now())
        session.permanent = True
        log.info("ログイン成功 ip=%s", key)
        return redirect(nxt)

    return auth


def main() -> None:
    """python -m toban.auth : パスワードのハッシュと、署名用の鍵を作って表示する。"""
    import getpass
    p1 = getpass.getpass("新しいパスワード（画面に出ません）: ")
    p2 = getpass.getpass("もう一度: ")
    if p1 != p2:
        sys.exit("一致しません。")
    if not MIN_PASSWORD_LEN <= len(p1) <= MAX_PASSWORD_LEN:
        sys.exit(f"{MIN_PASSWORD_LEN}〜{MAX_PASSWORD_LEN}文字にしてください。")
    print("# /etc/toban/auth.env に貼ります（持ち主 root、権限 600。GitHub には載せない）")
    print("TOBAN_PASSWORD_HASH=" + generate_password_hash(p1, method=HASH_METHOD))
    print("TOBAN_SECRET_KEY=" + secrets.token_hex(32))


if __name__ == "__main__":
    main()
