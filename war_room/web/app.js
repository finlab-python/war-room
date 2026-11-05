'use strict';
// 戰情室前端：圖一次載入，事件推論（沿供應鏈找受影響公司）全在瀏覽器裡算；股價反應與原文證據向 ClickHouse 查。

const REPO = 'git+https://github.com/finlab-python/war-room';
const $ = s => document.querySelector(s);
const KIND = {event: '事件', commodity: '原物料', entity: '企業', segment: '產業', company: '公司'};
const EDGE_KIND = {world: '產業常識', theme: '產業鏈分類', input: '年報・主要原料', product: '年報・主要產品',
  supplier: '年報・主要供應商', customer: '年報・主要客戶', exposure: '年報・公司自述'};
const ENGINE = {claude: 'Claude Code', codex: 'Codex'};
const HINDSIGHT = new Set(['exposure']); // 公司事後自述：拿來解釋，不拿來算命中率
const DEPTHS = [2, 3, 4, 5, 6, 7];
const WIN = {5: '一週', 20: '一個月', 60: '三個月'};
const S = {depth: 5, win: 5, filter: 'all', limit: 60, seeds: [], selected: null, react: null, evidence: new Map()};
const N = new Map(), OUT = new Map(), IN = new Map();
let cy;

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const pct = (x, d = 1) => x == null ? '—' : `${x > 0 ? '+' : x < 0 ? '−' : ''}${Math.abs(x * 100).toFixed(d)}%`;
const tone = x => x > 0 ? 'up' : x < 0 ? 'down' : 'muted';
const signWord = s => s > 0 ? ['受惠', 'up'] : s < 0 ? ['受害', 'down'] : ['未定', 'flat'];
const md = d => d ? `${+d.slice(5, 7)}/${+d.slice(8, 10)}` : '';
const ymd = d => d ? `${d.slice(0, 4)}/${+d.slice(5, 7)}/${+d.slice(8, 10)}` : '';
const size = b => b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(1)} MB`;
const name = id => N.get(id)?.name ?? id;
const api = async path => { const r = await fetch(S.cfg.api + path); if (!r.ok) throw new Error(`${r.status}`); return r.json(); };
const add = (m, k, v) => (m.get(k) || m.set(k, []).get(k)).push(v);

async function init() {
  S.cfg = await (await fetch('/api/config')).json();
  const t = performance.now();
  S.g = await api('/api/graph');
  S.loadMs = performance.now() - t;
  for (const [id, kind, nm, sub] of S.g.nodes) N.set(id, {id, kind, name: nm, sub});
  for (const [s, t2, e, w, k, id] of S.g.edges) { const edge = {s, t: t2, e, w, k, id}; add(OUT, s, edge); add(IN, t2, edge); }
  renderStatic();
  if (!fromHash()) pickEvent(S.g.events[0].id);
}

// ── 推論：從起點沿邊往外走，每個節點留分數最高的一條路（分數＝沿路權重相乘） ──
function ripple(seeds, depth, skip) {
  const best = new Map();
  let frontier = seeds.filter(id => N.has(id));
  for (const id of frontier) best.set(id, {score: 1, sign: 1, depth: 0, prev: null, edge: null});
  for (let d = 1; d <= depth && frontier.length; d++) {
    const next = new Set(), snap = frontier.map(id => [id, best.get(id)]);  // 這一層只從上一層的紀錄出發，層數才不會錯
    for (const [id, cur] of snap) {
      for (const e of OUT.get(id) || []) {
        if (skip?.has(e.k)) continue;
        const score = cur.score * e.w, old = best.get(e.t);
        if (score < .02 || (old && old.score >= score)) continue;
        best.set(e.t, {score, sign: cur.sign * e.e, depth: d, prev: id, edge: e});
        next.add(e.t);
      }
    }
    frontier = [...next];
  }
  // 公司常被好幾條路同時碰到（原料漲價受害、產品漲價受惠）：有方向的路全部加總看淨影響，路徑顯示同方向最強的那條
  for (const [id, r] of best) {
    if (N.get(id).kind !== 'company' || !r.depth) continue;
    let net = 0;
    const top = {};
    for (const e of IN.get(id) || []) {
      const s = best.get(e.s);
      if (!s || !e.e || !s.sign || skip?.has(e.k)) continue;
      const v = s.sign * e.e * s.score * e.w, k = v > 0 ? 'pro' : 'con';
      net += v;
      r[k] = (r[k] || 0) + 1;
      if (!top[k] || Math.abs(v) > Math.abs(top[k].v)) top[k] = {v, e, s};
    }
    const pick = net > 0 ? top.pro : net < 0 ? top.con : null;
    if (pick) Object.assign(r, {sign: Math.sign(net), prev: pick.e.s, edge: pick.e, depth: pick.s.depth + 1});
  }
  return best;
}
const pathOf = (best, id) => { const p = []; for (let c = id; c != null; c = best.get(c).prev) p.unshift(c); return p; };

function compute() {
  const t = performance.now();
  S.best = ripple(S.seeds, S.depth);
  S.prior = ripple(S.seeds, S.depth, HINDSIGHT);
  S.rippleMs = performance.now() - t;
  S.companies = [...S.best].filter(([id]) => N.get(id).kind === 'company')
    .map(([id, r]) => ({id, sid: N.get(id).sub, ...r})).sort((a, b) => b.score - a.score);
}

function run() {
  compute();
  if (S.selected && !S.best.has(S.selected)) S.selected = null;
  $('#summary').hidden = $('#grid').hidden = $('#how').hidden = false;
  renderTitle(); renderControls(); renderTiles(); renderList(); renderGraph(); renderDetail(); renderSpeed();
  writeHash();
  loadReaction();
}

async function loadReaction() {
  const ids = S.companies.map(c => c.sid).slice(0, 800), key = `${S.start}|${ids}`;
  if (S.reactKey === key) return;
  S.reactKey = key; S.react = null;
  try {
    const r = await api(`/api/reaction?start=${S.start}&ids=${ids.join(',')}`);
    if (S.reactKey !== key) return;
    S.react = r;
  } catch (e) { S.react = {error: true, stocks: {}}; }
  renderTiles(); renderList(); renderDetail(); renderSpeed();
}

// ── 選起點 ──
function pickEvent(id) {
  const ev = S.g.events.find(e => e.id === id);
  Object.assign(S, {eventId: id, seeds: ev.seeds, title: `${ev.name}：${ev.sub}`, note: ev.note, source: ev.source, start: ev.start, selected: null, limit: 60});
  run();
}
function pickSeeds(seeds, title, note = '', selected = null) {
  Object.assign(S, {eventId: null, seeds, title, note, source: '', selected, limit: 60});
  run();
}
function pickNode(id) {
  const n = N.get(id);
  const ev = n.kind === 'event' && S.g.events.find(e => e.seeds.includes(id));
  if (ev) return pickEvent(ev.id);
  pickSeeds([id], `如果「${n.name}」受到衝擊`, n.kind === 'company' ? '沿著它的客戶與供應商往外推。' : '沿著用到它的產業與公司往外推。', id);
}

async function askAI(text) {
  const engine = ENGINE[S.cfg.ai];
  $('#aiNote').innerHTML = `<span class="dot"></span>你的 ${engine} 正在判斷「${esc(text)}」會先衝擊哪裡…（約 10 秒，用的是你自己的訂閱）`;
  try {
    const r = await (await fetch('/api/ai', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text})})).json();
    if (r.error || !r.seeds?.length) throw new Error(r.error || 'AI 沒有找到起點');
    if (/^\d{4}-\d{2}-\d{2}$/.test(r.start) && r.start <= S.g.stats.latest) {
      S.start = r.start;
      S.aiEvent = {id: 'ai', name: r.title, start: r.start};
    }
    renderAiNote(`${engine} 判斷的起點：${r.seeds.map(s => `<b>${esc(name(s.id))}</b>（${esc(s.why)}）`).join('、')}`);
    pickSeeds(r.seeds.map(s => s.id), r.title, `由你的 ${engine} 從「${text}」推出起點。`);
  } catch (e) {
    renderAiNote(`<span class="error">${esc(e.message)}</span>`);
  }
}

// ── 畫面 ──
function renderStatic() {
  const st = S.g.stats;
  $('#meta').textContent = `股價至 ${ymd(st.latest)} ・ ${S.cfg.ai ? `AI：你的 ${ENGINE[S.cfg.ai]}` : '公開版'}`;
  $('#events').innerHTML = S.g.events.map(e => `<button class="chip" data-event="${e.id}">${esc(e.name)}<small>${esc(e.sub)}</small></button>`).join('');
  $('#events').onclick = ev => { const b = ev.target.closest('[data-event]'); if (b) pickEvent(b.dataset.event); };
  renderAiNote();
  const ratio = Math.round(st.pdf_bytes / st.docs_stored);
  $('#steps').innerHTML = [
    ['年報', `${st.reports.toLocaleString()} 份`, `公開資訊觀測站的 2025 年度股東會年報，PDF 共 ${size(st.pdf_bytes)}、${st.pages.toLocaleString()} 頁。`],
    ['存進 ClickHouse', size(st.docs_stored), `全文逐頁存進 ClickHouse，只剩 PDF 的 1/${ratio}；任何一頁都能即時調出來當證據。`],
    ['AI 抽出關係', `${st.relations.toLocaleString()} 條`, `原料、產品、供應商、客戶與公司自述的風險，每條附頁碼與引文；引文對不上原文的 ${st.dropped} 條直接丟掉。`],
    ['供應鏈圖', `${st.edges.toLocaleString()} 條邊`, `再加上產業鏈分類與原物料常識。事件推論在你的瀏覽器裡算，不用等伺服器。`],
  ].map(([k, v, p]) => `<div class="step"><span>${k}</span><b>${v}</b><p>${p}</p></div>`).join('');
  bindSearch();
  $('#depth').onclick = ev => { const b = ev.target.closest('button'); if (b) { S.depth = +b.dataset.v; run(); } };
  $('#filter').onclick = ev => { const b = ev.target.closest('button'); if (b) { S.filter = b.dataset.v; S.limit = 60; renderControls(); renderList(); } };
  $('#more').onclick = () => { S.limit += 100; renderList(); };
  $('#win').onclick = ev => { const b = ev.target.closest('button'); if (b) { S.win = +b.dataset.v; renderControls(); renderTiles(); renderList(); renderDetail(); } };
  $('#list').onclick = ev => { const b = ev.target.closest('[data-id]'); if (b) select(b.dataset.id, true); };
  $('#verify').onchange = ev => { S.start = ev.target.value; writeHash(); loadReaction(); renderTitle(); };
}

function renderAiNote(html) {
  $('#aiNote').innerHTML = html ?? (S.cfg.ai
    ? `也可以直接打一段事件描述按 Enter，例如「台積電美國廠停電」，由你這台電腦上的 ${ENGINE[S.cfg.ai]} 判斷起點。`
    : `想推論任何事件？在你的電腦上執行 <code>uvx --from ${REPO} war-room</code>，就會用你自己的 Claude Code 或 Codex。`);
}

function renderTitle() {
  $('#title').textContent = S.title;
  $('#note').innerHTML = `${esc(S.note)}${S.source ? ` <a href="${esc(S.source)}" target="_blank" rel="noopener">出處</a>` : ''}`;
  const opts = [...(S.aiEvent ? [S.aiEvent] : []), ...S.g.events];
  $('#verify').innerHTML = opts.map(e => `<option value="${e.start}" ${e.start === S.start ? 'selected' : ''}>${esc(e.name)}（${ymd(e.start)}）</option>`).join('');
  document.querySelectorAll('#events .chip').forEach(b => b.setAttribute('aria-pressed', b.dataset.event === S.eventId));
}

function renderControls() {
  $('#win').innerHTML = Object.entries(WIN).map(([v, l]) => `<button data-v="${v}" aria-pressed="${+v === S.win}">${l}</button>`).join('');
  $('#depth').innerHTML = DEPTHS.map(d => `<button data-v="${d}" aria-pressed="${d === S.depth}">${d} 層</button>`).join('');
  const c = {all: S.companies.length, up: 0, down: 0, flat: 0};
  for (const x of S.companies) c[signWord(x.sign)[1]]++;
  $('#filter').innerHTML = [['all', '全部'], ['up', '受惠'], ['down', '受害'], ['flat', '未定']]
    .map(([v, l]) => `<button data-v="${v}" aria-pressed="${v === S.filter}">${l} ${c[v]}</button>`).join('');
  $('#count').textContent = `${S.companies.length} 家`;
}

function renderTiles() {
  const r = S.react, n = S.companies.length;
  const groups = {1: [], '-1': []};
  let hit = 0, total = 0;
  if (r?.stocks) for (const [id, p] of S.prior) {
    const nd = N.get(id), v = r.stocks[nd.sub]?.at[S.win];
    if (nd.kind !== 'company' || !p.sign || v == null) continue;
    groups[p.sign].push(v); total++; if (Math.sign(v) === p.sign) hit++;
  }
  const avg = a => a.length ? a.reduce((s, x) => s + x, 0) / a.length : null;
  const up = avg(groups[1]), down = avg(groups[-1]);
  const wait = r ? '' : ' <small>計算中</small>';
  const cnt = {up: 0, down: 0, flat: 0};
  for (const x of S.companies) cnt[signWord(x.sign)[1]]++;
  $('#tiles').innerHTML = [
    ['受影響的公司', `${n}<small>家</small>`, `受惠 ${cnt.up}・受害 ${cnt.down}・方向未定 ${cnt.flat}`],
    [`推論受惠的公司，${WIN[S.win]}後`, `<span class="${tone(up)}">${pct(up)}</span>${wait}`, `${groups[1].length} 家的平均超額報酬`],
    [`推論受害的公司，${WIN[S.win]}後`, `<span class="${tone(down)}">${pct(down)}</span>${wait}`, `${groups[-1].length} 家的平均超額報酬`],
    ['方向猜對', total ? `${Math.round(hit / total * 100)}<small>%</small>` : `—${wait}`, total ? `${hit} / ${total} 家；${up != null && down != null ? `兩組差 ${((up - down) * 100).toFixed(1)} 個百分點` : ''}` : '這段期間沒有股價'],
  ].map(([k, v, p]) => `<div class="tile"><span>${k}</span><b>${v}</b><p>${p}</p></div>`).join('');
  const w = r?.windows?.[S.win];
  $('#method').textContent = w?.end
    ? `以 ${ymd(r.base)} 收盤為基準，算事件後 ${S.win} 個交易日（到 ${ymd(w.end)}）相對全體上市櫃股票等權平均的超額報酬；同期等權大盤 ${pct(w.market)}、加權指數 ${pct(w.taiex)}。驗證只用事前就知道的關係（產業鏈、原料、產品、供應商），不含公司事後在年報裡的自述。`
    : r?.base ? `事件後還不到 ${S.win} 個交易日。` : '';
  $('#colA').textContent = WIN[S.win];
  $('#colB').textContent = r?.end ? `到 ${md(r.end)}${r.end.slice(0, 4) !== S.start.slice(0, 4) ? `/${r.end.slice(2, 4)}` : ''}` : '之後';
}

function spark(values, w = 64, h = 24) {
  if (!values?.length) return '';
  const lo = Math.min(0, ...values), hi = Math.max(0, ...values), span = hi - lo || 1;
  const pts = values.map((v, i) => `${(i / Math.max(1, values.length - 1) * w).toFixed(1)},${(h - (v - lo) / span * h).toFixed(1)}`).join(' ');
  const zero = (h - (0 - lo) / span * h).toFixed(1), last = values[values.length - 1];
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><line x1="0" x2="${w}" y1="${zero}" y2="${zero}" stroke="currentColor" stroke-opacity=".18" vector-effect="non-scaling-stroke"/><polyline points="${pts}" fill="none" stroke="var(--${last >= 0 ? 'up' : 'down'})" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>`;
}

function renderList() {
  const rows = S.companies.filter(c => S.filter === 'all' || signWord(c.sign)[1] === S.filter);
  const st = S.react?.stocks;
  $('#list').innerHTML = rows.slice(0, S.limit).map(c => {
    const [word, cls] = signWord(c.sign), x = st?.[c.sid];
    const path = pathOf(S.best, c.id).slice(0, -1).map(name).join(' → ');
    const a = st ? x ? `<span class="${tone(x.at[S.win])}">${pct(x.at[S.win])}</span>` : '—' : '…';
    const b = st ? x ? `<span class="spark"><span class="${tone(x.now)}">${pct(x.now, 0)}</span>${spark(x.spark)}</span>` : '<small>沒有股價</small>' : '…';
    return `<button class="row" data-id="${c.id}" aria-current="${c.id === S.selected}">
      <span><span class="row-title"><span class="pill ${cls}">${word}</span>${esc(name(c.id))}<small>${c.sid}</small></span>
      <span class="row-path">${esc(path)}</span></span><span class="ret">${a}</span><span class="ret">${b}</span></button>`;
  }).join('') || '<p class="loading">這個起點在目前的層數內沒有碰到上市櫃公司，試著多擴散幾層。</p>';
  $('#more').hidden = rows.length <= S.limit;
  $('#more').textContent = `再顯示 ${Math.min(100, rows.length - S.limit)} 家（共 ${rows.length} 家）`;
}

// ── 傳導圖：Cytoscape，同心圓一圈一層（漣漪） ──
function palette() {
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  return dark ? {bg: '#100b0d', text: '#e5e7eb', faint: '#6b7280', rule: '#3f3a3c', accent: '#3378f6', amber: '#f0a72a', up: '#f2555c', down: '#2fbf7f'}
    : {bg: '#ffffff', text: '#1f2937', faint: '#9ca3af', rule: '#d1d5db', accent: '#3378f6', amber: '#d98a00', up: '#d9363e', down: '#10935a'};
}
function renderGraph() {
  const best = S.best, c = palette();
  const top = [...best].sort((a, b) => b[1].score - a[1].score).slice(0, 55).map(([id]) => id);
  const keep = new Set();
  for (const id of top) for (const p of pathOf(best, id)) keep.add(p);
  // 同一圈裡照「父節點」順序排，兄弟節點擺在一起，線才不會交叉成一團
  const kids = new Map(), order = [];
  for (const id of keep) if (best.get(id).prev != null) add(kids, best.get(id).prev, id);
  const visit = id => { order.push(id); (kids.get(id) || []).sort((a, b) => best.get(b).score - best.get(a).score).forEach(visit); };
  S.seeds.filter(id => keep.has(id)).forEach(visit);
  const color = (kind, sign) => kind === 'event' ? c.accent : kind === 'commodity' || kind === 'entity' ? c.amber
    : kind === 'segment' ? c.bg : sign > 0 ? c.up : sign < 0 ? c.down : c.faint;
  const els = [];
  for (const id of order) {
    const n = N.get(id), r = best.get(id);
    els.push({data: {id, label: n.name, kind: n.kind, depth: r.depth, size: n.kind === 'event' ? 40 : 12 + 22 * Math.sqrt(r.score), color: color(n.kind, r.sign)}});
  }
  for (const id of order) {
    const r = best.get(id);
    if (r.prev && keep.has(r.prev)) els.push({data: {id: `e${r.edge.id}`, source: r.prev, target: id, color: r.sign > 0 ? c.up : r.sign < 0 ? c.down : c.faint, w: .6 + 2 * r.edge.w}});
  }
  if (!cy) {
    cy = cytoscape({container: $('#cy'), wheelSensitivity: .25, minZoom: .25, maxZoom: 3, boxSelectionEnabled: false});
    cy.on('tap', 'node', ev => select(ev.target.id(), false));
    cy.on('tap', ev => { if (ev.target === cy) select(null); });
  }
  cy.style([
    {selector: 'node', style: {'background-color': 'data(color)', width: 'data(size)', height: 'data(size)', label: 'data(label)', color: c.text,
      'font-size': 11, 'font-family': 'PingFang TC, Noto Sans TC, sans-serif', 'text-valign': 'bottom', 'text-margin-y': 3,
      'text-outline-color': c.bg, 'text-outline-width': 2.5, 'border-width': 0, 'min-zoomed-font-size': 7}},
    {selector: 'node[kind="segment"]', style: {'border-width': 1.5, 'border-color': c.faint, shape: 'round-rectangle'}},
    {selector: 'node[kind="event"]', style: {'font-size': 13, 'font-weight': 600, 'text-valign': 'center', color: '#fff', 'text-outline-width': 0}},
    {selector: 'edge', style: {width: 'data(w)', 'line-color': 'data(color)', 'line-opacity': .35, 'curve-style': 'straight',
      'target-arrow-shape': 'triangle', 'target-arrow-color': 'data(color)', 'arrow-scale': .7}},
    {selector: '.dim', style: {opacity: .15}},
    {selector: 'node.hl', style: {'border-width': 3, 'border-color': c.text}},
    {selector: 'edge.hl', style: {'line-opacity': 1, width: 3}},
  ]);
  cy.batch(() => { cy.elements().remove(); cy.add(els); });
  // 放射狀樹（漣漪）：一圈一層；葉子照順序平均分角度，父節點放在子節點的中間，兄弟擺在一起
  // 越內圈的葉子半徑越小，要分到越大的角度，間距才一樣
  const ring = new Map(order.map(id => [id, pathOf(best, id).length - 1]));  // 用實際路徑長度當圈數
  const share = id => 1 / Math.max(1, ring.get(id));
  const total = order.filter(id => !kids.get(id)?.length).reduce((s, id) => s + share(id), 0), angle = new Map();
  let at = 0;
  const place = id => {
    const ks = kids.get(id) || [], a = ks.length ? ks.map(place) : [(at += share(id)) - share(id) / 2].map(x => x / total * 2 * Math.PI);
    angle.set(id, (Math.min(...a) + Math.max(...a)) / 2);
    return angle.get(id);
  };
  S.seeds.filter(id => keep.has(id)).forEach(place);
  const radius = id => (ring.get(id) || (S.seeds.length > 1 ? .4 : 0)) * 150;
  cy.layout({name: 'preset', positions: n => ({x: radius(n.id()) * Math.cos(angle.get(n.id())), y: radius(n.id()) * Math.sin(angle.get(n.id()))}), fit: true, padding: 24, animate: false}).run();
  highlight();
}
function highlight() {
  if (!cy) return;
  cy.elements().removeClass('hl dim');
  if (!S.selected || !S.best.has(S.selected)) return;
  const path = pathOf(S.best, S.selected);
  cy.elements().addClass('dim');
  path.forEach((id, i) => {
    cy.getElementById(id).removeClass('dim').addClass('hl');
    if (i) cy.getElementById(`e${S.best.get(id).edge.id}`).removeClass('dim').addClass('hl');
  });
}

// ── 細節：為什麼受影響、證據、實際走勢 ──
function select(id, fromList) {
  S.selected = id;
  document.querySelectorAll('#list .row').forEach(r => r.setAttribute('aria-current', r.dataset.id === id));
  highlight(); renderDetail();
  if (id && fromList !== false) $('#detail').scrollIntoView({behavior: 'smooth', block: 'nearest'});
}

async function evidence(eid) {
  if (!S.evidence.has(eid)) S.evidence.set(eid, api(`/api/evidence?id=${eid}`).catch(() => ({})));
  return S.evidence.get(eid);
}

function evidenceHtml(e, ev) {
  const src = ev.source?.startsWith('http')
    ? `<a href="${esc(ev.source)}" target="_blank" rel="noopener">${ev.page ? `年報第 ${ev.page} 頁` : '出處'}</a>`
    : esc(ev.source || '');
  const quote = ev.context ? `<p class="quote">…${esc(ev.context[0])}<mark>${esc(ev.context[1])}</mark>${esc(ev.context[2])}…</p>` : '';
  return `<p class="why"><span class="kind">${EDGE_KIND[e.k]}</span>${esc(ev.why || '')}</p>${quote}${src ? `<p class="src">${src}</p>` : ''}`;
}

async function renderDetail() {
  const id = S.selected, box = $('#detail');
  if (!id || !S.best.has(id)) { box.hidden = true; return; }
  const n = N.get(id), r = S.best.get(id), path = pathOf(S.best, id), [word, cls] = signWord(r.sign);
  const x = n.kind === 'company' ? S.react?.stocks?.[n.sub] : null;
  box.hidden = false;
  const token = S.detailToken = Symbol();
  const chainItems = await Promise.all(path.map(async (pid, i) => {
    const pn = N.get(pid), pr = S.best.get(pid), dot = pn.kind === 'event' ? 'ev' : pn.kind === 'commodity' || pn.kind === 'entity' ? 'cm' : pn.kind === 'company' ? signWord(pr.sign)[1] : '';
    const ev = i ? evidenceHtml(pr.edge, await evidence(pr.edge.id)) : '<p class="why">起點</p>';
    return `<li class="${dot}"><b>${esc(pn.name)}</b> <small class="muted">${KIND[pn.kind]}${pn.kind === 'segment' ? `・${esc(pn.sub)}` : ''}</small>${ev}</li>`;
  }));
  const others = (IN.get(id) || []).filter(e => e !== r.edge && e.k !== 'theme' && S.best.has(e.s))
    .map(e => ({e, v: S.best.get(e.s).sign * e.e})).sort((a, b) => b.v - a.v).slice(0, 8);
  const otherItems = await Promise.all(others.map(async ({e, v}) => {
    const [w, c] = signWord(v);
    return `<li><span class="pill ${c}">${w}</span> <b>${esc(name(e.s))}</b>${evidenceHtml(e, await evidence(e.id))}</li>`;
  }));
  if (S.detailToken !== token) return;
  const rev = x?.revenue?.length ? `<h4>事件後的月營收年增率</h4><div class="bars">${x.revenue.map(([m, y]) => `<div><small>${+m.slice(5)} 月</small><b class="${tone(y)}">${y > 0 ? '+' : ''}${y}%</b></div>`).join('')}</div>` : '';
  const chart = x ? `<h4>相對等權大盤的累積超額報酬</h4><div class="chart">${spark(x.spark, 300, 96)}<p class="axis-row"><span>${ymd(S.react.base)}</span><span class="${tone(x.now)}">${pct(x.now)}</span><span>${ymd(S.react.end)}</span></p></div>` : '';
  box.innerHTML = `<div>
      <h3>${esc(n.name)} ${n.kind === 'company' ? `<small class="muted num">${n.sub}</small>` : ''}</h3>
      <p class="sub"><span class="pill ${cls}">${word}</span> ${KIND[n.kind]}・離起點 ${r.depth} 層${r.pro || r.con ? `・受惠路徑 ${r.pro || 0} 條、受害路徑 ${r.con || 0} 條` : ''}${x ? Object.entries(WIN).map(([n, l]) => `・${l} <span class="${tone(x.at[n])}">${pct(x.at[n])}</span>`).join('') + `・到 ${md(S.react.end)} <span class="${tone(x.now)}">${pct(x.now)}</span>` : ''}</p>
      <h4>為什麼會受影響</h4><ol class="chain">${chainItems.join('')}</ol>
      <button class="btn" id="whatif">如果「${esc(n.name)}」出事，會影響誰？</button>
    </div><div>${chart}${rev}${otherItems.length ? `<h4>其他碰到它的路</h4><ul class="others">${otherItems.join('')}</ul>` : ''}${!chart && !rev && !otherItems.length ? '<p class="muted">點列表裡的公司，看它的實際走勢與年報證據。</p>' : ''}</div>`;
  $('#whatif').onclick = () => pickNode(id);
}

function renderSpeed() {
  $('#speed').textContent = `載入整張圖 ${Math.round(S.loadMs)} ms ・ 推論 ${S.rippleMs.toFixed(1)} ms${S.react?.ms != null ? ` ・ ClickHouse 查 ${S.companies.length} 家股價與營收 ${S.react.ms} ms` : ''}`;
}

// ── 搜尋 ──
function bindSearch() {
  const q = $('#q'), box = $('#suggest');
  let items = [], at = 0;
  const order = {event: 0, commodity: 1, entity: 1, segment: 2, company: 3};
  const draw = () => {
    box.hidden = !items.length;
    box.innerHTML = items.map((it, i) => `<li role="option" aria-selected="${i === at}" data-i="${i}"><span class="kind">${it.kind}</span>${it.html}</li>`).join('');
  };
  q.oninput = () => {
    const s = q.value.trim().toLowerCase();
    items = [];
    if (s) {
      const hits = [];
      for (const n of N.values()) {
        const nm = n.name.toLowerCase(), sub = (n.sub || '').toLowerCase();
        const rank = nm === s ? 0 : nm.startsWith(s) ? 1 : nm.includes(s) ? 2 : sub.startsWith(s) ? 3 : -1;
        if (rank >= 0) hits.push([rank, order[n.kind], n]);
      }
      hits.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2].name.length - b[2].name.length);
      items = hits.slice(0, 8).map(([, , n]) => ({kind: KIND[n.kind], html: `${esc(n.name)} <small>${esc(n.sub || '')}</small>`, go: () => pickNode(n.id)}));
      if (S.cfg.ai && s.length >= 3) items.unshift({kind: 'AI 推論', html: `用你的 ${ENGINE[S.cfg.ai]} 推論「${esc(q.value.trim())}」`, go: () => askAI(q.value.trim())});
      if (!items.length) items = [{kind: '', html: '<small>找不到。在你的電腦上執行桌機版，就能用自己的 AI 推論任何事件。</small>', go: () => {}}];
    }
    at = 0; draw();
  };
  q.onkeydown = ev => {
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') { at = (at + (ev.key === 'ArrowDown' ? 1 : items.length - 1)) % Math.max(1, items.length); draw(); ev.preventDefault(); }
    if (ev.key === 'Enter' && items[at]) { items[at].go(); items = []; draw(); q.blur(); }
    if (ev.key === 'Escape') { items = []; draw(); }
  };
  box.onmousedown = ev => { const li = ev.target.closest('[data-i]'); if (li) { ev.preventDefault(); items[+li.dataset.i].go(); items = []; draw(); q.blur(); } };
  q.onblur = () => setTimeout(() => { items = []; draw(); }, 100);
}

// ── 網址：可以把目前的推論分享給別人 ──
function writeHash() {
  const p = new URLSearchParams(S.eventId ? {e: S.eventId} : {s: S.seeds.join(','), t: S.title});
  if (!S.eventId || S.start !== S.g.events.find(e => e.id === S.eventId).start) p.set('d', S.start);
  if (S.depth !== 5) p.set('n', S.depth);
  history.replaceState(null, '', `#${p}`);
}
function fromHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  if (p.get('n')) S.depth = +p.get('n');
  if (p.get('e') && S.g.events.some(e => e.id === p.get('e'))) {
    const ev = S.g.events.find(e => e.id === p.get('e'));
    Object.assign(S, {eventId: ev.id, seeds: ev.seeds, title: `${ev.name}：${ev.sub}`, note: ev.note, source: ev.source, start: p.get('d') || ev.start});
    run(); return true;
  }
  const seeds = (p.get('s') || '').split(',').filter(id => N.has(id));
  if (!seeds.length) return false;
  Object.assign(S, {eventId: null, seeds, title: p.get('t') || '自訂起點', note: '', source: '', start: p.get('d') || S.g.events[0].start});
  run(); return true;
}

init().catch(e => { document.querySelector('main').innerHTML = `<p class="loading error">載入失敗：${esc(e.message)}</p>`; });
