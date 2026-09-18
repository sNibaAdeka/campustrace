const $ = (id) => document.getElementById(id);
const state = { profile: null, extras: null, filter: 'all', compare: null, researching: false, loaderStartedAt: 0 };
const searchSequence = { 'search-results': 0, 'home-search-results': 0, 'compare-search-results': 0 };
const searchTimers = {};
let profileSequence = 0;
let loaderTimer = null;
let loaderExitTimer = null;
const labels = { campus: 'Кампус', dormitory: 'Общежитие', classroom: 'Аудитории', library: 'Библиотека', city: 'Город', sports: 'Спорт', laboratories: 'Лаборатории', student_life: 'Студенческая жизнь', unknown: 'Требует проверки' };
const categories = ['campus', 'dormitory', 'classroom', 'library', 'city', 'sports', 'laboratories', 'student_life'];
// 'unknown' is deliberately a visible bucket rather than a silent deletion: the
// material has a real source and licence, we simply cannot say what it shows.
const filters = ['all', ...categories, 'unknown'];
const evidenceLabels = { wikidata_type: 'Wikidata: здание вуза', wikidata_image: 'Wikidata: фото вуза', depicts: 'Commons: depicts', category: 'Категория Commons', name_in_text: 'Название в файле', geo_near: 'Геотег у кампуса', vision: 'Проверено по изображению' };
const statusLabels = { city_context: 'Городской контекст', probable: 'Вероятно', unknown: 'Не подтверждено' };

function node(tag, text, cls) {
  const item = document.createElement(tag);
  if (text != null) item.textContent = String(text);
  if (cls) item.className = cls;
  return item;
}
function link(label, url) {
  let parsed;
  try { parsed = new URL(url); }
  catch { return node('span', label); }
  if (!['http:', 'https:'].includes(parsed.protocol)) return node('span', label);
  const item = node('a', label);
  item.href = parsed.href;
  item.target = '_blank';
  item.rel = 'noopener noreferrer';
  return item;
}
function status(message, tone = 'info') {
  $('status').textContent = message;
  $('status').dataset.tone = tone;
}
const loaderPhrases = [
  'Ищем университет в реестрах…',
  'Находим здания кампуса…',
  'Собираем фото с открытой лицензией…',
  'Проверяем источники и права…',
  'Убираем дубликаты…',
  'Раскладываем по разделам…',
];
function showExplorer() {
  $('landing').hidden = true;
  $('explorer-shell').hidden = false;
  document.body.classList.add('has-profile');
  window.setTimeout(() => window.CampusAtlas?.resize?.(), 40);
}
function stopResearch(error = false) {
  clearTimeout(loaderExitTimer);
  document.body.classList.remove('is-researching');
  window.clearInterval(loaderTimer); loaderTimer = null;
  $('research-loader').hidden = true;
  state.researching = false;
  if (error) {
    $('explorer-shell').hidden = true;
    $('landing').hidden = false;
    document.body.classList.remove('has-profile');
  }
}
function beginResearch(item) {
  if (location.protocol === 'file:') {
    location.href = `http://127.0.0.1:8765/#${item.ror_id}`;
    return;
  }
  state.researching = true;
  document.body.classList.add('is-researching');
  clearTimeout(loaderExitTimer);
  state.loaderStartedAt = Date.now();
  $('home-search-results').replaceChildren();
  showExplorer();
  $('research-loader').hidden = false;
  $('loader-title').textContent = `Собираем профиль: ${item.name}`;
  let position = 0;
  const phrase = $('loader-phrase'); phrase.textContent = loaderPhrases[position];
  window.clearInterval(loaderTimer);
  loaderTimer = window.setInterval(() => {
    phrase.classList.add('is-changing');
    window.setTimeout(() => { position = (position + 1) % loaderPhrases.length; phrase.textContent = loaderPhrases[position]; phrase.classList.remove('is-changing'); }, 180);
  }, 3000);
  window.scrollTo({ top: 0, behavior: 'instant' });
  loadPreview(item.ror_id);
  loadProfile(item.ror_id);
}
// First licensed photographs from structured sources (Wikidata) while the
// full build runs. Each thumbnail links to its primary source.
async function loadPreview(rorId) {
  const holder = $('loader-preview'); holder.hidden = true; holder.replaceChildren();
  const sequence = profileSequence + 1;
  try {
    const data = await api(`/api/profiles/${rorId}/preview`);
    if (sequence !== profileSequence || !state.researching || !data.assets?.length) return;
    const list = node('ul');
    for (const item of data.assets.slice(0, 12)) {
      const li = node('li'), a = node('a'), img = node('img');
      a.href = item.source_url; a.target = '_blank'; a.rel = 'noopener noreferrer';
      img.src = String(item.image_url).replace('/960px-', '/330px-'); img.alt = item.title; img.decoding = 'async';
      a.append(img); li.append(a); list.append(li);
    }
    holder.append(node('p', `Первые ${Math.min(12, data.assets.length)} кадров с лицензией и источником${data.from_cache ? '' : ` — за ${(data.elapsed_ms / 1000).toFixed(1)} с`}. Полная проверка продолжается…`), list);
    holder.hidden = false;
  } catch (_) { /* the full profile still arrives; the preview is optional */ }
}
async function api(url) {
  const base = location.protocol === 'file:' ? 'http://127.0.0.1:8765' : '';
  let response = await fetch(base + url);
  // A dependent endpoint can be asked a moment before the profile is stored.
  if (response.status === 404 && /\/(extras|student-voices)$/.test(url)) {
    await new Promise(resolve => window.setTimeout(resolve, 1500));
    response = await fetch(base + url);
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}
function institutionLabel(item) { return [item.name, item.city, item.country].filter(Boolean).join(' · '); }

async function search(query, resultId, onChoose, quiet = false, page = 1, submit = false) {
  clearTimeout(searchTimers[resultId]);
  const holder = $(resultId);
  if (page === 1) holder.replaceChildren(node('p', 'Ищем университеты…', 'search-message'));
  holder.querySelector('.search-more')?.remove();
  const sequence = ++searchSequence[resultId];
  if (query.trim().length < 2) return;
  if (!quiet) status('Ищу университеты…', 'loading');
  try {
    const data = await api(`/api/search/suggest?q=${encodeURIComponent(query)}&page=${page}`);
    if (sequence !== searchSequence[resultId]) return;
    if (page === 1) holder.replaceChildren();
    if (submit && data.results.length && (data.results.length === 1 || data.results[0].match === 'точное совпадение')) {
      onChoose(data.results[0]); return;
    }
    if (data.warning) holder.append(node('p', data.warning, 'search-message'));
    if (!data.results.length) holder.append(node('p', 'Совпадений нет. Попробуйте полное название или другой язык.'));
    for (const item of data.results) {
      const button = node('button', null, 'result');
      button.append(node('strong', item.name), node('small', [item.city, item.country, item.match].filter(Boolean).join(' · ')));
      button.type = 'button';
      button.addEventListener('click', () => { holder.replaceChildren(); onChoose(item); });
      holder.append(button);
    }
    if (data.has_more) {
      const more = node('button', 'Показать ещё', 'search-more'); more.type = 'button';
      more.addEventListener('click', () => search(query, resultId, onChoose, true, page + 1)); holder.append(more);
    }
    if (!quiet) status(data.ambiguous ? 'Проверьте город и выберите нужную организацию.' : 'Выберите организацию.');
  } catch (error) { if (sequence === searchSequence[resultId]) { holder.replaceChildren(node('p', `Поиск недоступен: ${error.message}. Повторите.`, 'search-message')); status(`Ошибка поиска: ${error.message}`, 'error'); } }
}

async function loadProfile(rorId, refresh = false) {
  const sequence = ++profileSequence;
  window.CampusAtlas?.load(rorId);
  status('Собираю профиль: источники, права, категории и дубли…', 'loading');
  $('profile').setAttribute('aria-busy', 'true');
  try {
    const profile = await api(`/api/profiles/${rorId}${refresh ? '?refresh=true' : ''}`);
    if (sequence !== profileSequence) return;
    state.profile = profile; state.extras = null; state.filter = 'all';
    history.replaceState(null, '', `#${rorId}`);
    renderProfile();
    $('profile').hidden = false;
    document.body.classList.add('has-profile');
    window.CampusAtlas?.load(rorId);
    $('extras').replaceChildren(node('p', 'Загружаем дополнительные источники.', 'hint'));
    $('student-voices').replaceChildren(node('p', 'Ищу публичные обсуждения студентов и общежитий…', 'hint'));
    status(profile.from_cache ? 'Профиль загружен из кэша.' : `Профиль собран за ${Math.round(profile.elapsed_ms / 1000)} с.`, 'success');
    loadExtras(rorId);
    loadVoices();
    if (state.researching) {
      const remaining = Math.max(0, 3200 - (Date.now() - state.loaderStartedAt));
      loaderExitTimer = window.setTimeout(() => { if (sequence !== profileSequence) return; stopResearch(); window.scrollTo({ top: 0, behavior: 'instant' }); }, remaining);
    }
  } catch (error) {
    if (sequence !== profileSequence) return;
    status(`Не удалось собрать профиль: ${error.message}`, 'error');
    if (state.researching) { stopResearch(true); $('home-search-results').replaceChildren(node('p', `Не удалось загрузить университет: ${error.message}. Повторите.`, 'search-message')); }
  }
  finally { if (sequence === profileSequence) $('profile').removeAttribute('aria-busy'); }
}

// A stable colour per platform, so the same platform reads the same everywhere.
const PLATFORM_COLORS = { Reddit: '#ff8b60', Quora: '#ff9a8f', YouTube: '#ff7a7a', Facebook: '#8fb3ff', Instagram: '#f59ad0',
  'The Student Room': '#9fd3ff', Niche: '#9ee6b8', Unigo: '#c4b5ff', StudentCrowd: '#ffd27a', 'Официальный сайт вуза': '#cfd3dc',
  Telegram: '#8fd3ff', VK: '#9fb8ff', X: '#e5e7eb', LinkedIn: '#8fc2ff', 'Википедия': '#e5e7eb' };
const KIND_LABELS = { forum: 'форум', review_site: 'сайт отзывов', video: 'видео', blog: 'блог', social: 'соцсеть',
  news: 'СМИ', official: 'вуз о себе', reference: 'справка', web: 'веб' };
function platformDot(name) {
  const dot = node('span', (name || '?').replace(/^www\./, '').slice(0, 1).toUpperCase(), 'platform-dot');
  dot.style.setProperty('--p', PLATFORM_COLORS[name] || '#cfd3dc');
  dot.setAttribute('aria-hidden', 'true');
  return dot;
}
function sourceRefs(ids, sources) {
  const sup = node('sup');
  for (const id of ids || []) { const s = sources.find(x => x.id === id); if (s) { const a = link(`[${id}]`, s.url); a.title = s.title; sup.append(a); } }
  return sup;
}

function renderVoices(holder, data) {
  holder.replaceChildren();
  if (!data.available) { holder.append(node('p', data.reason, 'hint')); return; }
  holder.append(node('p', data.summary, 'voices-summary'));
  const sources = data.sources || [];
  if (data.platforms?.length) {
    const list = node('ul', null, 'voices-platforms'); list.setAttribute('aria-label', 'Где найдены обсуждения');
    for (const name of data.platforms) {
      const count = sources.filter(s => s.platform === name).length;
      const li = node('li'); li.append(platformDot(name), node('span', `${name} · ${count}`)); list.append(li);
    }
    holder.append(list);
  }
  if (data.pros?.length || data.cons?.length) {
    const grid = node('div', null, 'pros-cons');
    for (const [key, title, icon, items] of [['pros', 'Что хвалят', 'plus', data.pros], ['cons', 'На что жалуются', 'minus', data.cons]]) {
      const box = node('div', null, key); box.append(iconNode('h4', icon, title));
      const ul = node('ul');
      if (!items?.length) ul.append(node('li', 'В найденных источниках прямо не сказано.', 'hint'));
      for (const item of items || []) { const li = node('li', item.text); li.append(sourceRefs(item.source_ids, sources)); ul.append(li); }
      box.append(ul); grid.append(box);
    }
    holder.append(grid);
  }
  if (data.themes?.length && data.ai_available) {
    const themes = node('div', null, 'voice-themes');
    for (const item of data.themes) {
      const article = node('article');
      const p = node('p', item.finding); p.append(sourceRefs(item.source_ids, sources));
      article.append(node('h4', item.title), p);
      themes.append(article);
    }
    holder.append(themes);
  }
  if (sources.length) {
    const cards = node('div', null, 'voice-cards');
    for (const source of sources) {
      const card = node('a', null, `voice-card${source.kind === 'official' ? ' official' : ''}`);
      card.href = source.url; card.target = '_blank'; card.rel = 'noopener noreferrer';
      const head = node('header');
      head.append(platformDot(source.platform), node('strong', source.platform || source.provider));
      if (source.community) head.append(node('span', `r/${source.community}`));
      if (source.date) head.append(node('span', String(source.date).slice(0, 10)));
      head.append(node('span', KIND_LABELS[source.kind] || 'веб', 'voice-kind'));
      card.append(node('span', `[${source.id}]`, 'source-num'), head, node('h5', source.title));
      if (source.excerpt && source.excerpt.trim() !== '…') card.append(node('p', source.excerpt));
      cards.append(card);
    }
    holder.append(cards);
  }
  holder.append(node('p', [data.caveat, data.media_policy].filter(Boolean).join(' '), 'hint'));
}

async function loadVoices() {
  const profile = state.profile;
  if (!profile) return;
  const holder = $('student-voices');
  holder.replaceChildren(node('p', 'ИИ просматривает форумы, сайты отзывов, студенческие СМИ и карты. Это займёт до 40 секунд — галерея уже доступна.', 'hint'));
  $('voices-refresh').disabled = true;
  try {
    const data = await api(`/api/profiles/${profile.institution.ror_id}/student-voices`);
    if (state.profile?.institution.ror_id !== profile.institution.ror_id) return;
    renderVoices(holder, data);
    status(`Исследование отзывов готово за ${Math.max(1, Math.round(data.elapsed_ms / 1000))} с.`, 'success');
  } catch (error) { if (state.profile?.institution.ror_id === profile.institution.ror_id) holder.replaceChildren(node('p', `Отзывы недоступны: ${error.message}`, 'hint')); }
  finally { if (state.profile?.institution.ror_id === profile.institution.ror_id) $('voices-refresh').disabled = false; }
}

function renderProfile() {
  const p = state.profile, inst = p.institution;
  $('institution-name').textContent = inst.name;
  $('institution-meta').replaceChildren(node('span', [inst.city, inst.country].filter(Boolean).join(', ') + ' · '));
  if (inst.official_website) $('institution-meta').append(link('Официальный сайт', inst.official_website));
  $('summary').textContent = p.summary;
  const parts = [`ROR: ${inst.ror_id}`, `кандидатов: ${p.candidate_count}`,
    `показано: ${p.assets.length}`, `удалено дублей: ${p.duplicate_count}`];
  if (p.vision?.available) parts.push(`визуально проверено: ${p.vision.checked}`);
  if (typeof p.elapsed_ms === 'number') parts.push(`собрано за ${(p.elapsed_ms / 1000).toFixed(1)} с`);
  parts.push(`обновлено: ${new Date(p.generated_at * 1000).toLocaleString('ru-RU')}`);
  $('profile-meta').textContent = parts.join(' · ');
  // Technical notes (hashing, quotas, skipped sources) are kept visible for
  // the jury but folded: the student sees one line, not a wall of warnings.
  const notes = p.warnings || [];
  const log = node('details', null, 'tech-log glass');
  log.append(node('summary', `Журнал проверки: ${notes.length} ${notes.length === 1 ? 'запись' : notes.length < 5 ? 'записи' : 'записей'} — что проверено, что пропущено и почему`));
  const body = node('div', null, 'tech-body'); for (const w of notes) body.append(node('p', w)); log.append(body);
  $('warnings').replaceChildren(...(notes.length ? [log] : []));
  renderProfileStatus(); renderFacts(); renderHero();
  renderFilters(); renderGallery(); renderCoverage(); renderFunnel();
}

function renderProfileStatus() {
  const p = state.profile, holder = $('profile-status');
  if (!holder) return;
  holder.replaceChildren();
  const partial = p.profile_status === 'partial';
  holder.dataset.state = partial ? 'partial' : 'complete';
  holder.append(node('strong', partial ? 'Профиль неполный' : 'Профиль собран полностью'));
  holder.append(node('span', partial
    ? 'Часть источников не ответила, поэтому ноль в разделе не означает отсутствие материалов.'
    : 'Все подключённые источники ответили; нули ниже — это проверенное отсутствие материалов.'));
  const check = p.vision?.available
    ? `Независимая визуальная проверка: ${p.vision.model}.`
    : 'Независимая визуальная проверка не выполнена (не задан ключ) — категории основаны только на тексте источника.';
  holder.append(node('span', check));
}

// Case requirement 7: the description is assembled from what was actually
// found, and each object it names links to the file that justifies it.
function renderFacts() {
  const holder = $('campus-facts');
  if (!holder) return;
  holder.replaceChildren();
  const facts = state.profile.campus_facts || [];
  if (!facts.length) return;
  for (const fact of facts) {
    const article = node('article', null, 'fact-card');
    article.append(node('h4', `${fact.label} · ${fact.count}`));
    const list = node('ul');
    for (const example of fact.examples || []) {
      const row = node('li');
      row.append(link(example.title, example.source_url));
      row.append(node('span', ` — ${example.license || 'лицензия не указана'}`, 'hint'));
      list.append(row);
    }
    article.append(list);
    holder.append(article);
  }
}

function renderFilters() {
  const holder = $('filters'); holder.replaceChildren();
  for (const filter of filters) {
    const button = node('button');
    button.type = 'button'; button.setAttribute('aria-pressed', String(state.filter === filter));
    const count = state.profile.assets.filter(a => filter === 'all' || a.category === filter || (a.tags || []).includes(filter)).length;
    button.append(node('span', filter === 'all' ? 'Все материалы' : labels[filter]), node('span', count, 'filter-count'));
    button.addEventListener('click', () => { state.filter = filter; renderFilters(); renderGallery(); });
    holder.append(button);
  }
}

const relText = { high: 'высокая', medium: 'средняя', low: 'низкая' };
const ICON = {
  external: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/></svg>',
  shield: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 5 6v6c0 4.4 3 7.6 7 9 4-1.4 7-4.6 7-9V6z"/><path d="m9 12 2 2 4-4"/></svg>',
  pin: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s7-6.3 7-12a7 7 0 0 0-14 0c0 5.7 7 12 7 12z"/><circle cx="12" cy="9" r="2.5"/></svg>',
  plus: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>',
  minus: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14"/></svg>',
  share: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="18" cy="5" r="2.5"/><circle cx="6" cy="12" r="2.5"/><circle cx="18" cy="19" r="2.5"/><path d="m8.2 10.8 7.6-4.4M8.2 13.2l7.6 4.4"/></svg>',
};
function iconNode(tag, name, text, cls) {
  const item = node(tag, null, cls);
  item.insertAdjacentHTML('afterbegin', ICON[name]); // static, trusted markup only
  if (text) item.append(node('span', text));
  return item;
}
function selectedAssets() {
  return state.profile.assets.filter(a => state.filter === 'all' || a.category === state.filter || (a.tags || []).includes(state.filter));
}
function thumbOf(item, size = 330) {
  return String(item.image_url).includes('/960px-') ? item.image_url.replace('/960px-', `/${size}px-`) : item.image_url;
}

function renderGallery() {
  const holder = $('gallery'); holder.replaceChildren();
  const selected = selectedAssets();
  $('gallery-count').textContent = `${selected.length} ${selected.length % 10 === 1 && selected.length % 100 !== 11 ? 'материал' : [2, 3, 4].includes(selected.length % 10) && ![12, 13, 14].includes(selected.length % 100) ? 'материала' : 'материалов'}`;
  if (!selected.length) { holder.append(node('p', 'Для этого раздела пока нет материалов с указанными источниками и лицензиями.', 'gallery-empty')); return; }
  selected.forEach((item, index) => {
    const card = node('article', null, 'card');
    card.style.setProperty('--i', String(Math.min(index, 14)));
    const image = node('img'); image.src = item.image_url; image.alt = item.title; image.loading = 'lazy'; image.decoding = 'async';
    if (String(item.image_url).includes('/960px-')) {
      image.srcset = `${thumbOf(item)} 330w, ${item.image_url} 960w`;
      image.sizes = '(max-width: 640px) 100vw, 330px';
    }
    // The photo opens the viewer, whose main action is the primary source; the
    // "Источник" button under the photo links the source directly.
    const photo = node('button', null, 'card-photo'); photo.type = 'button';
    photo.setAttribute('aria-label', `Открыть фото и первоисточник: ${item.title}`);
    photo.addEventListener('click', () => openViewer(index));
    photo.append(image);
    let retried = false;
    image.addEventListener('error', () => {
      if (!retried) { retried = true; window.setTimeout(() => { image.removeAttribute('srcset'); image.src = item.image_url + (item.image_url.includes('?') ? '&' : '?') + 'retry=1'; }, 1200); return; }
      image.replaceWith(node('div', 'Превью недоступно — откройте первоисточник.', 'image-fallback'));
    });
    const badges = node('div', null, 'card-badges');
    const level = item.reliability?.level || 'low';
    badges.append(node('span', labels[item.category] || item.category, 'card-badge'),
                  node('span', `Достоверность: ${relText[level]}`, `card-badge rel-${level}`));
    photo.append(badges);

    const body = node('div', null, 'card-body');
    body.append(node('h4', item.title));
    const credit = node('p', null, 'card-credit');
    credit.append(node('span', `${item.author || 'Автор не указан'} · `));
    credit.append(item.license_url ? link(item.license || 'Лицензия', item.license_url) : node('span', item.license || 'Лицензия не указана'));
    credit.append(node('span', ` · ${item.provider}`));
    body.append(credit);
    const evidence = (item.evidence || []).filter(e => e.supports !== false);
    if (evidence.length) {
      const chips = node('ul', null, 'evidence-chips');
      chips.setAttribute('aria-label', `Подтверждений: ${evidence.length}`);
      for (const e of evidence) {
        const chip = node('li', null, 'evidence-chip'); chip.title = e.detail || '';
        chip.append(e.url ? link(evidenceLabels[e.kind] || e.kind, e.url) : node('span', evidenceLabels[e.kind] || e.kind));
        chips.append(chip);
      }
      body.append(chips);
    }
    if (item.vision?.available) {
      const agreement = String(item.vision.agreement || '');
      const tone = agreement.startsWith('confirmed') ? 'confirmed' : agreement.startsWith('conflict') ? 'conflict' : 'partial';
      body.append(node('p', tone === 'confirmed' ? `Изображение проверено ИИ: ${item.vision.scene_label}`
        : tone === 'conflict' ? `ИИ видит другое: «${item.vision.scene_label}»` : `Категория по изображению: ${item.vision.scene_label}`, `vision-badge ${tone}`));
    }
    const actions = node('div', null, 'card-actions');
    const source = iconNode('a', 'external', 'Источник');
    source.href = item.source_url; source.target = '_blank'; source.rel = 'noopener noreferrer';
    const passport = iconNode('button', 'shield', 'Паспорт'); passport.type = 'button';
    passport.addEventListener('click', () => showEvidence(item.id));
    actions.append(source, passport);
    if (item.coordinates) {
      const where = iconNode('button', 'pin', 'На карте'); where.type = 'button';
      where.addEventListener('click', () => window.CampusAtlas?.showPhoto(item.id));
      actions.append(where);
    }
    body.append(actions);
    card.append(photo, body); holder.append(card);
  });
}

// Photo viewer: the large image, author, licence, evidence and the primary
// source as the main action. Arrow keys navigate, Esc closes, focus returns.
let viewerIndex = 0, viewerReturnFocus = null;
function openViewer(index) {
  viewerReturnFocus = document.activeElement;
  viewerIndex = index; renderViewer();
  $('photo-viewer').hidden = false;
  document.body.style.overflow = 'hidden';
  $('viewer-close').focus();
}
function closeViewer() {
  $('photo-viewer').hidden = true; document.body.style.overflow = '';
  viewerReturnFocus?.focus?.();
}
function stepViewer(delta) {
  const list = selectedAssets(); if (!list.length) return;
  viewerIndex = (viewerIndex + delta + list.length) % list.length; renderViewer();
}
function renderViewer() {
  const list = selectedAssets(); const item = list[viewerIndex]; if (!item) return;
  const img = $('viewer-img'); img.src = item.image_url; img.alt = item.title;
  $('viewer-counter').textContent = `${viewerIndex + 1} / ${list.length}`;
  const info = $('viewer-info'); info.replaceChildren();
  const level = item.reliability?.level || 'low';
  info.append(node('p', `${labels[item.category] || item.category} · достоверность ${relText[level]}`, 'eyebrow'), node('h3', item.title));
  const source = iconNode('a', 'external', 'Открыть первоисточник', 'viewer-source');
  source.href = item.source_url; source.target = '_blank'; source.rel = 'noopener noreferrer';
  info.append(source);
  const dl = node('dl');
  const date = item.captured_at || item.published_at;
  for (const [k, v] of [['Автор', item.author], ['Лицензия', item.license], ['Источник', item.provider], ['Дата', date ? String(date).slice(0, 10) : 'не указана']]) dl.append(node('dt', k), node('dd', v || '—'));
  info.append(dl);
  const evidence = item.evidence || [];
  if (evidence.length) {
    info.append(node('h4', 'Почему этот кадр здесь'));
    const ul = node('ul', null, 'viewer-evidence');
    for (const e of evidence) ul.append(node('li', `${e.supports === false ? 'Против: ' : ''}${evidenceLabels[e.kind] || e.kind}${e.detail ? ' — ' + e.detail : ''}`));
    info.append(ul);
  }
  if (item.license_url) info.append(link('Условия лицензии', item.license_url));
  // Preload the neighbours so arrow navigation feels instant.
  for (const d of [1, -1]) { const next = list[(viewerIndex + d + list.length) % list.length]; if (next) { const pre = new Image(); pre.src = next.image_url; } }
}
document.addEventListener('keydown', (event) => {
  if ($('photo-viewer')?.hidden !== false) return;
  if (event.key === 'Escape') closeViewer();
  else if (event.key === 'ArrowRight') stepViewer(1);
  else if (event.key === 'ArrowLeft') stepViewer(-1);
});

// Hero: the best-evidenced photograph behind the name, and the numbers a
// student asks first. Every number is a count from this profile, not a score.
function renderHero() {
  const p = state.profile, inst = p.institution;
  const best = [...p.assets].filter(a => a.category !== 'city').sort((a, b) => (b.evidence_level || 0) - (a.evidence_level || 0))[0];
  document.querySelector('.profile-overview')?.style.setProperty('--hero-image', best ? `url("${thumbOf(best, 960).replace(/"/g, '%22')}")` : 'none');
  let stats = $('hero-stats');
  if (!stats) { stats = node('ul', null, 'hero-stats'); stats.id = 'hero-stats'; document.querySelector('.profile-identity')?.append(stats); }
  stats.replaceChildren();
  const high = p.assets.filter(a => a.reliability?.level === 'high').length;
  const sections = Object.values(p.coverage || {}).filter(Boolean).length;
  const items = [[p.assets.length, 'кадров с лицензией'], [sections, 'разделов из 8'], [high, 'с высокой достоверностью']];
  if (inst.city_center_distance) items.push([`${inst.city_center_distance.km} км`, 'до центра города по прямой']);
  if (typeof p.elapsed_ms === 'number' && !p.from_cache) items.push([`${(p.elapsed_ms / 1000).toFixed(1)} с`, 'время сборки']);
  for (const [value, label] of items) { const li = node('li'); li.append(node('strong', value), node('span', label)); stats.append(li); }
  let actions = $('hero-actions');
  if (!actions) {
    actions = node('div', null, 'hero-actions'); actions.id = 'hero-actions';
    document.querySelector('.profile-identity')?.append(actions);
    const share = iconNode('button', 'share', 'Поделиться профилем', 'glass-button'); share.type = 'button';
    share.addEventListener('click', async () => {
      const url = `${location.origin}/#${state.profile.institution.ror_id}`;
      try { if (navigator.share) await navigator.share({ title: `CampusTrace: ${state.profile.institution.name}`, url }); else { await navigator.clipboard.writeText(url); status('Ссылка на профиль скопирована.', 'success'); } }
      catch (_) { status(url, 'info'); }
    });
    const voices = node('button', 'Что говорят студенты', 'glass-button'); voices.type = 'button';
    voices.addEventListener('click', () => document.querySelector('.voices-section')?.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' }));
    actions.append(share, voices);
  }
}

async function showEvidence(assetId) {
  try {
    const item = await api(`/api/assets/${assetId}?ror_id=${state.profile.institution.ror_id}`);
    const holder = $('evidence-content'); holder.replaceChildren();
    holder.append(node('h3', item.title));
    const dl = node('dl');
    const scopeLabel = item.scope === 'wikidata_image' ? 'Изображение вуза в Wikidata (P18/P8517/P3451/P5775)' : item.scope === 'wikidata_building' ? 'Здание вуза в Wikidata (P18)' : item.scope === 'depicts' ? 'Структурированные данные Commons (depicts)' : item.scope === 'geo' ? 'Геопоиск Commons у точки кампуса' : item.scope === 'category' ? 'Тематическая категория' : item.scope === 'search' ? 'Поиск по названию' : item.scope === 'city' ? 'Городской контекст' : item.scope === 'flickr_search' ? 'Поиск Flickr' : item.scope?.startsWith('category_sub:') ? 'Подкатегория источника' : item.scope;
    const dateLabel = value => {
      if (!value) return 'Неизвестна';
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU');
    };
    const visionText = !item.vision ? 'Не выполнялась'
      : !item.vision.available ? 'Не выполнена для этого файла'
      : `${item.vision.scene_label} (${item.vision.model}; самооценка модели не публикуется как вероятность)`;
    const agreementText = !item.vision?.available ? '—'
      : String(item.vision.agreement).startsWith('confirmed') ? 'Текст и изображение согласуются'
      : String(item.vision.agreement).startsWith('conflict') ? 'Текст и изображение расходятся — уверенность понижена'
      : String(item.vision.agreement).startsWith('vision_only') ? 'Категория получена только из изображения'
      : item.vision.agreement;
    const evidenceText = (item.evidence || []).map(e => `${evidenceLabels[e.kind] || e.kind}${e.supports === false ? ' (не подтверждает)' : ''}: ${e.detail || ''}`).join('; ') || 'Только лицензия и источник';
    for (const [key, value] of Object.entries({ 'Независимые подтверждения': evidenceText, 'Категория': labels[item.category] || item.category, 'Статус': statusLabels[item.status] || item.status, 'Источник': item.provider, 'Контекст поиска': scopeLabel, 'Автор': item.author, 'Лицензия': item.license, 'Дата публикации/загрузки': dateLabel(item.published_at), 'Дата съёмки': dateLabel(item.captured_at), 'Хеш файла': item.sha1 || 'Нет', 'Визуальный хеш': item.dhash || 'Не вычислен', 'Визуальный классификатор': visionText, 'Согласие двух проверок': agreementText })) {
      dl.append(node('dt', key), node('dd', value));
    }
    holder.append(dl, node('h4', 'Основания'));
    const ul = node('ul'); for (const reason of item.reasons) ul.append(node('li', reason));
    holder.append(ul, link('Открыть исходную страницу', item.source_url));
    if (item.license_url) holder.append(node('br'), link('Условия лицензии', item.license_url));
    $('evidence-dialog').showModal();
  } catch (error) { status(`Не удалось открыть доказательства: ${error.message}`, 'error'); }
}

function renderCoverage() {
  const table = node('table', null, 'coverage-table');
  const head = node('tr'); for (const cell of ['Категория', 'Материалов', 'Состояние']) head.append(node('th', cell)); table.append(head);
  for (const category of categories) {
    const count = state.profile.coverage[category] || 0;
    const row = node('tr');
    row.append(node('td', labels[category]));
    const amount = node('td'); amount.append(node('span', count));
    const bar = node('div', null, 'bar'), fill = node('span'); fill.style.width = `${100 * count / Math.max(state.profile.assets.length, 1)}%`; bar.append(fill); amount.append(bar);
    const rawStatus = state.profile.category_status ? state.profile.category_status[category] : null;
    const stateLabel = count ? 'Есть материалы с источником'
      : rawStatus === 'source_failed' ? 'Не проверено: источник был недоступен'
      : 'Проверено — материалов не найдено';
    const stateCell = node('td', stateLabel);
    if (!count && rawStatus !== 'source_failed' && category !== 'city') {
      // An honest gap is also an invitation: anyone can close it on Commons.
      const upload = link('добавить фото на Commons ↗', `https://commons.wikimedia.org/wiki/Special:UploadWizard?categories=${encodeURIComponent(state.profile.institution.name)}`);
      stateCell.append(node('br'), upload);
    }
    row.append(amount, stateCell);
    table.append(row);
  }
  const unclassified = state.profile.unclassified_count || 0;
  if (unclassified) {
    const row = node('tr', null, 'coverage-unknown');
    row.append(node('td', labels.unknown), node('td', String(unclassified)),
               node('td', 'Есть источник и лицензия, но тип объекта не подтверждён'));
    table.append(row);
  }
  const rejected = (state.profile.rejected_by_vision || []).length;
  const holder = $('coverage');
  holder.replaceChildren(table);
  if (rejected) {
    holder.append(node('p', `Визуальный классификатор снял с публикации ${rejected} кандидатов как не относящихся к кампусу.`, 'hint'));
  }
}

function renderFunnel() {
  const holder = $('evidence-funnel'); holder.replaceChildren();
  const p = state.profile;
  const stages = [
    ['Найдено кандидатов', p.candidate_count],
    ['С метаданными и лицензией', p.license_eligible_count ?? p.assets.length + p.duplicate_count],
    ['После удаления дублей', p.unique_count ?? p.assets.length],
    ['Показано в профиле', p.assets.length],
  ];
  const max = Math.max(stages[0][1], 1);
  for (const [label, value] of stages) {
    const row = node('div', null, 'funnel-row');
    row.append(node('span', label), node('span', value, 'funnel-value'));
    const bar = node('div', null, 'bar'), fill = node('span'); fill.style.width = `${100 * value / max}%`; bar.append(fill); row.append(bar);
    holder.append(row);
  }
  holder.append(node('p', 'Это воронка отбора файлов, а не оценка университета. Категория источника ещё не доказывает место съёмки.', 'hint'));
}

async function loadExtras(rorId) {
  try {
    const extras = await api(`/api/profiles/${rorId}/extras`);
    if (state.profile?.institution.ror_id !== rorId) return;
    state.extras = extras; renderExtras();
  } catch (error) {
    if (state.profile?.institution.ror_id === rorId) $('extras').replaceChildren(node('p', `Дополнительные данные недоступны: ${error.message}`));
  }
}

function renderExtras() {
  const holder = $('extras'), extras = state.extras, inst = state.profile.institution;
  // Only a point that is genuinely unconfirmed gets the "unverified" marker.
  // A Wikidata institution point, or one two independent geocoders agree on,
  // is the best we have and must not be drawn as a guess.
  const candidate = extras.campus_candidate;
  if (candidate && candidate.status === 'unverified_map_candidate' && candidate.provider !== 'Wikidata') {
    window.CampusAtlas?.setUnverified(candidate);
  }
  holder.replaceChildren();
  const map = node('article'); map.append(node('h4', 'Карта и перекрёстная проверка координат'));
  if (extras.campus_candidate?.lat && extras.campus_candidate?.lon) {
    const { lat, lon, display_name } = extras.campus_candidate;
    map.append(node('p', display_name));
    map.append(link('Открыть местоположение в OpenStreetMap', `https://www.openstreetmap.org/?mlat=${encodeURIComponent(lat)}&mlon=${encodeURIComponent(lon)}#map=15/${encodeURIComponent(lat)}/${encodeURIComponent(lon)}`));
    map.append(node('p', extras.campus_candidate.warning, 'hint'));
  } else map.append(node('p', 'Подтверждённая точка кампуса не найдена.'));
  const cross = extras.geocode_crosscheck;
  if (cross?.points?.length) {
    const agreementLabel = { confirmed: 'Совпадение независимых геокодеров', conflict: 'Геокодеры расходятся',
                             single_source: 'Только один источник координаты' }[cross.agreement] || cross.agreement;
    map.append(node('p', agreementLabel, `geo-agreement ${cross.agreement}`));
    const list = node('ul', null, 'geo-points');
    for (const point of cross.points) {
      const row = node('li');
      row.append(node('span', `${point.provider}: ${Number(point.lat).toFixed(4)}, ${Number(point.lon).toFixed(4)}`));
      if (point.source_url) { row.append(node('span', ' · ')); row.append(link('запись', point.source_url)); }
      list.append(row);
    }
    map.append(list);
  }
  holder.append(map);

  const research = node('article'); research.append(node('h4', 'Университет в цифрах'));
  if (extras.students) {
    research.append(node('p', `Студенты: ${extras.students.count.toLocaleString('ru-RU')} (${extras.students.year})`));
    research.append(link('Запись Wikidata', extras.students.source));
    if (extras.students.reference_url) research.append(node('br'), link('Первичный источник в записи', extras.students.reference_url));
    research.append(node('p', extras.students.warning, 'hint'));
  }
  if (extras.research?.years?.length) {
    research.append(node('p', extras.research.label));
    const years = extras.research.years.slice(-7), max = Math.max(...years.map(x => x.works_count || 0), 1);
    for (const year of years) {
      research.append(node('div', `${year.year}: ${year.works_count || 0}`));
      const bar = node('div', null, 'bar'), fill = node('span'); fill.style.width = `${100 * (year.works_count || 0) / max}%`; bar.append(fill); research.append(bar);
    }
    research.append(node('p', extras.research.warning, 'hint'), link('Источник: OpenAlex', extras.research.source));
  } else research.append(node('p', 'Сопоставимый временной ряд недоступен.'));
  holder.append(research);

  const video = node('article'); video.append(node('h4', 'Видео'));
  if (extras.videos.length) {
    for (const item of extras.videos) { const frame = node('iframe'); frame.src = `https://www.youtube-nocookie.com/embed/${encodeURIComponent(item.video_id)}`; frame.title = item.title; frame.loading = 'lazy'; frame.allowFullscreen = true; frame.className = 'campus-video'; video.append(frame,link(item.title,item.source_url)); }
  } else video.append(node('p', 'Официальный канал пока не привязан или подходящих роликов нет.'));
  holder.append(video);

  const other = node('article'); other.append(node('h4', 'Контекст города'));
  if (extras.weather?.current) other.append(node('p', `${inst.city}: ${extras.weather.current.temperature_2m} °C сейчас. Это данные города, не кампуса.`), link('Источник: Open-Meteo', extras.weather.source));
  else other.append(node('p', 'Погода недоступна.'));
  for (const lead of extras.official_page_leads.slice(0, 3)) other.append(node('p', null), link(lead.title || lead.url, lead.url), node('p', lead.note, 'hint'));
  holder.append(other);
  if (extras.warnings.length) holder.append(node('p', extras.warnings.join(' · '), 'hint'));
}

async function compareWith(item) {
  if (!state.profile) { status('Сначала откройте первый профиль.'); return; }
  status(`Собираю второй профиль: ${item.name}…`, 'loading');
  try {
    await api(`/api/profiles/${item.ror_id}`);
    const data = await api(`/api/compare?left=${state.profile.institution.ror_id}&right=${item.ror_id}`);
    const table = node('table', null, 'compare-table');
    const header = node('tr'); for (const label of ['Раздел', ...data.profiles.map(x => x.institution.name)]) header.append(node('th', label)); table.append(header);
    // All eight sections are compared, and a zero produced by a dead source is
    // marked so it is never read as a confirmed absence.
    for (const category of (data.categories || categories)) {
      const row = node('tr'); row.append(node('td', labels[category] || category));
      for (const profile of data.profiles) {
        const count = profile.coverage?.[category] || 0;
        const failed = profile.category_status?.[category] === 'source_failed';
        const cell = node('td', `${count} материалов`);
        if (!count && failed) { cell.textContent = '0 — источник не ответил'; cell.className = 'compare-gap'; }
        row.append(cell);
      }
      table.append(row);
    }
    for(const [label,read] of [
      ['Требует проверки',p=>`${p.unclassified_count || 0} материалов`],
      ['Город и страна',p=>[p.institution.city,p.institution.country].filter(Boolean).join(', ')],
      ['Полнота профиля',p=>p.profile_status === 'partial' ? 'Неполный' : 'Полный'],
      ['Визуальная проверка',p=>p.visual_check ? 'Выполнена' : 'Не выполнена'],
      ['Версия пайплайна',p=>p.pipeline_version || '—'],
      ['Обновлено',p=>new Date(p.generated_at*1000).toLocaleString('ru-RU')],
      ['Всего материалов',p=>String(p.asset_count)],
    ]){const row=node('tr');row.append(node('td',label));for(const profile of data.profiles)row.append(node('td',read(profile)));table.append(row);}
    const caveat = node('p', data.caveat, `hint ${data.comparable ? '' : 'compare-warning'}`);
    $('compare-result').replaceChildren(table, caveat, node('p', data.profiles[0].caveat, 'hint'));
    status('Сравнение готово.', 'success');
  } catch (error) { status(`Сравнение не удалось: ${error.message}`, 'error'); }
}

$('home-search-form').addEventListener('submit', e => {
  e.preventDefault();
  const query = $('home-search-input').value.trim();
  if (location.protocol === 'file:') {
    location.href = `http://127.0.0.1:8765/?search=${encodeURIComponent(query)}`;
    return;
  }
  search(query, 'home-search-results', beginResearch, true, 1, true);
});
$('search-form').addEventListener('submit', e => { e.preventDefault(); search($('search-input').value.trim(), 'search-results', beginResearch, false, 1, true); });
$('compare-form').addEventListener('submit', e => { e.preventDefault(); search($('compare-input').value.trim(), 'compare-search-results', compareWith); });
for (const [inputId, resultId, choose] of [
  ['home-search-input', 'home-search-results', beginResearch],
  ['search-input', 'search-results', beginResearch],
  ['compare-input', 'compare-search-results', compareWith],
]) {
  $(inputId).addEventListener('input', () => {
    clearTimeout(searchTimers[resultId]);
    const query = $(inputId).value.trim();
    if (query.length < 2) { $(resultId).replaceChildren(); ++searchSequence[resultId]; return; }
    searchTimers[resultId] = setTimeout(() => search(query, resultId, choose, true), 260);
  });
}
const homeInput = $('home-search-input');
function resizeHomeInput() {
  homeInput.style.height = '28px';
  homeInput.style.height = `${Math.min(Math.max(homeInput.scrollHeight, 28), 118)}px`;
  homeInput.closest('.home-ai-input').classList.toggle('has-value', Boolean(homeInput.value.trim()));
}
homeInput.addEventListener('input', resizeHomeInput);
homeInput.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('home-search-form').requestSubmit(); }
});
resizeHomeInput();
const startupQuery = new URLSearchParams(location.search).get('search');
if (startupQuery && startupQuery.length >= 2) {
  homeInput.value = startupQuery;
  resizeHomeInput();
  search(startupQuery, 'home-search-results', beginResearch, true);
}
$('refresh-button').addEventListener('click', () => state.profile && loadProfile(state.profile.institution.ror_id, true));
$('voices-refresh').addEventListener('click', loadVoices);
$('loader-cancel').addEventListener('click', () => { ++profileSequence; stopResearch(true); history.replaceState(null, '', location.pathname); $('home-search-input').focus(); });
api('/api/integrations').then(data => { $('integration-status').textContent = `Обязательные: ${data.required.join(', ')}. Дополнительно настроено: ${Object.entries(data.optional_configured).filter(([, yes]) => yes).map(([name]) => name).join(', ') || 'ничего'}.`; }).catch(() => {});
if (/^#[0-9a-z]{9}$/.test(location.hash)) { showExplorer(); loadProfile(location.hash.slice(1)); }

// Viewer wiring (elements exist in index.html).
$('viewer-close')?.addEventListener('click', closeViewer);
$('viewer-prev')?.addEventListener('click', () => stepViewer(-1));
$('viewer-next')?.addEventListener('click', () => stepViewer(1));
$('photo-viewer')?.addEventListener('click', (event) => { if (event.target.id === 'photo-viewer') closeViewer(); });
