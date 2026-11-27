"""発展問題（パッチ 0014）アプリ側の認証のテスト。

「正しく使えるか」だけでなく、「攻撃されても崩れないか」を確かめるテストが中心です。
実行: python -m pytest tests/test_auth.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from werkzeug.security import generate_password_hash

from toban import auth as authmod
from toban.app import create_app, load_config, demo_backend

TZ = ZoneInfo("Asia/Tokyo")
PW = "correct-horse-battery"
HASH = generate_password_hash(PW, method="pbkdf2:sha256:1000")   # テストを速くするため、計算の回数は少なくしている
SECRET = "s" * 40
STAFF = "192.168.1.0/24"
PROXY = "192.168.2.220"
CSRF = {"X-Requested-With": "toban"}
ENV_KEYS = ("TOBAN_PASSWORD_HASH", "TOBAN_SECRET_KEY", "TOBAN_TRUSTED_NETS",
            "TOBAN_TRUSTED_PROXIES", "TOBAN_COOKIE_SECURE")


def make(monkeypatch, **env):
    """環境変数を整えてアプリを作る。値を None にすると、その変数は付けない。"""
    base = {"TOBAN_PASSWORD_HASH": HASH, "TOBAN_SECRET_KEY": SECRET, "TOBAN_COOKIE_SECURE": "0"}
    base.update(env)
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in base.items():
        if v is not None:
            monkeypatch.setenv(k, v)
    app = create_app(load_config("/nonexistent"), demo_backend(),
                     now_fn=lambda: datetime(2026, 11, 24, 14, 0, tzinfo=TZ), start_thread=False)
    if not app.config["STORE"].rows:
        app.config["STORE"].allocate()
    return app


def login(c, pw=PW, nxt="/1", **kw):
    return c.post("/login", data={"password": pw, "next": nxt}, **kw)


def at(addr):
    return {"environ_base": {"REMOTE_ADDR": addr}}


# ---------- ふつうの使い方 ----------

def test_auth_is_off_by_default(monkeypatch):
    app = make(monkeypatch, TOBAN_PASSWORD_HASH=None, TOBAN_SECRET_KEY=None)
    assert "AUTH" not in app.config
    assert app.test_client().get("/1").status_code == 200       # 設定が無ければ、今までどおり


def test_pages_and_api_need_login(monkeypatch):
    app = make(monkeypatch)
    c = app.test_client()
    r = c.get("/1")
    assert r.status_code == 302 and r.headers["Location"].startswith("/login?next=%2F1")
    r = c.get("/api/state?b=1")
    assert r.status_code == 401 and "ログイン" in r.get_json()["error"]
    r = login(c)
    assert r.status_code == 302 and r.headers["Location"] == "/1"
    assert c.get("/1").status_code == 200
    assert c.get("/api/state?b=1").status_code == 200


def test_wrong_password_gets_a_plain_answer(monkeypatch):
    app = make(monkeypatch)
    c = app.test_client()
    r = login(c, "wrong-password-xx")
    assert r.status_code == 401 and "パスワードが違います" in r.get_data(as_text=True)
    assert not r.headers.getlist("Set-Cookie")                  # 失敗したら Cookie を渡さない
    assert c.get("/api/state?b=1").status_code == 401


def test_open_places_need_no_login(monkeypatch):
    app = make(monkeypatch)
    c = app.test_client()
    assert c.get("/healthz").status_code == 200
    assert c.get("/static/style.css").status_code == 200
    assert c.get("/login").status_code == 200
    assert c.get("/nope").status_code == 404                    # 無い場所は、ログイン画面へ飛ばさず 404


# ---------- Cookie と期限 ----------

def test_cookie_has_protective_flags(monkeypatch):
    app = make(monkeypatch, TOBAN_COOKIE_SECURE=None)           # 初期値は Secure あり
    cookie = login(app.test_client()).headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" in cookie


def test_login_expires_after_twelve_hours(monkeypatch):
    app = make(monkeypatch)
    c = app.test_client()
    login(c)
    assert c.get("/1").status_code == 200
    app.config["AUTH"].now = lambda: time.time() + 13 * 3600
    assert c.get("/1").status_code == 302


def test_forged_cookie_is_refused(monkeypatch):
    app = make(monkeypatch)
    c = app.test_client()
    c.set_cookie("session", "eyJvayI6MX0.AAAAAA.forged-signature")
    assert c.get("/api/state?b=1").status_code == 401


# ---------- 総当たり・重い計算・巨大な入力 ----------

def test_lockout_after_repeated_failures_then_unlocks(monkeypatch):
    app = make(monkeypatch)
    a, c = app.config["AUTH"], app.test_client()
    t = [1000.0]
    a.throttle.clock = lambda: t[0]
    codes = [login(c, f"bad-guess-{i:02d}-xx").status_code for i in range(authmod.MAX_FAILS)]
    assert codes == [401] * authmod.MAX_FAILS
    assert login(c).status_code == 429                          # 正しいパスワードでも、ロック中は通さない
    t[0] += authmod.LOCK_SEC + 1
    assert login(c).status_code == 302                          # 時間がたてば戻る


def test_lockout_is_per_client(monkeypatch):
    app = make(monkeypatch)
    c = app.test_client()
    for i in range(authmod.MAX_FAILS):
        login(c, f"bad-guess-{i:02d}-xx", **at("203.0.113.9"))
    assert login(c, **at("203.0.113.9")).status_code == 429
    assert login(c, **at("203.0.113.10")).status_code == 302    # 別の人は、巻き込まない


def test_overlong_password_is_refused_without_hashing(monkeypatch):
    app = make(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("長すぎる入力で、重い計算をしてしまった")
    monkeypatch.setattr(authmod, "check_password_hash", boom)
    r = login(app.test_client(), "a" * (authmod.MAX_PASSWORD_LEN + 1))
    assert r.status_code == 401
    assert app.config["AUTH"].throttle.size() == 1              # 失敗として数える


def test_huge_requests_are_rejected_early(monkeypatch):
    app = make(monkeypatch)
    c = app.test_client()
    assert c.post("/login", data={"password": "a" * 70000}).status_code == 413
    login(c)
    r = c.post("/api/swap", data="x" * 70000, content_type="application/json", headers=CSRF)
    assert r.status_code == 413


def test_password_check_is_limited_to_a_few_at_once(monkeypatch):
    app = make(monkeypatch)
    a, c = app.config["AUTH"], app.test_client()
    for _ in range(authmod.MAX_HASH_AT_ONCE):
        assert a.gate.acquire(blocking=False)
    try:
        r = login(c)
        assert r.status_code == 429 and "混み合っています" in r.get_data(as_text=True)
    finally:
        for _ in range(authmod.MAX_HASH_AT_ONCE):
            a.gate.release()
    assert login(c).status_code == 302


def test_failure_table_stays_small():
    th = authmod.Throttle()
    for i in range(authmod.MAX_TRACKED + 500):
        th.fail(f"10.{i // 65536}.{(i // 256) % 256}.{i % 256}")
    assert th.size() <= authmod.MAX_TRACKED                     # 偽の接続元を大量に送られても、メモリを使い切らない


# ---------- 校内（IP）で通す・ヘッダーの偽装 ----------

def test_staff_network_needs_no_login(monkeypatch):
    app = make(monkeypatch, TOBAN_TRUSTED_NETS=STAFF)
    c = app.test_client()
    assert c.get("/1", **at("192.168.1.20")).status_code == 200
    assert c.get("/1", **at("203.0.113.9")).status_code == 302
    assert c.get("/1", **at("192.168.2.220")).status_code == 302   # DMZ（プロキシ自身）は、通さない


def test_forwarded_header_from_a_stranger_is_ignored(monkeypatch):
    app = make(monkeypatch, TOBAN_TRUSTED_NETS=STAFF, TOBAN_TRUSTED_PROXIES=PROXY)
    r = app.test_client().get("/1", headers={"X-Forwarded-For": "192.168.1.20"}, **at("203.0.113.9"))
    assert r.status_code == 302                                 # 信用するプロキシ以外が書いたヘッダーは読まない


@pytest.mark.parametrize("xff, expected", [
    ("192.168.1.20", 200),                                      # プロキシが見た本当の接続元が、校内
    ("198.51.100.7", 302),                                      # 外から
    ("192.168.1.20, 198.51.100.7", 302),                        # 左に校内の偽アドレスを書いても、右（本当）が外なら通さない
    ("198.51.100.7, 192.168.1.20", 200),
    ("not-an-address", 302),                                    # 読めない値は信用しない
    ("192.168.1.20, <script>", 302),
    ("", 302),                                                  # ヘッダーが無い（プロキシ自身の接続）
])
def test_forwarded_header_is_read_from_the_right(monkeypatch, xff, expected):
    app = make(monkeypatch, TOBAN_TRUSTED_NETS=STAFF, TOBAN_TRUSTED_PROXIES=PROXY)
    headers = {"X-Forwarded-For": xff} if xff else {}
    assert app.test_client().get("/1", headers=headers, **at(PROXY)).status_code == expected


def test_ipv4_mapped_ipv6_is_read_as_ipv4(monkeypatch):
    app = make(monkeypatch, TOBAN_TRUSTED_NETS=STAFF)
    assert app.test_client().get("/1", **at("::ffff:192.168.1.20")).status_code == 200


# ---------- 他のサイトからの送信・戻り先 ----------

def test_changes_need_the_csrf_header_even_for_staff_ip_and_logged_in(monkeypatch):
    app = make(monkeypatch, TOBAN_TRUSTED_NETS=STAFF)
    staff = at("192.168.1.20")
    c = app.test_client()
    assert c.post("/api/admin/reload", **staff).status_code == 400
    assert c.post("/api/admin/reload", headers=CSRF, **staff).status_code == 200
    c2 = app.test_client()
    login(c2)
    assert c2.post("/api/admin/reload").status_code == 400
    assert c2.post("/api/admin/reload", headers=CSRF).status_code == 200


def test_front_end_sends_the_csrf_header():
    js = (pathlib.Path(__file__).resolve().parents[1] / "toban" / "static" / "app.js").read_text(encoding="utf-8")
    assert '"X-Requested-With": "toban"' in js                  # 画面側とサーバー側の約束が、ずれていないこと


@pytest.mark.parametrize("nxt", [
    "//evil.example.com", "https://evil.example.com", "/\\evil.example.com",
    "javascript:alert(1)", "/ok\r\nSet-Cookie: x=1", "", "x" * 500,
])
def test_login_never_redirects_outside_the_site(monkeypatch, nxt):
    app = make(monkeypatch)
    r = login(app.test_client(), nxt=nxt)
    assert r.status_code == 302 and r.headers["Location"] == "/"


def test_login_returns_to_a_page_inside_the_site(monkeypatch):
    app = make(monkeypatch)
    assert login(app.test_client(), nxt="/2").headers["Location"] == "/2"
    assert authmod.safe_next("/1?x=1") == "/1?x=1"


# ---------- 設定・ヘッダー・ログ ----------

@pytest.mark.parametrize("env, exc", [
    ({"TOBAN_SECRET_KEY": None}, RuntimeError),                 # 署名の鍵が無い
    ({"TOBAN_SECRET_KEY": "short"}, RuntimeError),              # 短すぎる
    ({"TOBAN_PASSWORD_HASH": "plain-password"}, ValueError),    # ハッシュではなく、パスワードをそのまま書いた
    ({"TOBAN_TRUSTED_NETS": "192.168.1.0/99"}, ValueError),     # 読めない範囲
    ({"TOBAN_TRUSTED_PROXIES": "not-an-ip"}, ValueError),
    ({"TOBAN_PASSWORD_HASH": None, "TOBAN_TRUSTED_NETS": STAFF}, ValueError),   # 認証が効かないまま、範囲だけ書いた
])
def test_unsafe_settings_stop_the_app(monkeypatch, env, exc):
    with pytest.raises(exc):
        make(monkeypatch, **env)


def test_response_headers(monkeypatch):
    app = make(monkeypatch)
    r = app.test_client().get("/login")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Cache-Control"] == "no-store"
    assert "no-store" not in app.test_client().get("/static/style.css").headers.get("Cache-Control", "")


def test_login_page_escapes_the_next_value(monkeypatch):
    app = make(monkeypatch)
    html = app.test_client().get('/login?next=/1"><script>alert(1)</script>').get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html


def test_secrets_are_never_logged(monkeypatch, caplog):
    app = make(monkeypatch)
    caplog.set_level(logging.INFO)
    c = app.test_client()
    login(c, "my-wrong-guess-12345")
    login(c)
    assert "ログイン失敗" in caplog.text and "ログイン成功" in caplog.text
    for secret in ("my-wrong-guess-12345", PW, HASH, SECRET):
        assert secret not in caplog.text
