"use strict";
const $ = (selector) => document.querySelector(selector);
let profile, catalog = [], feed = [], selected = new Set(), toastTimer;
const esc = (value) => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
async function api(path, options = {}) {
  const response = await fetch('/api/demo/' + path, {headers: {'Content-Type':'application/json'}, ...options});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Проверь поля анкеты: возраст, расписание и выбор игр.');
  return result;
}
function toast(text) { $('#toast').textContent = text; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 4000); }
async function changeTab(tab) {
  for (const view of document.querySelectorAll('.view')) view.hidden = view.id !== tab + '-view';
  for (const nav of document.querySelectorAll('.nav')) nav.classList.toggle('active', nav.dataset.tab === tab);
  if (tab === 'matches') await renderMatches();
  if (tab === 'profile') fillForm();
}
function chips(values) { return values.map(value => `<span class="chip">${esc(value)}</span>`).join(''); }
function renderCard() {
  $('#feed-count').textContent = feed.length ? `${feed.length} АНКЕТ` : 'НОВЫХ АНКЕТ НЕТ';
  if (!feed.length) { $('#cards').innerHTML = '<div class="empty"><h2>Пока всё ✳</h2><p>Измени интересы или начни демо заново.<br>В настоящем боте здесь будут новые люди.</p></div>'; return; }
  const item = feed[0], p = item.profile;
  $('#cards').innerHTML = `<article class="card"><div class="card-art"><span class="status">В поиске своих</span><span class="fit-badge">${esc(item.score)} / 100 сходство</span><span class="card-avatar">${esc(p.name.slice(0,1))}</span><span class="art-caption">PLAYER ${String(p.id).padStart(3,'0')} / READY TO CONNECT</span></div><div class="card-body"><div class="person-top"><h2>${esc(p.name)}, ${p.age}</h2><small>18+</small></div><div class="person-meta">${esc(p.purpose_label)} · ${esc(p.timezone.replaceAll('_',' '))}</div><p class="bio">${esc(p.bio)}</p><div class="chips">${chips(p.games)}</div><div class="shared-box"><span class="small-label">${item.common_games.length ? '✳ ВЫ НА ОДНОЙ ВОЛНЕ' : '✳ БЛИЗКИЕ ИГРОВЫЕ ИНТЕРЕСЫ'}</span><div class="chips">${chips(item.common_games.length ? item.common_games : item.common_genres.slice(0,3))}</div></div><p class="time-note">◷ ${item.overlap_hours ? `Около ${item.overlap_hours} ч общего времени каждый день` : 'Пока без пересечения расписания'} · ${String(p.start_hour).padStart(2,'0')}:00–${String(p.end_hour).padStart(2,'0')}:00 по местному</p></div><div class="card-actions"><button class="secondary" data-action="skip">Пропустить →</button><button class="primary" data-action="like">♡ Нравится</button><button class="report-button" data-action="report" aria-label="Пожаловаться на анкету">Жалоба</button></div></article>`;
  for (const button of $('#cards').querySelectorAll('[data-action]')) button.addEventListener('click', () => react(button.dataset.action));
}
async function react(kind) {
  if (!feed.length) return;
  const target = feed[0].profile;
  if (kind === 'report' && !confirm('Отправить жалобу и скрыть эту вымышленную анкету?')) return;
  const buttons = $('#cards').querySelectorAll('button'); buttons.forEach(button => button.disabled = true);
  try {
    const result = await api('reaction', {method:'POST', body:JSON.stringify({target_id:target.id, kind})});
    feed.shift(); renderCard();
    if (result.matched) { $('#match-text').textContent = `${target.name} тоже хочет познакомиться. Теперь можно начать с общей игры.`; $('#match-dialog').showModal(); }
    if (kind === 'report') toast('Жалоба сохранена. Анкета скрыта.');
    await updateMatchesCount();
  } catch(error) { toast(error.message); buttons.forEach(button => button.disabled = false); }
}
async function refreshFeed() { feed = await api('feed'); renderCard(); }
async function updateMatchesCount() { const matches = await api('matches'); $('#match-count').textContent = matches.length; return matches; }
async function renderMatches() {
  const matches = await updateMatchesCount();
  $('#match-list').innerHTML = matches.length ? matches.map(item => `<article class="match-card"><div class="eyebrow">♡ ВЗАИМНО</div><h2>${esc(item.profile.name)}, ${item.profile.age}</h2><p>${esc(item.profile.bio)}</p><div class="chips">${chips(item.profile.games)}</div><p>Общение через бота доступно в Telegram-версии.</p></article>`).join('') : '<div class="empty"><h2>Здесь пока тихо ♡</h2><p>Лайкни кого-нибудь в ленте.<br>Для демонстрации у Миры и Дани есть встречный лайк.</p></div>';
}
function fillForm() {
  const form = $('#profile-form');
  for (const [key, value] of Object.entries(profile)) if (form.elements.namedItem(key)) form.elements.namedItem(key).value = value;
  selected = new Set(profile.game_ids); $('#game-search').value = ''; renderGameOptions('');
}
function renderGameOptions(query) {
  const options = catalog.filter(game => game.name.toLowerCase().includes(query.toLowerCase()));
  $('#game-options').innerHTML = options.map(game => `<button type="button" class="game-option ${selected.has(game.app_id) ? 'selected' : ''}" data-game="${game.app_id}" aria-pressed="${selected.has(game.app_id)}">${selected.has(game.app_id) ? '✓ ' : ''}${esc(game.name)}</button>`).join('');
  for (const button of $('#game-options').querySelectorAll('button')) button.addEventListener('click', () => {
    const id = Number(button.dataset.game);
    if (selected.has(id)) selected.delete(id); else if (selected.size < 10) selected.add(id); else { toast('Можно выбрать до 10 игр'); return; }
    renderGameOptions($('#game-search').value);
  });
}
function renderProfile() { $('#side-name').textContent = profile.name; $('#my-games').innerHTML = chips(profile.games); }
$('#profile-form').addEventListener('submit', async event => {
  event.preventDefault(); const form = new FormData(event.target); const payload = Object.fromEntries(form.entries());
  for (const key of ['age','age_min','age_max','start_hour','end_hour']) payload[key] = Number(payload[key]);
  payload.game_ids = [...selected];
  try { profile = await api('profile', {method:'PUT',body:JSON.stringify(payload)}); renderProfile(); await refreshFeed(); toast('Анкета сохранена. Подбор обновлён.'); await changeTab('feed'); }
  catch(error) { $('#form-status').textContent = error.message; }
});
$('#game-search').addEventListener('input', event => renderGameOptions(event.target.value));
for (const button of document.querySelectorAll('[data-tab]')) button.addEventListener('click', () => changeTab(button.dataset.tab).catch(error => toast(error.message)));
$('#reset').addEventListener('click', async () => { try { await api('reset', {method:'POST'}); await refreshFeed(); await updateMatchesCount(); toast('Демонстрация началась заново'); } catch(error) { toast(error.message); } });
$('.dialog-close').addEventListener('click', () => $('#match-dialog').close());
$('#view-matches').addEventListener('click', () => { $('#match-dialog').close(); changeTab('matches').catch(error => toast(error.message)); });
async function init() { [profile, catalog] = await Promise.all([api('profile'), api('catalog')]); renderProfile(); await Promise.all([refreshFeed(), updateMatchesCount()]); }
init().catch(error => { $('#cards').innerHTML = '<div class="empty">Не удалось загрузить демо. Проверь, запущен ли сервер.</div>'; toast(error.message); });
