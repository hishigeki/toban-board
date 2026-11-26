(() => {
  const B = Number(document.body.dataset.b);
  const DOW = "日月火水木金土";
  const SLOT_LABEL = { kagi: "鍵", gomi1: "ごみ(正)", gomi2: "ごみ(副)" };
  const COLS = '<colgroup><col class="c1"><col class="c2"><col><col><col></colgroup>';
  const typeOf = (s) => (s === "kagi" ? "kagi" : "gomi");
  let S = null; // サーバーの状態
  let mode = false;
  let sel = []; // [{date, slot, name}]
  let findText = "";

  const $ = (id) => document.getElementById(id);
  const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const md = (iso) => { const [, m, d] = iso.split("-"); return `${+m}/${+d}`; };
  const mdw = (iso) => { const [y, m, d] = iso.split("-").map(Number); return `${m}/${d}(${DOW[new Date(y, m - 1, d).getDay()]})`; };

  function toast(msg, err) {
    const t = $("toast");
    t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), err ? 6000 : 2800);
  }

  async function api(url, body) {
    const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const r = await fetch(url, opt);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || `エラー(${r.status})`);
    return j;
  }

  async function load(keepScroll = true) {
    const sc = $("scroller");
    const top = sc.scrollTop;
    S = await api(`/api/state?b=${B}`);
    render();
    if (keepScroll && top) { sc.style.scrollBehavior = "auto"; sc.scrollTop = top; sc.style.scrollBehavior = ""; }
  }

  // ---------- 描画 ----------
  function dutyHtml(row) {
    if (!row) return "";
    const who = (n, cls = "") => (n ? `<span class="who ${cls}">${esc(n)}</span>` : `<span class="who none">未定</span>`);
    return `<div class="duty"><span class="lbl">鍵</span>${who(row.kagi)}</div>
      <div class="duty"><span class="lbl">ごみ 正</span>${who(row.gomi1)}</div>
      <div class="duty"><span class="lbl">ごみ 副</span>${who(row.gomi2, "sub")}</div>`;
  }
  function card(cls, title, iso, row, nextRow) {
    let body;
    if (row) body = dutyHtml(row);
    else {
      body = `<div class="empty">当番はありません（休校日など）` +
        (nextRow && nextRow.date > iso ? `<br>次の当番日: <b>${mdw(nextRow.date)}</b>` : "") + `</div>`;
    }
    return `<div class="card ${cls}"><h2><span>${title}</span><b>${mdw(iso)}</b></h2>${body}</div>`;
  }
  function renderCards() {
    // 2枚目は「明日」ではなく「次の当番日」（休校日・土日をとばして、次に当番がある日）
    const nx = S.next_row;
    const next = nx
      ? card("tomorrow", `次の当番　${esc(S.building_name)}`, nx.date, nx, null)
      : `<div class="card tomorrow"><h2><span>次の当番　${esc(S.building_name)}</span></h2><div class="empty">この先の当番はありません</div></div>`;
    $("cards").innerHTML = card("today", `今日　${esc(S.building_name)}`, S.today, S.today_row, S.next_row) + next;
  }

  function renderBanner() {
    const b = S.banner || {}, el = $("banner"), msgs = [];
    if (S.roster_error) msgs.push(`⚠ ${esc(S.roster_error)}`);
    if (S.last_error) msgs.push(`⚠ スプレッドシートに接続できていません（最後に取得した内容を表示中）`);
    if (S.pending_log) msgs.push(`⚠ 交代ログ ${S.pending_log} 件の書き込みが保留中です（自動で再試行します）`);
    if ((b.stale || []).length) msgs.push(`名簿で担当できなくなった人が今後の当番に入っています: <b>${b.stale.map(esc).join("、")}</b>`);
    if ((b.new || []).length) msgs.push(`今年度の当番表にまだ入っていない人がいます: <b>${b.new.map(esc).join("、")}</b>`);
    if ((b.stale_locked || []).length) msgs.push(`交代済みの枠に名簿外の人が残っています: ${b.stale_locked.map(esc).join("、")}（交代で調整してください）`);
    if ((b.stale || []).length || (b.new || []).length) msgs.push(`→「管理」タブから再割り振りできます`);
    el.hidden = !msgs.length;
    el.innerHTML = msgs.join("<br>");
  }

  function nameCell(row, slot) {
    const n = row[slot];
    if (!n) return `<span class="nm dim">未定</span>`;
    const cls = ["nm"];
    const past = row.date < S.today;
    if (row.locked.includes(slot)) cls.push("swapped");
    if (findText && n.includes(findText)) cls.push("hit");
    if (mode && !past) cls.push("pick");
    const s = sel.find((x) => x.date === row.date && x.slot === slot);
    if (s) cls.push("sel");
    else if (mode && sel.length && typeOf(sel[0].slot) !== typeOf(slot)) cls.push("off");
    const attr = mode && !past ? ` data-date="${row.date}" data-slot="${slot}" data-name="${esc(n)}"` : "";
    const tip = row.locked.includes(slot) ? ' title="交代済み"' : "";
    return `<span class="${cls.join(" ")}"${attr}${tip}>${esc(n)}</span>`;
  }

  function renderTable() {
    const sc = $("scroller");
    sc.className = "scroller" + (mode ? " mode" : "");
    let html = "", cur = "";
    for (const r of S.rows) {
      const ym = r.date.slice(0, 7);
      if (ym !== cur) {
        if (cur) html += `</tbody></table></div>`;
        cur = ym;
        const [y, m] = ym.split("-");
        html += `<div class="month" id="m-${ym}"><h3>${y}年${+m}月</h3><table class="duties">${COLS}<tbody>`;
      }
      const cls = r.date < S.today ? "past" : r.date === S.today ? "today" : (S.next_row && r.date === S.next_row.date) ? "tomorrow" : "";
      html += `<tr class="${cls}" id="r-${r.date}"><td class="date">${md(r.date)}</td><td class="dow">${r.dow}</td>
        <td>${nameCell(r, "kagi")}</td><td>${nameCell(r, "gomi1")}</td><td>${nameCell(r, "gomi2")}</td></tr>`;
    }
    if (cur) html += `</tbody></table></div>`;
    sc.innerHTML = html || `<div class="empty" style="padding:20px">当番表がまだありません。「管理」タブから割り振ってください。</div>`;
  }

  function scrollToToday(smooth) {
    const sc = $("scroller");
    let row = $("r-" + S.today) || [...sc.querySelectorAll("tr[id^=r-]")].find((tr) => tr.id.slice(2) >= S.today);
    if (!row) return;
    const hdr = row.closest(".month").querySelector("h3").offsetHeight + 4;
    const y = row.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop - hdr;
    sc.style.scrollBehavior = smooth ? "smooth" : "auto";
    sc.scrollTop = Math.max(0, y);
  }

  function renderMode() {
    $("modebar").hidden = !mode;
    $("btnMode").textContent = mode ? "変更をやめる" : "変更";
    $("btnSwap").disabled = sel.length !== 2;
    const m = $("modeMsg");
    if (sel.length === 0) m.textContent = "入れ替えたい2名をタップしてください（鍵どうし、ごみどうし）";
    else if (sel.length === 1) m.innerHTML = `<b>${esc(sel[0].name)}</b>（${mdw(sel[0].date)} ${SLOT_LABEL[sel[0].slot]}）を選択中。もう1名を選んでください`;
    else m.innerHTML = `<b>${esc(sel[0].name)}</b>（${mdw(sel[0].date)}）⇄ <b>${esc(sel[1].name)}</b>（${mdw(sel[1].date)}）→「交代」を押すと入れ替わります`;
  }

  function renderCounts() {
    $("countsNote").textContent = `${S.fiscal_year}年度の予定回数です（括弧内は今日以降の残り）。回数が「−」の列は担当できません。`;
    const cell = (c, ok) => (!ok && !c.total ? `<td class="n dim">−</td>` : `<td class="n">${c.total}<span class="dim"> (${c.left})</span></td>`);
    let h = `<thead><tr><th>氏名</th><th class="n">鍵</th><th class="n">ごみ(正)</th><th class="n">ごみ(副)</th><th>次回 鍵</th><th>次回 ごみ</th></tr></thead><tbody>`;
    for (const e of S.counts) {
      const hit = findText && e.name.includes(findText) ? ' class="hit"' : "";
      h += `<tr${hit}><td>${esc(e.name)}${e.in_roster ? "" : ' <span class="out">(名簿外)</span>'}</td>
        ${cell(e.kagi, e.can_kagi)}${cell(e.gomi1, e.can_gomi)}${cell(e.gomi2, e.can_gomi)}
        <td>${e.kagi.next ? mdw(e.kagi.next) : "−"}</td><td>${e.gomi_next ? mdw(e.gomi_next) : "−"}</td></tr>`;
    }
    $("countsTable").innerHTML = h + "</tbody>";
  }

  function renderLog() {
    const swaps = S.log.filter((e) => e.kind === "交代");
    const badge = $("logBadge");
    badge.hidden = !swaps.length; badge.textContent = swaps.length;
    let h = `<thead><tr><th>操作日時</th><th>種別</th><th>入れ替えた2名</th><th>変更前 → 変更後</th></tr></thead><tbody>`;
    if (!S.log.length) h += `<tr><td colspan="4" class="dim">まだ変更はありません</td></tr>`;
    for (const e of S.log) {
      if (e.kind === "交代") {
        h += `<tr><td>${esc(e.at.slice(0, 16))}</td><td>${e.r1.startsWith("鍵") ? "鍵" : "ごみ"}</td>
          <td>${esc(e.p1)} ⇄ ${esc(e.p2)}</td>
          <td>${esc(e.p1)}: ${mdw(e.d1)} ${esc(e.r1)}<span class="arrow">→</span>${mdw(e.d2)} ${esc(e.r2)}<br>
              ${esc(e.p2)}: ${mdw(e.d2)} ${esc(e.r2)}<span class="arrow">→</span>${mdw(e.d1)} ${esc(e.r1)}</td></tr>`;
      } else {
        h += `<tr><td>${esc(e.at.slice(0, 16))}</td><td>${esc(e.kind)}</td><td class="dim">−</td><td>${esc(e.note)}</td></tr>`;
      }
    }
    $("logTable").innerHTML = h + "</tbody>";
  }

  function renderAdmin() {
    $("adminStatus").innerHTML = `<dt>最終同期</dt><dd>${esc(S.last_sync || "−")}</dd>
      <dt>現在時刻</dt><dd>${esc(S.now)}</dd>
      <dt>今年度</dt><dd>${S.fiscal_year}年度</dd>
      <dt>名簿の状態</dt><dd>${S.roster_error ? esc(S.roster_error) : "正常"}</dd>`;
  }

  function render() {
    renderBanner(); renderCards(); renderTable(); renderMode(); renderCounts(); renderLog(); renderAdmin();
  }

  // ---------- 操作 ----------
  $("scroller").addEventListener("click", (ev) => {
    const el = ev.target.closest(".nm.pick");
    if (!mode || !el) return;
    const item = { date: el.dataset.date, slot: el.dataset.slot, name: el.dataset.name };
    const i = sel.findIndex((x) => x.date === item.date && x.slot === item.slot);
    if (i >= 0) sel.splice(i, 1);
    else {
      if (sel.length && typeOf(sel[0].slot) !== typeOf(item.slot)) return toast("鍵当番とごみ当番をまたぐ交代はできません", true);
      if (sel.length === 1 && sel[0].name === item.name) return toast("同じ人どうしは入れ替えできません", true);
      if (sel.length >= 2) sel.shift();
      sel.push(item);
    }
    renderTable(); renderMode();
  });

  function setMode(on) { mode = on; sel = []; renderTable(); renderMode(); }
  $("btnMode").onclick = () => setMode(!mode);
  $("btnCancel").onclick = () => setMode(false);
  $("btnToday").onclick = () => scrollToToday(true);
  $("find").oninput = (e) => { findText = e.target.value.trim(); renderTable(); renderCounts(); };

  $("btnSwap").onclick = async () => {
    if (sel.length !== 2) return;
    const [a, c] = sel;
    if (!confirm(`${a.name}（${mdw(a.date)} ${SLOT_LABEL[a.slot]}）と\n${c.name}（${mdw(c.date)} ${SLOT_LABEL[c.slot]}）を入れ替えます。よろしいですか？`)) return;
    try {
      await api("/api/swap", { b: B, a: { date: a.date, slot: a.slot, name: a.name }, c: { date: c.date, slot: c.slot, name: c.name } });
      toast(`${a.name} と ${c.name} を入れ替えました`);
      mode = false; sel = [];
      await load();
    } catch (e) { toast(e.message, true); await load().catch(() => {}); sel = []; renderTable(); renderMode(); }
  };

  document.querySelectorAll("#tabs button").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
      document.querySelectorAll(".tabpane").forEach((p) => (p.hidden = p.id !== "tab-" + b.dataset.tab));
      if (b.dataset.tab === "table") scrollToToday(false);
    };
  });

  async function admin(label, fn) {
    try { const r = await fn(); toast(label + "：完了"); await load(); return r; }
    catch (e) { toast(e.message, true); }
  }
  $("btnReload").onclick = () => admin("再読込", () => api("/api/admin/reload", {}));
  $("btnAlloc").onclick = () => {
    if (!confirm("今日以降の当番を名簿に合わせて割り振り直します。\n過去分と交代済みの枠は変わりません。よろしいですか？")) return;
    admin("再割り振り", async () => {
      const r = await api("/api/admin/allocate", { fy: S.fiscal_year });
      if (r.unassigned) toast(`人数不足で ${r.unassigned} 枠が未定です`, true);
      return r;
    });
  };
  $("btnAllocNext").onclick = () => {
    if (!confirm(`${S.next_fiscal_year}年度の当番表を作成します（今の名簿で割り振り）。よろしいですか？`)) return;
    admin("次年度作成", () => api("/api/admin/allocate", { fy: S.next_fiscal_year }));
  };

  // ---------- 起動 ----------
  function tick() {
    const d = new Date();
    $("clock").textContent = `${d.getMonth() + 1}/${d.getDate()}(${DOW[d.getDay()]}) ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
  tick(); setInterval(tick, 30000);

  let lastDay = "";
  load(false).then(() => { scrollToToday(false); lastDay = S.today; }).catch((e) => toast(e.message, true));
  // 変更操作中でなければ1分ごとに更新。日付が変わったら今日の位置へ。
  setInterval(async () => {
    if (mode || document.hidden) return;
    try { await load(); if (S.today !== lastDay) { lastDay = S.today; scrollToToday(false); } } catch (_) {}
  }, 60000);
})();
