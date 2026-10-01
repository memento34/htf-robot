const $ = id => document.getElementById(id);
const fmt = (n, d=2) => Number(n).toLocaleString('en-US', {minimumFractionDigits:d,maximumFractionDigits:d});
const money = n => `${fmt(n)} USDT`;
function el(tag, cls, content) { const node=document.createElement(tag); if(cls) node.className=cls; if(content!==undefined) node.textContent=content; return node; }
function signed(node, value) { node.classList.toggle('negative', value<0); node.classList.toggle('positive', value>0); }
function render(s) {
  $('equity').textContent=money(s.equity); $('cash').textContent=money(s.cash);
  $('unrealized').textContent=money(s.unrealized); signed($('unrealized'),s.unrealized);
  $('drawdown').textContent=fmt(s.drawdown_pct)+'%';
  $('return').textContent=`Başlangıç ${money(s.start_cash)} · ${s.return_pct>=0?'+':''}${fmt(s.return_pct)}% · Sanal hesap`;
  $('clock').textContent=new Date(s.server_time).toLocaleString('tr-TR',{timeZone:'UTC'})+' UTC';
  $('last-refresh').textContent='Son veri: '+new Date().toLocaleTimeString('tr-TR');
  $('engine-status').textContent=s.last_error?'Piyasa verisi bekleniyor':'Motor çalışıyor';
  $('position-count').textContent=s.positions.length+' açık'; $('event-count').textContent=s.events.length+' kayıt';
  $('error-box').classList.toggle('hidden', !s.last_error); $('error-box').textContent=s.last_error || '';
  const markets=$('markets'); markets.replaceChildren(); markets.classList.remove('empty');
  s.symbols.forEach(symbol=>{ const mark=s.marks[symbol]; const row=el('div','market-row'); const left=el('div');
    left.append(el('b','',symbol),el('small','',mark?'Son gözlem: '+new Date(mark.market_ts).toLocaleTimeString('tr-TR'):'Fiyat bekleniyor'));
    row.append(left,el('strong','',mark?fmt(mark.price):'—'));markets.append(row); });
  const positions=$('positions'); positions.replaceChildren(); positions.classList.remove('empty');
  if(!s.positions.length) positions.append(el('div','empty','Henüz açık pozisyon yok. İlk kapalı mum ve risk kuralları bekleniyor.'));
  s.positions.forEach(p=>{const row=el('div','position-row'),left=el('div'),name=el('b','',p.symbol),badge=el('span','badge '+(p.side==='SHORT'?'short':''),p.side);
    name.append(badge);left.append(name,el('small','',`Giriş ${fmt(p.entry)} · Stop ${fmt(p.stop)} · Hedef ${fmt(p.take)} · Miktar ${fmt(p.qty,5)}`));
    const right=el('div');right.append(el('strong',p.unrealized>=0?'positive':'negative',(p.unrealized>=0?'+':'')+money(p.unrealized)),el('small','','İşaret '+(p.mark?fmt(p.mark):'—')));row.append(left,right);positions.append(row);});
  const body=$('events');body.replaceChildren();
  if(!s.events.length){const tr=el('tr'),td=el('td','empty-cell','İlk değerlendirme bekleniyor…');td.colSpan=7;tr.append(td);body.append(tr);}
  s.events.forEach(e=>{const tr=el('tr'); const time=el('td','',e.ts.replace('T',' ').slice(0,19));
    const type=el('td'),tag=el('span','tag '+(e.kind==='BLOCK'?'block':e.kind==='DATA'?'data':e.kind==='DECISION'?'decision':''),e.kind);type.append(tag);
    const sym=el('td');sym.append(el('b','',e.symbol));const act=el('td','',e.action);
    const price=el('td','',e.price==null?'—':fmt(e.price));const pnl=el('td',e.pnl<0?'negative':e.pnl>0?'positive':'',e.pnl==null?'—':(e.pnl>=0?'+':'')+fmt(e.pnl));
    const reason=el('td','reason',e.reason);if(e.kind==='DECISION'&&e.details.fast){reason.title=`Hızlı ortalama ${fmt(e.details.fast)} · Yavaş ortalama ${fmt(e.details.slow)} · ATR ${fmt(e.details.atr)}`;}
    tr.append(time,type,sym,act,price,pnl,reason);body.append(tr);});
}
async function refresh(){try{const r=await fetch('/api/state',{cache:'no-store'});if(!r.ok)throw Error('Sunucu yanıt vermedi');render(await r.json());$('connection').textContent='Sistem bağlı';$('connection').classList.remove('off');}
  catch(err){$('connection').textContent='Bağlantı kesildi';$('connection').classList.add('off');$('error-box').textContent=String(err);$('error-box').classList.remove('hidden');}}
refresh();setInterval(refresh,10000);
