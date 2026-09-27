"use strict";
const $ = (id) => document.getElementById(id);
let symbols = [];
let latest = null;

function money(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return new Intl.NumberFormat("tr-TR", {style:"currency",currency:"USD",maximumFractionDigits:2}).format(Number(value));
}
function percent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const number = Number(value);
  return `${number >= 0 ? "+" : ""}${number.toFixed(2)}%`;
}
function timeText(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : new Intl.DateTimeFormat("tr-TR", {timeZone:"UTC",day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}).format(date);
}
function setTone(element, value) {
  element.classList.remove("positive", "negative");
  if (value === null || value === undefined) return;
  element.classList.add(Number(value) >= 0 ? "positive" : "negative");
}
function td(text, extraClass="") {
  const cell = document.createElement("td");
  cell.textContent = text ?? "—";
  if (extraClass) cell.className = extraClass;
  return cell;
}
function renderChart(points) {
  const line = $("chart-line");
  const empty = $("chart-empty");
  if (!Array.isArray(points) || points.length < 2) {
    line.setAttribute("points", "");
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  const values = points.map((item) => Number(item.value)).filter(Number.isFinite);
  if (values.length < 2) return;
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || Math.max(Math.abs(max) * .01, 1);
  const coordinates = values.map((v,i) => `${(i/(values.length-1)*720).toFixed(2)},${(183-(v-min)/range*153).toFixed(2)}`);
  line.setAttribute("points", coordinates.join(" "));
}
function renderDecisions(items) {
  const body = $("decision-rows");
  body.replaceChildren();
  if (!items || !items.length) {
    const row = document.createElement("tr");
    const cell = td("Henüz karar kaydı yok.", "empty-row"); cell.colSpan=5; row.append(cell); body.append(row); return;
  }
  for (const item of items) {
    const row = document.createElement("tr");
    row.append(td(timeText(item.ts)),td(item.symbol));
    const decisionCell = document.createElement("td");
    const pill = document.createElement("span");
    const decision = String(item.decision || "hold").toLowerCase();
    pill.className = `decision-pill ${decision === "long" || decision === "short" ? decision : ""}`;
    pill.textContent = decision.toUpperCase();
    decisionCell.append(pill);row.append(decisionCell);
    row.append(td(item.confidence === null || item.confidence === undefined ? "—" : `${(Number(item.confidence)*100).toFixed(0)}%`));
    const validation=String(item.validation_status||"");
    const suffix=validation === "PENDING_PAPER" ? " · GEÇİCİ PAPER" : validation === "PENDING_BLOCKED" ? " · WFA BEKLENİYOR" : validation === "REJECTED" ? " · WFA RED" : validation === "QUICK_NO_EDGE" ? " · KISA TEST GEÇİLMEDİ" : "";
    row.append(td(`${item.reason || "—"}${suffix}`));body.append(row);
  }
}
function renderSymbols(filter="") {
  const list = $("symbol-list");
  list.replaceChildren();
  const term = filter.trim().toUpperCase();
  const shown = symbols.filter(s => s.includes(term)).slice(0,120);
  if (!shown.length) {const div=document.createElement("div");div.className="empty-row";div.textContent=symbols.length ? "Eşleşen parite yok." : "Pariteler henüz yüklenmedi.";list.append(div);return;}
  for (const symbol of shown) {
    const div=document.createElement("div");div.className="symbol-item";
    const name=document.createElement("strong");name.textContent=symbol;
    const icon=document.createElement("span");icon.textContent="↗";
    div.append(name,icon);list.append(div);
  }
}
function renderOrders(items) {
  const list=$("order-list");list.replaceChildren();
  if (!items || !items.length) {const div=document.createElement("div");div.className="empty-row";div.textContent="Henüz paper işlem yok.";list.append(div);return;}
  for (const item of items) {
    const row=document.createElement("div");row.className="order-item";
    const left=document.createElement("div"),right=document.createElement("div");
    const name=document.createElement("strong");name.textContent=(item.signal_key || "Paper işlem").split(":")[0];
    const detail=document.createElement("small");detail.textContent=`${timeText(item.ts)} · ${item.signal_key || "—"}`;
    left.append(name,detail);
    right.className=`order-status ${item.kind === "order_error" ? "error" : ""}`;
    right.textContent=item.kind === "order_error" ? "HATA" : (item.status || "İŞLENDİ").toUpperCase();
    row.append(left,right);list.append(row);
  }
}
function renderPositions(items) {
  const body=$("position-rows"); body.replaceChildren();
  if (!items || !items.length) {
    const row=document.createElement("tr"), cell=td("Açık paper pozisyon yok.","empty-row");
    cell.colSpan=7; row.append(cell); body.append(row); return;
  }
  for (const item of items) {
    const row=document.createElement("tr");
    row.append(td(item.symbol),td(String(item.direction||"—").toUpperCase()),td(item.contracts),
      td(Number(item.entry_price).toLocaleString("tr-TR",{maximumFractionDigits:8})),
      td(Number(item.stop_price).toLocaleString("tr-TR",{maximumFractionDigits:8})),
      td(Number(item.take_price).toLocaleString("tr-TR",{maximumFractionDigits:8})),td(money(item.notional_usd)));
    body.append(row);
  }
}
function render(data) {
  latest=data;
  $("server-time").textContent=`UTC ${timeText(data.server_time)}`;
  const state=data.state || "unknown";
  const badge=$("engine-state");
  badge.className=`state-pill ${state === "running" || state === "observe" ? "" : state === "starting" ? "warn" : "bad"}`;
  badge.textContent=state === "running" ? "AKTİF" : state === "observe" ? "İZLEME" : state === "starting" ? "BAŞLATILIYOR" : "DURUM UYARISI";
  $("engine-message").textContent=data.message || "—";
  $("last-cycle").textContent=timeText(data.last_cycle);
  $("equity").textContent=money(data.equity_usd);
  $("equity-at").textContent=data.equity_at ? `Son gözlem: ${timeText(data.equity_at)} UTC` : "Henüz paper özsermaye kaydı yok";
  $("day-return").textContent=percent(data.day_return_pct);setTone($("day-return"),data.day_return_pct);
  $("test-return").textContent=percent(data.test_return_pct);setTone($("test-return"),data.test_return_pct);
  $("closed-count").textContent=data.closed_positions ?? "—";
  $("realized-pnl").textContent=`Gerçekleşen PnL: ${money(data.realized_pnl_usd)}`;
  renderChart(data.equity_curve);
  const risk=$("risk-state");
  const halted=data.fast_halt || data.storage_error || state === "error" || state === "setup";
  risk.className=`risk-state ${halted ? "bad" : data.bot_enabled ? "" : "warn"}`;
  risk.textContent=halted ? "Yeni giriş durduruldu" : data.bot_enabled ? "Koruma aktif" : "İzleme modu";
  $("risk-description").textContent=data.fast_halt || data.storage_error || (data.bot_enabled ? (data.allow_pending_wfa ? "WFA beklerken geçici paper sinyalleri açık. Risk ve sanal stop kontrolleri sürer." : "WFA onayı gereklidir. Sanal stop ve hedef 5 saniyede bir kontrol edilir.") : "BOT_ENABLED=false. Yeni paper girişleri kapalı.");
  $("trading-mode").textContent=data.bot_enabled ? "Sanal girişler açık" : "Yeni girişler kapalı";
  $("paper-cash").textContent=money(data.paper_cash_usdt);
  $("wfa-state").textContent=data.wfa_in_progress || "Beklemede";
  const quick=data.quick_backtests || {};
  $("quick-count").textContent=`${Object.values(quick).reduce((a,b)=>a+Number(b||0),0)} test · ${quick.PASS||0} olumlu`;
  const full=data.full_backtests || {};
  $("full-count").textContent=`${full.DONE||0}/${data.universe_count||0}${data.full_backtest_symbol ? ` · ${data.full_backtest_symbol}` : ""}`;
  $("eligible-count").textContent=data.eligible_count ?? "—";
  $("test-start").textContent=timeText(data.test_start);
  renderDecisions(data.decisions);
  renderPositions(data.open_positions);
  symbols=Array.isArray(data.symbols) ? data.symbols : [];
  $("universe-count").textContent=data.universe_count ?? symbols.length;
  renderSymbols($("symbol-search").value);
  renderOrders(data.orders);
}
async function refresh() {
  try {
    const response=await fetch("/api/status",{cache:"no-store"});
    if(response.status===401){window.location.href="/";return;}
    if(!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch(error) {
    $("engine-state").textContent="BAĞLANTI HATASI";
    $("engine-state").className="state-pill bad";
    $("engine-message").textContent=`Panel verisi alınamadı: ${error.message}`;
  }
}
$("symbol-search").addEventListener("input",event=>renderSymbols(event.target.value));
refresh();
setInterval(refresh,12000);
