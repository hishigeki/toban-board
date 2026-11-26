"""当番の自動割り当て。

ルール
  * 鍵・ごみ(正)・ごみ(副)それぞれの担当回数を、担当できる人の間でできるだけ均等にする（差を1回以内に）
  * 連日（前後の当番日）の担当を避ける（人数が足りない場合のみ許容）
  * 同じ日に鍵とごみの両方、または両館に入らない
  * ごみの正副の組み合わせが偏らないようにする
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date

SLOTS = ("kagi", "gomi1", "gomi2")
SLOT_LABEL = {"kagi": "鍵", "gomi1": "ごみ(正)", "gomi2": "ごみ(副)"}


def slot_type(slot: str) -> str:
    return "kagi" if slot == "kagi" else "gomi"


@dataclass(frozen=True)
class Person:
    name: str
    kagi: bool
    gomi: bool
    building: int | None = None  # None = どちらの館でも可

    def can(self, stype: str, building: int) -> bool:
        if self.building not in (None, building):
            return False
        return self.kagi if stype == "kagi" else self.gomi


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def allocate(
    days: list[date],
    buildings: list[int],
    roster: list[Person],
    fixed: dict[tuple[int, date], dict[str, str]],
    freeze_before: date | None = None,
    seed: int = 0,
) -> dict[tuple[int, date], dict[str, str]]:
    """全 (館, 日付) の {slot: 氏名} を返す。

    fixed         … そのまま残す割り当て（過去分・交代済み）。回数集計にも使う。
    freeze_before … この日付より前の日は fixed のみ使い、新規に埋めない。
    人数不足で埋められない枠は空文字。
    """
    rnd = random.Random(seed)
    days = sorted(days)
    result: dict[tuple[int, date], dict[str, str]] = {
        (b, d): {s: "" for s in SLOTS} for d in days for b in buildings
    }
    on_day: dict[date, set[str]] = {d: set() for d in days}
    cnt = {"kagi": {}, "gomi": {}}
    slot_cnt = {s: {} for s in SLOTS}
    total: dict[str, int] = {}
    pair: dict[tuple[str, str], int] = {}

    def bump(name: str, slot: str) -> None:
        t = slot_type(slot)
        cnt[t][name] = cnt[t].get(name, 0) + 1
        slot_cnt[slot][name] = slot_cnt[slot].get(name, 0) + 1
        total[name] = total.get(name, 0) + 1

    for (b, d), slots in fixed.items():
        if (b, d) not in result:
            continue
        for s, n in slots.items():
            if s in SLOTS and n:
                result[(b, d)][s] = n
                on_day[d].add(n)
                bump(n, s)
        g1, g2 = slots.get("gomi1", ""), slots.get("gomi2", "")
        if g1 and g2:
            k = _pair_key(g1, g2)
            pair[k] = pair.get(k, 0) + 1

    last: dict[str, int] = {}
    for i, d in enumerate(days):
        if freeze_before is not None and d < freeze_before:
            for n in on_day[d]:
                last[n] = i
            continue
        prev = on_day[days[i - 1]] if i > 0 else set()
        nxt = on_day[days[i + 1]] if i + 1 < len(days) else set()
        order = list(buildings)
        if i % 2:
            order.reverse()  # 館の偏りを避けるため日ごとに先頭を入れ替える
        slot_order = [(b, "kagi") for b in order]
        slot_order += [(b, s) for b in order for s in ("gomi1", "gomi2")]

        for b, s in slot_order:
            if result[(b, d)][s]:
                continue  # 交代済みなどで固定
            stype = slot_type(s)
            partner = ""
            if s == "gomi1":
                partner = result[(b, d)]["gomi2"]
            elif s == "gomi2":
                partner = result[(b, d)]["gomi1"]
            best = None
            best_key = None
            for p in roster:
                if not p.can(stype, b) or p.name in on_day[d]:
                    continue
                near = p.name in prev or p.name in nxt
                c = cnt[stype].get(p.name, 0)
                pc = pair.get(_pair_key(partner, p.name), 0) if partner else 0
                rest = i - last.get(p.name, -10_000)
                key = (
                    near,
                    slot_cnt[s].get(p.name, 0) + 0.7 * pc,  # 鍵・正・副それぞれの回数を均す
                    c,
                    total.get(p.name, 0),
                    -min(rest, 60),
                    rnd.random(),
                )
                if best_key is None or key < best_key:
                    best, best_key = p, key
            if best is None:
                continue
            result[(b, d)][s] = best.name
            on_day[d].add(best.name)
            bump(best.name, s)
            if partner:
                k = _pair_key(partner, best.name)
                pair[k] = pair.get(k, 0) + 1
        for n in on_day[d]:
            last[n] = i
    fixed_slots = {(b, d, s) for (b, d), sl in fixed.items() for s, n in sl.items() if s in SLOTS and n}
    _rebalance(result, days, roster, on_day, fixed_slots, freeze_before, rnd)
    return result


def _rebalance(result, days, roster, on_day, fixed_slots, freeze_before, rnd) -> None:
    """仕上げ：鍵・ごみ(正)・ごみ(副)の回数が人によって2回以上ちがう間、多い人の枠を少ない人に移す。

    移すときは「担当できる人・同じ日に重複しない・連日にならない」を必ず守る。
    直接移せないときは、間に一人はさんで（多い人→C、Cの別の日→少ない人）の2段で移す。
    過去分・交代済みの枠は動かさない。
    """
    idx = {d: i for i, d in enumerate(days)}
    pmap = {p.name: p for p in roster}
    elig = {s: [p.name for p in roster if (p.kagi if s == "kagi" else p.gomi)] for s in SLOTS}
    cnt = {s: {n: 0 for n in elig[s]} for s in SLOTS}
    for sl in result.values():
        for s in SLOTS:
            n = sl[s]
            if n in cnt[s]:
                cnt[s][n] += 1

    def movable(b, d, s):
        return (freeze_before is None or d >= freeze_before) and (b, d, s) not in fixed_slots and result[(b, d)][s]

    def can_take(name, b, d, s):
        p = pmap.get(name)
        if p is None or not p.can(slot_type(s), b) or name in on_day[d]:
            return False
        i = idx[d]
        if i > 0 and name in on_day[days[i - 1]]:
            return False
        if i + 1 < len(days) and name in on_day[days[i + 1]]:
            return False
        return True

    def put(b, d, s, new):
        old = result[(b, d)][s]
        result[(b, d)][s] = new
        if old:
            on_day[d].discard(old)
            if old in cnt[s]:
                cnt[s][old] -= 1
        if new:
            on_day[d].add(new)
            if new in cnt[s]:
                cnt[s][new] += 1

    def holdings(name, s):
        return [(b, d) for (b, d), sl in result.items() if sl[s] == name and movable(b, d, s)]

    def try_move(a, low, s) -> bool:
        mine = holdings(a, s)
        rnd.shuffle(mine)
        for b, d in mine:                      # 直接移す
            if can_take(low, b, d, s):
                put(b, d, s, low)
                return True
        for b, d in mine:                      # 間に一人（C）はさむ
            for c in elig[s]:
                if c in (a, low) or not can_take(c, b, d, s):
                    continue
                put(b, d, s, c)
                for b2, e in holdings(c, s):
                    if (b2, e) != (b, d) and can_take(low, b2, e, s):
                        put(b2, e, s, low)
                        return True
                put(b, d, s, a)                # 戻す
        return False

    for _ in range(400):
        moved = False
        for s in SLOTS:
            names = sorted(elig[s], key=lambda n: cnt[s][n])
            pairs = [(a, lo) for lo in names for a in reversed(names) if cnt[s][a] - cnt[s][lo] >= 2]
            pairs.sort(key=lambda t: -(cnt[s][t[0]] - cnt[s][t[1]]))
            for a, lo in pairs:
                if try_move(a, lo, s):
                    moved = True
                    break
        # ごみ(正)と(副)の偏り：同じ日の正副を入れ替えると、鍵やごみ全体の回数は変えずに正副だけ整う
        for (b, d), sl in result.items():
            g1, g2 = sl["gomi1"], sl["gomi2"]
            if not (g1 and g2) or not movable(b, d, "gomi1") or not movable(b, d, "gomi2"):
                continue
            if g1 not in cnt["gomi1"] or g2 not in cnt["gomi2"]:
                continue
            before = cnt["gomi1"][g1] ** 2 + cnt["gomi2"][g2] ** 2 + cnt["gomi1"][g2] ** 2 + cnt["gomi2"][g1] ** 2
            after = (cnt["gomi1"][g1] - 1) ** 2 + (cnt["gomi2"][g2] - 1) ** 2 \
                + (cnt["gomi1"][g2] + 1) ** 2 + (cnt["gomi2"][g1] + 1) ** 2
            if after < before:
                sl["gomi1"], sl["gomi2"] = g2, g1
                cnt["gomi1"][g1] -= 1; cnt["gomi1"][g2] += 1
                cnt["gomi2"][g2] -= 1; cnt["gomi2"][g1] += 1
                moved = True
        if not moved:
            break
