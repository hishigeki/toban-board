"""画面(ブラウザ)テスト。Playwright が無い環境では自動でスキップする。
実行: pip install pytest playwright && playwright install chromium && python -m pytest tests/test_ui.py
"""
import sys, pathlib, threading
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server

from toban.app import create_app, load_config, demo_backend

TZ = ZoneInfo("Asia/Tokyo")


@pytest.fixture(scope="module")
def server():
    be = demo_backend()
    be.roster.append(["<b>危険</b>名", "○", "○", ""])            # 画面に出る名前のエスケープ確認用
    app = create_app(load_config("/nonexistent"), be, start_thread=False,
                     now_fn=lambda: datetime(2026, 11, 24, 14, 0, tzinfo=TZ))
    app.config["STORE"].allocate()
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", app.config["STORE"]
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture()
def page(browser, server):
    pg = browser.new_page(viewport={"width": 1100, "height": 900})
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("dialog", lambda d: d.accept())
    yield pg
    assert pg.errors == [], pg.errors
    pg.close()


def open_page(page, server, b=1):
    page.goto(f"{server[0]}/{b}")
    page.wait_for_selector("tr.today")


def test_ui_today_and_next_cards(page, server):
    open_page(page, server)
    cards = page.locator(".card")
    assert cards.count() == 2
    assert "11/24(火)" in cards.nth(0).inner_text() and "11/25(水)" in cards.nth(1).inner_text()
    assert "次の当番" in cards.nth(1).inner_text() and "明日" not in cards.nth(1).inner_text()
    assert page.locator("tr.today").count() == 1


def test_ui_building_switch(page, server):
    open_page(page, server, 2)
    assert "2号館" in page.locator(".card").first.inner_text()
    assert page.locator(".bld a.on").inner_text() == "2号館"


def test_ui_mode_disables_other_type(page, server):
    open_page(page, server)
    page.click("#btnMode")
    page.locator("tr:not(.past) td:nth-child(3) .nm.pick").nth(2).click()
    assert page.locator("td:nth-child(4) .nm.off").count() > 0         # ごみ側が選べなくなる
    assert page.locator("#btnSwap").is_disabled()


def test_ui_swap_flow_and_log(page, server):
    open_page(page, server)
    page.click("#btnMode")
    k = page.locator("tr:not(.past) td:nth-child(3) .nm.pick")
    n1, n2 = k.nth(3).inner_text(), k.nth(8).inner_text()
    k.nth(3).click(); k.nth(8).click()
    assert page.locator("#btnSwap").is_enabled()
    page.click("#btnSwap")
    page.wait_for_selector(".toast")
    assert "入れ替えました" in page.inner_text("#toast")
    assert page.locator(".nm.swapped").count() == 2
    page.click("[data-tab=log]")
    t = page.inner_text("#logTable")
    assert n1 in t and n2 in t and "鍵" in t


def test_ui_same_person_rejected(page, server):
    open_page(page, server)
    page.click("#btnMode")
    # 同じ名前のセルを2つ選ぶ(鍵の列で同じ人が出る日を探す)
    cells = page.locator("tr:not(.past) td:nth-child(3) .nm.pick")
    names = [cells.nth(i).inner_text() for i in range(cells.count())]
    pair = next(((i, j) for i in range(len(names)) for j in range(i + 1, len(names)) if names[i] == names[j]), None)
    assert pair, "同じ人が2回以上当番になる日がある"
    cells.nth(pair[0]).click(); cells.nth(pair[1]).click()
    assert "同じ人" in page.inner_text("#toast")
    assert page.locator("#btnSwap").is_disabled()


def test_ui_past_rows_not_selectable(page, server):
    open_page(page, server)
    page.click("#btnMode")
    assert page.locator("tr.past .nm.pick").count() == 0


def test_ui_name_highlight(page, server):
    open_page(page, server)
    page.fill("#find", "田中")
    assert page.locator(".nm.hit").count() > 0
    assert all("田中" in t for t in page.locator(".nm.hit").all_inner_texts())


def test_ui_counts_tab(page, server):
    open_page(page, server)
    page.click("[data-tab=counts]")
    assert page.locator("#countsTable tbody tr").count() >= 20
    assert "回数" in page.inner_text("#countsNote")


def test_ui_name_is_escaped(page, server):
    open_page(page, server)
    page.click("[data-tab=counts]")
    html = page.inner_html("#countsTable")
    assert "<b>危険</b>" not in html and "&lt;b&gt;危険&lt;/b&gt;" in html


def test_ui_admin_reallocate(page, server):
    open_page(page, server)
    page.click("[data-tab=admin]")
    page.click("#btnAlloc")
    page.wait_for_selector(".toast")
    assert "完了" in page.inner_text("#toast")


def test_ui_mobile_no_horizontal_scroll(browser, server):
    pg = browser.new_page(viewport={"width": 390, "height": 800})
    pg.goto(f"{server[0]}/1"); pg.wait_for_selector("tr.today")
    assert pg.evaluate("document.documentElement.scrollWidth") <= 392
    pg.close()
