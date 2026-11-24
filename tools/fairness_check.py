"""割り振りの公平さを数えて表示する確認用スクリプト。 python tools/fairness_check.py"""
import sys; import pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from datetime import date
from collections import Counter
from toban.allocator import allocate, Person
from toban.calendar_util import duty_days
names=[f"P{i:02d}" for i in range(20)]
roster=[Person(n, i%5!=4, i%7!=6) for i,n in enumerate(names)]
days=duty_days(date(2026,11,25),date(2027,3,31))
res=allocate(days,[1,2],roster,{},seed=1)
k=Counter();g=Counter();g1=Counter();pairs=Counter();adj=0;same=0;un=0
prev=None
for d in days:
    day=set()
    for b in (1,2):
        r=res[(b,d)]
        for s,n in r.items():
            if not n: un+=1; continue
            if n in day: same+=1
            day.add(n)
        k[r['kagi']]+=1; g[r['gomi1']]+=1; g[r['gomi2']]+=1; g1[r['gomi1']]+=1
        pairs[tuple(sorted((r['gomi1'],r['gomi2'])))]+=1
    if prev: adj+=len(day&prev)
    prev=day
print("days",len(days),"unassigned",un,"same-day dup",same,"consecutive",adj)
print("kagi",min(k.values()),max(k.values()),len(k))
print("gomi",min(g.values()),max(g.values()),len(g))
print("gomi正",min(g1.values()),max(g1.values()))
print("pairs",min(pairs.values()),max(pairs.values()),len(pairs))
