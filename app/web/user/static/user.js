const screen = document.querySelector('#screen');
const view = document.body.dataset.view;
const money = n => new Intl.NumberFormat('uz-UZ').format(n || 0) + ' so‘m';
const escapeHtml = value => String(value).replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
const cookie = name => document.cookie.split('; ').find(value => value.startsWith(name + '='))?.split('=')[1] || '';
const win = (title, body) => `<section class="window"><div class="window-title"><span>${title}</span><span>■ □ ×</span></div><div class="window-body">${body}</div></section>`;
const empty = text => `<div class="empty">${text}</div>`;

async function api(path, options = {}) {
  options.headers = {...(options.headers || {}), 'X-CSRF-Token': cookie('us_csrf')};
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({detail: 'Server javobi noto‘g‘ri'}));
  if (!response.ok) throw new Error(data.detail || 'Xato');
  return data;
}

async function login() {
  try { return await api('/web-api/auth/me'); }
  catch {
    const initData = window.Telegram?.WebApp?.initData;
    if (!initData) throw new Error('Ilovani Telegram ichidan oching');
    await api('/web-api/auth/telegram', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({init_data:initData})});
    return api('/web-api/auth/me');
  }
}

async function dashboard() {
  const d = await api('/web-api/user/dashboard');
  screen.innerHTML = `<section class="hero"><small>PLAYER PROFILE</small><h2>Salom, ${escapeHtml(d.name)}</h2><p>Xizmatlar, balans va pixel ferma bitta cho‘ntak konsolida.</p></section>` +
    win('STATUS.EXE', `<div class="stats"><div class="stat">Asosiy balans<b>${money(d.balance_som)}</b></div><div class="stat">Bonus<b>${money(d.bonus_balance_som)}</b></div><div class="stat">Farm ball<b>${d.farm_points}</b></div><div class="stat">Reyting<b>${d.ranking_points}</b></div><div class="stat">Aktiv order<b>${d.active_orders}</b></div></div>`) +
    win('TEZKOR XIZMATLAR', `<div class="service-grid"><a class="service" href="/app/stars"><span class="icon">⭐</span><strong>Stars olish</strong><small>Server hisoblagan xavfsiz narx</small></a><a class="service" href="/app/premium"><span class="icon">💎</span><strong>Premium</strong><small>3, 6 yoki 12 oy</small></a><a class="service" href="/app/gifts"><span class="icon">🎁</span><strong>Gifts</strong><small>Faol sovg‘alar katalogi</small></a><a class="service" href="/app/topup"><span class="icon">💳</span><strong>Hisob to‘ldirish</strong><small>Chek bilan xavfsiz ariza</small></a></div>`);
}

async function catalog(type) {
  const d = await api('/web-api/user/catalog?service_type=' + type);
  const title = type === 'STARS' ? '⭐ TELEGRAM STARS' : type === 'PREMIUM' ? '💎 TELEGRAM PREMIUM' : '🎁 TELEGRAM GIFTS';
  const rows = d.items.map(x => `<article class="list-row"><div><strong>${escapeHtml(x.name)}</strong><div>Bizdagi narx: <span class="price">${money(x.sale_price_som)}</span></div></div><button class="quote-button" data-price-id="${escapeHtml(x.id)}" data-type="${x.type}">Tanlash</button></article>`).join('');
  screen.innerHTML = win(title, d.items.length ? `<div class="list">${rows}</div><p class="notice">Xizmat hozircha test rejimida. Real providerga buyurtma yuborilmaydi.</p>` : empty('Hozir faol narx mavjud emas.'));
  document.querySelectorAll('.quote-button').forEach(button => button.addEventListener('click', () => quote(button.dataset.priceId, button.dataset.type)));
}

async function quote(priceId, type) {
  let quantity = null;
  if (type === 'STARS') { quantity = Number(prompt('Stars miqdori: 50, 100, 250, 500 yoki boshqa', '100')); if (!quantity) return; }
  try {
    const d = await api('/web-api/user/quotes', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({price_id:priceId, quantity})});
    alert(`Jami: ${money(d.sale_price_som)}\n${d.message}`);
  } catch (error) { alert(error.message); }
}

async function balance() {
  const d = await api('/web-api/user/balance');
  const history = d.history.map(x => `<div class="list-row"><span>${x.type}</span><b>${money(x.amount_som)}</b></div>`).join('');
  screen.innerHTML = win('HISOB HOLATI', `<div class="stats"><div class="stat">Mavjud<b>${money(d.available_som)}</b></div><div class="stat">Band qilingan<b>${money(d.reserved_som)}</b></div><div class="stat">Bonus<b>${money(d.bonus_som)}</b></div></div>`) + win('BALANS TARIXI', history ? `<div class="list">${history}</div>` : empty('Balans tarixi bo‘sh.'));
}

async function orders() {
  const d = await api('/web-api/user/orders');
  const rows = d.items.map(x => `<div class="list-row"><div><b>#${x.number}</b><small> ${x.type}</small></div><span class="price">${money(x.sale_price_som)}</span><span>${x.status}</span></div>`).join('');
  screen.innerHTML = win('BUYURTMALAR', rows ? `<div class="list">${rows}</div>` : empty('Hali buyurtma yo‘q.'));
}

async function rewards() {
  const d = await api('/web-api/user/rewards');
  screen.innerHTML = win('BALLAR VA BONUSLAR', `<div class="stats"><div class="stat">Bonus balans<b>${money(d.bonus_som)}</b></div><div class="stat">Farm ball<b>${d.farm_points}</b></div><div class="stat">Reyting ball<b>${d.ranking_points}</b></div></div><p class="notice">Ball va bonuslar sotilmaydi yoki naqdlashtirilmaydi.</p>`);
}

async function rankingPage() {
  const d = await api('/web-api/user/ranking');
  const rows = d.items.map((x, i) => `<div class="list-row"><b>#${i + 1}</b><span>${escapeHtml(x.name)}</span><b>${x.points}</b></div>`).join('');
  screen.innerHTML = win('REYTING', rows ? `<div class="list">${rows}</div>` : empty('Reyting hali bo‘sh.'));
}

async function topup() {
  const payments = await api('/web-api/user/payments');
  const statuses = payments.items.map(x => `<div class="list-row"><span>${money(x.amount_som)}</span><b>${x.status}</b><small>${escapeHtml(x.review_note || '')}</small></div>`).join('');
  screen.innerHTML = win('HISOB TO‘LDIRISH', `<form id="topup"><label>Summa</label><input name="amount" inputmode="numeric" placeholder="50 000" required><button>Davom etish</button></form><div id="topup-result"></div>`) + win('ARIZALAR', statuses ? `<div class="list">${statuses}</div>` : empty('Ariza yo‘q.'));
  document.querySelector('#topup').onsubmit = async event => {
    event.preventDefault();
    try {
      const amount = Number(event.target.amount.value.replaceAll(' ', ''));
      const d = await api('/web-api/user/topup', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({amount_som:amount})});
      document.querySelector('#topup-result').innerHTML = `<div class="notice"><b>${d.card_number}</b><br>${escapeHtml(d.card_holder)}<br>${money(amount)}</div><form id="receipt"><input type="file" name="receipt" accept="image/jpeg,image/png,application/pdf" required><button>Chek yuborish</button></form>`;
      document.querySelector('#receipt').onsubmit = async uploadEvent => {
        uploadEvent.preventDefault();
        const form = new FormData(uploadEvent.target); form.append('csrf', cookie('us_csrf'));
        const response = await fetch(`/web-api/user/topup/${d.payment.id}/receipt`, {method:'POST', body:form});
        const result = await response.json(); if (!response.ok) throw new Error(result.detail); alert('Chek tekshiruvga yuborildi');
      };
    } catch (error) { alert(error.message); }
  };
}

function farmButton(plot) {
  if (plot.state === 'EMPTY') return `<button onclick="farmAct('plant',${plot.slot})">Ekish</button>`;
  if (plot.state === 'WATER_NEEDED') return `<button onclick="farmAct('water',${plot.slot})">Sug‘orish</button>`;
  if (plot.state === 'GROWING' || plot.state === 'READY') return `<button onclick="farmAct('harvest',${plot.slot})">Yig‘ish</button>`;
  return '';
}
async function farm() {
  const d = await api('/web-api/user/farm');
  const plots = d.plots.map(p => `<div class="plot ${p.state}"><b>#${p.slot + 1}</b><p>${p.state}</p>${farmButton(p)}</div>`).join('');
  screen.innerHTML = `<div class="farm-scene" aria-label="Pixel uy, daraxt, quduq va hayvonlari bor ferma"></div>` + win('FERMA RESURSLARI', `<div class="farm-stats"><div class="stat">⚡ Energiya<b>${d.profile.energy}</b></div><div class="stat">💧 Suv<b>${d.profile.water}</b></div><div class="stat">🌾 Urug‘<b>${d.profile.seeds}</b></div><div class="stat">XP / Level<b>${d.profile.xp} / ${d.profile.level}</b></div></div>`) + win('EKIN MAYDONLARI', `<div class="farm-grid">${plots}</div>`);
}
window.farmAct = async (action, slot) => { try { await api('/web-api/user/farm/' + action, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({slot})}); await farm(); } catch (error) { alert(error.message); } };

async function profile(me) { screen.innerHTML = win('PROFIL', `<div class="stat">Ism<b>${escapeHtml(me.full_name || '—')}</b></div><div class="stat">Username<b>@${escapeHtml(me.username || '—')}</b></div><div class="stat">Telegram ID<b>${me.telegram_id}</b></div>`); }

async function boot() {
  try {
    const me = await login(); window.Telegram?.WebApp?.ready();
    if (view === 'dashboard') return dashboard();
    if (view === 'catalog-stars') return catalog('STARS');
    if (view === 'catalog-premium') return catalog('PREMIUM');
    if (view === 'catalog-gift') return catalog('GIFT');
    if (view === 'balance') return balance();
    if (view === 'topup') return topup();
    if (view === 'orders') return orders();
    if (view === 'farm') return farm();
    if (view === 'bonuses' || view === 'points') return rewards();
    if (view === 'ranking') return rankingPage();
    if (view === 'profile') return profile(me);
    screen.innerHTML = win('YORDAM', empty('Yordam uchun Telegram botdagi support bo‘limiga yozing.'));
  } catch (error) { screen.innerHTML = win('KIRISH XATOSI', `<p>${escapeHtml(error.message)}</p><a class="btn" href="/app">Qayta urinish</a>`); }
}
boot();
