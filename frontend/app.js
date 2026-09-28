/* Prequal review screen. Vanilla JS, no build step. Talks only to /api. */

const $ = (id) => document.getElementById(id);
const state = { questionnaires: [], current: null, selectedId: null, filter: "all", running: false, logSeen: new Set() };

/* ---------- motion layer (GSAP). Every animation explains a state change; all of it is skipped under reduced motion. ---------- */
const G = window.gsap;
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const MOTION = !!G && !REDUCED;
if (G && window.SplitText) G.registerPlugin(SplitText);
if (G && window.DrawSVGPlugin) G.registerPlugin(DrawSVGPlugin);
const EASE = { std: "power2.inOut", enter: "power3.out", exit: "power2.in" };
let titleSplit = null;

function countTo(el, to, suffix) {
  const from = Number(el.dataset.val || 0);
  el.dataset.val = to;
  if (!MOTION || from === to) { el.textContent = `${to}${suffix}`; return; }
  const o = { v: from };
  G.to(o, { v: to, duration: Math.min(1.4, 0.35 + Math.abs(to - from) * 0.03), ease: "power3.out",
    onUpdate: () => { el.textContent = `${Math.round(o.v)}${suffix}`; } });
}

function revealTitle() {
  const el = $("d-text");
  if (titleSplit) { titleSplit.revert(); titleSplit = null; }
  if (!MOTION || !window.SplitText) return;
  titleSplit = new SplitText(el, { type: "words", mask: "words" });
  G.from(titleSplit.words, { yPercent: 110, duration: 0.5, ease: EASE.enter, stagger: 0.025 });
}

/* Provenance threads: the answer is tied, by a drawn line, to each memory it cited. */
function drawThreads() {
  const svg = $("threads"), card = $("qcard"), wrap = $("answer-wrap");
  svg.innerHTML = "";
  const cited = [...document.querySelectorAll("#d-evidence li.cited")];
  if (!cited.length || card.classList.contains("hidden")) return;
  const cr = card.getBoundingClientRect();
  const wr = wrap.getBoundingClientRect();
  svg.setAttribute("width", cr.width); svg.setAttribute("height", card.scrollHeight);
  const x0 = -18, yA = wr.top - cr.top + 22;
  const paths = [];
  const node = (x, y, r) => { const c = document.createElementNS("http://www.w3.org/2000/svg", "rect"); c.setAttribute("x", x - r); c.setAttribute("y", y - r); c.setAttribute("width", r * 2); c.setAttribute("height", r * 2); c.setAttribute("class", "thread-node"); svg.appendChild(c); return c; };
  cited.forEach((li) => {
    const lr = li.getBoundingClientRect();
    const yB = lr.top - cr.top + 22;
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", `M 0 ${yA} H ${x0} V ${yB} H -2`);
    p.setAttribute("class", "thread");
    svg.appendChild(p); paths.push(p);
  });
  const nodes = [node(x0, yA, 3.5), ...cited.map((li) => node(-2, li.getBoundingClientRect().top - cr.top + 22, 3))];
  if (MOTION && window.DrawSVGPlugin) {
    G.fromTo(paths, { drawSVG: "0%" }, { drawSVG: "100%", duration: 0.7, ease: EASE.std, stagger: 0.12, delay: 0.25 });
    G.from(nodes, { scale: 0, transformOrigin: "50% 50%", duration: 0.25, ease: "back.out(3)", stagger: 0.1, delay: 0.25 });
  }
}
window.addEventListener("resize", () => { if (!$("qcard").classList.contains("hidden")) drawThreads(); });

function stampApproved() {
  const s = $("stamp");
  if (!MOTION) { s.style.opacity = 1; setTimeout(() => (s.style.opacity = 0), 1400); return; }
  G.timeline()
    .fromTo(s, { opacity: 0, scale: 1.8, rotate: -14 }, { opacity: 1, scale: 1, rotate: -6, duration: 0.28, ease: "power4.in" })
    .to($("answer-wrap"), { x: 2, y: 2, duration: 0.05, yoyo: true, repeat: 1 }, "<0.26")
    .to(s, { opacity: 0, duration: 0.4, ease: EASE.exit }, "+=1.1");
}

const STATUS_LABEL = { auto: "auto-filled", needs_review: "needs you", gap: "gap", approved: "approved", pending: "not run" };

async function api(method, path, body) {
  const res = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, err = false) {
  const t = $("toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), err ? 5000 : 2600);
}

function esc(s) { return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

/* ---------------- memory status ---------------- */
async function refreshMemory() {
  try {
    const s = await api("GET", "/api/memory/status");
    $("memdot").className = "dot " + (s.ingested ? "on" : "off");
    $("memlabel").textContent = s.ingested
      ? `${s.company.name} in memory · ${s.fields} fields · ${s.documents_on_file} docs on file, ${s.documents_missing} missing${s.mock ? " · MOCK" : ""}`
      : `memory empty: the agent knows nothing yet${s.mock ? " · MOCK" : ""}`;
    $("bankmeta").innerHTML = s.ingested
      ? `bank: ${esc(s.company.slug)}<br>ingested ${esc((s.ingested_at || "").replace("T", " ").slice(0, 16))}`
      : `bank empty`;
    $("btn-ingest").disabled = s.ingested;
    $("btn-ingest").textContent = s.ingested ? "Company memory ingested" : "Ingest company memory";
    if (s.demo_update) {
      const [fy, v] = Object.entries(s.demo_update)[0];
      if (!$("retain-content").value) $("retain-content").value = `${s.company?.name || "The company"}'s annual turnover for FY ${fy} was ${v.label} (provisional).`;
      if (!$("retain-date").value) $("retain-date").value = `20${fy.split("-")[1]}-03-31`;
    }
  } catch (e) { $("memlabel").textContent = "API unreachable"; }
}

async function refreshLog() {
  try {
    const { events } = await api("GET", "/api/memory/log?limit=60");
    const ul = $("log");
    ul.innerHTML = events.map((e) => `
      <li class="${e.ok ? "" : "fail"} ${state.logSeen.has(e.id) ? "" : "new"}">
        <span class="op ${esc(e.op)}">${esc(e.op)}</span>
        <span class="d" title="${esc(e.detail)}">${esc(e.detail)}${e.hits !== undefined ? ` <em>(${e.hits} hits)</em>` : ""}</span>
        <span class="ms">${e.ms} ms</span>
      </li>`).join("");
    const fresh = [...ul.querySelectorAll("li.new")];
    if (MOTION && fresh.length && fresh.length < 40) G.from(fresh, { x: -10, opacity: 0, duration: 0.35, ease: EASE.enter, stagger: 0.04 });
    events.forEach((e) => state.logSeen.add(e.id));
  } catch (_) {}
}

/* ---------------- questionnaires ---------------- */
async function loadQuestionnaires() {
  let { questionnaires } = await api("GET", "/api/questionnaires");
  if (!questionnaires.length) {
    await api("POST", "/api/questionnaires/load-samples");
    ({ questionnaires } = await api("GET", "/api/questionnaires"));
  }
  state.questionnaires = questionnaires;
  const sel = $("qsel");
  sel.innerHTML = questionnaires.map((q) => `<option value="${q.id}">${esc(q.client)} · ${esc(q.title)}</option>`).join("");
  const keep = state.current?.id || questionnaires[0]?.id;
  sel.value = keep;
  await selectQuestionnaire(keep);
}

async function selectQuestionnaire(id) {
  state.current = await api("GET", `/api/questionnaires/${id}`);
  const q = state.current;
  $("qmeta").innerHTML = `<b>${esc(q.client)}</b> · ${esc(q.client_type || "")}<br>received ${esc(q.received_on || "?")}, due ${esc(q.due_on || "?")} · ${q.questions.length} questions<br><span title="${esc(q.scope)}">${esc((q.scope || "").slice(0, 110))}${(q.scope || "").length > 110 ? "…" : ""}</span>`;
  $("brief-text").textContent = q.brief_text || "Not generated yet.";
  renderList();
  renderCounts();
  if (state.selectedId && !q.questions.some((x) => x.id === state.selectedId)) state.selectedId = null;
  renderDetail();
}

function renderCounts() {
  const c = { auto: 0, needs_review: 0, gap: 0, approved: 0, pending: 0 };
  state.current.questions.forEach((q) => { c[q.status] = (c[q.status] || 0) + 1; });
  const done = c.auto + c.approved, total = state.current.questions.length;
  const fig = $("figure-num"), prev = fig.dataset.val ? Number(fig.dataset.val) : null;
  countTo(fig, done, ` / ${total}`);
  fig.classList.toggle("up", prev !== null && done > prev);
  $("counts").innerHTML = `<span class="c-auto">${c.auto} auto</span><span class="c-review">${c.needs_review} need you</span><span class="c-gap">${c.gap} gaps</span><span class="c-approved">${c.approved} approved</span>`;
}

function renderList() {
  const list = $("qlist");
  const qs = state.current.questions.filter((q) => state.filter === "all" || q.status === state.filter);
  list.innerHTML = qs.map((q) => `
    <li data-id="${q.id}" class="${q.status} ${q.id === state.selectedId ? "sel" : ""}">
      <span class="n">${q.ordinal}</span>
      <span class="t">${esc(q.text)}<small><span>${esc(q.section || "")}${q.type === "field" ? " · exact field" : q.type === "document" || q.type === "yes_no" ? " · document" : ""}</span><span class="pill ${q.status}">${STATUS_LABEL[q.status] || q.status}</span></small></span>
    </li>`).join("");
  list.querySelectorAll("li").forEach((li) => li.addEventListener("click", () => { state.selectedId = Number(li.dataset.id); renderList(); renderDetail(); }));
}

function renderDetail() {
  const q = state.current?.questions.find((x) => x.id === state.selectedId);
  $("detail-empty").classList.toggle("hidden", !!q);
  $("qcard").classList.toggle("hidden", !q);
  if (!q) return;
  $("d-section").textContent = q.section || "";
  $("d-ordinal").textContent = `Q${q.ordinal} · ${q.type.replace("_", " ")}`;
  const titleChanged = $("d-text").dataset.qid !== String(q.id);
  if (titleSplit) { titleSplit.revert(); titleSplit = null; }
  $("d-text").textContent = q.text;
  $("d-text").dataset.qid = q.id;
  $("d-status").className = "pill " + q.status;
  $("d-status").textContent = STATUS_LABEL[q.status] || q.status;
  $("d-conf").textContent = q.confidence != null ? `confidence ${Number(q.confidence).toFixed(2)}` : "";
  $("d-source").textContent = q.source ? `via ${q.source}` : "";
  const note = $("d-note");
  note.textContent = q.agent_note || "";
  note.className = "note " + (q.status === "gap" ? "bad" : q.status === "needs_review" ? "warn" : "");
  note.classList.toggle("hidden", !q.agent_note);
  $("d-answer").value = q.final_text || q.draft_text || "";
  $("d-reason").value = "";
  $("btn-approve").disabled = q.status === "approved";
  $("btn-approve").textContent = q.status === "approved" ? "Approved" : "Approve";

  const tl = q.evidence?.timeline || [];
  $("d-timeline").classList.toggle("hidden", tl.length === 0);
  $("d-timeline-list").innerHTML = tl.map((t) => `<li><span class="when">${esc(t.label)}</span>${esc(t.text)}</li>`).join("");

  const items = q.evidence?.items || [];
  const cited = new Set(q.evidence?.cited || []);
  $("d-evcount").textContent = items.length ? `· ${items.length} item${items.length > 1 ? "s" : ""}${cited.size ? `, ${cited.size} cited` : ""}` : "· none";
  $("d-evidence").innerHTML = items.length
    ? items.map((e) => `
      <li class="${cited.has(e.id) ? "cited" : ""}">
        <span class="meta"><span class="kind ${esc(e.type)}">${esc(e.type)}</span>${e.occurred_start ? `<span>${esc(e.occurred_start.slice(0, 10))}</span>` : ""}${(e.tags || []).length ? `<span>${esc(e.tags.join(", "))}</span>` : ""}${cited.has(e.id) ? `<span class="cited-mark">cited by the draft</span>` : ""}</span>
        <span>${esc(e.text)}</span>
      </li>`).join("")
    : `<li>${q.source === "lookup" ? "Answered by exact field lookup, no memory search needed." : q.status === "pending" ? "Run the agent first." : "Nothing recalled. The agent did not guess."}</li>`;
  if (titleChanged) revealTitle();
  requestAnimationFrame(drawThreads);
}

/* ---------------- actions ---------------- */
async function runAgent() {
  if (state.running || !state.current) return;
  state.running = true;
  $("btn-run").disabled = true; $("btn-run").textContent = "Running…";
  $("bar").style.transform = "scaleX(0)";
  try {
    const res = await fetch(`/api/questionnaires/${state.current.id}/run`, { method: "POST" });
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const line = chunk.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const ev = JSON.parse(line.slice(6));
        if (ev.event === "answer") {
          $("bar").style.transform = `scaleX(${ev.i / ev.total})`;
          const q = state.current.questions.find((x) => x.id === ev.question_id);
          if (q) { q.status = ev.status; q.confidence = ev.confidence; q.source = ev.source; }
          renderList(); renderCounts();
          const li = document.querySelector(`.qlist li[data-id="${ev.question_id}"]`);
          if (li) { li.classList.add("fresh"); if (MOTION) G.from(li.querySelector(".pill"), { opacity: 0, x: 8, duration: 0.3, ease: EASE.enter }); }
          if (ev.i % 4 === 0) refreshLog();
        } else if (ev.event === "done") {
          toast(`Done: ${ev.counts.auto || 0} auto, ${ev.counts.needs_review || 0} need you, ${ev.counts.gap || 0} gaps`);
        } else if (ev.event === "error") {
          toast(ev.message, true);
        }
      }
    }
    await selectQuestionnaire(state.current.id);
  } catch (e) { toast(`Run failed: ${e.message}`, true); }
  finally { state.running = false; $("btn-run").disabled = false; $("btn-run").textContent = "Run agent"; refreshLog(); refreshMemory(); }
}

async function review(action) {
  const q = state.current?.questions.find((x) => x.id === state.selectedId);
  if (!q) return;
  try {
    const body = { action, final_text: $("d-answer").value, note: $("d-reason").value || null };
    const r = await api("PATCH", `/api/answers/${q.answer_id}`, body);
    if (action === "approve") { stampApproved(); toast(r.edited ? "Approved with your edit. Retained as a memory." : "Approved. Retained as a memory."); await new Promise((ok) => setTimeout(ok, MOTION ? 700 : 0)); }
    else if (action === "gap") toast("Marked as a gap.");
    else toast("Reopened. Run the agent to re-draft it.");
    await selectQuestionnaire(state.current.id);
    refreshLog();
  } catch (e) { toast(e.message, true); }
}

async function ingest() {
  $("btn-ingest").disabled = true; $("btn-ingest").textContent = "Ingesting…";
  const stop = setInterval(refreshLog, 700);
  try {
    const r = await api("POST", "/api/ingest");
    toast(`Ingested ${r.counts.total} memories: ${r.counts.profile} profile, ${r.counts.documents} documents, ${r.counts.past_answers} past answers`);
  } catch (e) { toast(e.message, true); }
  finally { clearInterval(stop); await refreshMemory(); await refreshLog(); }
}

async function freshAgent() {
  if (!confirm("Clear the memory bank and this questionnaire's answers? This is the 'before' state for the demo.")) return;
  try {
    await api("POST", "/api/memory/reset");
    if (state.current) await api("POST", `/api/questionnaires/${state.current.id}/reset`);
    toast("Memory cleared. The agent now knows nothing about the company.");
    await refreshMemory(); await refreshLog();
    if (state.current) await selectQuestionnaire(state.current.id);
  } catch (e) { toast(e.message, true); }
}

async function brief() {
  if (!state.current) return;
  $("btn-brief").disabled = true; $("brief-text").textContent = "Reflecting over memory…";
  try {
    const r = await api("POST", `/api/questionnaires/${state.current.id}/brief`);
    $("brief-text").textContent = r.brief;
  } catch (e) { $("brief-text").textContent = e.message; }
  finally { $("btn-brief").disabled = false; refreshLog(); }
}

async function retainLive(ev) {
  ev.preventDefault();
  const content = $("retain-content").value.trim();
  if (!content) return;
  try {
    await api("POST", "/api/memory/retain", { content, context: "fact added by the reviewer", tags: ["profile", "financial", "live"], timestamp: $("retain-date").value || null });
    toast("Retained. Re-draft the turnover question to see the timeline update.");
    $("retain-content").value = "";
    refreshLog();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- wiring ---------------- */
$("qsel").addEventListener("change", (e) => selectQuestionnaire(Number(e.target.value)));
$("btn-run").addEventListener("click", runAgent);
$("btn-ingest").addEventListener("click", ingest);
$("btn-fresh").addEventListener("click", freshAgent);
$("btn-brief").addEventListener("click", brief);
$("btn-approve").addEventListener("click", () => review("approve"));
$("btn-gap").addEventListener("click", () => review("gap"));
$("btn-reopen").addEventListener("click", () => review("reopen"));
$("retain-form").addEventListener("submit", retainLive);
$("filters").querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
  state.filter = b.dataset.f;
  $("filters").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  renderList();
}));
$("btn-export-csv").addEventListener("click", (e) => { e.preventDefault(); if (state.current) window.open(`/api/questionnaires/${state.current.id}/export?format=csv`); });
$("btn-export-json").addEventListener("click", (e) => { e.preventDefault(); if (state.current) window.open(`/api/questionnaires/${state.current.id}/export?format=json`); });

(async function init() {
  await refreshMemory();
  await refreshLog();
  try { await loadQuestionnaires(); } catch (e) { toast(`Could not load questionnaires: ${e.message}`, true); }
  setInterval(refreshLog, 4000);
})();
