import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from collections import Counter
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from toban.app import create_app, load_config, demo_backend
from toban.store import TobanError, parse_roster

TZ = ZoneInfo("Asia/Tokyo")


def make(now=datetime(2026, 11, 24, 14, 0, tzinfo=TZ), backend=None):
    clock = {"now": now}
    app = create_app(load_config("/nonexistent"), backend or demo_backend(),
                     now_fn=lambda: clock["now"], start_thread=False)
    store = app.config["STORE"]
    if not store.rows:
        store.allocate()
    return app, store, clock


def test_parse_roster_flags_and_building():
    ppl = parse_roster([["氏名", "鍵当番", "ごみ当番", "館"],
                        ["A", "○", "", "1号館"], ["B", "TRUE", "可", ""], ["", "○", "○", ""], ["A", "○", "○", ""]])
    assert [p.name for p in ppl] == ["A", "B"]
    assert ppl[0].kagi and not ppl[0].gomi and ppl[0].building == 1
    assert ppl[1].kagi and ppl[1].gomi and ppl[1].building is None


def test_parse_roster_missing_column():
    with pytest.raises(TobanError):
        parse_roster([["氏名", "鍵"], ["A", "○"]])


def test_allocation_rules():
    _, st, _ = make()
    rows = st.rows
    assert rows and min(r.date for r in rows) == date(2026, 11, 24)
    assert all(r.date.weekday() < 5 for r in rows)
    assert date(2026, 11, 23) not in {r.date for r in rows}      # 勤労感謝の日
    assert not any(r.date == date(2026, 12, 29) for r in rows)    # 年末年始
    by_day = {}
    for r in rows:
        for s in ("kagi", "gomi1", "gomi2"):
            assert r.get(s), "未割当なし"
            by_day.setdefault(r.date, []).append(r.get(s))
    for d, names in by_day.items():                               # 同日に重複しない(両館含む)
        assert len(names) == len(set(names))
    days = sorted(by_day)
    for a, b in zip(days, days[1:]):                              # 連日なし
        assert not set(by_day[a]) & set(by_day[b])
    ppl = {p.name: p for p in st.roster}
    kagi = Counter(r.kagi for r in rows)
    gomi = Counter(n for r in rows for n in (r.gomi1, r.gomi2))
    assert all(ppl[n].kagi for n in kagi) and all(ppl[n].gomi for n in gomi)
    assert max(kagi.values()) - min(kagi.values()) <= 1
    assert max(gomi.values()) - min(gomi.values()) <= 1


def test_swap_ok_and_logged_and_persisted():
    app, st, _ = make()
    c = app.test_client()
    k = [r for r in st.rows if r.b == 1]
    a, b = k[2], k[9]
    pa, pb = a.kagi, b.kagi
    r = c.post("/api/swap", json={"b": 1, "a": {"date": a.date.isoformat(), "slot": "kagi"},
                                  "c": {"date": b.date.isoformat(), "slot": "kagi"}})
    assert r.status_code == 200, r.json
    j = c.get("/api/state?b=1").json
    row = {x["date"]: x for x in j["rows"]}
    assert row[a.date.isoformat()]["kagi"] == pb and row[b.date.isoformat()]["kagi"] == pa
    assert "kagi" in row[a.date.isoformat()]["locked"]
    assert j["log"][0]["kind"] == "交代" and j["log"][0]["p1"] == pa
    # スプレッドシート(メモリ)側にも反映され、再読込しても残る
    st.load()
    assert any(x.date == a.date and x.b == 1 and x.kagi == pb for x in st.rows)
    assert len(st.backend.log) == 3   # ヘッダ + 割り振り + 交代


def test_swap_rejections():
    app, st, _ = make()
    c = app.test_client()
    rows = [r for r in st.rows if r.b == 1]
    d0, d1 = rows[1].date.isoformat(), rows[5].date.isoformat()
    def post(a, b, sa, sb):
        return c.post("/api/swap", json={"b": 1, "a": {"date": a, "slot": sa}, "c": {"date": b, "slot": sb}})
    assert post(d0, d1, "kagi", "gomi1").status_code == 400            # 鍵×ごみ
    assert "またぐ" in post(d0, d1, "kagi", "gomi1").json["error"]
    assert post(d0, d0, "kagi", "kagi").status_code == 400             # 同じ枠
    assert post(d0, d1, "gomi1", "gomi2").status_code == 200           # ごみ同士(正↔副)は可


def test_swap_past_rejected():
    app, st, clock = make()
    c = app.test_client()
    rows = [r for r in st.rows if r.b == 1]
    a, b = rows[0].date.isoformat(), rows[4].date.isoformat()
    clock["now"] = datetime(2026, 11, 30, 9, 0, tzinfo=TZ)             # a は過去になる
    r = c.post("/api/swap", json={"b": 1, "a": {"date": a, "slot": "kagi"}, "c": {"date": b, "slot": "kagi"}})
    assert r.status_code == 400 and "過去" in r.json["error"]


def test_swap_rollback_when_sheet_write_fails():
    app, st, _ = make()
    c = app.test_client()
    rows = [r for r in st.rows if r.b == 1]
    a, b = rows[2], rows[6]
    before = (a.kagi, b.kagi)
    st.backend.fail_writes = True
    r = c.post("/api/swap", json={"b": 1, "a": {"date": a.date.isoformat(), "slot": "kagi"},
                                  "c": {"date": b.date.isoformat(), "slot": "kagi"}})
    assert r.status_code == 500
    cur = {(x.b, x.date): x for x in st.rows}
    assert (cur[(1, a.date)].kagi, cur[(1, b.date)].kagi) == before     # メモリは変わらない


def test_reallocate_keeps_past_and_swapped_and_adds_new_hire():
    app, st, clock = make()
    c = app.test_client()
    rows = [r for r in st.rows if r.b == 1]
    a, b = rows[3], rows[8]
    c.post("/api/swap", json={"b": 1, "a": {"date": a.date.isoformat(), "slot": "kagi"},
                              "c": {"date": b.date.isoformat(), "slot": "kagi"}})
    snap = {(r.b, r.date): (r.kagi, r.gomi1, r.gomi2) for r in st.rows}
    swapped = {(1, a.date): st.rows[[i for i, r in enumerate(st.rows) if (r.b, r.date) == (1, a.date)][0]].kagi,
               (1, b.date): st.rows[[i for i, r in enumerate(st.rows) if (r.b, r.date) == (1, b.date)][0]].kagi}
    # 4日後に新採用「新人」追加・「青木」退職
    st.backend.roster.append(["新人", "○", "○", ""])
    st.backend.roster = [r for r in st.backend.roster if r[0] != "青木"]
    clock["now"] = datetime(2026, 11, 30, 9, 0, tzinfo=TZ)
    j = c.get("/api/state?b=1").json
    st.refresh_roster()
    assert "青木" in st.banner()["stale"] and "新人" in st.banner()["new"]
    res = c.post("/api/admin/allocate", json={}).json
    assert res["from"] == "2026-12-01"
    cur = {(r.b, r.date): r for r in st.rows}
    for (b_, d), v in snap.items():                                     # 過去分(〜11/30)は不変
        if d <= date(2026, 11, 30):
            assert (cur[(b_, d)].kagi, cur[(b_, d)].gomi1, cur[(b_, d)].gomi2) == v
    for k, n in swapped.items():                                        # 交代済みは不変
        if k[1] > date(2026, 11, 30):
            assert cur[k].kagi == n
    future = [r for r in st.rows if r.date > date(2026, 11, 30)]
    names = {n for r in future for n in (r.kagi, r.gomi1, r.gomi2)}
    assert "新人" in names and "青木" not in names
    st.load()                                                           # シートに保存されている
    assert {(r.b, r.date) for r in st.rows} == set(cur)


def test_two_buildings_independent_pages():
    app, st, _ = make()
    c = app.test_client()
    assert c.get("/1").status_code == 200 and c.get("/2").status_code == 200
    j1, j2 = c.get("/api/state?b=1").json, c.get("/api/state?b=2").json
    assert j1["building_name"] == "1号館" and j2["building_name"] == "2号館"
    assert j1["rows"] != j2["rows"]
    assert j1["today_row"]["date"] == "2026-11-24"
    assert j1["tomorrow_row"]["date"] == "2026-11-25"


def test_next_fiscal_year():
    app, st, _ = make()
    c = app.test_client()
    r = c.post("/api/admin/allocate", json={"fy": 2027}).json
    assert r["from"] == "2027-04-01" and r["unassigned"] == 0
    assert any(x.date.year == 2027 and x.date.month == 4 for x in st.rows)
    assert any(x.date == date(2026, 11, 24) for x in st.rows)



def test_swap_rejects_stale_screen():
    app, st, _ = make()
    c = app.test_client()
    rows = [r for r in st.rows if r.b == 1]
    a, b, d = rows[2], rows[6], rows[9]
    A = {"date": a.date.isoformat(), "slot": "kagi", "name": a.kagi}
    B = {"date": b.date.isoformat(), "slot": "kagi", "name": b.kagi}
    D = {"date": d.date.isoformat(), "slot": "kagi", "name": d.kagi}
    assert c.post("/api/swap", json={"b": 1, "a": A, "c": B}).status_code == 200
    # 別の人の画面はまだ A の旧担当を表示している → 拒否される
    r = c.post("/api/swap", json={"b": 1, "a": A, "c": D})
    assert r.status_code == 400 and "古く" in r.json["error"]
    cur = {x.date: x for x in st.rows if x.b == 1}
    assert cur[d.date].kagi == D["name"]          # D の人は動いていない

