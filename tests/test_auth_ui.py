"""発展問題（パッチ 0014）ブラウザで、ログインから「交代」まで通して動くことの確認。
Playwright が無い環境では自動でスキップします。
"""
import sys, pathlib, threading
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from werkzeug.security import generate_password_hash

from toban.app import create_app, load_config, demo_backend

TZ = ZoneInfo("Asia/Tokyo")
PW = "correct-horse-battery"


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("TOBAN_PASSWORD_HASH", generate_password_hash(PW, method="pbkdf2:sha256:1000"))
    mp.setenv("TOBAN_SECRET_KEY", "s" * 40)
    mp.setenv("TOBAN_COOKIE_SECURE", "0")                   # 手元の http で確認するため
    app = create_app(load_config("/nonexistent"), demo_backend(), start_thread=False,
                     now_fn=lambda: datetime(2026, 11, 24, 14, 0, tzinfo=TZ))
    app.config["STORE"].allocate()
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    mp.undo()


def test_ui_login_then_swap(server):
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1100, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("dialog", lambda d: d.accept())                          # 交代の確認ダイアログは「OK」にする
        page.goto(f"{server}/1")
        assert page.url.startswith(f"{server}/login?next=")             # ログインしていないので、ログイン画面へ
        page.fill("input[name=password]", "wrong-password-xx")
        page.click("button[type=submit]")
        assert "パスワードが違います" in page.inner_text(".login-error")
        page.fill("input[name=password]", PW)
        page.click("button[type=submit]")
        page.wait_for_selector("tr.today")                              # 元のページ(/1)に戻って表が出る
        page.click("#btnMode")                                          # 「変更」で交代 = POST /api/swap（X-Requested-With 付き）
        k = page.locator("tr:not(.past) td:nth-child(3) .nm.pick")
        k.nth(3).click(); k.nth(8).click()
        page.click("#btnSwap")
        page.wait_for_selector(".toast")
        assert "入れ替えました" in page.inner_text("#toast")
        assert errors == []
        b.close()
