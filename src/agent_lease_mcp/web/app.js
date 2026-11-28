
const KEY_STORE = "agentLeaseKey";
const feed = document.getElementById("feed");
const statusDot = document.querySelector("#status .dot");
const statusText = document.getElementById("statusText");
const toEl = document.getElementById("to");
const textEl = document.getElementById("text");
const hintEl = document.getElementById("hint");
const showSystemEl = document.getElementById("showSystem");
let me = "";
let kind = "chat";
let knownAgents = ["claude-code", "codex"];
// Pouze prezentace existujících dat; žádné nové endpointy nebo odvozování fází.
const messageCache = new Map();
const expandedMessages = new Set();
let activeFilter = "all";
let selectedTask = null;
let renderingRoom = false;
let latestPeers = [];
let latestBoard = null; // null = autoritativní přehled není dostupný
let latestReviews = null;
let lastPaused; // undefined = stav dosud neznáme, null = provoz povolen
let connectionMode = "loading";

const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
let highestMessageId = 0;
let historyReady = false;
function motion(el, frames, duration) {
  if (!el || reducedMotion.matches || document.hidden || !el.getClientRects().length) return;
  el.animate(frames, {duration, easing:"cubic-bezier(0.22,1,0.36,1)"});
}
function setMetric(id, value) {
  const el = document.getElementById(id), next = String(value);
  if (el.textContent === next) return;
  const known = !["—", "Načítání…", "Připojování…"].includes(el.textContent);
  el.textContent = next;
  if (known) motion(el, [{opacity:.65,transform:"translateY(2px)"},{opacity:1,transform:"translateY(0)"}],240);
}
function setHTMLIfChanged(el, html) {
  if (el._renderHTML !== html) { el.innerHTML = html; el._renderHTML = html; }
}
function stateTone(state) {
  return state === "succeeded" ? "ok" : ["started","leased"].includes(state) ? "work"
    : ["failed","dead_letter"].includes(state) ? "bad" : state === "needs_review" ? "warn" : "muted";
}

// One native modal reuses the original panels and emergency control; no duplicate actions.
const drawer = document.getElementById("workspaceDrawer");
const panelPlacements = {};
for (const id of ["side","inspector","pause"]) {
  const node = document.getElementById(id), marker = document.createComment(id + " home");
  node.before(marker); panelPlacements[id] = {node, marker};
}
let drawerOpener = null;
function restorePanels() {
  for (const {node,marker} of Object.values(panelPlacements)) marker.after(node);
  document.body.classList.remove("drawer-open");
  document.querySelectorAll("[data-panel]").forEach(b => b.setAttribute("aria-expanded","false"));
  if (drawerOpener?.isConnected) drawerOpener.focus({preventScroll:true});
}
function openPanel(id, trigger = document.activeElement) {
  const narrow = id === "side" ? matchMedia("(max-width:899px)").matches : true;   // detail úkolu vždy ve vysouvacím panelu
  if (!narrow) { document.getElementById(id).scrollIntoView({block:"start"}); return; }
  if (drawer.open) { drawer.close(); restorePanels(); }
  drawerOpener = trigger;
  document.getElementById("drawerContent").appendChild(panelPlacements[id].node);
  document.getElementById("drawerPause").appendChild(panelPlacements.pause.node);
  document.getElementById("drawerTitle").textContent = id === "side" ? "Agenti a pracovní prostor" : "Detail a záznamy úkolu";
  document.body.classList.add("drawer-open");
  drawer.showModal();
  document.querySelectorAll(`[data-panel="${id}"]`).forEach(b => b.setAttribute("aria-expanded","true"));
  motion(document.getElementById("drawerContent"),[{opacity:0},{opacity:1}],180);
}
drawer.addEventListener("close", () => { if (!drawer.open) restorePanels(); });
drawer.addEventListener("keydown", e => {
  if (e.key !== "Tab") return;
  const focusable = [...drawer.querySelectorAll('button,a[href],input,select,textarea,summary,[tabindex]')].filter(el => !el.disabled && el.tabIndex >= 0 && el.getClientRects().length);
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (!first) { e.preventDefault(); return; }
  if (e.shiftKey && (document.activeElement === first || !drawer.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && (document.activeElement === last || !drawer.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
});
document.getElementById("drawerClose").onclick = () => drawer.close();
document.querySelectorAll("[data-panel]").forEach(b => b.onclick = () => openPanel(b.dataset.panel,b));
for (const query of ["(max-width:899px)","(max-width:1280px)"]) matchMedia(query).addEventListener("change",()=>{ if(drawer.open) drawer.close(); });
document.addEventListener("visibilitychange",()=>document.body.classList.toggle("page-hidden",document.hidden));
reducedMotion.addEventListener("change",()=>{ if(reducedMotion.matches) document.getAnimations().forEach(a=>a.cancel()); });

function renderAgentList(id, peers, board) {
  const el = document.getElementById(id);
  const old = new Map([...el.children].filter(c=>c.dataset.agent).map(c=>[c.dataset.agent,c]));
  el.querySelectorAll(":scope > .empty").forEach(n=>n.remove());
  peers.forEach((p,i)=>{
    const html = agentCard(p,board[p.agent]);
    let card = old.get(p.agent);
    if (!card || card._renderHTML !== html) {
      const template = document.createElement("template"); template.innerHTML = html;
      const next = template.content.firstElementChild;
      next._renderHTML = html; next.dataset.agent = p.agent;
      const work = next.querySelector(".agent-work"); if (work) work.title = work.textContent;
      if (card) card.replaceWith(next);
      card = next;
    }
    if (el.children[i] !== card) el.insertBefore(card,el.children[i] || null);
    old.delete(p.agent);
  });
  old.forEach(card=>card.remove());
  if (!peers.length && id === "agents") el.innerHTML = "<div class='empty'>nikdo není online</div>";
}

function historyMessages() { return [...messageCache.values()].sort((a, b) => a.id - b.id); }
function taskState(m) { const s = STATE[m.state] || [m.state || "stav není dostupný", ""]; return `<span class="tag task-state ${s[1]}">${esc(s[0])}</span>`; }
function roleOf(agent) { return agent === me ? "operátor" : agent === "broker" ? "systém" : String(agent).endsWith("-bridge") ? "most" : "agent"; }
function applyFilters() {
  const query = document.getElementById("search").value.trim().toLocaleLowerCase("cs-CZ");
  let visible = 0;
  for (const m of messageCache.values()) {
    const div = document.getElementById("msg-" + m.id);
    if (!div) continue;
    const system = isSystem(m);
    const matchesKind = activeFilter === "all" ? (!system || showSystemEl.checked)
      : activeFilter === "system" ? system : !system && m.kind === activeFilter;
    const haystack = ["#" + m.id, m.text, m.agent, m.recipient, nameOf(m.agent), nameOf(m.recipient)].join(" ").toLocaleLowerCase("cs-CZ");
    const show = matchesKind && (!query || haystack.includes(query));
    div.classList.toggle("hidden", !show);
    if (show) visible++;
  }
  document.getElementById("messageCount").textContent = `${visible} z ${messageCache.size} načtených zpráv`;
  let empty = document.getElementById("filterEmpty");
  if (!empty) { empty = document.createElement("div"); empty.id = "filterEmpty"; empty.className = "empty"; feed.appendChild(empty); }
  empty.textContent = messageCache.size ? "Tomuto filtru neodpovídají žádné zprávy." : "Místnost zatím nemá žádné zprávy.";
  empty.hidden = visible > 0;
}
// Pruh „Teď“: jedna věta, co se děje, a co potřebuje člověka (s tlačítkem rovnou na to).
function renderNowBar() {
  const el = document.getElementById("nowBar"); if (!el) return;
  const items = attentionItems(), n = items.reduce((s, i) => s + (i.kind === "more" ? i.n : 1), 0);
  const st = {ada: agentStatus("ada"), nox: agentStatus("nox")};
  const down = connectionMode !== "live";
  const main = down ? "Spojení s místností je přerušené — data se neobnovují." : lastPaused ? "Agenti jsou zastavení. Nic na pozadí nepracuje."
    : n ? `Na tebe čeká ${n} ${n === 1 ? "věc" : n < 5 ? "věci" : "věcí"}.` : "Vše v pořádku — agenti pracují sami.";
  el.dataset.tone = down || lastPaused ? "down" : n ? "attn" : "ok";
  const agent = k => `<span class="now-agent"><img src="/assets/${k}.webp" alt=""><b>${k === "ada" ? "Ada" : "Nox"}</b> ${esc(st[k].label)}${st[k].detail && st[k].state === "working" ? " · " + esc(st[k].detail.slice(0, 60)) : ""}</span>`;
  setHTMLIfChanged(el, `<span class="now-main">${esc(main)}</span>${agent("ada")}${agent("nox")}${n ? '<button type="button" class="btn-new" id="nowAttn">Vyřešit</button>' : ""}`);
  const b = document.getElementById("nowAttn");
  if (b) b.onclick = () => { renderAttention(); document.getElementById("attnDialog").showModal(); };
}
function refreshDashboard() {
  renderNowBar();
  const all = historyMessages();
  const tasks = all.filter(m => m.kind === "task");
  // Board pokrývá celou frontu; historie zpráv je omezena na 200 položek.
  // API poskytuje nejvýše jeden aktivní úkol na agenta (leased nebo started).
  setMetric("metricDone", tasks.filter(m => m.state === "succeeded").length);
  renderSystemMetric();
  renderHeroes();
  renderActivity();
  renderWork();
  renderDash();
  const picker = document.getElementById("taskPicker");
  const optionsKey = JSON.stringify(tasks.map(m => [m.id, m.state, m.text.slice(0, 65)]));
  if (picker.dataset.optionsKey !== optionsKey) {
    picker.dataset.optionsKey = optionsKey;
    picker.replaceChildren(new Option("Vyber úkol z místnosti", ""));
    [...tasks].reverse().forEach(m => picker.add(new Option(`#${m.id} · ${(STATE[m.state] || [m.state || "?"])[0]} · ${m.text.slice(0, 65)}`, String(m.id))));
  }
  picker.value = selectedTask === null ? "" : String(selectedTask);
  renderTaskDetail();
  const events = all.filter(m => m.kind === "result" || m.kind === "control").slice(-5).reverse();
  setHTMLIfChanged(document.getElementById("recentEvents"), events.length ? `<ol class="event-list">${events.map(m => `<li><div class="event-meta">#${m.id} · ${esc(nameOf(m.agent))} · ${ago(m.seconds_ago)}</div><p>${esc(m.text.slice(0, 170))}${m.text.length > 170 ? "…" : ""}</p></li>`).join("")}</ol><p class="note">Původní záznamy z načtené historie. Celý text je v místnosti.</p>` : "<div class='empty'>V načtené historii nejsou žádné události.</div>");
  applyFilters();
}
// Stav Ady a Noxe jen ze skutečných dat (peers + board), každý zvlášť; nikdy nezůstane „Pracuje“ při výpadku spojení.
const HERO = { ada: ["claude-code", "claude-code-bridge"], nox: ["codex", "codex-bridge"] };
const ONLINE_S = 120, STALE_MS = 60000;   // stav místnosti chodí po SSE každé 2 s; 60 s bez něj = data neplatí
let lastRoomAt = 0;
function agentStatus(key) {
  if (connectionMode !== "live") return {state: "unknown", label: "Spojení přerušeno", detail: ""};
  if (!lastRoomAt || Date.now() - lastRoomAt > STALE_MS) return {state: "unknown", label: "Neznámý stav", detail: "data nejsou aktuální"};
  const [main, bridge] = HERO[key];
  const peer = a => latestPeers.find(p => p.agent === a);
  const card = a => (latestBoard || []).find(b => b.agent === a);
  const pm = peer(main), pb = peer(bridge), bm = card(main), bb = card(bridge);
  const fresh = p => p && p.active && p.seen_seconds_ago != null && p.seen_seconds_ago < ONLINE_S;
  // limit hlášený poskytovatelem i ručně (`limit` → presence rate-limited + limited_until)
  const nowS = Date.now() / 1000;
  const limited = [pm, pb, bm, bb].find(x => x && (x.provider_state === "limited" || x.presence === "rate-limited"
    || (x.limited_until && x.limited_until > nowS)));
  const active = (bb && bb.active) || (bm && bm.active);
  if (active) return {state: "working", label: "Pracuje", detail: `#${active.id} ${active.text || ""}`};
  if (limited) { const until = limited.provider_resets_at || limited.limited_until;
    return {state: "limited", label: "Limit" + (until ? " do " + clock(until) : ""), detail: limited.provider_reason || ""}; }
  if (fresh(pm) && pm.presence === "busy" && pm.status && pm.status !== "start session") return {state: "working", label: "Pracuje", detail: pm.status};
  const problem = (bm && bm.needs_review) ? `ke kontrole ${bm.needs_review}`
    : (bm && bm.blocked_reason && fresh(pm)) ? bm.blocked_reason
    : (bb && bb.blocked_reason && fresh(pb)) ? "most: " + bb.blocked_reason : "";
  if (problem) return {state: "error", label: "Vyžaduje pozornost", detail: problem};
  const unknownLimit = [pm, pb, bm, bb].some(x => x && x.provider_state === "unknown");
  if (fresh(pm) || fresh(pb)) return {state: "idle", label: "Čeká", detail: unknownLimit ? "stav limitu poskytovatele neznámý"
    : fresh(pm) ? (pm.status || "") : "most připraven převzít úkol"};
  return {state: "offline", label: "Offline", detail: pm && pm.seen_seconds_ago != null ? "naposledy " + ago(pm.seen_seconds_ago) : ""};
}
function renderHeroes() {
  for (const key of ["ada", "nox"]) {
    const el = document.querySelector(`.agent-hero[data-agent="${key}"]`); if (!el) continue;
    const s = agentStatus(key);
    if (el.dataset.state !== s.state) el.dataset.state = s.state;
    el.querySelector(".state-text").textContent = s.label;
    const task = el.querySelector(".agent-task"); task.textContent = s.detail.slice(0, 90); task.title = s.detail;
    el.title = s.label + (s.detail ? ": " + s.detail : "");
  }
}
setInterval(() => { if (!document.hidden) renderHeroes(); }, 5000);
function renderWork() {
  const tasks = historyMessages().filter(m => m.kind === "task");
  const board = latestBoard || [];
  const attention = attentionItems().reduce((s, i) => s + (i.kind === "more" ? i.n : 1), 0);
  const rows = [["ok", "Dokončeno", tasks.filter(m => m.state === "succeeded").length, "úkoly v načtené historii"],
                ["work", "Probíhá", board.filter(b => b.active).length, "právě běžící úkoly"],
                ["wait", "Ve frontě", board.reduce((s, b) => s + (b.queued || 0), 0), "nepřevzaté doručenky ve frontě agentů (zprávy i úkoly)"],
                ["warn", "Vyžaduje pozornost", attention, "ke kontrole a review čekající na tebe"]];
  setHTMLIfChanged(document.getElementById("workList"), rows.map(([cls, label, n, hint]) =>
    `<li class="work work-${cls}" title="${esc(hint)}"><span class="work-ic" aria-hidden="true"></span><span>${label}</span><b>${latestBoard === null && cls !== "ok" ? "—" : n}</b></li>`).join(""));
  const bell = document.getElementById("bellCount");
  bell.hidden = !attention; bell.textContent = attention;
}
function openRoomTasks() { showView("room", {focus: false}); document.querySelector('[data-filter="task"]').click(); }
// ── „Čeká na tebe“: jedno místo pro všechno, co potřebuje člověka ──
const TASK_ERROR = e => !e ? "" : e === "worker lease expired after start" ? "Běh na pozadí nedoběhl (most přestal hlásit průběh). Výsledek není jistý, proto se sám neopakuje."
  : e.startsWith("quarantine") ? "Úkol zadržela bezpečnostní kontrola." : e.startsWith("fix:") ? "Oprava se zápisem do repozitáře čeká na tvé povolení."
  : e.startsWith("checkpoint:") ? "Pracovní složka se od uložení změnila." : e;
function attentionItems() {
  const items = [];
  (latestReviewsAll || latestReviews || []).filter(r => r.state === "escalated").forEach(r => items.push({kind: "review", r}));
  const tasks = historyMessages().filter(m => m.kind === "task" && ["needs_review", "failed", "dead_letter"].includes(m.state));
  tasks.forEach(m => items.push({kind: "task", m}));
  (qgRecords || []).filter(r => r.policy_review_required && r.policy_review_status !== "approved" && r.run_id).forEach(r => items.push({kind: "policy", r}));
  const hidden = (latestBoard || []).reduce((s, b) => s + (b.needs_review || 0), 0) - tasks.filter(m => m.state === "needs_review").length;
  if (hidden > 0) items.push({kind: "more", n: hidden});
  return items;
}
function renderAttention() {
  const list = document.getElementById("attnList"), items = attentionItems();
  // Klíč nese vše, co se v panelu zobrazuje nebo určuje tlačítka (review #57: last_error, fix, stav review/kvality).
  const key = JSON.stringify(items.map(i => [i.kind, i.n, i.m && [i.m.id, i.m.state, i.m.last_error, i.m.fix, i.m.recipient],
    i.r && [i.r.id, i.r.run_id, i.r.state, i.r.policy_review_status, i.r.topic]]));
  if (list._key === key) return;
  list._key = key;
  list.innerHTML = items.length ? "" : '<div class="attn-empty">✓ Nic nečeká. Agenti si poradí sami.</div>';
  items.forEach(it => {
    const div = document.createElement("div");
    div.className = "attn-item"; div.dataset.kind = it.kind;
    if (it.kind === "review") {
      const v = it.r;
      div.innerHTML = `<h3>Review #${v.id}: ${esc(v.topic.slice(0, 140))}</h3><p>${esc(nameOf(v.author))} → ${esc(nameOf(v.reviewer))} · vyčerpaná 3 kola oprav, rozhoduješ ty.</p><div class="btns"><button class="btn" data-a="grant">Povolit ještě 1 kolo</button><button class="btn" data-a="close">Uzavřít review</button></div>`;
      div.querySelector('[data-a="grant"]').onclick = () => { const reason = prompt("Proč povolit další kolo oprav? (zapíše se)"); if (reason && reason.trim()) post("/api/review-grant", {id: v.id, reason}); };
      div.querySelector('[data-a="close"]').onclick = () => { const reason = prompt("Proč uzavřít? (zapíše se)", "převezmu sám"); if (reason && reason.trim()) post("/api/review-close", {id: v.id, reason}); };
    } else if (it.kind === "task") {
      const m = it.m;
      div.innerHTML = `<h3>Úkol #${m.id} pro ${esc(nameOf(m.recipient))}: ${esc((m.text || "").split(String.fromCharCode(10))[0].slice(0, 120))}</h3><p class="why">${esc(TASK_ERROR(m.last_error) || (STATE[m.state] || [m.state])[0])}</p>`;
      renderTaskActions(m, div);
    } else if (it.kind === "policy") {
      const r = it.r;
      div.innerHTML = `<h3>Kontrola kvality: ${esc(String(r.repository).split("/").pop())}</h3><p>Commit mění testy nebo pravidla a čeká na tvé schválení.</p><div class="btns"><button class="btn">Otevřít v Kvalitě</button></div>`;
      div.querySelector("button").onclick = () => { document.getElementById("attnDialog").close(); qgSelected = r.run_id; showView("quality"); };
    } else {
      div.innerHTML = `<h3>Dalších ${it.n} úkolů ke kontrole</h3><p>Jsou starší než načtená historie místnosti.</p><div class="btns"><button class="btn">Ukázat v místnosti</button></div>`;
      div.querySelector("button").onclick = () => { document.getElementById("attnDialog").close(); openRoomTasks(); };
    }
    list.appendChild(div);
  });
}
document.getElementById("bellBtn").onclick = () => { renderAttention(); document.getElementById("attnDialog").showModal(); };
document.getElementById("attnClose").onclick = () => document.getElementById("attnDialog").close();
// ── Živá data: kdy přišla poslední aktualizace ──
let lastLiveTick = 0;
setInterval(() => {
  const pill = document.getElementById("livePill"), txt = document.getElementById("liveText");
  const age = lastRoomAt ? Math.max(0, Math.round((Date.now() - lastRoomAt) / 1000)) : null;
  const state = connectionMode !== "live" ? "down" : age === null || age > 15 ? "stale" : "live";
  pill.dataset.state = state;
  txt.textContent = state === "down" ? "Spojení přerušeno" : age === null ? "Připojuji…" : state === "stale" ? `Data stará ${age} s` : "Živě";
  if (lastRoomAt && lastRoomAt !== lastLiveTick) { lastLiveTick = lastRoomAt; pill.classList.remove("tick"); void pill.offsetWidth; pill.classList.add("tick"); }
  if (document.getElementById("attnDialog").open) renderAttention();
}, 1000);
document.querySelectorAll("[data-goto]").forEach(b => { b.onclick = () => b.dataset.goto === "room-tasks" ? openRoomTasks() : showView(b.dataset.goto);
  if (b.getAttribute("role") === "link") b.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); b.onclick(); } }; });
document.querySelectorAll("[data-qa]").forEach(b => b.onclick = () => {
  const qa = b.dataset.qa;
  if (qa === "research") return showView("research");
  showView("room", {focus: false});
  setKind("task"); toEl.value = "claude-code"; syncHint(); textEl.focus();
});
// ── Přehled podle předlohy 02: jen skutečná data ──
let latestReviewsAll = null, dashResearch = null, dashKnowledge = null;
const DONE_REVIEW = r => r.verdict === "approved" && r.verdict_at;
function loadDashData() {
  fetch("/api/research").then(r => r.ok ? r.json() : null).then(d => { if (d) { dashResearch = d.research || []; renderDash(); } }).catch(() => {});
  fetch("/api/knowledge").then(r => r.ok ? r.json() : null).then(d => { if (d) { dashKnowledge = d.entries || []; renderDash(); } }).catch(() => {});
}
setInterval(() => { if (!document.hidden && currentView === "overview") loadDashData(); }, 15000);
function renderChart() {
  const all = latestReviewsAll || [], svg = document.getElementById("chartSvg");
  if (!all.length) { document.getElementById("chartPct").textContent = "—"; svg.innerHTML = ""; return; }
  const day = 86400, end = new Date(); end.setHours(24, 0, 0, 0);
  const points = [...Array(7)].map((_, i) => {
    const t = end.getTime() / 1000 - (6 - i) * day;
    const created = all.filter(r => r.created_at <= t), done = created.filter(r => DONE_REVIEW(r) && r.verdict_at <= t);
    return {t, pct: created.length ? Math.round(100 * done.length / created.length) : 0};
  });
  const now = points[6].pct, delta = now - points[0].pct;
  document.getElementById("chartPct").textContent = now + " %";
  const d = document.getElementById("chartDelta"); d.textContent = (delta >= 0 ? "▲ +" : "▼ ") + delta + " p. b. (7 dní)"; d.dataset.tone = delta >= 0 ? "ok" : "bad";
  const W = 360, H = 150, L = 42, B = 20, x = i => L + i * (W - L - 8) / 6, y = p => 8 + (H - B - 8) * (1 - p / 100);
  const line = points.map((p, i) => `${x(i)},${y(p.pct)}`).join(" ");
  const days = ["Ne","Po","Út","St","Čt","Pá","So"];
  svg.innerHTML = `<defs><linearGradient id="cg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#923BFF" stop-opacity=".55"/><stop offset="1" stop-color="#923BFF" stop-opacity="0"/></linearGradient></defs>`
    + [0, 25, 50, 75, 100].map(p => `<line x1="${L}" x2="${W - 8}" y1="${y(p)}" y2="${y(p)}" class="grid"/><text x="${L - 6}" y="${y(p) + 4}" class="ax" text-anchor="end">${p} %</text>`).join("")
    + `<polygon points="${x(0)},${y(0)} ${line} ${x(6)},${y(0)}" fill="url(#cg)"/><polyline points="${line}" class="pl"/>`
    + points.map((p, i) => `<circle cx="${x(i)}" cy="${y(p.pct)}" r="${i === 6 ? 5 : 3.5}" class="${i === 6 ? "pt last" : "pt"}"><title>${p.pct} % schváleno</title></circle><text x="${x(i)}" y="${H - 4}" class="ax" text-anchor="middle">${days[new Date((p.t - day) * 1000).getDay()]}</text>`).join("");
}
function renderDash() {
  const all = latestReviewsAll || [], board = latestBoard || [];
  const tasks = historyMessages().filter(m => m.kind === "task");
  const openRepos = new Set(all.filter(r => !["approved", "closed"].includes(r.state)).map(r => r.repo));
  setMetric("metricAgents", latestReviewsAll === null ? "—" : openRepos.size);
  const OPEN = ["pending", "leased", "started", "accepted", "unread"];
  setMetric("metricRunning", tasks.filter(m => OPEN.includes(m.state || "pending")).length);
  setMetric("metricQueued", dashResearch === null ? "—" : dashResearch.filter(r => ["running", "synthesizing"].includes(r.state)).length);
  setMetric("metricReview", dashResearch === null ? "—" : dashResearch.filter(r => r.state === "done" || r.state === "partial").length);
  setMetric("metricReviews", all.length ? Math.round(100 * all.filter(DONE_REVIEW).length / all.length) + " %" : "—");
  renderChart();
  // aktivní úkoly: otevřené úkoly v načtené historii, se štítkem adresáta
  const open = tasks.filter(m => ["pending", "leased", "started", "accepted", "unread"].includes(m.state || "pending")).slice(-6).reverse();
  document.getElementById("tasksCount").textContent = `(${tasks.filter(m => ["pending", "leased", "started", "accepted", "unread"].includes(m.state || "pending")).length})`;
  setHTMLIfChanged(document.getElementById("tasksList"), open.length ? open.map(m => {
    const who = m.recipient === "claude-code" ? ["Ada", "ada"] : m.recipient === "codex" ? ["Nox", "nox"] : [nameOf(m.recipient), "other"];
    return `<li class="task-row"><span class="box ${m.state === "started" ? "run" : ""}" aria-hidden="true"></span><span class="task-text" title="${esc(m.text)}">${esc((m.text || "").split(String.fromCharCode(10))[0].slice(0, 90))}</span><span class="who-badge ${who[1]}">${esc(who[0])}</span></li>`;
  }).join("") : "<li class='empty'>Žádné otevřené úkoly.</li>");
  // zdroje znalostí: sdílená paměť
  const kb = dashKnowledge || [];
  document.getElementById("sourcesCount").textContent = dashKnowledge === null ? "" : `(${kb.length})`;
  setHTMLIfChanged(document.getElementById("sourcesList"), kb.length ? kb.slice(0, 5).map(e => `<li class="src-row"><span class="src-ic" aria-hidden="true">▤</span><span class="task-text" title="${esc(e.symptom)}">${esc((e.project ? e.project + " – " : "") + e.symptom.slice(0, 70))}</span><span class="src-st ${e.status === "verified" ? "ok" : ""}" title="${esc(KB_LABEL[e.status] || e.status)}">${e.status === "verified" ? "✓" : "○"}</span></li>`).join("")
    : `<li class="empty">${dashKnowledge === null ? "Načítám…" : "Paměť je zatím prázdná."}</li>`);
  // klíčové insighty: dokončené výzkumy
  const done = (dashResearch || []).filter(r => r.state === "done" || r.state === "partial");
  const fresh = done.filter(r => r.finished_at && Date.now() / 1000 - r.finished_at < 86400).length;
  const nb = document.getElementById("insightsNew"); nb.hidden = !fresh; nb.textContent = `${fresh} nové`;
  setHTMLIfChanged(document.getElementById("insightsList"), done.length ? done.slice(0, 3).map(r => `<li title="${esc(r.question)}">${esc(r.question.slice(0, 95))}</li>`).join("")
    : `<li class="empty">${dashResearch === null ? "Načítám…" : "Zatím žádný dokončený výzkum."}</li>`);
  // doporučení: co čeká na člověka (review a úkoly ke kontrole)
  const reco = [];
  all.filter(r => r.state === "escalated").forEach(r => reco.push(`Rozhodnout review #${r.id} – ${r.topic.slice(0, 60)}`));
  all.filter(r => r.state === "approved").forEach(r => reco.push(`Sloučit schválené review #${r.id} – ${r.topic.slice(0, 55)}`));
  board.filter(b => b.needs_review).forEach(b => reco.push(`Zkontrolovat ${b.needs_review} úkol(y) u ${nameOf(b.agent)}`));
  all.filter(r => r.state === "changes_requested" && r.author === me).forEach(r => reco.push(`Opravit review #${r.id} podle připomínek`));
  setHTMLIfChanged(document.getElementById("recoList"), reco.length ? reco.slice(0, 4).map(x => `<li>${esc(x)}</li>`).join("") : "<li class='empty'>Nic nečeká na tvé rozhodnutí.</li>");
}   // stáří dat se projeví i bez nové zprávy
function renderActivity() {
  const items = dedupeActivity(historyMessages().filter(m => (m.agent === "claude-code" || m.agent === "codex") && !isSystem(m)).reverse()).slice(0, 8);
  setHTMLIfChanged(document.getElementById("activityList"), items.length ? items.map(m => {
    const key = m.agent === "claude-code" ? "ada" : "nox";
    return `<li class="act" data-agent="${key}"><img class="act-portrait" src="/assets/${key}.webp" alt=""><div class="act-body"><div class="act-head"><b>${key === "ada" ? "Ada" : "Nox"}</b><time>${clock(Date.now() / 1000 - (m.seconds_ago || 0))}</time></div><p>${esc((m.text || "").slice(0, 120))}${(m.text || "").length > 120 ? "…" : ""}</p></div></li>`;
  }).join("") : "<li class='empty'>Čekám na zprávy Ady a Noxe.</li>");
}
function renderSystemMetric() {
  const el = document.getElementById("metricSystem");
  setMetric("metricSystem", connectionMode !== "live" ? (connectionMode === "retry" ? "Spojení přerušeno" : "Připojování…")
    : lastPaused === undefined ? "Stav není dostupný" : lastPaused ? "Pozastaveno" : "Provoz povolen");
  el.dataset.tone = connectionMode !== "live" ? "warn" : lastPaused === undefined ? "muted" : lastPaused ? "bad" : "ok";
}
function selectTask(id, focus = false) {
  selectedTask = id === "" || id === null ? null : Number(id);
  document.querySelectorAll(".bubble.selected").forEach(el => el.classList.remove("selected"));
  if (selectedTask !== null) document.getElementById("msg-" + selectedTask)?.classList.add("selected");
  document.getElementById("taskPicker").value = selectedTask === null ? "" : String(selectedTask);
  renderTaskDetail();
  highlightAgent();
  if (focus) { openPanel("inspector"); document.getElementById("detailTitle").focus({preventScroll:true}); }
}
function highlightAgent() {
  const recipient = messageCache.get(selectedTask)?.recipient;
  document.querySelectorAll("[data-agent]").forEach(card => card.classList.toggle("selected-agent", card.dataset.agent === recipient));
}
function renderTaskDetail() {
  const el = document.getElementById("taskDetail");
  const m = messageCache.get(selectedTask);
  document.getElementById("detailTitle").textContent = m ? `Detail úkolu #${m.id}` : "Detail úkolu";
  document.getElementById("timelineTitle").textContent = m ? `Záznamy úkolu #${m.id}` : "Záznamy úkolu";
  const timeline = document.getElementById("taskTimeline");
  if (!m || m.kind !== "task") {
    timeline.innerHTML = `<div class="empty">Vyber úkol z načtené historie.</div>`;
    el.dataset.detailKey = "";
    el.innerHTML = `<div class="empty">${selectedTask === null ? "Vyber úkol. Zobrazí se jeho skutečný stav a navázané zprávy." : "Vybraný úkol už není v načtené historii."}</div>`;
    return;
  }
  const related = historyMessages().filter(r => r.reply_to === m.id);
  const qualityRun = (qgRecords || []).find(r => r.task_id === m.id);
  const detailKey = JSON.stringify([m.id, m.text, m.agent, m.recipient, m.state, m.attempts, m.accept_note, m.last_error, qualityRun?.run_id, qualityRun?.overall_status, related.map(r => [r.id, r.kind, r.text])]);
  if (el.dataset.detailKey === detailKey) {
    [...el.querySelectorAll("[data-relative-id]"), ...timeline.querySelectorAll("[data-relative-id]")].forEach(span => {
      const record = messageCache.get(Number(span.dataset.relativeId));
      if (record) span.textContent = ago(record.seconds_ago);
    });
    return; // Otevřené zadání/výsledky a fokus přežijí pravidelnou SSE aktualizaci.
  }
  const openSections = el.dataset.taskId === String(m.id) ? [...el.querySelectorAll("details[open][data-section]"), ...timeline.querySelectorAll("details[open][data-section]")].map(d => d.dataset.section) : [];
  const switched = el.dataset.taskId !== String(m.id);
  el.dataset.taskId = String(m.id);
  el.dataset.detailKey = detailKey;
  const fields = [["ID", esc("#" + m.id)], ["Zadavatel", esc(nameOf(m.agent))], ["Přiřazeno", esc(nameOf(m.recipient))], ["Stav", taskState(m)], ["Pokusy", esc(m.attempts ?? "není dostupné")], ["Zadáno", `<span data-relative-id="${m.id}">${ago(m.seconds_ago)}</span>`]];
  el.innerHTML = `<dl class="detail-grid">${fields.map(([key,value]) => `<dt>${key}</dt><dd>${value}</dd>`).join("")}</dl>`
    + `<details class="detail-section task-description" data-section="assignment"><summary>Zadání úkolu v plném znění</summary><div class="detail-text">${esc(m.text)}</div></details>`
    + (m.accept_note ? `<div class="detail-section"><h3>Potvrzení převzetí</h3><div class="detail-text">${esc(m.accept_note)}</div></div>` : "")
    + (m.last_error ? `<div class="detail-section"><h3>Poslední hlášená chyba</h3><div class="detail-text">${esc(m.last_error)}</div></div>` : "")
    + `<section class="task-quality"><h3><svg class="qg-shield" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="m8 12 3 3 5-6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg> Quality Gate</h3>${qualityRun ? qgBadge(qualityRun.overall_status) + `<p class="note">Commit: ${esc(qualityRun.commit_sha)}</p>` : '<span class="tag">NOT RUN</span><p class="note">Kontrola zatím není dostupná. Pro tento úkol není zaznamenaný běh.</p>'}</section>`;
  timeline.innerHTML = `<div class="timeline-current" data-tone="${stateTone(m.state)}"><span class="timeline-marker" aria-hidden="true"></span><span>Aktuální stav: ${esc((STATE[m.state] || [m.state || "nedostupný"])[0])}</span></div><ol class="event-list"><li><div class="event-meta">#${m.id} · zadání úkolu · <span data-relative-id="${m.id}">${ago(m.seconds_ago)}</span></div></li>`
    + related.map(r => `<li><details data-section="reply-${r.id}"><summary><span class="event-meta">#${r.id} · ${esc(KIND[r.kind] || r.kind)} · <span data-relative-id="${r.id}">${ago(r.seconds_ago)}</span></span><br>${esc(r.text.slice(0,80))}${r.text.length > 80 ? "…" : ""}</summary><div class="detail-text">${esc(r.text)}</div></details></li>`).join("")
    + `</ol><p class="note">${related.length ? "Přímo navázané zprávy z načtené historie." : "Žádné navázané zprávy v načtené historii."} Strukturované kroky ani úplná historie přechodů nejsou v API dostupné.</p>`;
  if (switched) { motion(el,[{opacity:.3},{opacity:1}],180); motion(timeline,[{opacity:.3},{opacity:1}],180); }
  [...el.querySelectorAll("details[data-section]"), ...timeline.querySelectorAll("details[data-section]")].forEach(d => { d.open = openSections.includes(d.dataset.section); });
}
let projFilter = "all", lastProjReviews = [];
let latestProjects = {};
function renderProjects(reviews) {
  lastProjReviews = reviews || [];
  const by = new Map();
  // Pracovní kopie (worktree) se počítají do svého hlavního repozitáře — mapu posílá server.
  lastProjReviews.forEach(r => { if (!r.repo) return; const key = latestProjects[r.repo] || r.repo.split("/").filter(Boolean).pop();
    const s = by.get(key) || {total: 0, done: 0, open: 0, repos: new Set()}; s.total++; s.repos.add(r.repo);
    if (["approved", "closed"].includes(r.state)) s.done++; else s.open++; by.set(key, s); });
  const rows = [...by.entries()].sort((a, b) => b[1].open - a[1].open || a[0].localeCompare(b[0]))
    .filter(([, s]) => projFilter === "all" || (projFilter === "open" ? s.open > 0 : s.open === 0));
  const folder = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
  setHTMLIfChanged(document.getElementById("projects"), rows.length ? rows.map(([repo, s], i) => {
    const pct = Math.round(100 * s.done / s.total);
    return `<div class="proj ${i === 0 && s.open ? "proj-hot" : ""}" title="${esc([...s.repos].join(String.fromCharCode(10)))}"><span class="proj-ic">${folder}</span><div class="proj-body"><div class="proj-head"><b>${esc(repo)}</b><span>${pct} %</span></div><div class="proj-sub">${s.done}/${s.total} review schváleno${s.open ? ` · ${s.open} otevřených` : ""}</div><div class="bar"><i style="width:${pct}%"></i></div></div></div>`;
  }).join("") : `<div class="empty">Pro tento filtr nejsou žádné projekty.</div>`);
}
document.querySelectorAll("[data-proj]").forEach(b => b.onclick = () => {
  projFilter = b.dataset.proj;
  document.querySelectorAll("[data-proj]").forEach(x => { x.classList.toggle("on", x === b); x.setAttribute("aria-pressed", x === b); });
  renderProjects(lastProjReviews);
});


const NAMES = {
  "claude-code": "Ada · Claude Code", "codex": "Nox · Codex",
  "claude-code-bridge": "Most Ady", "codex-bridge": "Most Noxe",
  "broker": "systém",
};
const KIND = { chat: "zpráva", task: "úkol", broadcast: "všem", control: "řízení", result: "výsledek" };
const STATE = {
  pending: ["čeká", ""], leased: ["přiděleno", "work"], started: ["pracuje se", "work"],
  succeeded: ["hotovo", "ok"], failed: ["selhalo", "bad"], needs_review: ["ke kontrole", "warn"],
  dead_letter: ["nedoručitelné", "bad"], cancelled: ["zrušeno", "gone"], expired: ["propadlo", "gone"],
  accepted: ["převzato", "ok"], unread: ["nepřečteno", ""],
};
const PRESENCE = { busy: ["pracuje", "work"], idle: ["volný", "ok"], offline: ["offline", ""], stalled: ["neodpovídá", "warn"] };

// escapuje i uvozovky: výstup se vkládá do textu i do atributů (title="…") — bezpečnostní kontrola 21. 11.
function esc(s) { return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;"); }
function nameOf(a) { return a === me ? "Ty" : (NAMES[a] || a); }
function hueOf(name) { let h = 0; for (const c of String(name)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function avatarStyle(name) {
  const known = {"claude-code":"--avatar-ada", "codex":"--avatar-nox", "claude-code-bridge":"--avatar-bridge", "codex-bridge":"--avatar-bridge"};
  const palette = ["--accent-1","--blue","--ok","--warn"];
  return `--avatar-color:var(${name === me ? "--avatar-user" : known[name] || palette[hueOf(name) % palette.length]})`;
}
function initial(name) { return String(nameOf(name) || "?").trim().slice(0, 1).toUpperCase(); }
function ago(seconds) {
  const s = Math.max(0, seconds | 0);
  if (s < 60) return "právě teď";
  if (s < 3600) return `před ${Math.floor(s / 60)} min`;
  if (s < 86400) return `před ${Math.floor(s / 3600)} h`;
  return `před ${Math.floor(s / 86400)} d`;
}
function ttl(seconds) {
  const s = Math.max(0, seconds | 0), m = Math.floor(s / 60);
  return m > 0 ? `${m} min` : `${s} s`;
}
function clock(t) { return new Date(t * 1000).toLocaleTimeString("cs-CZ", {hour: "2-digit", minute: "2-digit"}); }
function tag(text, cls) { return `<span class="tag ${cls || ""}">${esc(text)}</span>`; }

function updateStatus(mode) {
  connectionMode = mode;
  renderSystemMetric();
  renderHeroes();
  setTimeout(renderPages);   // Nastavení hned ukáže neznámý stav mostů
  statusDot.className = "dot" + (mode === "live" ? " live" : mode === "retry" ? " retry" : "");
  statusText.textContent = mode === "live" ? "živě" : mode === "retry" ? "spojení přerušeno, zkouším znovu…" : "připojuji…";
}

// Systémový šum: potvrzení od brokeru a automatické „✔ taking #…" příjemky.
function isSystem(m) {
  return m.agent === "broker" || (m.kind === "result" && /^✔ taking #/.test(m.text || ""));
}

function renderMessage(m, {live = false} = {}) {
  const previous = messageCache.get(m.id);
  const newLive = live && historyReady && !previous && m.id > highestMessageId;
  highestMessageId = Math.max(highestMessageId,m.id);
  messageCache.set(m.id,m);
  if (messageCache.size > 200) {
    const oldest = Math.min(...messageCache.keys());
    messageCache.delete(oldest); expandedMessages.delete(oldest);
    document.getElementById("msg-" + oldest)?.remove();
    if (oldest === m.id) return;
  }
  let div = document.getElementById("msg-" + m.id);
  const fresh = !div;
  if (fresh) { div = document.createElement("div"); div.className = "bubble"; div.id = "msg-" + m.id; div.dataset.messageId = String(m.id); }
  const k = m.kind || "chat";
  const structuralKey = JSON.stringify([m.text,m.kind,m.agent,m.recipient]);
  if (div.dataset.renderKey !== structuralKey) {
    div.dataset.renderKey = structuralKey;
    const to = m.recipient && m.recipient !== "*" ? `→ <span class="who">${esc(nameOf(m.recipient))}</span>` : "";
    const long = (m.text || "").length > 600 || (m.text || "").split(String.fromCharCode(10)).length > 8;
    div.innerHTML = `<div class="meta"><span class="kind kind-${esc(k)}">${esc(KIND[k] || k)}</span><span class="who">${esc(nameOf(m.agent))}</span> ${to}<span class="message-time" title="Stáří podle poslední aktualizace"></span><span class="delivery-meta"></span><span class="num">#${m.id}</span></div><div class="text${long && !expandedMessages.has(m.id) ? " clip" : ""}">${esc(m.text)}</div><span class="completion-flash" aria-hidden="true"></span>`;
    if (long) {
      const more = document.createElement("button"); more.className = "more";
      more.textContent = expandedMessages.has(m.id) ? "Sbalit" : "Zobrazit celé";
      more.onclick = () => {
        const clipped = div.querySelector(".text").classList.toggle("clip");
        if (clipped) expandedMessages.delete(m.id); else expandedMessages.add(m.id);
        more.textContent = clipped ? "Zobrazit celé" : "Sbalit";
      };
      div.appendChild(more);
    }
    if (k === "task") {
      const detail = document.createElement("button"); detail.className = "btn detail"; detail.textContent = "Detail úkolu";
      detail.setAttribute("aria-label",`Detail úkolu #${m.id}`); detail.onclick = () => selectTask(m.id,true); div.appendChild(detail);
    }
    div._actionsKey = null;
  }
  div.dataset.agent = m.agent === "claude-code" ? "ada" : m.agent === "codex" ? "nox" : m.agent === me ? "me" : "other";
  div.classList.toggle("mine",m.agent === me);
  div.classList.toggle("system",isSystem(m));
  div.classList.toggle("task",k === "task");
  div.classList.toggle("result",k === "result");
  div.classList.toggle("selected",selectedTask === m.id);
  div.querySelector(".message-time").textContent = "· " + ago(m.seconds_ago);
  const state = k === "task" && m.state ? taskState(m) : "";
  const receipt = m.recipient && m.recipient !== "*" && k !== "result" && m.agent !== "broker"
    ? m.accepted ? tag("✓ převzal" + (m.accept_note ? ": " + m.accept_note : ""),"ok") : m.seen ? tag("viděl, zatím nepřevzal","work") : tag("ještě nedoručeno") : "";
  setHTMLIfChanged(div.querySelector(".delivery-meta"),state + receipt);
  const actionsKey = JSON.stringify([m.state,m.recipient,m.last_error]);
  if (div._actionsKey !== actionsKey) { div.querySelector(".actions")?.remove(); renderTaskActions(m,div); div._actionsKey = actionsKey; }
  if (fresh) {
    const stick = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
    const next = [...feed.querySelectorAll(".bubble")].find(el=>Number(el.dataset.messageId)>m.id);
    feed.insertBefore(div,next || document.getElementById("filterEmpty"));
    if (stick) feed.scrollTop = feed.scrollHeight;
    if (newLive) motion(div,[{opacity:0,transform:"translateY(6px)"},{opacity:1,transform:"translateY(0)"}],210);
  } else if (previous?.state !== m.state) {
    if (m.state === "succeeded") motion(div.querySelector(".completion-flash"),[{opacity:.12},{opacity:0}],450);
    else if (stateTone(m.state) !== "bad") motion(div.querySelector(".task-state"),[{opacity:.65},{opacity:1}],180);
  } else if (previous?.accepted !== m.accepted) {
    motion(div.querySelector(".delivery-meta .tag:last-child"),[{opacity:.65},{opacity:1}],180);
  }
  if (!renderingRoom) refreshDashboard();
}

function renderTaskActions(m,div) {
  if (m.kind === "task" && m.recipient && m.state) {
    const actions = document.createElement("div");
    actions.className = "actions";
    const add = (label, fn) => { const b = document.createElement("button"); b.className = "btn"; b.textContent = label; b.onclick = fn; actions.appendChild(b); };
    if (["pending", "leased", "started", "failed", "needs_review", "dead_letter"].includes(m.state)) {
      add("✕ Zrušit úkol", () => { if (confirm(`Zrušit úkol #${m.id}?`)) control("cancel", m.id, m.recipient); });
    }
    if (m.state === "needs_review" && (m.last_error || "").startsWith("quarantine")) {
      add("Schválit a spustit jednou", () => {
        const reason = prompt("Proč je bezpečné tento zadržený úkol spustit? (zaznamená se)");
        if (reason) post("/api/approve", {id: m.id, to: m.recipient, reason});
      });
    }
    if (m.fix) {
      const info = document.createElement("div");
      info.className = "fix-scope";
      info.textContent = `Oprava se zápisem · repozitář ${m.fix.repo} · základ ${m.fix.base_commit.slice(0, 12)} · cesty: ${m.fix.allowed.length ? m.fix.allowed.join(", ") : "celé repo"}`;
      actions.appendChild(info);
    }
    if (m.fix && ["pending", "needs_review"].includes(m.state) && (m.state === "pending" || (m.last_error || "").startsWith("fix:"))) {
      add("Povolit zápis jednou", () => {
        const scope = `Repozitář: ${m.fix.repo}
Základ: ${m.fix.base_commit}
Smí měnit: ${m.fix.allowed.length ? m.fix.allowed.join(", ") : "celé repo"}`;
        const reason = prompt(`Tato oprava smí zapisovat do repozitáře.
${scope}

Proč ji povolit? (zaznamená se)`);
        if (reason) post("/api/approve-fix", {id: m.id, to: m.recipient, reason});
      });
    }
    if (m.state === "needs_review" && (m.last_error || "").startsWith("checkpoint:")) {
      add("Přesto navázat (jednou)", () => {
        const reason = prompt("Pracovní složka se od uložení změnila. Proč je bezpečné pokračovat? (zaznamená se)");
        if (reason) post("/api/approve-resume", {id: m.id, to: m.recipient, reason});
      });
    }
    if (["failed", "needs_review", "dead_letter"].includes(m.state)) {
      add("↻ Zkusit znovu", () => control("retry", m.id, m.recipient));
    }
    if (actions.childElementCount) div.appendChild(actions);
  }
}

function renderPause(p) {
  if (p === undefined) return;  // zpráva bez stavu pauzy nesmí přepsat tlačítko
  lastPaused = p;
  renderSystemMetric();
  const el = document.getElementById("pause");
  const pauseKey = JSON.stringify(p);
  if (el._pauseKey === pauseKey) return;
  el._pauseKey = pauseKey;
  el.innerHTML = "";
  const bar = document.createElement("div");
  const b = document.createElement("button");
  if (p) {
    bar.className = "pausebar stopped";
    bar.innerHTML = `<div class="ptext"><div class="big">⏸ Agenti jsou ZASTAVENÍ — na pozadí nic nepracuje</div>`
      + `<div class="why">Zastavil: ${esc(nameOf(p.by))}${p.reason ? " · důvod: " + esc(p.reason) : ""}${p.at ? " · v " + clock(p.at) : ""}</div></div>`;
    b.textContent = "▶ Pustit agenty znovu";
    b.onclick = () => { if (confirm("Pustit agenty znovu do samostatné práce?")) pauseCall("resume", ""); };
  } else {
    bar.className = "pausebar running";
    bar.innerHTML = `<div class="ptext"><div class="big">● Provoz povolen</div></div>`;
    b.textContent = "Zastavit všechny agenty";
    b.onclick = () => { const r = prompt("Opravdu zastavit všechny agenty? Napiš důvod (zaznamená se):", "nouzové zastavení"); if (r !== null) pauseCall("pause", r); };
  }
  bar.appendChild(b);
  el.appendChild(bar);
}
function pauseCall(action, reason) {
  fetch("/api/" + action, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({reason})})
    .then(r => {
      if (!r.ok) return r.text().then(t => alert(t));
      // Odpověď nese jen ano/ne; skutečný stav (kdo, proč) dočteme ze snímku.
      return fetch("/api/snapshot?since=" + Number.MAX_SAFE_INTEGER).then(s => s.json()).then(s => renderPause(s.paused));
    });
}

function agentCard(p, b) {
  const pres = PRESENCE[(b && b.presence) || p.presence] || [p.active ? "online" : "offline", p.active ? "ok" : ""];
  const tags = [tag(pres[0], pres[1])];
  const src = b || p;
  if (src.provider_state === "limited") tags.push(tag("vyčerpaný limit" + (src.provider_resets_at ? " do " + clock(src.provider_resets_at) : ""), "bad"));
  else if (src.provider_state === "unknown") tags.push(tag("stav limitu neznámý", "warn"));
  if (src.limited_until) tags.push(tag("limit do " + clock(src.limited_until), "bad"));
  if (b && b.queued) tags.push(tag(`ve frontě ${b.queued}` + (b.unaccepted ? `, nepřevzato ${b.unaccepted}` : ""), ""));
  if (b && b.needs_review) tags.push(tag(`ke kontrole ${b.needs_review}`, "warn"));
  let line = "";
  if (b && b.active) line = `▶ #${b.active.id}: ${esc(b.active.text)}`;
  else if (p.status) line = esc(p.status);
  else if (!p.active) line = "naposledy " + ago(p.seen_seconds_ago);
  if (b && b.blocked_reason) line += `<br>čeká: ${esc(b.blocked_reason)}`;
  if (b && b.blocked_task) line += `<br>⌑ #${b.blocked_task.id} ${esc(b.blocked_task.path)} drží ${esc(nameOf(b.blocked_task.held_by))}`;
  return `<div class="card ${p.active ? "" : "offline"} ${roleOf(p.agent) === "most" ? "bridge" : roleOf(p.agent) === "agent" ? "interactive" : ""}">
    <div class="head"><div class="avatar" style="${avatarStyle(p.agent)}">${esc(initial(p.agent))}<span class="dot ${p.active ? "on" : ""}"></span></div>
      <div class="who"><div class="name">${esc(nameOf(p.agent))}</div>${NAMES[p.agent] || p.agent === me ? `<div class="id">${esc(p.agent)}</div>` : ""}</div><span class="role">${roleOf(p.agent)}</span></div>
    ${line ? `<div class="line agent-work">${line}</div>` : ""}
    <div class="tags">${tags.join("")}</div>${p.seen_seconds_ago == null ? "" : `<div class="last-active">Aktivita ${ago(p.seen_seconds_ago)}</div>`}</div>`;
}

const REVIEW = {
  working: ["rozpracováno", ""], awaiting_review: ["čeká na review", "work"],
  changes_requested: ["požadované změny", "warn"], approved: ["schváleno", "ok"],
  escalated: ["ČEKÁ NA TEBE", "bad"], closed: ["uzavřeno", "gone"],
};
// B6: výsledek kontroly tajných údajů a bezpečnostní verdikt (bez hodnot tajemství)
function scanTag(v) {
  if (!v.head_commit || v.state === "closed") return "";
  const f = v.secret_findings;
  if (f === null || f === undefined) return tag("bez kontroly tajemství", "warn");
  if (f.length) return tag(`tajné údaje: ${f.length} (${[...new Set(f.map(x => x.file))].slice(0, 2).join(", ")})`, "bad");
  return v.security === "pass" ? tag("bezpečnost OK", "ok") : tag("bez tajemství", "ok");
}
function renderReviews(list) {
  if (list === undefined) return;
  document.getElementById("reviewCount").textContent = list.length;
  const el = document.getElementById("reviews");
  const reviewKey = JSON.stringify(list);
  if (el._reviewKey === reviewKey) return;
  el._reviewKey = reviewKey;
  el.innerHTML = "";
  if (!list.length) { el.innerHTML = "<div class='empty'>žádné otevřené review</div>"; return; }
  // Co čeká na člověka, nahoru.
  [...list].sort((x, y) => (y.state === "escalated") - (x.state === "escalated")).forEach(v => {
    const st = REVIEW[v.state] || [v.state, ""];
    const card = document.createElement("div");
    card.className = "card" + (v.state === "escalated" ? " alert" : "");
    const who = `${esc(nameOf(v.author))}${v.reviewer ? " → " + esc(nameOf(v.reviewer)) : ""}`;
    const sha = v.head_commit ? ` · ${esc(v.head_commit.slice(0, 10))}` : "";
    const why = v.verdict_note && v.state !== "approved" ? `<div class="line">„${esc(v.verdict_note.slice(0, 240))}"</div>` : "";
    card.innerHTML = `<div class="name">#${v.id} ${esc(v.topic)}</div>`
      + `<div class="line">${who}${sha}</div><div class="path">${esc(v.repo || "")}</div>${why}`
      + `<div class="tags">${tag(st[0], st[1])}${tag(`kolo oprav ${v.fix_cycles}/${v.max_fix_cycles}`, v.cycles_left ? "" : "bad")}${scanTag(v)}</div>`;
    if (v.state === "escalated") {
      const btns = document.createElement("div");
      btns.className = "btns";
      const grant = document.createElement("button");
      grant.className = "btn"; grant.textContent = "Povolit ještě 1 kolo";
      grant.onclick = () => {
        const reason = prompt("Proč povolit další kolo oprav? (zaznamená se)");
        if (reason) post("/api/review-grant", {id: v.id, reason});
      };
      const close = document.createElement("button");
      close.className = "btn"; close.textContent = "Uzavřít review";
      close.onclick = () => {
        const reason = prompt("Proč uzavřít? (zaznamená se)", "převezmu sám");
        if (reason !== null) post("/api/review-close", {id: v.id, reason});
      };
      btns.append(grant, close);
      card.appendChild(btns);
    }
    el.appendChild(card);
  });
}

function renderRoom(r) {
  lastRoomAt = Date.now();
  if (r.me) { me = r.me; document.getElementById("humanBadge").textContent = String(me).slice(0, 1).toUpperCase(); }
  if (Object.prototype.hasOwnProperty.call(r, "quality_runs")) {
    const records = Array.isArray(r.quality_runs) ? r.quality_runs : [];
    qgRecords = records.filter(record => record && ((record.kind === "quality_run" && record.executed === true) || (["quality_attempt", "quality_refusal"].includes(record.kind) && Number.isInteger(record.run_id) && record.run_id > 0)));
  }
  if (Array.isArray(r.quality_requests)) qgRequests = r.quality_requests;
  if (Object.prototype.hasOwnProperty.call(r, "quality_runs") || Object.prototype.hasOwnProperty.call(r, "quality_requests")) renderQualityGate();
  latestBoard = Array.isArray(r.board) ? r.board : null;
  renderReviews(r.reviews);
  if (Array.isArray(r.reviews)) latestReviews = r.reviews;
  if (r.projects) latestProjects = r.projects;
  if (Array.isArray(r.reviews_all)) { latestReviewsAll = r.reviews_all; renderProjects(r.reviews_all); }
  onDataVersion(r.version);
  if (r.mode && r.mode.mode) renderMode(r.mode);
  if (Array.isArray(r.claims)) latestClaims = r.claims;
  renderingRoom = true;
  (r.messages || []).forEach(m => renderMessage(m));
  renderingRoom = false;
  if (currentView === "ops") renderOpsTasks();   // stav úkolu se změní hned s událostí, bez čekání na obnovu
  renderPause(r.paused);
  const board = {};
  (r.board || []).forEach(b => { board[b.agent] = b; });
  const peers = (r.peers || []).filter(p => !String(p.agent).startsWith("untrusted-"));
  (r.board || []).forEach(b => { if (!peers.some(p => p.agent === b.agent)) peers.push({agent: b.agent, active: false, status: b.status}); });
  latestPeers = peers;
  const coreAgent = p => ["claude-code", "codex"].includes(p.agent);
  const online = peers.filter(p => p.active || coreAgent(p)), offline = peers.filter(p => !p.active && !coreAgent(p));
  document.getElementById("agentCount").textContent = peers.length;
  renderAgentList("agents",online,board);
  renderAgentList("offline",offline,board);
  highlightAgent();
  document.getElementById("offlineSummary").textContent = `Offline (${offline.length})`;
  document.getElementById("offlineBox").style.display = offline.length ? "" : "none";
  setHTMLIfChanged(document.getElementById("claims"), (r.claims || []).map(c => `
    <div class="card"><div class="path">⌑ ${esc(c.path)}</div>
      <div class="line">${esc(nameOf(c.held_by))}${c.purpose ? " · " + esc(c.purpose) : ""} · ještě ${ttl(c.expires_in_seconds)}</div></div>`).join("")
    || "<div class='empty'>nic není zamčené</div>");
  // Komu: skuteční agenti, bez mostů, systému a sebe.
  const names = new Set(["claude-code", "codex"]);
  peers.forEach(p => { const a = String(p.agent); if (a !== me && a !== "broker" && !a.endsWith("-bridge")) names.add(a); });
  const list = [...names].sort((a, b) => (NAMES[a] ? 0 : 1) - (NAMES[b] ? 0 : 1) || a.localeCompare(b));
  if (list.join() !== knownAgents.join()) { knownAgents = list; fillRecipients(); }
  refreshDashboard();
  renderPages();
  historyReady = true;
}

function fillRecipients() {
  const prev = toEl.value;
  toEl.innerHTML = "";
  knownAgents.forEach(a => toEl.add(new Option(nameOf(a), a)));
  const all = new Option("Všem v místnosti", "*");
  all.disabled = kind === "task";
  toEl.add(all);
  if ([...toEl.options].some(o => o.value === prev && !o.disabled)) toEl.value = prev;
  syncHint();
}
function setKind(k) {
  kind = k;
  document.querySelectorAll("#kindSeg button").forEach(b => { b.classList.toggle("on", b.dataset.kind === k); b.setAttribute("aria-pressed", b.dataset.kind === k); });
  fillRecipients();
}
function syncHint() {
  const to = toEl.value === "*" ? "všem v místnosti" : nameOf(toEl.value);
  if (kind === "task") {
    hintEl.className = "task";
    hintEl.textContent = `Úkol: ${to} ho převezme a udělá sám, klidně i na pozadí bez tebe. Posílej jen to, co má opravdu udělat.`;
  } else {
    hintEl.className = "";
    hintEl.textContent = `Zpráva pro: ${to}. Jen informace — přečte si ji, až se podívá do místnosti. Nic se samo nespustí.`;
  }
  textEl.placeholder = kind === "task" ? "Popiš, co má udělat…" : "Napiš zprávu… (Enter pošle, Shift+Enter nový řádek)";
}

function connect(since) {
  const es = new EventSource("/api/events?since=" + since + "&k=" + encodeURIComponent(sessionKey()));
  es.addEventListener("message", e => renderMessage(JSON.parse(e.data), {live:true}));
  es.addEventListener("room", e => renderRoom(JSON.parse(e.data)));
  es.onopen = () => updateStatus("live");
  es.onerror = () => updateStatus("retry");
}

// Nejdřív snapshot, ať karta nezačíná prázdná, pak SSE od jeho posledního ID —
// `Last-Event-ID`/`since` na serveru zajistí navazující stream bez díry i bez duplicit.
async function establishSession() {
  const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
  history.replaceState(null, "", location.pathname + location.search);
  if (!token) return;  // bez tokenu zkusíme existující cookie a uložený klíč
  const response = await fetch("/session", { method: "POST", headers: { "X-Agent-Lease-Token": token } });
  if (!response.ok) throw new Error("Login failed");
  localStorage.setItem(KEY_STORE, response.headers.get("X-Agent-Lease-Key") || "");
}

// 403 = přihlášení na tomto serveru neplatí (např. po jeho restartu): říct to jasně, ne jen chybou v konzoli.
const rawFetch = window.fetch.bind(window);
let sessionLostShown = false;
// Druhý klíč přihlášení: localStorage je oddělené podle portu, cookie ne (bezpečnostní audit 26. 11.).
const sessionKey = () => localStorage.getItem(KEY_STORE) || "";
window.fetch = (url, opts = {}) => rawFetch(url, {...opts, headers: {...(opts.headers || {}), "X-Agent-Lease-Key": sessionKey()}}).then(r => {
  if (r.status === 403 && !String(url).startsWith("/session") && !sessionLostShown) { sessionLostShown = true; showSessionLost(); }
  return r;
});
function showSessionLost() {
  updateStatus("retry");
  const bar = document.createElement("div"); bar.className = "session-lost"; bar.setAttribute("role", "alert");
  bar.innerHTML = "Přihlášení k webu vypršelo. Přihlas se znovu přes Windows Hello, nebo ikonou <b>Agent Lease</b> na ploše.";
  document.body.prepend(bar);
  showLoginHelp();   // rovnou nabídnout přihlášení Windows Hello
}
// ── Windows Hello (WebAuthn): base64url ↔ ArrayBuffer ──
const b64uToBuf = s => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)), c => c.charCodeAt(0)).buffer;
const bufToB64u = b => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/[+]/g, "-").replace(/[/]/g, "_").replace(/=+$/, "");
const helloSupported = () => !!window.PublicKeyCredential && location.hostname === "localhost";
async function helloLogin() {
  const btn = document.getElementById("loginHello"), st = document.getElementById("loginStatus");
  btn.disabled = true; st.className = "login-status"; st.textContent = "Potvrď přihlášení PINem ve Windows…";
  try {
    const opt = await rawFetch("/session/passkey-options", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
    if (!opt.ok) throw new Error(opt.status === 429 ? "Příliš mnoho pokusů. Zkus to za 5 minut, nebo použij ikonu na ploše." : "Přihlášení teď nejde.");
    const o = await opt.json();
    if (!o.allowCredentials.length) throw new Error("Windows Hello zatím není nastavené. Přihlas se ikonou na ploše a v Nastavení → Přihlášení klikni na „Přidat Windows Hello“.");
    const cred = await navigator.credentials.get({publicKey: {challenge: b64uToBuf(o.challenge), rpId: o.rpId, userVerification: "required", timeout: o.timeout,
      allowCredentials: o.allowCredentials.map(c => ({type: "public-key", id: b64uToBuf(c.id)}))}});
    const r = cred.response;
    const res = await rawFetch("/session/passkey", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      id: cred.id, clientDataJSON: bufToB64u(r.clientDataJSON), authenticatorData: bufToB64u(r.authenticatorData), signature: bufToB64u(r.signature)})});
    if (!res.ok) throw new Error(res.status === 429 ? "Příliš mnoho pokusů. Zkus to za 5 minut." : "Ověření se nepovedlo.");
    localStorage.setItem(KEY_STORE, res.headers.get("X-Agent-Lease-Key") || "");
    st.className = "login-status ok"; st.textContent = "Přihlášeno.";
    location.reload();
  } catch (e) {
    st.textContent = e && e.name === "NotAllowedError" ? "Přihlášení bylo zrušeno nebo vypršelo." : (e && e.message) || "Přihlášení se nepovedlo.";
    btn.disabled = false;
  }
}
document.getElementById("loginHello").onclick = helloLogin;
async function addPasskey() {
  const st = document.getElementById("pkStatus");
  if (!helloSupported()) { st.textContent = stPkStatus = "Windows Hello funguje jen na adrese http://localhost:8765 v prohlížeči s WebAuthn."; return; }
  try {
    st.textContent = stPkStatus = "Potvrď přidání PINem ve Windows…";
    const o = await (await fetch("/api/passkey/register-options", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"})).json();
    const cred = await navigator.credentials.create({publicKey: {...o, challenge: b64uToBuf(o.challenge), user: {...o.user, id: b64uToBuf(o.user.id)},
      excludeCredentials: o.excludeCredentials.map(c => ({type: "public-key", id: b64uToBuf(c.id)}))}});
    const ok = await post("/api/passkey/register", {id: cred.id, clientDataJSON: bufToB64u(cred.response.clientDataJSON),
      attestationObject: bufToB64u(cred.response.attestationObject), name: "Windows Hello · " + new Date().toLocaleDateString("cs-CZ")});
    st.textContent = stPkStatus = ok ? "Hotovo. Příště se přihlásíš PINem přímo na stránce." : "";
    loadPasskeys();
  } catch (e) { st.textContent = stPkStatus = e && e.name === "NotAllowedError" ? "Přidání bylo zrušeno." : "Přidání se nepovedlo."; }
}

function showLoginHelp() {
  // Bez přihlášení: přihlašovací obrazovka s Windows Hello; ikona na ploše zůstává jako záloha.
  const screen = document.getElementById("loginScreen");
  screen.hidden = false;
  const btn = document.getElementById("loginHello");
  if (!helloSupported()) { btn.hidden = true; document.getElementById("loginStatus").textContent = location.hostname === "localhost" ? "Tento prohlížeč neumí Windows Hello." : "Windows Hello funguje na adrese http://localhost:8765."; }
  else btn.focus();
  // Bez platného přihlášení by horní pruh navždy ukazoval „Načítám…“ (review #17).
  const help = "Nejsi přihlášený. Klikni na ikonu <b>Agent Lease</b> na ploše, nebo v terminálu spusť <b>agent-lease web-login</b>.";
  document.getElementById("pause").innerHTML = `<div class="pausebar"><div class="big">${help}</div></div>`;
  feed.innerHTML = `<div class="bubble"><div class="text">${help}</div></div>`;
}

establishSession()
  .then(() => fetch("/api/snapshot?since=0"))
  .then(r => { if (!r.ok) throw new Error("auth"); return r.json(); })
  .then(s => {
    if (s.me) me = s.me;
    renderRoom(s);
    feed.scrollTop = feed.scrollHeight;
    connect(s.messages.length ? s.messages[s.messages.length - 1].id : 0);
    loadDashData();
  })
  .catch(() => { updateStatus("retry"); showLoginHelp(); });

function post(url, body) {
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then(r => { if (!r.ok) r.text().then(t => alert(t)); return r.ok; })
    .catch(() => { alert("Spojení se nezdařilo. Zkontroluj stav v místnosti před opakováním akce."); return false; });
}
function send() {
  const text = textEl.value;
  if (!text.trim()) return;
  if (!toEl.value) { alert("Vyber, komu to je."); return; }
  const broadcast = toEl.value === "*";
  if (broadcast && kind === "task") { alert("Úkol musí mít jednoho adresáta."); return; }
  const btn = document.getElementById("sendBtn");
  btn.disabled = true;
  post("/api/send", { text, kind: broadcast ? "broadcast" : kind, to: broadcast ? "" : toEl.value })
    .then(ok => { if (ok) { textEl.value = ""; setKind("chat"); } })
    .finally(() => { btn.disabled = false; textEl.focus(); });
}
function control(action, id, to) { post("/api/" + action, { id, to }); }

document.querySelectorAll("#kindSeg button").forEach(b => b.onclick = () => setKind(b.dataset.kind));
toEl.addEventListener("change", syncHint);
showSystemEl.addEventListener("change", applyFilters);
document.getElementById("search").addEventListener("input", applyFilters);
document.getElementById("taskPicker").addEventListener("change", e => selectTask(e.target.value));
document.querySelectorAll("[data-filter]").forEach(b => b.onclick = () => {
  activeFilter = b.dataset.filter;
  document.querySelectorAll("[data-filter]").forEach(el => { el.classList.toggle("on", el === b); el.setAttribute("aria-pressed", el === b); });
  applyFilters();
});
document.getElementById("sendBtn").onclick = send;
textEl.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
fillRecipients();
// Read-only presentation of server-derived runs. Demo never enters the live data.
const QG_STATUS = {
  PASS:{label:"PASS",tone:"ok",icon:"✓",meaning:"Všechny povinné kontroly prošly."},
  REVIEW:{label:"REVIEW",tone:"warn",icon:"◇",meaning:"Vyžaduje kontrolu."},
  FAIL:{label:"FAIL",tone:"bad",icon:"✕",meaning:"Povinná kontrola selhala."},
  NA:{label:"N/A",tone:"muted",icon:"—",meaning:"Kontrola není použitelná nebo chybí metriky."},
  SETUP_REQUIRED:{label:"SETUP REQUIRED",tone:"work",icon:"⚙",meaning:"Projekt nemá nastavené povinné kontroly."},
  NOT_RUN:{label:"NOT RUN",tone:"muted",icon:"○",meaning:"Kontrola dosud neproběhla."},
};
const QG_CHECKS = [["tests","Testy"],["lint","Lint"],["typecheck","Type check"],["build","Build"],["coverage","Coverage"],["security","Security"],["crap","CRAP score"]];
const QG_SUMMARY = ["PASS","REVIEW","FAIL","NA","SETUP_REQUIRED"];
let qgDemo = false;
let qgRecords = null;
let qgRequests = [];
let qgSelected = null;
let currentView = "overview";
function qgBadge(status) {
  const s = QG_STATUS[status] || QG_STATUS.NOT_RUN;
  return `<span class="tag qg-badge ${s.tone}"><span aria-hidden="true">${s.icon}</span> ${s.label}</span>`;
}
function qgSummary(records) {
  return QG_SUMMARY.map(status=>{const s=QG_STATUS[status];return `<div class="qg-stat" data-qg-state="${status}" data-tone="${s.tone}"><span aria-hidden="true">${s.icon}</span><span class="qg-stat-name">${s.label}</span><strong>${qgDemo || qgRecords !== null ? records.filter(r=>r.overall_status===status).length : "—"}</strong></div>`;}).join("");
}
function qgLogs(record) {
  const logs = Array.isArray(record.logs) ? record.logs : [];
  const safe = logs.filter(log => /^[/]api[/]quality-log[?]run_id=[1-9][0-9]*&check=(tests|lint|typecheck|build|coverage)$/.test(log.url || ""));
  return safe.length ? `<ul>${safe.map(log => `<li><a href="${esc(log.url + "&k=" + encodeURIComponent(sessionKey()))}" target="_blank" rel="noopener noreferrer">${esc(log.check)} · procesní protokol</a></li>`).join("")}</ul>` : "";
}
function qgDate(value) { return value ? new Date(value).toLocaleString("cs-CZ") : "Není známá"; }
function qgTable(records,{compact=false}={}) {
  const headers = compact ? ["Repozitář",...QG_CHECKS.map(c=>c[1]),"Stav"] : ["Repozitář / větev","Commit","Agent","Kontrola",...QG_CHECKS.map(c=>c[1]),"Stav"];
  return `<div class="qg-scroll" tabindex="0" role="region" aria-label="Výsledky kontrol repozitářů; tabulku lze posouvat vodorovně"><table class="qg-table"><caption class="sr-only">${qgDemo ? "DEMO — ukázkové výsledky, ne skutečné kontroly" : records.length ? "Zaznamenané výsledky kontrol" : "Quality Gate dosud nemá výsledky"}</caption><thead><tr>${headers.map(h=>`<th scope="col">${h}</th>`).join("")}</tr></thead><tbody>`
    + (records.length ? records.map((r,i)=>`<tr><th scope="row"><button type="button" class="qg-repo" data-qg-project="${i}">${esc(r.repository)}</button>${compact ? "" : `<span class="qg-branch">${esc(r.branch || "Větev není uvedená")}</span>`}</th>${compact ? "" : `<td class="path">${esc(r.commit_sha ? r.commit_sha.slice(0,10) : "Neuveden")}</td><td>${esc(r.agent || "Neuveden")}</td><td>${esc(qgDate(r.timestamp))}</td>`}${QG_CHECKS.map(([key])=>`<td>${qgBadge(r.checks?.[key]?.status || "NOT_RUN")}</td>`).join("")}<td>${qgBadge(r.overall_status)}</td></tr>`).join("") : `<tr><td colspan="${headers.length}" class="qg-empty">Dosud bez výsledků. Pro tyto repozitáře není zaznamenaný běh kontrol.</td></tr>`)
    + `</tbody></table></div>`;
}
// ── Stránky Kvalita, Výzkum, Paměť, Nastavení (předlohy 01/03/04/06, jen skutečná data) ──
const IC = {
  search:'<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/>',
  layers:'<path d="m12 3 9 5-9 5-9-5z"/><path d="m3 13 9 5 9-5"/>',
  flask:'<path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3"/><path d="M7.5 15h9"/>',
  check:'<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
  bookmark:'<path d="M6 3h12v18l-6-4-6 4z"/>',
  bars:'<path d="M5 20V12M10 20V8M15 20v-6M20 20V4"/>',
  target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  doc:'<path d="M6 3h9l4 4v14H6z"/><path d="M9 12h7M9 16h5"/>',
  link:'<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  pulse:'<path d="M3 12h4l3-7 4 14 3-7h4"/>',
  shield:'<path d="M12 3 4 6v6c0 4 4 7 8 9 4-2 8-5 8-9V6Z"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
  star:'<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1-4.4-4.3 6.1-.9z"/>',
  alert:'<path d="M12 4 2.5 20h19z"/><path d="M12 10v4M12 17v.5"/>',
  db:'<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
  folder:'<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  network:'<circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/><path d="M12 7.5v4M12 11.5 6.5 17M12 11.5l5.5 5.5"/>',
  flag:'<path d="M5 21V4"/><path d="M5 4h12l-2 4 2 4H5"/>',
  gear:'<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/>',
  users:'<circle cx="9" cy="8" r="3.5"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6M16 4.5a3.5 3.5 0 0 1 0 7M18 14c2 .8 3 2.7 3 6"/>',
  lock:'<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  stop:'<circle cx="12" cy="12" r="9"/><path d="M9 9h6v6H9z"/>',
  sliders:'<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
  cube:'<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  review:'<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="12" r="2.5"/><path d="M6 8.5v7M8.5 6H12a4 4 0 0 1 4 4v-.5"/>',
  bridge:'<path d="M3 17h18M5 17V9M19 17V9M5 9c4 4 10 4 14 0"/>',
};
function icon(n) { return `<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${IC[n] || ""}</svg>`; }
function paintIcons(root = document) {
  root.querySelectorAll("i[data-ic]:not([data-painted])").forEach(i => { i.innerHTML = icon(i.dataset.ic); i.dataset.painted = "1"; i.setAttribute("aria-hidden", "true"); });
}
paintIcons();
function metricsRow(id, items) {
  setHTMLIfChanged(document.getElementById(id), items.map(([label, value, ic, tone, title, goto]) =>
    `<div class="metric" data-tone-card="${tone}"${title ? ` title="${esc(title)}"` : ""}${goto ? ` data-mgoto="${goto}" tabindex="0" role="link"` : ""}><span class="metric-icon" aria-hidden="true">${icon(ic)}</span><div><div class="metric-label">${esc(label)}</div><div class="metric-value${String(value).length > 7 ? " small" : ""}">${esc(value)}</div></div></div>`).join(""));
}
// Klik na dlaždici se cílem (data-mgoto): „attn“ = panel Čeká na tebe, jinak stránka.
document.addEventListener("click", e => { const m = e.target.closest("[data-mgoto]"); if (!m) return;
  if (m.dataset.mgoto === "attn") { renderAttention(); document.getElementById("attnDialog").showModal(); } else showView(m.dataset.mgoto); });
document.addEventListener("keydown", e => { const m = e.target.closest && e.target.closest("[data-mgoto]"); if (m && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); m.click(); } });
function when(t) {
  if (t === null || t === undefined || t === "") return "";
  const d = typeof t === "number" ? new Date(t * 1000) : new Date(t);
  if (isNaN(d)) return "";
  const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime(), today = day(new Date());
  const hm = d.toLocaleTimeString("cs-CZ", {hour: "2-digit", minute: "2-digit"});
  return day(d) === today ? "Dnes " + hm : day(d) === today - 86400000 ? "Včera " + hm : d.toLocaleDateString("cs-CZ", {day: "numeric", month: "numeric"}) + " " + hm;
}
function agentKey(a) { return a === "claude-code" || a === "claude-code-bridge" ? "ada" : a === "codex" || a === "codex-bridge" ? "nox" : ""; }
function whoBadge(a) { const k = agentKey(a); return `<span class="who-badge ${k || "other"}">${esc(k === "ada" ? "Ada" : k === "nox" ? "Nox" : nameOf(a))}</span>`; }
function actItem(agent, t, text) {
  const k = agentKey(agent), plain = String(text || "").replace(/[*`]/g, "");
  const face = k ? `<img class="act-portrait" src="/assets/${k}.webp" alt="">` : `<span class="act-portrait act-initial" aria-hidden="true">${esc(initial(agent))}</span>`;
  return `<li class="act" data-agent="${k || "human"}">${face}<div class="act-body"><div class="act-head"><b>${esc(k === "ada" ? "Ada" : k === "nox" ? "Nox" : nameOf(agent))}</b><time>${esc(when(t))}</time></div><p>${esc(plain.slice(0, 140))}${plain.length > 140 ? "…" : ""}</p></div></li>`;
}
function chipsHTML(items, current, attr) {
  return items.map(([id, label]) => `<button type="button" class="chip${id === current ? " on" : ""}" ${attr}="${esc(id)}" aria-pressed="${id === current}">${esc(label)}</button>`).join("");
}
const STEP_SR = {done: "hotovo", now: "probíhá", fail: "selhalo", warn: "vyžaduje kontrolu", "": "čeká"};
function stepperHTML(steps) {
  return `<ol class="stepper">${steps.map(([label, s]) => `<li class="${s}"><span class="node" aria-hidden="true">${s === "done" ? "✓" : s === "fail" ? "✕" : s === "warn" ? "!" : ""}</span>${esc(label)}<span class="sr-only">: ${STEP_SR[s] || s}</span></li>`).join("")}</ol>`;
}
function inlineMd(s) { return esc(s).replace(/[*][*](.+?)[*][*]/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>"); }
function mdLite(text) {
  const out = []; let list = false;
  for (const raw of String(text || "").split(String.fromCharCode(10))) {
    const line = raw.trimEnd(), li = /^[ ]*(?:[-*•]|[0-9]+[.)])[ ]+(.*)/.exec(line);
    if (li) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inlineMd(li[1])}</li>`); continue; }
    if (list) { out.push("</ul>"); list = false; }
    if (!line.trim()) continue;
    const h = /^#{1,6}[ ]+(.*)/.exec(line);
    out.push(h ? `<h4>${inlineMd(h[1])}</h4>` : `<p>${inlineMd(line)}</p>`);
  }
  if (list) out.push("</ul>");
  return `<div class="md">${out.join("")}</div>`;
}
function dedupeActivity(list) {
  const seen = new Set();   // Nox posílá výsledek úkolu i jako odpověď na každou navázanou zprávu
  return list.filter(m => { const k = m.agent + "|" + String(m.text || "").replace(/^(Done|Hotovo) #[0-9]+: */, "").slice(0, 120); return !seen.has(k) && seen.add(k); });
}
function renderPageActivity() {
  const items = dedupeActivity(historyMessages().filter(m => agentKey(m.agent) && !isSystem(m)).reverse()).slice(0, 8);
  const html = items.length ? items.map(m => actItem(m.agent, Date.now() / 1000 - (m.seconds_ago || 0), m.text)).join("") : "<li class='empty'>Čekám na zprávy Ady a Noxe.</li>";
  document.querySelectorAll('[data-activity="agents"]').forEach(el => setHTMLIfChanged(el, html));
}
function renderPages() {
  if (currentView === "quality") renderQualityGate();
  else if (currentView === "research") renderResearchPage();
  else if (currentView === "memory") renderMemoryPage();
  else if (currentView === "settings") renderSettings();
  else if (currentView === "ops") renderOpsTasks();
  if (["quality", "research"].includes(currentView)) renderPageActivity();
}

// ── Kvalita ──
let qgFilter = "all", rvFilter = "you";
const QG_OPEN = s => ["REVIEW", "SETUP_REQUIRED"].includes(s);
function qgLatest() {
  const seen = new Set();
  return (qgRecords || []).filter(r => !seen.has(r.repository) && seen.add(r.repository));
}
function renderQualityGate() {
  const latest = qgLatest(), all = latestReviewsAll || [];
  document.querySelectorAll("[data-qg-mode]").forEach(el => { el.textContent = qgRecords === null ? "Výsledky nejsou dostupné" : "Zaznamenané běhy kontrol"; });
  document.getElementById("qgCompactSummary").innerHTML = qgSummary(latest);
  document.querySelectorAll("[data-qg-state]").forEach(el => { el.title = QG_STATUS[el.dataset.qgState].meaning; });
  document.getElementById("qgCompactTable").innerHTML = qgTable(latest, {compact: true});
  document.querySelectorAll("#qgCompactTable [data-qg-project]").forEach(b => b.onclick = () => { qgSelected = latest[Number(b.dataset.qgProject)].run_id; showView("quality"); });
  const n = s => latest.filter(r => r.overall_status === s).length, known = qgRecords !== null;
  const approved = all.filter(r => r.verdict === "approved").length;
  metricsRow("qgMetrics", [
    ["Repozitáře", known ? latest.length : "—", "layers", "cyan"],
    ["Kontroly PASS", known ? n("PASS") : "—", "check", "green", "Poslední běh v repozitáři prošel všemi povinnými kontrolami"],
    ["Kontroly FAIL", known ? n("FAIL") : "—", "alert", "orange"],
    ["Ke kontrole", known ? latest.filter(r => QG_OPEN(r.overall_status)).length : "—", "flask", "purple"],
    ["Schválená review", all.length ? `${approved} / ${all.length}` : "—", "review", "purple", "Review se schvalujícím verdiktem ze všech zaznamenaných"],
    ["Čeká na tebe", latestReviewsAll === null ? "—" : all.filter(r => r.state === "escalated").length, "flag", "cyan", "Eskalovaná review po 3 kolech oprav", "attn"],
  ]);
  if (currentView !== "quality") return;
  const rows = latest.filter(r => qgFilter === "all" || (qgFilter === "open" ? QG_OPEN(r.overall_status) : r.overall_status === qgFilter));
  setHTMLIfChanged(document.getElementById("qgChips"), chipsHTML([["all", `Vše (${latest.length})`], ["PASS", `PASS (${n("PASS")})`], ["FAIL", `FAIL (${n("FAIL")})`], ["open", `Ke kontrole (${latest.filter(r => QG_OPEN(r.overall_status)).length})`]], qgFilter, "data-qg-filter"));
  document.querySelectorAll("[data-qg-filter]").forEach(b => b.onclick = () => { qgFilter = b.dataset.qgFilter; renderQualityGate(); });
  document.getElementById("qgRepoCount").textContent = known ? `(${latest.length})` : "";
  if (qgSelected === null || !latest.some(r => r.run_id === qgSelected)) qgSelected = rows[0] ? rows[0].run_id : null;
  setHTMLIfChanged(document.getElementById("qgProjectTable"), rows.length ? rows.map(r => {
    const name = String(r.repository || "").split("/").filter(Boolean).pop() || r.repository;
    return `<button type="button" class="pg-item${r.run_id === qgSelected ? " on" : ""}" data-qg-run="${r.run_id}" title="${esc(r.repository)}"><span class="pg-ic">${icon("shield")}</span><span class="pg-item-body"><span class="pg-item-title">${esc(name)}</span><span class="pg-item-sub">${esc(r.branch || "větev neuvedena")} · ${esc(when(r.timestamp))}</span><span class="pg-item-foot">${qgBadge(r.overall_status)}${r.agent ? whoBadge(r.agent) : ""}</span></span></button>`;
  }).join("") : `<div class="empty">${known ? "Žádný zaznamenaný běh kontrol pro tento filtr." : "Výsledky kontrol nejsou dostupné."}</div>`);
  document.querySelectorAll("[data-qg-run]").forEach(b => b.onclick = () => { qgSelected = Number(b.dataset.qgRun); renderQualityGate(); });
  const pending = qgRequests.filter(q => q.state !== "completed").slice(0, 5);
  const requestLabels = {queued: "čeká ve frontě", running: "kontroly běží", busy: "čeká na volný runner", refused: "je potřeba nastavení", interrupted: "běh byl přerušen"};
  setHTMLIfChanged(document.getElementById("qgRequests"), pending.length ? `<div class="qg-req"><b>Požadavky na kontrolu</b><ul>${pending.map(q => `<li>${esc(String(q.repository).split("/").pop())} · ${esc(requestLabels[q.state] || q.state)}${q.detail ? " · " + esc(q.detail) : ""}</li>`).join("")}</ul></div>` : "");
  renderQualityProject();
  // stav review
  const RS = [["approved", "Schválená", "ok", "✓"], ["awaiting_review", "Čeká na review", "work", "○"], ["changes_requested", "Požadované změny", "warn", "!"], ["escalated", "Čeká na tebe", "bad", "!"], ["closed", "Uzavřená", "", "–"]];
  setHTMLIfChanged(document.getElementById("qgReviewStats"), latestReviewsAll === null ? "<li class='empty'>Načítám…</li>" : RS.map(([s, label, tone, sym]) => `<li><span class="st-dot ${tone}" aria-hidden="true">${sym}</span>${label}<b>${all.filter(r => r.state === s).length}</b></li>`).join(""));
  const openAll = all.filter(r => r.state !== "closed").sort((a, b) => (b.state === "escalated") - (a.state === "escalated") || b.id - a.id);
  const RV = {you: r => r.state === "escalated", merge: r => r.state === "approved", work: r => ["awaiting_review", "changes_requested", "working"].includes(r.state), all: () => true};
  if (rvFilter === "you" && latestReviewsAll !== null && !openAll.some(RV.you)) rvFilter = "all";
  const open = openAll.filter(RV[rvFilter]);
  document.getElementById("qgOpenCount").textContent = latestReviewsAll === null ? "" : `(${openAll.length})`;
  setHTMLIfChanged(document.getElementById("rvChips"), chipsHTML([["you", `Čeká na tebe (${openAll.filter(RV.you).length})`], ["merge", `Schválené k uzavření (${openAll.filter(RV.merge).length})`], ["work", `Rozpracované (${openAll.filter(RV.work).length})`], ["all", "Vše"]], rvFilter, "data-rv-filter"));
  document.querySelectorAll("[data-rv-filter]").forEach(b => b.onclick = () => { rvFilter = b.dataset.rvFilter; renderQualityGate(); });
  setHTMLIfChanged(document.getElementById("qgOpenReviews"), open.length ? open.map(v => {
    const st = REVIEW[v.state] || [v.state, ""];
    return `<div class="rv-row"><span class="st-dot ${st[1] === "gone" ? "" : st[1]}" aria-hidden="true">${v.state === "approved" ? "✓" : v.state === "escalated" || v.state === "changes_requested" ? "!" : "○"}</span><div class="rv-main"><b title="${esc(v.topic)}">#${v.id} ${esc(v.topic)}</b><span>${esc(String(v.repo || "").split("/").pop())} · ${esc(nameOf(v.author))}${v.reviewer ? " → " + esc(nameOf(v.reviewer)) : ""} · ${esc(st[0])}</span></div>${v.state === "escalated" ? `<button class="btn" data-rv-grant="${v.id}">Povolit kolo</button>` : ""}<button class="btn" data-rv-close="${v.id}" data-rv-state="${esc(v.state)}">Uzavřít</button></div>`;
  }).join("") : `<div class="empty">${latestReviewsAll === null ? "Načítám…" : rvFilter === "all" ? "Žádné otevřené review." : "V této skupině nic není."}</div>`);
  document.querySelectorAll("[data-rv-close]").forEach(b => b.onclick = () => {
    const reason = prompt(`Proč uzavřít review #${b.dataset.rvClose}? (zapíše se do historie)`, b.dataset.rvState === "approved" ? "zamergováno" : "nahrazeno novějším review");
    if (reason && reason.trim()) { b.disabled = true; post("/api/review-close", {id: Number(b.dataset.rvClose), reason}).then(ok => { if (!ok) b.disabled = false; }); }
  });
  document.querySelectorAll("[data-rv-grant]").forEach(b => b.onclick = () => {
    const reason = prompt("Proč povolit další kolo oprav? (zapíše se do historie)");
    if (reason && reason.trim()) { b.disabled = true; post("/api/review-grant", {id: Number(b.dataset.rvGrant), reason}).then(ok => { if (!ok) b.disabled = false; }); }
  });
}
function renderQualityProject() {
  const r = qgSelected === null ? null : (qgRecords || []).find(x => x.run_id === qgSelected);
  const el = document.getElementById("qgProjectDetail");
  document.getElementById("qgProjectTitle").textContent = r ? String(r.repository).split("/").filter(Boolean).pop() : "Detail kontroly";
  document.getElementById("qgLastCheck").textContent = r ? when(r.timestamp) : "";
  if (!r) { setHTMLIfChanged(el, `<div class="empty">${qgRecords === null ? "Výsledky nejsou dostupné." : "Vyber kontrolu ze seznamu."}</div>`); return; }
  const tone = s => s === "PASS" ? "done" : s === "FAIL" ? "fail" : s === "REVIEW" ? "warn" : "";
  const history = (qgRecords || []).filter(x => x.repository === r.repository && x.run_id !== r.run_id).slice(0, 6);
  const canApprove = r.policy_review_required && r.policy_review_status !== "approved" && r.review_status === "approved" && Object.values(r.checks || {}).every(c => !c.required || c.status === "PASS");
  const executionLabels = {not_started: "Kontroly nebyly spuštěny", busy: "Runner je obsazený", unverified: "Průběh kontrol nelze ověřit", interrupted: "Kontroly byly přerušeny", completed: "Kontroly byly dokončeny"};
  setHTMLIfChanged(el, `<div class="pg-progress">Celkový stav ${qgBadge(r.overall_status)}<span class="h-sub">${esc(r.repository)}</span></div>`
    + (r.execution_status ? `<p class="pg-desc">${esc(executionLabels[r.execution_status] || "Stav spuštění není známý")}${r.error ? ": " + esc(r.error) : ""}</p>` : "")
    + stepperHTML(QG_CHECKS.map(([key, label]) => [label, tone(r.checks?.[key]?.status)]))
    + `<dl class="kv qg-kv"><dt>Větev</dt><dd>${esc(r.branch || "neuvedena")}</dd><dt>Commit</dt><dd class="path">${esc(r.commit_sha || "neuveden")}</dd><dt>Agent</dt><dd>${esc(r.agent ? nameOf(r.agent) : "neuveden")}</dd><dt>Review</dt><dd>${esc(r.review_status === "approved" ? "schváleno pro tento commit" : r.review_status === "pending" ? "čeká na nezávislé review" : r.review_status || "není dostupné")}${r.reviewer ? " · " + esc(nameOf(r.reviewer)) : ""}</dd><dt>Změny testů a pravidel</dt><dd>${r.policy_review_required ? r.policy_review_status === "approved" ? "kritické změny schválil člověk" : "kritické změny čekají na tvé schválení" : "schválení není potřeba"}</dd></dl>`
    + `<div class="qg-check-grid">${QG_CHECKS.map(([key, label]) => { const c = r.checks?.[key]; return `<section class="card"><h3>${label}</h3>${qgBadge(c?.status || "NOT_RUN")}<p>${esc(c?.value || c?.detail || "Výsledek není dostupný.")}</p></section>`; }).join("")}</div>`
    + (canApprove ? '<div class="pg-actions"><button class="btn" id="qgApprovePolicy">Schválit kritické změny</button></div>' : "")
    + (qgLogs(r) ? `<div class="detail-section"><h3>Protokoly</h3>${qgLogs(r)}</div>` : "")
    + (history.length ? `<div class="detail-section"><h3>Předchozí běhy</h3><ul>${history.map(h => `<li>${esc(when(h.timestamp))} · ${qgBadge(h.overall_status)} · ${esc((h.commit_sha || "").slice(0, 10))}</li>`).join("")}</ul></div>` : ""));
  const approve = document.getElementById("qgApprovePolicy");
  if (approve) approve.onclick = () => {
    const reason = prompt("Proč schvaluješ změny testů nebo pravidel pro tento commit?");
    if (!reason || reason.trim().length < 10 || !confirm("Schválit kritické změny pro commit " + r.commit_sha + "?")) return;
    approve.disabled = true;
    post("/api/quality-policy-approve", {run_id: r.run_id, commit_sha: r.commit_sha, reason}).then(ok => { if (!ok) approve.disabled = false; });
  };
}

// ── Výzkum ──
const RS_LABEL = {running: "Probíhající výzkum", synthesizing: "Syntéza", done: "Hotovo", partial: "Částečně", failed: "Selhalo", cancelled: "Zrušeno"};
const RS_FIN = ["succeeded", "failed", "dead_letter", "cancelled", "needs_review"];
let rsSel = null, rsFilter = "all", rsTab = "summary";
function loadResearch() {
  fetch("/api/research").then(r => r.ok ? r.json() : Promise.reject(r)).then(d => { dashResearch = d.research || []; renderResearchPage(); })
    .catch(() => { document.getElementById("rsList").textContent = "Výzkum se nepodařilo načíst."; });
}
const rsParts = r => (r.parts || []).filter(p => p.role === "part");
function rsProgress(r) {
  if (r.state === "done") return 100;
  const parts = rsParts(r), fin = parts.filter(p => RS_FIN.includes(p.state)).length;
  return Math.round(100 * (fin + (r.state === "partial" ? 1 : 0)) / (parts.length + 1));
}
function rsSteps(r) {
  const st = r.state, stopped = ["failed", "cancelled"].includes(st);
  return [["Zadání", "done"], ["Části", st === "running" ? "now" : stopped && !r.synth_id ? "fail" : "done"],
    ["Syntéza", st === "synthesizing" ? "now" : ["done", "partial"].includes(st) ? "done" : stopped && r.synth_id ? "fail" : ""],
    ["Hotovo", st === "done" ? "done" : st === "partial" ? "warn" : stopped ? "fail" : ""]];
}
const URL_RE = /https?:[/][/][^ <>()"'`]+/g, FILE_RE = /(?:^|[ (`])((?:[A-Za-z0-9_.-]+[/])*[A-Za-z0-9_.-]+[.](?:md|py|ts|tsx|js|cjs|json|toml|rs|yml|yaml))(?![A-Za-z0-9_])/g;
const flat = t => String(t || "").split(String.fromCharCode(10)).join(" ").split(String.fromCharCode(9)).join(" ");
const trimUrl = u => { while (u && ".,;:!?)]".includes(u.slice(-1))) u = u.slice(0, -1); return u; };
function rsRefs(r) {
  const text = flat([r.question, r.summary].join(" "));
  const urls = [...new Set((text.match(URL_RE) || []).map(trimUrl))];
  const files = [...new Set([...text.matchAll(FILE_RE)].map(m => m[1]))].filter(f => !urls.some(u => u.includes(f)));
  return {urls, files};
}
function renderResearchPage() {
  const runs = dashResearch;
  const kb = dashKnowledge;
  const active = (runs || []).filter(r => ["running", "synthesizing"].includes(r.state));
  const fin = (runs || []).filter(r => ["done", "partial", "failed", "cancelled"].includes(r.state));
  const allRefs = new Set(); (runs || []).forEach(r => { const x = rsRefs(r); x.urls.forEach(u => allRefs.add(u)); });
  const spend = (runs || []).reduce((s, r) => s + (r.spend_tokens || 0), 0);
  metricsRow("rsMetrics", [
    ["Aktivní výzkumy", runs ? active.length : "—", "search", "cyan"],
    ["Probíhající části", runs ? runs.reduce((s, r) => s + rsParts(r).filter(p => !RS_FIN.includes(p.state)).length, 0) : "—", "flask", "orange"],
    ["Dokončené", runs ? runs.filter(r => ["done", "partial"].includes(r.state)).length : "—", "check", "green"],
    ["Citované zdroje", runs ? allRefs.size : "—", "link", "purple", "Odkazy uvedené v zadáních a souhrnech"],
    ["Spotřeba tokenů", runs ? spend.toLocaleString("cs-CZ") : "—", "bars", "purple", "Součet spotřeby posledních výzkumů"],
    ["Úspěšnost", runs && fin.length ? Math.round(100 * runs.filter(r => r.state === "done").length / fin.length) + " %" : "—", "star", "cyan", "Hotové bez výpadku ze všech ukončených"],
  ]);
  if (currentView !== "research") return;
  if (runs === null) return;
  const F = {all: () => true, active: r => ["running", "synthesizing"].includes(r.state), done: r => ["done", "partial"].includes(r.state), failed: r => ["failed", "cancelled"].includes(r.state)};
  const list = runs.filter(F[rsFilter]);
  setHTMLIfChanged(document.getElementById("rsChips"), chipsHTML([["all", `Vše (${runs.length})`], ["active", `Probíhající (${runs.filter(F.active).length})`], ["done", `Dokončené (${runs.filter(F.done).length})`], ["failed", `Neúspěšné (${runs.filter(F.failed).length})`]], rsFilter, "data-rs-filter"));
  document.querySelectorAll("[data-rs-filter]").forEach(b => b.onclick = () => { rsFilter = b.dataset.rsFilter; renderResearchPage(); });
  if (rsSel === null || !runs.some(r => r.id === rsSel)) rsSel = (active[0] || runs[0] || {}).id ?? null;
  setHTMLIfChanged(document.getElementById("rsList"), list.length ? list.map(r => {
    const p = rsProgress(r), who = [...new Set(rsParts(r).map(x => x.recipient).filter(Boolean))];
    return `<button type="button" class="pg-item${r.id === rsSel ? " on" : ""}" data-rs="${r.id}"><span class="pg-ic">${icon(["running", "synthesizing"].includes(r.state) ? "search" : r.state === "done" ? "check" : "cube")}</span><span class="pg-item-body"><span class="pg-item-title">${esc(r.question)}</span><span class="pg-item-sub">#${r.id} · ${esc(RS_LABEL[r.state] || r.state)} · ${esc(when(r.created_at))}</span><span class="pg-item-foot">${(who.length ? who : [r.requester]).map(whoBadge).join("")}<span class="bar"><i style="width:${p}%"></i></span><b>${p} %</b></span></span></button>`;
  }).join("") : `<div class="empty">${runs.length ? "Pro tento filtr nic není." : "Zatím žádný výzkum. Zadej ho tlačítkem Nový výzkum."}</div>`);
  document.querySelectorAll("[data-rs]").forEach(b => b.onclick = () => { rsSel = Number(b.dataset.rs); renderResearchPage(); });
  const r = runs.find(x => x.id === rsSel), det = document.getElementById("rsDetail");
  if (!r) { setHTMLIfChanged(det, "<div class='empty'>Vyber výzkum ze seznamu.</div>"); setHTMLIfChanged(document.getElementById("rsParts"), ""); setHTMLIfChanged(document.getElementById("rsLinks"), ""); return; }
  const p = rsProgress(r), parts = rsParts(r), done = parts.filter(x => x.state === "succeeded").length, refs = rsRefs(r);
  const tone = r.state === "done" ? "ok" : ["failed", "cancelled"].includes(r.state) ? "bad" : r.state === "partial" ? "muted" : "";
  const concl = r.state === "done" ? ["", "Výzkum je hotový", "Zjištění jsou neověřená, dokud je nepotvrdí jiný agent nebo člověk v paměti."]
    : r.state === "partial" ? ["warn", "Souhrn je neúplný", "Část podtémat nedoběhla včas" + (r.reason ? ": " + r.reason : ".")]
    : ["failed", "cancelled"].includes(r.state) ? ["warn", RS_LABEL[r.state], r.reason || "Důvod není zaznamenaný."]
    : ["", "Výzkum běží", `Hotovo ${done} z ${parts.length} částí${r.state === "synthesizing" ? ", běží syntéza" : ""}. Termín ${when(r.deadline_at)}.`];
  const tabs = [["summary", "Hlavní zjištění"], ["question", "Zadání"], ["details", "Detaily"]];
  const body = rsTab === "summary" ? (r.summary ? mdLite(r.summary) : `<p class="note">${["running", "synthesizing"].includes(r.state) ? "Souhrn vznikne po syntéze." : "Souhrn není k dispozici."}</p>`)
    : rsTab === "question" ? `<p style="white-space:pre-wrap">${esc(r.question)}</p>`
    : `<dl class="kv"><dt>Zadal</dt><dd>${esc(nameOf(r.requester))}</dd><dt>Druh</dt><dd>${esc(r.kind)}</dd>${r.model ? `<dt>Model</dt><dd>${esc(r.model)}</dd>` : ""}${r.repo ? `<dt>Repozitář</dt><dd class="path">${esc(r.repo)}${r.repo_commit ? " @ " + esc(r.repo_commit.slice(0, 10)) : ""}</dd>` : ""}<dt>Tokeny</dt><dd>${esc((r.spend_tokens || 0).toLocaleString("cs-CZ"))} z ${esc((r.budget_tokens || 0).toLocaleString("cs-CZ"))}</dd><dt>Zadáno</dt><dd>${esc(when(r.created_at))}</dd><dt>Termín</dt><dd>${esc(when(r.deadline_at))}</dd>${r.finished_at ? `<dt>Dokončeno</dt><dd>${esc(when(r.finished_at))}</dd>` : ""}${r.reason ? `<dt>Poznámka</dt><dd>${esc(r.reason)}</dd>` : ""}</dl>`;
  setHTMLIfChanged(det, `<div class="pg-head">${icon("search")}<h2 title="${esc(r.question)}">${esc(r.question)}</h2><span class="pill ${tone}">${esc(RS_LABEL[r.state] || r.state)}</span></div>`
    + `<p class="pg-desc">#${r.id} · ${esc(r.kind)} · zadal ${esc(nameOf(r.requester))}</p>`
    + `<div class="pg-progress">Celkový postup<span class="bar" role="progressbar" aria-valuenow="${p}" aria-valuemin="0" aria-valuemax="100" aria-label="Postup výzkumu"><i style="width:${p}%"></i></span><b>${p} %</b></div>`
    + stepperHTML(rsSteps(r))
    + `<div class="tabs" role="tablist">${tabs.map(([id, l]) => `<button type="button" role="tab" data-rs-tab="${id}" aria-selected="${id === rsTab}">${l}</button>`).join("")}</div>`
    + `<div class="tab-body" role="tabpanel">${body}</div>`
    + `<div class="conclusion ${concl[0]}">${icon("flag")}<div><b>${esc(concl[1])}</b>${esc(concl[2])}</div></div>`
    + (["running", "synthesizing"].includes(r.state) ? '<div class="pg-actions"><button class="btn" data-rs-cancel>✕ Zrušit výzkum</button></div>' : ""));
  det.querySelectorAll("[data-rs-tab]").forEach(b => b.onclick = () => { rsTab = b.dataset.rsTab; renderResearchPage(); });
  const cancel = det.querySelector("[data-rs-cancel]");
  if (cancel) cancel.onclick = () => { if (!confirm("Zrušit výzkum včetně běžících částí a syntézy?")) return; cancel.disabled = true; post("/api/research-cancel", {id: r.id}).then(() => loadResearch()); };
  document.getElementById("rsPartsCount").textContent = `(${parts.length})`;
  const PART = s => s === "succeeded" ? ["ok", "✓"] : ["failed", "dead_letter", "cancelled"].includes(s) ? ["bad", "✕"] : s === "needs_review" ? ["warn", "!"] : ["started", "leased"].includes(s) ? ["work", "▸"] : ["", "○"];
  setHTMLIfChanged(document.getElementById("rsParts"), parts.length ? parts.map(x => { const [t, sym] = PART(x.state); return `<li class="src-row"><span class="st-dot ${t}" aria-hidden="true">${sym}</span><span class="task-text" title="${esc(x.topic)}">${esc(x.topic)}</span>${x.recipient ? whoBadge(x.recipient) : ""}<span class="sr-only">${esc(x.state || "neznámý stav")}</span></li>`; }).join("") : "<li class='empty'>Výzkum nemá samostatné části.</li>");
  setHTMLIfChanged(document.getElementById("rsLinks"), refs.urls.length || refs.files.length
    ? refs.urls.map(u => `<a class="link-row" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${icon("link")}<span>${esc(u.replace(/^https?:[/][/]/, ""))}</span>↗</a>`).join("") + refs.files.slice(0, 12).map(f => `<div class="link-row">${icon("doc")}<span title="${esc(f)}">${esc(f)}</span></div>`).join("")
    : "<div class='empty'>Souhrn neuvádí žádné odkazy ani soubory.</div>");
  void kb;
}
document.getElementById("rsNew").onclick = () => {
  const q = prompt("Na co se má výzkum zaměřit? Úkol dostane Ada a spustí ho nástrojem research.");
  if (!q || !q.trim()) return;
  post("/api/send", {kind: "task", to: "claude-code", text: "Spusť prosím výzkum nástrojem research a pak mi shrň výsledek: " + q.trim()})
    .then(ok => { if (ok) alert("Úkol jsem poslal Adě. Výzkum se tady objeví, jakmile ho spustí."); });
};

// ── Paměť ──
const KB_LABEL = {verified: "ověřeno", unverified: "neověřeno — jen tip", rejected: "zamítnuto", stale: "zastaralé"};
let kbTimer = null, kbStatusSel = "", kbProjectSel = null, kbSel = null, kbEntries = [];
function loadKnowledge() {
  const q = document.getElementById("kbQuery").value.trim();
  const params = new URLSearchParams({...(q ? {q} : {}), ...(kbStatusSel ? {status: kbStatusSel} : {}), ...(kbProjectSel !== null ? {project: kbProjectSel} : {})});
  fetch("/api/knowledge?" + params).then(r => r.ok ? r.json() : Promise.reject(r))
    .then(d => { kbEntries = d.entries || []; renderKnowledgeList(); })
    .catch(() => { document.getElementById("kbList").textContent = "Paměť se nepodařilo načíst."; });
  fetch("/api/knowledge").then(r => r.ok ? r.json() : null).then(d => { if (d) { dashKnowledge = d.entries || []; renderMemoryPage(); } }).catch(() => {});
}
function renderMemoryPage() {
  const all = dashKnowledge, n = s => (all || []).filter(e => e.status === s).length;
  const projects = new Map(); (all || []).forEach(e => projects.set(e.project || "", (projects.get(e.project || "") || 0) + 1));
  metricsRow("kbMetrics", [
    ["Uložené poznatky", all ? (all.length >= 100 ? "100+" : all.length) : "—", "db", "cyan"],
    ["Ověřené", all ? n("verified") : "—", "check", "green", "Za fakt platí jen ověřené záznamy"],
    ["Neověřené tipy", all ? n("unverified") : "—", "flask", "orange"],
    ["Zamítnuté", all ? n("rejected") + n("stale") : "—", "alert", "purple"],
    ["Projekty", all ? [...projects.keys()].filter(Boolean).length : "—", "folder", "purple"],
    ["Nové za 7 dní", all ? all.filter(e => Date.now() / 1000 - e.created_at < 7 * 86400).length : "—", "bars", "cyan"],
  ]);
  if (currentView !== "memory") return;
  const projItems = [[null, "Vše", all ? all.length : 0], ...[...projects.entries()].sort((a, b) => b[1] - a[1]).map(([p, c]) => [p, p || "Obecné", c])];
  setHTMLIfChanged(document.getElementById("kbProjects"), all === null ? "Načítám…" : projItems.map(([p, label, c]) =>
    `<button type="button" class="pg-item${p === kbProjectSel ? " on" : ""}" data-kb-proj="${p === null ? "__all__" : esc(p)}"><span class="pg-ic">${icon("folder")}</span><span class="pg-item-body"><span class="pg-item-title">${esc(label)}</span><span class="pg-item-sub">${p === null ? "Celá sdílená paměť" : `${(all || []).filter(e => (e.project || "") === p && e.status === "verified").length} ověřených`}</span></span><span class="count-pill">${c} ${c === 1 ? "záznam" : c >= 2 && c <= 4 ? "záznamy" : "záznamů"}</span></button>`).join("")
    + (all && !all.length ? "<p class='note'>Paměť je zatím prázdná. Agenti do ní ukládají ověřená řešení chyb; můžeš přidat i vlastní.</p>" : ""));
  document.querySelectorAll("[data-kb-proj]").forEach(b => b.onclick = () => { kbProjectSel = b.dataset.kbProj === "__all__" ? null : b.dataset.kbProj; kbSel = null; loadKnowledge(); });
  setHTMLIfChanged(document.getElementById("kbStatus"), chipsHTML([["", "Vše"], ["verified", "Ověřené"], ["unverified", "Neověřené"], ["rejected", "Zamítnuté"], ["stale", "Zastaralé"]], kbStatusSel, "data-kb-status"));
  document.querySelectorAll("[data-kb-status]").forEach(b => b.onclick = () => { kbStatusSel = b.dataset.kbStatus; kbSel = null; loadKnowledge(); });
  document.getElementById("kbProjectList").innerHTML = [...projects.keys()].filter(Boolean).map(p => `<option value="${esc(p)}">`).join("");
  const act = [...(all || [])].sort((a, b) => b.updated_at - a.updated_at).slice(0, 8);
  setHTMLIfChanged(document.getElementById("kbActivity"), act.length ? act.map(e => e.verified_by && e.updated_at > e.created_at
    ? actItem(e.verified_by, e.updated_at, `${KB_LABEL[e.status] || e.status}: ${e.symptom}`)
    : actItem(e.author, e.created_at, `Uložil(a) poznatek: ${e.symptom}`)).join("") : "<li class='empty'>V paměti zatím nic není.</li>");
  renderKnowledgeList();
}
function renderKnowledgeList() {
  const el = document.getElementById("kbList"), q = document.getElementById("kbQuery").value.trim();
  document.getElementById("kbCount").textContent = `(${kbEntries.length})`;
  document.getElementById("kbScope").textContent = [kbProjectSel !== null ? (kbProjectSel || "Obecné") : "", q ? `„${q}“` : ""].filter(Boolean).join(" · ");
  if (kbSel !== null && !kbEntries.some(e => e.id === kbSel)) kbSel = null;
  if (kbSel === null && kbEntries.length) kbSel = kbEntries[0].id;
  setHTMLIfChanged(el, kbEntries.length ? kbEntries.map(e => `<button type="button" class="kb-row${e.id === kbSel ? " on" : ""}" data-kb-id="${e.id}"><span class="st-dot ${e.status === "verified" ? "ok" : e.status === "rejected" ? "bad" : e.status === "stale" ? "" : "warn"}" aria-hidden="true">${e.status === "verified" ? "✓" : e.status === "rejected" ? "✕" : e.status === "stale" ? "–" : "?"}</span><span class="task-text" title="${esc(e.symptom)}">${esc(e.symptom)}</span>${e.project ? `<span class="kb-proj">${esc(e.project)}</span>` : ""}<time>${esc(when(e.updated_at))}</time><span class="sr-only">${esc(KB_LABEL[e.status] || e.status)}</span></button>`).join("")
    : `<p class="note">${q || kbStatusSel || kbProjectSel !== null ? "Nic neodpovídá hledání." : "Žádné záznamy."}</p>`);
  el.querySelectorAll("[data-kb-id]").forEach(b => b.onclick = () => { kbSel = Number(b.dataset.kbId); renderKnowledgeList(); });
  const e = kbEntries.find(x => x.id === kbSel), det = document.getElementById("kbDetail");
  document.getElementById("kbDetailTitle").textContent = e ? `Záznam #${e.id}` : "Detail záznamu";
  if (!e) { setHTMLIfChanged(det, "<div class='empty'>Vyber záznam ze seznamu.</div>"); return; }
  setHTMLIfChanged(det, `<div class="kb-detail"><div class="tags"><span class="kb-state ${esc(e.status)}">${esc(KB_LABEL[e.status] || e.status)}</span>${e.project ? `<span class="kb-proj">${esc(e.project)}</span>` : ""}</div>
    <dl><dt>Příznak</dt><dd>${esc(e.symptom)}</dd>${e.cause ? `<dt>Příčina</dt><dd>${esc(e.cause)}</dd>` : ""}${e.solution ? `<dt>Řešení</dt><dd>${esc(e.solution)}</dd>` : ""}${e.evidence ? `<dt>Důkaz</dt><dd>${esc(e.evidence)}</dd>` : ""}${e.verdict_evidence ? `<dt>Důkaz posudku</dt><dd>${esc(e.verdict_evidence)}${e.verdict_note ? " — " + esc(e.verdict_note) : ""}</dd>` : ""}<dt>Autor</dt><dd>${esc(nameOf(e.author))} · ${esc(when(e.created_at))}${e.verified_by ? ` · posoudil ${esc(nameOf(e.verified_by))}` : ""}</dd></dl>
    <div class="pg-actions" style="padding:0"><button class="btn" data-kb="verified">✓ Ověřit</button><button class="btn" data-kb="rejected">✕ Nefunguje</button><button class="btn" data-kb="stale">◷ Zastaralé</button></div></div>`);
  det.querySelectorAll("[data-kb]").forEach(b => b.onclick = () => {
    const evidence = prompt("Důkaz (commit, test, odkaz) — povinné:");
    if (!evidence || !evidence.trim()) return;
    b.disabled = true;
    post("/api/knowledge-verdict", {id: e.id, verdict: b.dataset.kb, evidence}).then(ok => { b.disabled = false; if (ok) loadKnowledge(); });
  });
}
document.getElementById("kbQuery").addEventListener("input", () => { clearTimeout(kbTimer); kbTimer = setTimeout(loadKnowledge, 250); });
document.getElementById("kbNew").onclick = () => document.getElementById("kbDialog").showModal();
document.getElementById("kbAdd").onclick = () => {
  const val = id => document.getElementById(id).value;
  if (!val("kbSymptom").trim()) { document.getElementById("kbSymptom").focus(); return; }
  const btn = document.getElementById("kbAdd"); btn.disabled = true;
  post("/api/knowledge-add", {symptom: val("kbSymptom"), cause: val("kbCause"), solution: val("kbSolution"), project: val("kbProject"), evidence: val("kbEvidence")})
    .then(ok => { btn.disabled = false; if (ok) { ["kbSymptom", "kbCause", "kbSolution", "kbEvidence"].forEach(i => document.getElementById(i).value = ""); document.getElementById("kbDialog").close(); loadKnowledge(); } });
};

// ── Nastavení ──
const MODES = [
  {id: "basic", name: "Základní", items: ["Bridge sám nepřebírá žádné úkoly", "Pracuje jen session, kterou spustíš", "Sdílená paměť vypnutá pro agenty", "Minimální spotřeba tokenů"]},
  {id: "intelligent", name: "Inteligentní", items: ["Bridge přebírá read-only úkoly", "Review mezi agenty (max 3 kola)", "Sdílená paměť zapnutá"]},
  {id: "autonomous", name: "Autonomní", items: ["Opravy v izolovaném worktree", "Povinné review, rozpočtové stropy", "Eskalace na tebe po 3 pokusech"]},
];
const MODE_NAME = {basic: "Základní", intelligent: "Inteligentní", autonomous: "Autonomní"};
let lastMode = null, latestClaims = [];
function renderMode(m) {
  if (!m) return;
  lastMode = m;
  document.getElementById("modeText").textContent = "Režim: " + (MODE_NAME[m.mode] || m.mode);
  if (currentView === "settings") renderSettings();
}
function loadMode() { fetch("/api/snapshot?since=" + Number.MAX_SAFE_INTEGER).then(r => r.json()).then(s => { renderMode(s.mode); renderSettings(); }).catch(() => {}); }
// ── Nastavení: seznam kategorií vlevo, jedna kategorie vpravo; potvrzení přímo v kartě (žádné prompt()) ──
const ST = {
  agents: ["users", "Agenti", "Kdo pro tebe pracuje, v jakém je stavu a jestli ho něco brzdí."],
  mode: ["sliders", "Provozní režim", "Kolik samostatnosti mají Ada a Nox. Přepnout smí jen člověk tady na webu."],
  stop: ["stop", "Nouzové zastavení", "Jedním tlačítkem zastavíš všechnu práci na pozadí. Nic se nezahodí."],
  access: ["lock", "Přihlášení a zabezpečení", "Jak se do webu dostaneš ty a proč se nedostane nikdo jiný."],
  about: ["cube", "O systému", "Co přesně běží a kde."],
};
const MODE_INFO = {
  basic: ["Pracuje jen ten, koho sám spustíš.", ["Mosty na pozadí nepřebírají úkoly", "Sdílená paměť je pro agenty vypnutá", "Nejmenší spotřeba tokenů"]],
  intelligent: ["Agenti si pomáhají, ale nic nemění sami.", ["Mosty přebírají úkoly jen ke čtení", "Review mezi agenty, nejvýš 3 kola", "Sdílená paměť zapnutá"]],
  autonomous: ["Agenti pracují samostatně, ty jen schvaluješ.", ["Opravy v izolované pracovní kopii", "Povinné review a stropy spotřeby", "Po 3 neúspěšných kolech rozhoduješ ty"]],
};
let stTab = localStorage.getItem("agentLeaseSettingsTab") || "agents", stConfirm = null, stAbout = null, stLogins = null;
const stRow = (label, hint, right) => `<div class="st-row"><div class="st-l"><b>${label}</b>${hint ? `<span>${hint}</span>` : ""}</div><div class="st-r">${right}</div></div>`;
const stPill = (text, tone) => `<span class="st-pill ${tone}">${esc(text)}</span>`;
function stBusy() { const a = document.activeElement, pane = document.getElementById("stPane"); return a && pane.contains(a) && /TEXTAREA|INPUT/.test(a.tagName); }
function renderSettings() {
  if (currentView !== "settings" || stBusy()) return;
  document.querySelectorAll("[data-st]").forEach(b => { const on = b.dataset.st === stTab; b.setAttribute("aria-selected", on); b.tabIndex = on ? 0 : -1; });
  const [ic, title, desc] = ST[stTab];
  const head = `<div class="st-head"><i data-ic="${ic}"></i><div><h3 id="stPaneTitle" tabindex="-1">${title}</h3><p>${desc}</p></div></div>`;
  const pane = document.getElementById("stPane");
  setHTMLIfChanged(pane, head + ({agents: stAgents, mode: stModes, stop: stStop, access: stAccess, about: stAboutPane}[stTab])());
  paintIcons(pane);
  bindSettings();
}
function stAgents() {
  const peer = a => latestPeers.find(p => p.agent === a);
  const stale = connectionMode !== "live" || !lastRoomAt || Date.now() - lastRoomAt > STALE_MS;
  const bridgeOn = a => { const p = peer(a); return !stale && !!(p && p.active && p.seen_seconds_ago != null && p.seen_seconds_ago < ONLINE_S); };
  const TONE = {working: "work", idle: "ok", limited: "bad", error: "warn", offline: "muted", unknown: "warn"};
  const INFO = {ada: ["Ada", "Anthropic · Claude", "Analýza, výzkum a návrhy", "claude-code"], nox: ["Nox", "OpenAI · GPT", "Implementace, testy a ověření", "codex"]};
  return `<div class="st-group"><h4>Agenti</h4>${["ada", "nox"].map(k => { const [name, vendor, role, id] = INFO[k], st = agentStatus(k), br = bridgeOn(id + "-bridge");
    return `<div class="st-agent" data-agent="${k}"><img src="/assets/${k}.webp" alt=""><div><h5>${name}<small>${vendor}</small></h5><p>${role}</p>${st.detail ? `<p title="${esc(st.detail)}">${esc(st.detail.slice(0, 140))}</p>` : ""}
      <div class="st-facts">${stPill(st.label, TONE[st.state] || "muted")}${stPill(stale ? "most: stav neznámý" : br ? "most připraven" : "most nedostupný", stale ? "warn" : br ? "ok" : "muted")}</div></div>
      <button class="st-btn" data-st-msg="${id}">Napsat ${name === "Ada" ? "Adě" : "Noxovi"}</button></div>`; }).join("")}</div>
    <div class="st-group"><h4>Co to znamená</h4>
      ${stRow("Most na pozadí", "Převezme úkol, i když zrovna nesedíš u počítače. Bez mostu pracuje agent jen v otevřené relaci.", "")}
      ${stRow("Limit poskytovatele", "Když agent vyčerpá limit, úkoly počkají nebo je převezme ten druhý (failover).", "")}</div>`;
}
function stModes() {
  const m = lastMode;
  if (!m) return '<p class="st-note">Načítám…</p>';
  return `<div class="st-modes" role="radiogroup" aria-label="Provozní režim">${MODES.map(x => { const on = m.mode === x.id, lock = m.locked && m.locked[x.id], [lead, items] = MODE_INFO[x.id];
    const confirm = stConfirm && stConfirm.kind === "mode" && stConfirm.mode === x.id
      ? `<div class="st-confirm" data-stop-prop><label for="stReason">Proč přepínáš? (zapíše se do historie, nepovinné)</label><textarea id="stReason" maxlength="500">${esc(stConfirm.reason || "")}</textarea><div class="st-btns"><button class="st-btn" data-st-cancel>Zrušit</button><button class="st-btn primary" data-st-do-mode="${x.id}">Přepnout na ${x.name}</button></div></div>` : "";
    return `<div class="st-mode" role="radio" tabindex="0" aria-checked="${on}" aria-disabled="${!!lock}" data-st-mode="${x.id}"><div class="st-mode-h"><span class="mode-dot ${x.id}" aria-hidden="true"></span><b>${x.name}</b>${on ? stPill("Aktivní", "work") : lock ? stPill("Zamčeno", "muted") : ""}</div><p>${lead}</p><ul>${items.map(i => `<li>${esc(i)}</li>`).join("")}</ul>${lock ? `<p class="st-note">🔒 ${esc(lock)}</p>` : ""}${confirm}</div>`; }).join("")}</div>
    <p class="st-note">${m.by ? `Naposledy přepnul ${esc(nameOf(m.by))} · ${esc(when(m.at))}${m.reason ? " · „" + esc(m.reason) + "“" : ""}` : "Výchozí režim, zatím nikdo nepřepínal."}</p>`;
}
function stStop() {
  const p = lastPaused;
  if (p === undefined) return '<p class="st-note">Stav není známý.</p>';
  // Potvrzení platí jen pro stav, ve kterém bylo otevřené (review #61): změna přes SSE ho zruší.
  if (stConfirm && stConfirm.kind === "stop" && stConfirm.action !== (p ? "resume" : "pause")) stConfirm = null;
  const confirm = stConfirm && stConfirm.kind === "stop"
    ? `<div class="st-confirm ${p ? "" : "danger"}"><label for="stReason">${p ? "Proč pouštíš agenty znovu? (nepovinné)" : "Proč zastavuješ? (zapíše se do historie)"}</label><textarea id="stReason" maxlength="500">${esc(stConfirm.reason ?? (p ? "" : "nouzové zastavení"))}</textarea><div class="st-btns"><button class="st-btn" data-st-cancel>Zrušit</button><button class="st-btn ${p ? "go" : "danger"}" data-st-do-stop>${p ? "Pustit agenty" : "Opravdu zastavit"}</button></div></div>` : "";
  return `<div class="st-hero ${p ? "bad" : "ok"}"><div><div class="big">${p ? "⏸ Agenti jsou zastavení" : "● Provoz povolen"}</div><p>${p ? `Zastavil ${esc(nameOf(p.by))}${p.at ? " · " + esc(when(p.at)) : ""}${p.reason ? " · „" + esc(p.reason) + "“" : ""}` : "Agenti pracují samostatně podle zvoleného režimu."}</p></div>${confirm ? "" : `<button class="st-btn ${p ? "go" : "danger"}" data-st-stop>${p ? "Pustit agenty znovu" : "Zastavit všechny agenty"}</button>`}</div>${confirm}
    <div class="st-group"><h4>Co se stane po zastavení</h4>
      ${stRow("Práce na pozadí", "Mosty okamžitě přestanou přebírat a spouštět úkoly.", stPill("zastaví se", "warn"))}
      ${stRow("Rozpracované úkoly", "Nic se nezahodí. Po obnovení pokračují nebo čekají na tvou kontrolu.", stPill("zůstanou", "ok"))}
      ${stRow("Kdo smí obnovit", "Jen člověk tady na webu. Agent se sám znovu nespustí.", stPill("jen ty", "work"))}</div>`;
}
function stAccess() {
  const keys = stKeys;
  const keyRows = keys === null ? stRow("Načítám…", "", "") : keys.length
    ? keys.map(k => stRow(esc(k.name), `přidáno ${esc(when(k.created_at))}${k.last_used ? " · naposledy použito " + esc(when(k.last_used)) : " · zatím nepoužito"}`, `<button class="st-btn" data-pk-remove="${esc(k.id)}">Odebrat</button>`)).join("")
    : stRow("Zatím žádný klíč", "Přidej Windows Hello a příště se přihlásíš jen PINem.", "");
  const logins = stLogins === null ? stRow("Načítám…", "", "") : stLogins.length
    ? stLogins.map(e => stRow(e.action === "web-login-passkey" ? "Přihlášení Windows Hello" : e.action === "web-logout" ? "Odhlášení" : "Neúspěšný pokus", esc(new Date(e.at * 1000).toLocaleString("cs-CZ")), stPill(e.action === "web-login-passkey-failed" ? "odmítnuto" : "v pořádku", e.action === "web-login-passkey-failed" ? "bad" : "ok"))).join("")
    : stRow("Zatím žádné přihlášení přes Windows Hello", "", "");
  return `<div class="st-group"><h4>Windows Hello</h4>${keyRows}<div class="st-row"><div class="st-l"><b>Přidat zařízení</b><span>Windows se zeptá na PIN. Funguje jen na adrese http://localhost:8765.</span></div><div class="st-r"><button class="st-btn primary" id="pkAdd">Přidat Windows Hello (PIN)</button></div></div><p class="st-note" id="pkStatus" role="status" style="padding:0 20px 14px">${esc(stPkStatus)}</p></div>
    <div class="st-group"><h4>Toto přihlášení</h4>${stRow("Platnost", "Po vypršení se přihlásíš znovu PINem.", `${SESSION_DAYS} dní`)}${stRow("Odhlásit se", "Ukončí přihlášení v tomto prohlížeči.", '<button class="st-btn" id="stLogout">Odhlásit se</button>')}</div>
    <div class="st-group"><h4>Poslední přihlášení</h4>${logins}</div>
    <div class="st-group"><h4>Záložní přihlášení</h4>${stRow("Ikona Agent Lease na ploše", "Otevře web rovnou přihlášený jednorázovým kódem.", stPill("vždy funguje", "ok"))}${stRow("Terminál", "<code>agent-lease web-login</code>", "")}</div>
    <div class="st-group"><h4>Ochrana</h4>
      ${stRow("Jen tento počítač", "Web poslouchá jen na localhost, zvenku není dostupný.", stPill("zapnuto", "ok"))}
      ${stRow("Přihlášení vázané na prohlížeč", "Odcizená cookie sama nestačí, je potřeba i klíč uložený jen pro tuto adresu.", stPill("zapnuto", "ok"))}
      ${stRow("Žádné hlavní heslo v adrese", "Ikona i web-login posílají jen jednorázový kód platný 2 minuty.", stPill("zapnuto", "ok"))}
      ${stRow("Vše se zapisuje", "Každé přihlášení i řídicí akce je v historii (Provoz → Historie akcí).", stPill("zapnuto", "ok"))}</div>`;
}
function stAboutPane() {
  const a = stAbout;
  if (!a) return '<p class="st-note">Načítám…</p>';
  return `<div class="st-group"><h4>Web</h4>${stRow("Adresa", "", `<code>${esc(a.address)}</code>`)}${stRow("Spojení", "Data se obnovují okamžitě po každé změně.", stPill(connectionMode === "live" ? "Živě" : "přerušeno", connectionMode === "live" ? "ok" : "bad"))}${stRow("Přihlášený člověk", "", esc(me || "—"))}</div>
    <div class="st-group"><h4>Verze</h4>${stRow("Agent Lease", "", esc(a.version))}${stRow("Nasazený runtime", "Složka v ~/.local/share/agent-lease", `<code>${esc(a.runtime)}</code>`)}${stRow("Python", "", esc(a.python))}</div>
    <div class="st-group"><h4>Databáze</h4>${stRow("Soubor", "", `<code>${esc(a.db)}</code>`)}${stRow("Režim deníku", "Musí být delete; jiný režim by sdílenou místnost rozbil.", stPill(a.journal, a.journal === "delete" ? "ok" : "bad"))}${stRow("Zpráv v místnosti", "", esc(a.messages.toLocaleString("cs-CZ")))}</div>`;
}
let stKeys = null, stPkStatus = "";
const SESSION_DAYS = 7;
function loadSettingsData() {
  fetch("/api/passkey").then(r => r.ok ? r.json() : null).then(d => { if (d) { stKeys = d.keys; renderSettings(); } }).catch(() => {});
  fetch("/api/about").then(r => r.ok ? r.json() : null).then(d => { if (d) { stAbout = d; renderSettings(); } }).catch(() => {});
  Promise.all(["web-login-passkey", "web-login-passkey-failed", "web-logout"].map(a => fetch("/api/history?" + new URLSearchParams({action: a, limit: "5"})).then(r => r.ok ? r.json() : {events: []})))
    .then(rs => { stLogins = rs.flatMap(r => r.events).sort((x, y) => y.at - x.at).slice(0, 6); renderSettings(); }).catch(() => {});
}
function loadPasskeys() { loadSettingsData(); }
function bindSettings() {
  const pane = document.getElementById("stPane");
  const ta = document.getElementById("stReason");
  if (ta && stConfirm) ta.oninput = () => { stConfirm.reason = ta.value; };
  pane.querySelectorAll("[data-st-msg]").forEach(b => b.onclick = () => { showView("room", {focus: false}); setKind("chat"); toEl.value = b.dataset.stMsg; syncHint(); textEl.focus(); });
  pane.querySelectorAll("[data-st-mode]").forEach(c => {
    const open = () => { if (c.getAttribute("aria-checked") === "true" || c.getAttribute("aria-disabled") === "true") return; stConfirm = {kind: "mode", mode: c.dataset.stMode}; renderSettings(); document.getElementById("stReason")?.focus(); };
    c.onclick = e => { if (!e.target.closest(".st-confirm")) open(); };
    c.onkeydown = e => { if ((e.key === "Enter" || e.key === " ") && e.target === c) { e.preventDefault(); open(); } };
  });
  pane.querySelectorAll("[data-st-cancel]").forEach(b => b.onclick = e => { e.stopPropagation(); stConfirm = null; renderSettings(); });
  const doMode = pane.querySelector("[data-st-do-mode]");
  if (doMode) doMode.onclick = e => { e.stopPropagation(); doMode.disabled = true;
    fetch("/api/mode", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({mode: doMode.dataset.stDoMode, reason: document.getElementById("stReason").value})})
      .then(r => r.ok ? r.json().then(m => { stConfirm = null; document.activeElement.blur(); renderMode(m); }) : r.text().then(t => { doMode.disabled = false; alert(t); }))
      .catch(() => { doMode.disabled = false; alert("Spojení se nezdařilo. Zkontroluj režim před opakováním."); }); };
  const stop = pane.querySelector("[data-st-stop]");
  if (stop) stop.onclick = () => { stConfirm = {kind: "stop", action: lastPaused ? "resume" : "pause"}; renderSettings(); document.getElementById("stReason")?.focus(); };
  const doStop = pane.querySelector("[data-st-do-stop]");
  if (doStop) doStop.onclick = () => { doStop.disabled = true; const reason = document.getElementById("stReason").value, action = stConfirm.action;
    stConfirm = null; document.activeElement.blur(); pauseCall(action, reason); setTimeout(renderSettings, 400); };
  pane.querySelectorAll("[data-pk-remove]").forEach(b => b.onclick = () => { if (confirm("Odebrat tento klíč Windows Hello?")) post("/api/passkey/remove", {id: b.dataset.pkRemove}).then(loadSettingsData); });
  const add = document.getElementById("pkAdd");
  if (add) add.onclick = addPasskey;
  const out = document.getElementById("stLogout");
  if (out) out.onclick = () => { if (!confirm("Odhlásit se z webu v tomto prohlížeči?")) return;
    fetch("/api/logout", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"}).finally(() => { localStorage.removeItem(KEY_STORE); location.reload(); }); };
}
function selectSettingsTab(tab, focusTitle) {
  stTab = tab; stConfirm = null; localStorage.setItem("agentLeaseSettingsTab", stTab); renderSettings();
  if (focusTitle) document.getElementById("stPaneTitle")?.focus({preventScroll: true});
}
document.querySelectorAll("[data-st]").forEach(b => {
  b.onclick = () => selectSettingsTab(b.dataset.st, true);
  b.onkeydown = e => { const tabs = [...document.querySelectorAll("[data-st]")], i = tabs.indexOf(b);
    const go = j => { e.preventDefault(); const t = tabs[(j + tabs.length) % tabs.length]; selectSettingsTab(t.dataset.st, false); t.focus(); };   // fokus zůstává na záložce (review #61)
    if (e.key === "ArrowDown" || e.key === "ArrowRight") go(i + 1);
    if (e.key === "ArrowUp" || e.key === "ArrowLeft") go(i - 1); };
});
// Stránky se obnovují i bez nové zprávy: stáří dat (Nastavení) a průběh výzkumu/paměti (review #54).
setInterval(() => { if (!document.hidden) renderPages(); }, 5000);
// Server pošle „room“ hned po každém zápisu do DB; při změně verze se obnoví i výzkum, paměť a provoz.
let lastDataVersion = null, versionReloadAt = 0, refreshPending = false, refreshTimer = null;
function refreshCurrentView() {
  refreshPending = false; versionReloadAt = Date.now();
  if (currentView === "research") loadResearch(); else if (currentView === "memory") loadKnowledge();
  else if (currentView === "ops") loadOps("live"); else if (currentView === "overview") loadDashData();
}
function scheduleRefresh() {
  // Nejvýš jednou za 1,5 s, ale poslední změna se nikdy nezahodí (dobíhající obnova, review #60).
  if (!refreshPending || document.hidden || refreshTimer) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; if (document.hidden) return; refreshCurrentView(); },
    Math.max(0, 1500 - (Date.now() - versionReloadAt)));
}
function onDataVersion(v) {
  if (v === undefined || v === lastDataVersion) return;
  const first = lastDataVersion === null;
  lastDataVersion = v;
  if (first) return;
  refreshPending = true;
  scheduleRefresh();
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) scheduleRefresh(); });
setInterval(() => { if (document.hidden) return; if (currentView === "research") loadResearch(); else if (currentView === "memory") loadKnowledge(); }, 30000);
// ── Provoz: spotřeba tokenů a historie akcí ──
const fmtTok = n => n == null ? "—" : n >= 1e6 ? (n / 1e6).toLocaleString("cs-CZ", {maximumFractionDigits: 1}) + " mil." : n >= 1e3 ? Math.round(n / 1e3).toLocaleString("cs-CZ") + " tis." : String(n);
const ACTION = {"claim": ["Zamkl(a) cestu", ""], "release": ["Uvolnil(a) cestu", ""], "claim-refused": ["Zámek odmítnut", "bad"], "accept": ["Převzal(a) zprávu", ""],
  "expire": ["Nájem vypršel", ""], "blocked": ["Úkol zablokován", "bad"], "failover": ["Převzetí úkolů jiným agentem", ""], "limit": ["Nahlášený limit", "bad"],
  "review-open": ["Otevřel(a) review", ""], "review-submit": ["Poslal(a) do review", ""], "review-verdict": ["Verdikt review", "ok"], "review-close": ["Uzavřel(a) review", "ok"], "review-grant-cycle": ["Povolil(a) další kolo", ""],
  "quality-run": ["Kontrola kvality", ""], "quality-request": ["Požadavek na kontrolu", ""], "quality-request-unavailable": ["Kontrola nedostupná", "bad"],
  "pause": ["Zastavil(a) agenty", "bad"], "resume": ["Pustil(a) agenty", "ok"], "mode": ["Přepnul(a) režim", ""], "provider-state": ["Stav poskytovatele", ""],
  "web-login-passkey": ["Přihlášení Windows Hello", "ok"], "web-login-passkey-failed": ["Neúspěšné přihlášení", "bad"], "web-passkey-added": ["Přidán klíč Windows Hello", ""], "web-passkey-removed": ["Odebrán klíč Windows Hello", ""]};
const HIST_GROUPS = [["main", "Důležité", a => !(a.startsWith("claim") || ["release", "accept", "expire", "provider-state"].includes(a))], ["all", "Vše", () => true], ["review", "Review", a => a.startsWith("review")], ["control", "Řízení a přihlášení", a => ["pause", "resume", "mode"].includes(a) || a.startsWith("web-")],
  ["quality", "Kvalita", a => a.startsWith("quality")], ["tasks", "Úkoly", a => ["accept", "expire", "blocked", "failover", "limit"].includes(a)], ["leases", "Zámky cest", a => a.startsWith("claim") || a === "release"]];
let histEvents = [], histGroup = "main", histTimer = null, opsData = null;
let histEnd = false;
function mergeHistory(current, fresh) {
  const byId = new Map(current.map(e => [e.id, e]));
  fresh.forEach(e => byId.set(e.id, e));
  return [...byId.values()].sort((a, b) => b.id - a.id);
}
function loadOps(mode = "reset") {   // "reset" = nové hledání/otevření, "more" = starší, "live" = sloučit nové
  const q = document.getElementById("histQuery").value.trim();
  const more = mode === "more" || mode === true;
  const before = more && histEvents.length ? histEvents[histEvents.length - 1].id : 0;
  fetch("/api/history?" + new URLSearchParams({limit: "200", ...(q ? {q} : {}), ...(before ? {before: String(before)} : {})}))
    .then(r => r.ok ? r.json() : Promise.reject(r)).then(d => {
      if (more) { histEvents = mergeHistory(histEvents, d.events); histEnd = d.events.length < 200; }
      else if (mode === "live" && histEvents.length) histEvents = mergeHistory(histEvents, d.events);
      else { histEvents = d.events; histEnd = d.events.length < 200; }
      renderHistory(histEnd);
    })
    .catch(() => { document.getElementById("histList").textContent = "Historii se nepodařilo načíst."; });
  if (!more) fetch("/api/usage").then(r => r.ok ? r.json() : null).then(d => { if (d) { opsData = d; renderUsage(); } }).catch(() => {});
  if (!more) fetch("/api/jobs").then(r => r.ok ? r.json() : null).then(d => { if (d) { opsJobs = d.jobs; renderOpsTasks(); } }).catch(() => {});
}
// Úkoly na pozadí: stav ze SSE (messageCache, okamžitě) má přednost; /api/jobs doplní starší otevřené úkoly.
let opsJobs = [];
const OPS_OPEN = ["pending", "leased", "started", "failed", "needs_review", "dead_letter"];
function renderOpsTasks() {
  const el = document.getElementById("opsTasks"); if (!el) return;
  const byId = new Map(opsJobs.map(j => [j.id, j]));
  historyMessages().filter(m => m.kind === "task" && m.recipient && m.state).forEach(m => byId.set(m.id, m));
  const rows = [...byId.values()].filter(m => OPS_OPEN.includes(m.state)).sort((a, b) => b.id - a.id);
  document.getElementById("opsTasksCount").textContent = `(${rows.length})`;
  const key = JSON.stringify(rows.map(m => [m.id, m.state, m.attempts, m.last_error, !!m.fix]));
  if (el._key === key) { el.querySelectorAll("time[data-ago]").forEach(t => t.textContent = when(Number(t.dataset.ago))); return; }   // jen čas, tlačítka zůstanou pod kurzorem
  el._key = key; el.innerHTML = "";
  if (!rows.length) { el.innerHTML = "<div class='empty'>Nic neběží a nic nečeká. Nový úkol se tu objeví hned, jak vznikne.</div>"; return; }
  const now = Date.now() / 1000;
  rows.forEach(m => {
    const row = document.createElement("div"); row.className = "hist-row ops-task"; row.dataset.tone = stateTone(m.state);
    const at = now - (m.seconds_ago || 0);
    row.innerHTML = `<time data-ago="${at}" title="${esc(new Date(at * 1000).toLocaleString("cs-CZ"))}">${esc(when(at))}</time>${whoBadge(m.recipient)}<div><b>Úkol #${m.id}</b> ${taskState(m)}${m.attempts > 1 ? ` <span class="tag">pokus ${m.attempts}</span>` : ""}<div class="det">${esc((m.text || "").split(String.fromCharCode(10))[0].slice(0, 220))}</div>${m.last_error ? `<div class="det bad">${esc(m.last_error.slice(0, 240))}</div>` : ""}</div>`;
    renderTaskActions(m, row.lastElementChild);
    el.appendChild(row);
  });
}
function renderHistory(end) {
  const g = HIST_GROUPS.find(x => x[0] === histGroup), rows = histEvents.filter(e => g[2](e.action));
  setHTMLIfChanged(document.getElementById("histChips"), chipsHTML(HIST_GROUPS.map(([id, l, f]) => [id, `${l} (${histEvents.filter(e => f(e.action)).length})`]), histGroup, "data-hist"));
  document.querySelectorAll("[data-hist]").forEach(b => b.onclick = () => { histGroup = b.dataset.hist; renderHistory(end); });
  document.getElementById("histCount").textContent = `(${rows.length})`;
  setHTMLIfChanged(document.getElementById("histList"), rows.length ? rows.map(e => { const [label, tone] = ACTION[e.action] || [e.action, ""];
    return `<div class="hist-row" data-tone="${tone}"><time title="${esc(new Date(e.at * 1000).toLocaleString("cs-CZ"))}">${esc(when(e.at))}</time>${whoBadge(e.agent)}<div><b>${esc(label)}</b>${e.path || e.detail ? `<div class="det">${esc([e.path, e.detail].filter(Boolean).join(" · ").slice(0, 300))}</div>` : ""}</div></div>`; }).join("")
    : "<div class='empty'>Žádné akce pro tento filtr.</div>");
  document.getElementById("histMore").hidden = !!end;
}
function renderUsage() {
  const d = opsData; if (!d) return;
  const tok = x => x.tokens ?? null;   // stejný součet jako vynucovaný strop (backend _TOKENS)
  const day = Object.values(d.agents).reduce((s, a) => s + tok(a.day), 0), week = Object.values(d.agents).reduce((s, a) => s + tok(a.week), 0);
  const cost = Object.values(d.agents).reduce((s, a) => s + (a.week.cost_usd || 0), 0);
  metricsRow("opsMetrics", [
    ["Tokeny za 24 h", fmtTok(day), "bars", "cyan", "Vstup + výstup + mezipaměť, stejně jako denní strop"],
    ["Tokeny za 7 dní", fmtTok(week), "layers", "purple"],
    ["Běhy za 24 h", Object.values(d.agents).reduce((s, a) => s + (a.day.attempts || 0), 0), "flask", "orange"],
    ["Cena za 7 dní", cost ? "$" + cost.toFixed(2) : "neuvedena", "star", "green", "Poskytovatel cenu u předplatného nehlásí"],
    ["Denní strop", d.caps.daily_tokens ? fmtTok(d.caps.daily_tokens) : "bez stropu", "shield", "purple"],
    ["Akce v historii", histEvents.length >= 200 ? "200+" : histEvents.length, "clock", "cyan"],
  ]);
  document.getElementById("opsCaps").textContent = d.caps.task_tokens ? `strop na úkol ${fmtTok(d.caps.task_tokens)}` : "";
  setHTMLIfChanged(document.getElementById("opsUsage"), Object.entries(d.agents).map(([a, u]) => { const k = agentKey(a), cap = d.caps.daily_tokens, used = tok(u.day);
    return `<div class="use-agent"><div class="who"><img src="/assets/${k}.webp" alt=""><b>${k === "ada" ? "Ada" : "Nox"}</b></div>${cap && used != null ? `<div class="bar" title="${esc(fmtTok(used))} z ${esc(fmtTok(cap))}"><i style="width:${Math.min(100, Math.round(100 * used / cap))}%"></i></div>` : ""}<dl><dt>24 h celkem (jako strop)</dt><dd>${esc(fmtTok(used))}</dd><dt>z toho vstup / výstup</dt><dd>${esc(fmtTok(u.day.input_tokens))} / ${esc(fmtTok(u.day.output_tokens))}</dd><dt>z toho mezipaměť (čtení / zápis)</dt><dd>${esc(fmtTok(u.day.cache_read_tokens))} / ${esc(fmtTok(u.day.cache_creation_tokens))}</dd>${u.day.tokens_unknown ? `<dt>běhy s neznámou spotřebou</dt><dd>${u.day.tokens_unknown}</dd>` : ""}<dt>7 dní celkem</dt><dd>${esc(fmtTok(tok(u.week)))}</dd><dt>Běhy 24 h / 7 dní</dt><dd>${u.day.attempts || 0} / ${u.week.attempts || 0}</dd></dl></div>`; }).join("")
    + '<p class="note">Počítají se jen běhy na pozadí přes mosty. Tvoje interaktivní relace s Adou a Noxem tu nejsou.</p>');
  setHTMLIfChanged(document.getElementById("opsRuns"), d.recent.length ? d.recent.map(r => `<div class="hist-row"><time>${esc(when(r.at))}</time>${whoBadge(r.recipient)}<div><b>Úkol #${r.message_id}${r.attempt > 1 ? ` · pokus ${r.attempt}` : ""}</b><div class="det">${r.tokens_known ? esc(fmtTok(r.tokens)) + " tokenů" : "spotřeba neznámá"}${r.model || r.requested_model ? " · " + esc(r.model || r.requested_model) : ""}</div></div></div>`).join("")
    : "<div class='empty'>Zatím žádný zaznamenaný běh.</div>");
}
document.getElementById("histQuery").addEventListener("input", () => { clearTimeout(histTimer); histTimer = setTimeout(() => loadOps(), 300); });
document.getElementById("histMore").onclick = () => loadOps("more");
// ── Katalog schopností z ověřené inventury ──
let catalog = null, catAgent = "ada", catStatus = "all";
const statusCol = sec => sec.columns.findIndex(c => /^stav/i.test(c));   // stav jen z explicitního sloupce (review #60)
const rowStatus = (sec, r) => { const i = statusCol(sec); return i < 0 ? "" : CAT_STATUS(r[i] || ""); };
const CAT_STATUS = s => { const t = String(s).toLowerCase(); return t.includes("verified") ? "verified" : t.includes("failed") ? "failed" : t.includes("enabled") ? "enabled" : t.includes("installed") ? "installed" : t.includes("path") ? "path" : ""; };
const CAT_LABEL = {verified: "ověřeno", enabled: "zapnuto", installed: "nainstalováno", failed: "nefunguje", path: "jen v PATH"};
function loadCatalog() {
  if (catalog) return renderCatalog();
  fetch("/api/catalog").then(r => r.ok ? r.json() : Promise.reject(r)).then(d => { catalog = d; renderCatalog(); })
    .catch(() => { document.getElementById("catBody").textContent = "Katalog se nepodařilo načíst."; });
}
function renderCatalog() {
  const all = catalog.agents, rows = a => all[a].flatMap(sec => sec.rows.map(r => ({sec, r, st: rowStatus(sec, r)})));
  const count = (a, st) => rows(a).filter(x => x.st === st).length;
  metricsRow("catMetrics", [["Položek u Ady", rows("ada").length, "users", "orange"], ["Položek u Noxe", rows("nox").length, "users", "green"],
    ["Ověřeno", count("ada", "verified") + count("nox", "verified"), "check", "green"], ["Zapnuto", count("ada", "enabled") + count("nox", "enabled"), "layers", "cyan"],
    ["Nefunguje", count("ada", "failed") + count("nox", "failed"), "alert", "orange"], ["Inventura", new Date(catalog.as_of).toLocaleDateString("cs-CZ"), "clock", "purple"]]);
  setHTMLIfChanged(document.getElementById("catAgents"), ["ada", "nox"].map(a => `<button type="button" class="pg-item${a === catAgent ? " on" : ""}" data-cat-agent="${a}"><img class="act-portrait" src="/assets/${a}.webp" alt=""><span class="pg-item-body"><span class="pg-item-title">${a === "ada" ? "Ada · Claude Code" : "Nox · Codex"}</span><span class="pg-item-sub">${rows(a).length} položek · ${count(a, "verified")} ověřeno</span></span></button>`).join(""));
  document.querySelectorAll("[data-cat-agent]").forEach(b => b.onclick = () => { catAgent = b.dataset.catAgent; renderCatalog(); });
  document.getElementById("catSource").textContent = `Zdroj: ${catalog.generated_from[catAgent]} (inventura ${new Date(catalog.as_of).toLocaleDateString("cs-CZ")}). Mezi agenty neplatí „co má jeden, má i druhý“.`;
  document.getElementById("catTitle").textContent = catAgent === "ada" ? "Schopnosti Ady" : "Schopnosti Noxe";
  setHTMLIfChanged(document.getElementById("catChips"), chipsHTML([["all", "Vše"], ...["verified", "enabled", "installed", "failed", "path"].filter(st => count(catAgent, st)).map(st => [st, `${CAT_LABEL[st]} (${count(catAgent, st)})`])], catStatus, "data-cat-st"));
  document.querySelectorAll("[data-cat-st]").forEach(b => b.onclick = () => { catStatus = b.dataset.catSt; renderCatalog(); });
  const q = document.getElementById("catQuery").value.trim().toLowerCase();
  setHTMLIfChanged(document.getElementById("catBody"), all[catAgent].map(sec => {
    const list = sec.rows.filter(r => (catStatus === "all" || rowStatus(sec, r) === catStatus) && (!q || r.join(" ").toLowerCase().includes(q)));
    if (!list.length) return "";
    return `<section class="cat-section"><h3>${esc(sec.section)}</h3><table class="cat-table"><thead><tr>${sec.columns.map(c => `<th scope="col">${esc(c)}</th>`).join("")}</tr></thead><tbody>${list.map(r => `<tr>${r.map((c, i) => { const st = i === statusCol(sec) ? CAT_STATUS(c) : ""; return `<td>${st ? `<span class="st-pill ${st}">${esc(c)}</span>` : esc(c)}</td>`; }).join("")}</tr>`).join("")}</tbody></table></section>`;
  }).join("") || "<div class='empty'>Nic neodpovídá filtru.</div>");
}
document.getElementById("catQuery").addEventListener("input", () => { if (catalog) renderCatalog(); });

function showView(view,{focus=true}={}) {
  if(view==="ops")loadOps();
  if(view==="catalog")loadCatalog();
  if(view==="settings"){loadMode();loadSettingsData();}
  if(view==="research")loadResearch();
  if(view==="memory")loadKnowledge();
  if(view==="overview")loadDashData();
  if(drawer.open){drawer.close();restorePanels();}
  currentView=view;
  const panelView=view==="room"?"overview":view;   // Místnost agentů = stejná místnost přes celou plochu
  document.body.classList.toggle("room-focus",view==="room");
  document.querySelectorAll("[data-view-panel]").forEach(el=>{el.hidden=el.dataset.viewPanel!==panelView;});
  document.querySelectorAll(".primary-nav [data-view]").forEach(b=>{const on=b.dataset.view===view;b.classList.toggle("current",on);if(on)b.setAttribute("aria-current","page");else b.removeAttribute("aria-current");});
  if(view==="room"){if(focus)document.getElementById("text").focus({preventScroll:true});feed.scrollTop=feed.scrollHeight;}
  else if(focus)document.querySelector(`[data-view-panel="${panelView}"] .view-title`)?.focus({preventScroll:true});
  motion(document.querySelector(`[data-view-panel="${panelView}"]`),[{opacity:.5},{opacity:1}],180);
  renderPages();
}
document.querySelectorAll("[data-view]").forEach(b=>b.addEventListener("click",()=>showView(b.dataset.view)));
renderQualityGate();

