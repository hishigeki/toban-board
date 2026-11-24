"""当番表の状態管理（メモリ上キャッシュ + スプレッドシートへ書き込み）。"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .allocator import SLOT_LABEL, SLOTS, Person, allocate, slot_type
from .backends import HEADER_CLOSED  # noqa: F401  (re-export for convenience)
from .calendar_util import (
    dow_label,
    duty_days,
    fiscal_range,
    fiscal_year_of,
    parse_date,
)

log = logging.getLogger("toban")

TRUE_SET = {"○", "〇", "◯", "●", "◎", "TRUE", "1", "可", "はい", "YES", "Y", "✓", "✔", "レ"}
LABEL_SLOT = {v: k for k, v in SLOT_LABEL.items()}


class TobanError(Exception):
    """利用者に見せてよいエラー。"""


@dataclass
class Row:
    b: int
    date: date
    kagi: str = ""
    gomi1: str = ""
    gomi2: str = ""
    locked: set = field(default_factory=set)

    def get(self, slot: str) -> str:
        return getattr(self, slot)

    def set(self, slot: str, name: str) -> None:
        setattr(self, slot, name)

    def copy(self) -> "Row":
        return Row(self.b, self.date, self.kagi, self.gomi1, self.gomi2, set(self.locked))

    def to_sheet(self, building_name: str) -> list[str]:
        return [
            building_name,
            self.date.isoformat(),
            dow_label(self.date),
            self.kagi,
            self.gomi1,
            self.gomi2,
            ",".join(SLOT_LABEL[s] for s in SLOTS if s in self.locked),
        ]

    def to_json(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "dow": dow_label(self.date),
            "kagi": self.kagi,
            "gomi1": self.gomi1,
            "gomi2": self.gomi2,
            "locked": sorted(self.locked),
        }


def parse_roster(values: list[list[str]]) -> list[Person]:
    if not values:
        raise TobanError("職員名簿シートが空です")
    header = [str(h).strip() for h in values[0]]

    def find(*keys):
        for i, h in enumerate(header):
            if any(k in h for k in keys):
                return i
        return None

    c_name = find("氏名", "名前", "職員")
    c_kagi = find("鍵")
    c_gomi = find("ごみ", "ゴミ")
    c_bld = find("館")
    missing = [n for n, c in (("氏名", c_name), ("鍵当番", c_kagi), ("ごみ当番", c_gomi)) if c is None]
    if missing:
        raise TobanError(f"職員名簿の1行目に {'・'.join(missing)} の列が見つかりません")

    def cell(row, c):
        return str(row[c]).strip() if c is not None and c < len(row) else ""

    people, seen = [], set()
    for row in values[1:]:
        name = cell(row, c_name)
        if not name or name in seen:
            continue
        seen.add(name)
        bld_text = cell(row, c_bld)
        bld = None
        for n in (1, 2):
            if str(n) in bld_text:
                bld = n
        people.append(
            Person(
                name=name,
                kagi=cell(row, c_kagi).upper() in TRUE_SET,
                gomi=cell(row, c_gomi).upper() in TRUE_SET,
                building=bld,
            )
        )
    return people


class Store:
    def __init__(self, backend, buildings: list[str], tz, fiscal_start_month=4,
                 weekdays=(0, 1, 2, 3, 4), now_fn=None):
        self.backend = backend
        self.buildings = buildings  # ["1号館", "2号館"]
        self.tz = tz
        self.fy_month = fiscal_start_month
        self.weekdays = tuple(weekdays)
        self._now_fn = now_fn
        self.lock = threading.RLock()
        self.rows: list[Row] = []
        self.log_entries: list[dict] = []
        self.roster: list[Person] = []
        self.roster_error = ""
        self.last_sync = ""
        self.last_error = ""
        self.pending_log: list[list[str]] = []

    # ---- 時刻 ----
    def now(self) -> datetime:
        if self._now_fn:
            return self._now_fn()
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()

    # ---- 読み込み ----
    def _bnum(self, text: str) -> int | None:
        for i, name in enumerate(self.buildings, 1):
            if text.strip() == name:
                return i
        return None

    def load(self) -> None:
        """スプレッドシートから当番表・ログを読み直す。"""
        sched = self.backend.read_schedule()
        logs = self.backend.read_log()
        rows: list[Row] = []
        for r in sched[1:]:
            if len(r) < 6:
                continue
            b = self._bnum(r[0])
            d = parse_date(r[1])
            if b is None or d is None:
                continue
            locked = {LABEL_SLOT[t.strip()] for t in (r[6] if len(r) > 6 else "").split(",")
                      if t.strip() in LABEL_SLOT}
            rows.append(Row(b, d, r[3].strip(), r[4].strip(), r[5].strip(), locked))
        rows.sort(key=lambda x: (x.b, x.date))
        entries = []
        for r in logs[1:]:
            r = list(r) + [""] * (10 - len(r))
            if not r[0]:
                continue
            entries.append(
                {"at": r[0], "building": r[1], "kind": r[2],
                 "p1": r[3], "d1": r[4], "r1": r[5],
                 "p2": r[6], "d2": r[7], "r2": r[8], "note": r[9]}
            )
        with self.lock:
            self.rows = rows
            self.log_entries = entries
            self.last_sync = self.now().strftime("%Y-%m-%d %H:%M:%S")
            self.last_error = ""

    def refresh_roster(self) -> list[Person]:
        try:
            people = parse_roster(self.backend.read_roster())
        except TobanError as e:
            self.roster_error = str(e)
            raise
        except Exception as e:  # ネットワーク等
            self.roster_error = f"職員名簿を読めません: {e}"
            raise TobanError(self.roster_error)
        with self.lock:
            self.roster = people
            self.roster_error = ""
        return people

    def read_closed(self) -> set[date]:
        out = set()
        for r in self.backend.read_closed_days()[1:]:
            d = parse_date(r[0]) if r else None
            if d:
                out.add(d)
        return out

    def retry_pending_log(self) -> None:
        with self.lock:
            rest = []
            for row in self.pending_log:
                try:
                    self.backend.append_log(row)
                except Exception:
                    rest.append(row)
            self.pending_log = rest

    # ---- ヘルパ ----
    def _find(self, b: int, d: date) -> tuple[int, Row] | None:
        for i, r in enumerate(self.rows):
            if r.b == b and r.date == d:
                return i, r
        return None

    def _sheet_rows(self, rows: list[Row]) -> list[list[str]]:
        return [r.to_sheet(self.buildings[r.b - 1]) for r in rows]

    def _log(self, kind, building_name, p1="", d1="", r1="", p2="", d2="", r2="", note=""):
        at = self.now().strftime("%Y-%m-%d %H:%M:%S")
        line = [at, building_name, kind, p1, d1, r1, p2, d2, r2, note]
        self.log_entries.append(
            {"at": at, "building": building_name, "kind": kind,
             "p1": p1, "d1": d1, "r1": r1, "p2": p2, "d2": d2, "r2": r2, "note": note}
        )
        try:
            self.backend.append_log(line)
        except Exception as e:
            log.warning("交代ログの書き込みに失敗(後で再試行): %s", e)
            self.pending_log.append(line)

    # ---- 交代 ----
    def swap(self, b: int, d1: str, s1: str, d2: str, s2: str) -> dict:
        if not (1 <= b <= len(self.buildings)):
            raise TobanError("館が正しくありません")
        if s1 not in SLOTS or s2 not in SLOTS:
            raise TobanError("役割が正しくありません")
        if slot_type(s1) != slot_type(s2):
            raise TobanError("鍵当番とごみ当番をまたぐ交代はできません")
        date1, date2 = parse_date(d1), parse_date(d2)
        if not date1 or not date2:
            raise TobanError("日付が正しくありません")
        if (date1, s1) == (date2, s2):
            raise TobanError("同じ枠は選べません")
        with self.lock:
            today = self.today()
            if date1 < today or date2 < today:
                raise TobanError("過去日の当番は変更できません")
            f1, f2 = self._find(b, date1), self._find(b, date2)
            if not f1 or not f2:
                raise TobanError("当番表にない日付です")
            n1, n2 = f1[1].get(s1), f2[1].get(s2)
            if not n1 or not n2:
                raise TobanError("担当者のいない枠は入れ替えできません")
            if n1 == n2:
                raise TobanError("同じ人どうしは入れ替えできません")

            new1, new2 = f1[1].copy(), f2[1].copy()
            if date1 == date2:
                new2 = new1  # 同じ日の正副入れ替え
            new1.set(s1, n2)
            new2.set(s2, n1)
            new1.locked.add(s1)
            new2.locked.add(s2)

            updates = {f1[0]: new1, f2[0]: new2}
            bname = self.buildings[b - 1]
            self.backend.update_schedule_rows(
                [(i + 2, r.to_sheet(bname)) for i, r in sorted(updates.items())]
            )  # 失敗したら例外 → メモリは変更しない
            for i, r in updates.items():
                self.rows[i] = r

            self._log("交代", bname, n1, date1.isoformat(), SLOT_LABEL[s1],
                      n2, date2.isoformat(), SLOT_LABEL[s2])
            return {"ok": True, "p1": n1, "p2": n2}

    # ---- 自動割り当て ----
    def allocate(self, fy: int | None = None) -> dict:
        with self.lock:
            today = self.today()
            if fy is None:
                fy = fiscal_year_of(today, self.fy_month)
            roster = self.refresh_roster()
            if not any(p.kagi or p.gomi for p in roster):
                raise TobanError("鍵・ごみ当番を担当できる職員が名簿にいません")
            start, end = fiscal_range(fy, self.fy_month)
            fy_rows = [r for r in self.rows if start <= r.date <= end]
            from_date = max(start, today if not fy_rows else today + timedelta(days=1))
            if from_date > end:
                raise TobanError("この年度にはもう割り振る日がありません")

            days = set(duty_days(start, end, self.read_closed(), self.weekdays))
            fixed: dict[tuple[int, date], dict[str, str]] = {}
            keep_rows: list[Row] = []
            for r in fy_rows:
                if r.date < from_date:
                    keep_rows.append(r)
                    fixed[(r.b, r.date)] = {s: r.get(s) for s in SLOTS}
                elif r.locked:
                    days.add(r.date)  # 交代済みの日は休校日になっても残す
                    fixed[(r.b, r.date)] = {s: r.get(s) for s in r.locked}
            for r in fy_rows:  # 過去日(固定)の日付も集計対象にする
                if r.date < from_date:
                    days.add(r.date)

            bnums = list(range(1, len(self.buildings) + 1))
            seed = int(today.strftime("%Y%m%d"))
            result = allocate(sorted(days), bnums, roster, fixed, freeze_before=from_date, seed=seed)

            new_rows: list[Row] = list(keep_rows)
            unassigned = 0
            for (b, d), slots in sorted(result.items(), key=lambda kv: (kv[0][0], kv[0][1])):
                if d < from_date:
                    continue
                old = self._find(b, d)
                locked = set(old[1].locked) if old else set()
                row = Row(b, d, slots["kagi"], slots["gomi1"], slots["gomi2"], locked)
                unassigned += sum(1 for s in SLOTS if not row.get(s))
                new_rows.append(row)

            others = [r for r in self.rows if not (start <= r.date <= end)]
            merged = sorted(others + new_rows, key=lambda r: (r.b, r.date))
            self.backend.write_schedule(self._sheet_rows(merged))
            self.rows = merged
            self._log("割り振り", "全館", note=f"{fy}年度 {from_date.isoformat()}以降を割り振り"
                      + (f"(未割当{unassigned}枠)" if unassigned else ""))
            return {"fy": fy, "from": from_date.isoformat(), "rows": len(new_rows),
                    "unassigned": unassigned}

    # ---- 画面用データ ----
    def _roster_can(self, name: str, b: int, stype: str) -> bool:
        return any(p.name == name and p.can(stype, b) for p in self.roster)

    def banner(self) -> dict:
        today = self.today()
        fy = fiscal_year_of(today, self.fy_month)
        start, end = fiscal_range(fy, self.fy_month)
        stale, stale_locked = set(), set()
        in_fy = set()
        for r in self.rows:
            if not (start <= r.date <= end):
                continue
            for s in SLOTS:
                n = r.get(s)
                if not n:
                    continue
                in_fy.add(n)
                if r.date > today and not self._roster_can(n, r.b, slot_type(s)):
                    (stale_locked if s in r.locked else stale).add(n)
        new = sorted(p.name for p in self.roster if (p.kagi or p.gomi) and p.name not in in_fy) \
            if any(start <= r.date <= end for r in self.rows) else []
        return {"stale": sorted(stale), "stale_locked": sorted(stale_locked), "new": new}

    def view(self, b: int) -> dict:
        if not (1 <= b <= len(self.buildings)):
            raise TobanError("館が正しくありません")
        with self.lock:
            now = self.now()
            today = now.date()
            tomorrow = today + timedelta(days=1)
            rows = [r for r in self.rows if r.b == b]
            rj = [r.to_json() for r in rows]
            by_date = {r["date"]: r for r in rj}
            nxt = next((r for r in rj if r["date"] > today.isoformat()), None)

            fy = fiscal_year_of(today, self.fy_month)
            start, end = fiscal_range(fy, self.fy_month)
            counts = self._counts(b, rows, start, end, today)

            bname = self.buildings[b - 1]
            logs = [e for e in self.log_entries if e["building"] in (bname, "全館")]
            logs = sorted(logs, key=lambda e: e["at"], reverse=True)[:300]
            return {
                "now": now.strftime("%Y-%m-%d %H:%M:%S"),
                "today": today.isoformat(),
                "tomorrow": tomorrow.isoformat(),
                "building": b,
                "building_name": bname,
                "buildings": self.buildings,
                "fiscal_year": fy,
                "next_fiscal_year": fy + 1,
                "rows": rj,
                "today_row": by_date.get(today.isoformat()),
                "tomorrow_row": by_date.get(tomorrow.isoformat()),
                "next_row": nxt,
                "counts": counts,
                "log": logs,
                "banner": self.banner(),
                "roster_error": self.roster_error,
                "last_sync": self.last_sync,
                "last_error": self.last_error,
                "pending_log": len(self.pending_log),
            }

    def _counts(self, b, rows, start, end, today) -> list[dict]:
        stats: dict[str, dict] = {}

        def entry(name):
            return stats.setdefault(name, {
                "name": name,
                "kagi": {"total": 0, "left": 0, "next": ""},
                "gomi1": {"total": 0, "left": 0, "next": ""},
                "gomi2": {"total": 0, "left": 0, "next": ""},
                "gomi_next": "",
            })

        for p in self.roster:
            if p.can("kagi", b) or p.can("gomi", b):
                entry(p.name)
        for r in rows:
            if not (start <= r.date <= end):
                continue
            for s in SLOTS:
                n = r.get(s)
                if not n:
                    continue
                e = entry(n)
                e[s]["total"] += 1
                if r.date >= today:
                    e[s]["left"] += 1
                    if not e[s]["next"]:
                        e[s]["next"] = r.date.isoformat()
        out = []
        order = {p.name: i for i, p in enumerate(self.roster)}
        for name, e in stats.items():
            nexts = [e[s]["next"] for s in ("gomi1", "gomi2") if e[s]["next"]]
            e["gomi_next"] = min(nexts) if nexts else ""
            p = next((p for p in self.roster if p.name == name), None)
            e["can_kagi"] = bool(p and p.can("kagi", b))
            e["can_gomi"] = bool(p and p.can("gomi", b))
            e["in_roster"] = p is not None
            out.append(e)
        out.sort(key=lambda e: (order.get(e["name"], 10_000), e["name"]))
        return out
