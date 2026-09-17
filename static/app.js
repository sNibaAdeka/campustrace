const $ = (id) => document.getElementById(id);
const state = { profile: null, extras: null, filter: 'all', compare: null, researching: false, loaderStartedAt: 0 };
const searchSequence = { 'search-results': 0, 'home-search-results': 0, 'compare-search-results': 0 };
const searchTimers = {};
let profileSequence = 0;
let loaderTimer = null;
let loaderExitTimer = null;
const labels = { campus: 'Кампус', dormitory: 'Общежитие', classroom: 'Аудитории', library: 'Библиотека', city: 'Город', sports: 'Спорт', laboratories: 'Лаборатории', student_life: 'Студенческая жизнь' };
const categories = ['campus', 'dormitory', 'classroom', 'library', 'city', 'sports', 'laboratories', 'student_life'];
const filters = ['all', ...categories];

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
  'Locating the university…',
  'Reading the campus footprint…',
  'Finding licensed visual material…',
  'Tracing sources and permissions…',
  'Connecting the evidence map…',
  'Preparing a field guide…',
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
  $('loader-title').textContent = `Building ${item.name}'s field guide.`;
  let position = 0;
  const phrase = $('loader-phrase'); phrase.textContent = loaderPhrases[position];
  window.clearInterval(loaderTimer);
  loaderTimer = window.setInterval(() => {
    phrase.classList.add('is-changing');
    window.setTimeout(() => { position = (position + 1) % loaderPhrases.length; phrase.textContent = loaderPhrases[position]; phrase.classList.remove('is-changing'); }, 180);
  }, 3000);
  window.scrollTo({ top: 0, behavior: 'instant' });
  loadProfile(item.ror_id);
}
async function api(url) {
  const base = location.protocol === 'file:' ? 'http://127.0.0.1:8765' : '';
  const response = await fetch(base + url);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}
function institutionLabel(item) { return [item.name, item.city, item.country].filter(Boolean).join(' · '); }

async function search(query, resultId, onChoose, quiet = false, page = 1, submit = false) {
  clearTimeout(searchTimers[resultId]);
  const holder = $(resultId);
  if (page === 1) holder.replaceChildren(node('p', 'Searching universities…', 'search-message'));
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
      const more = node('button', 'Show more universities', 'search-more'); more.type = 'button';
      more.addEventListener('click', () => search(query, resultId, onChoose, true, page + 1)); holder.append(more);
    }
    if (!quiet) status(data.ambiguous ? 'Проверьте город и выберите нужную организацию.' : 'Выберите организацию.');
  } catch (error) { if (sequence === searchSequence[resultId]) { holder.replaceChildren(node('p', `Search unavailable: ${error.message}. Please retry.`, 'search-message')); status(`Ошибка поиска: ${error.message}`, 'error'); } }
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
    if (state.researching) { stopResearch(true); $('home-search-results').replaceChildren(node('p', `Could not load this university: ${error.message}. Please retry.`, 'search-message')); }
  }
  finally { if (sequence === profileSequence) $('profile').removeAttribute('aria-busy'); }
}

async function loadVoices() {
  const profile = state.profile;
  if (!profile) return;
  const holder = $('student-voices');
  holder.replaceChildren(node('p', 'Ищу публичные обсуждения и сверяю ссылки. Это может занять до 25 секунд.', 'hint'));
  $('voices-refresh').disabled = true;
  try {
    const data = await api(`/api/profiles/${profile.institution.ror_id}/student-voices`);
    if (state.profile?.institution.ror_id !== profile.institution.ror_id) return;
    holder.replaceChildren();
    if (!data.available) { holder.append(node('p', data.reason, 'hint')); return; }
    holder.append(node('p', data.summary, 'voices-summary'));
    const themes = node('div', null, 'voice-themes');
    for (const item of data.themes || []) {
      const article = node('article');
      article.append(node('h4', item.title), node('p', item.finding), node('p', `Уверенность: ${item.confidence || 'низкая'}`, 'hint'));
      for (const id of item.source_ids || []) { const source = data.sources.find(s => s.id === id); if(source) article.append(link(`Источник ${id}`,source.url)); }
      themes.append(article);
    }
    if (data.themes?.length) holder.append(themes);
    if (data.sources?.length) {
      const sourceBox = node('div', null, 'voice-sources'); sourceBox.append(node('h4', 'Ссылки, найденные исследованием'));
      for (const source of data.sources) {
        const article = node('article', null, 'discussion-card');
        article.append(node('p', [source.provider, source.community ? `r/${source.community}` : '', source.date?.slice(0,10)].filter(Boolean).join(' · '), 'hint'), link(source.title, source.url));
        if (source.excerpt) article.append(node('blockquote', source.excerpt));
        sourceBox.append(article);
      }
      holder.append(sourceBox);
    }
    holder.append(node('p', data.caveat, 'hint'));
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
  $('profile-meta').textContent = `ROR: ${inst.ror_id} · кандидатов: ${p.candidate_count} · показано: ${p.assets.length} · удалено дублей: ${p.duplicate_count} · обновлено: ${new Date(p.generated_at * 1000).toLocaleString('ru-RU')}`;
  $('warnings').replaceChildren(...(p.warnings || []).map(w => node('p', w)));
  renderFilters(); renderGallery(); renderCoverage(); renderFunnel();
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

function renderGallery() {
  const holder = $('gallery'); holder.replaceChildren();
  const selected = state.profile.assets.filter(a => state.filter === 'all' || a.category === state.filter || (a.tags || []).includes(state.filter));
  $('gallery-count').textContent = `${selected.length} ${selected.length % 10 === 1 && selected.length % 100 !== 11 ? 'материал' : [2, 3, 4].includes(selected.length % 10) && ![12, 13, 14].includes(selected.length % 100) ? 'материала' : 'материалов'}`;
  if (!selected.length) { holder.append(node('p', 'Для этого раздела пока нет материалов с указанными источниками и лицензиями.', 'gallery-empty')); return; }
  for (const item of selected) {
    const card = node('article', null, 'card');
    const image = node('img'); image.src = item.image_url; image.alt = item.title; image.loading = 'lazy';
    image.addEventListener('error', () => image.replaceWith(node('div', 'Превью недоступно. Источник можно открыть в паспорте материала.', 'image-fallback')), { once: true });
    const body = node('div', null, 'card-body');
    const topline = node('div', null, 'card-topline');
    topline.append(node('span', labels[item.category] || item.category, 'card-type'), node('span', item.status === 'city_context' ? 'Городской контекст' : 'Вероятно', `pill ${item.status}`));
    body.append(topline, node('h4', item.title), node('p', item.provider, 'card-meta'));
    body.append(node('p', `${item.author || 'Автор не указан'} · ${item.license || 'Лицензия не указана'}`, 'card-credit'));
    const button = node('button', 'Проверить источник'); button.type = 'button';
    button.addEventListener('click', () => showEvidence(item.id));
    if(item.coordinates){const mapButton=node('button','Место съёмки ↗','photo-map-button');mapButton.type='button';mapButton.addEventListener('click',()=>window.CampusAtlas?.showPhoto(item.id));body.append(mapButton);}
    body.append(button); card.append(image, body); holder.append(card);
  }
}

async function showEvidence(assetId) {
  try {
    const item = await api(`/api/assets/${assetId}?ror_id=${state.profile.institution.ror_id}`);
    const holder = $('evidence-content'); holder.replaceChildren();
    holder.append(node('h3', item.title));
    const dl = node('dl');
    const scopeLabel = item.scope === 'category' ? 'Тематическая категория' : item.scope === 'search' ? 'Поиск по названию' : item.scope === 'city' ? 'Городской контекст' : item.scope === 'flickr_search' ? 'Поиск Flickr' : item.scope?.startsWith('category_sub:') ? 'Подкатегория источника' : item.scope;
    const dateLabel = value => {
      if (!value) return 'Неизвестна';
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU');
    };
    for (const [key, value] of Object.entries({ 'Категория': labels[item.category] || item.category, 'Статус': item.status === 'city_context' ? 'Городской контекст' : 'Вероятно', 'Источник': item.provider, 'Контекст поиска': scopeLabel, 'Автор': item.author, 'Лицензия': item.license, 'Дата публикации/загрузки': dateLabel(item.published_at), 'Дата съёмки': dateLabel(item.captured_at), 'Хеш файла': item.sha1 || 'Нет', 'Визуальный хеш': item.dhash || 'Не вычислен' })) {
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
    row.append(amount, node('td', count ? 'Есть материалы с источником' : 'Нет опубликованных кадров'));
    table.append(row);
  }
  $('coverage').replaceChildren(table);
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
  if(extras.campus_candidate?.status !== 'institution_point') window.CampusAtlas?.setUnverified(extras.campus_candidate);
  holder.replaceChildren();
  const map = node('article'); map.append(node('h4', 'Карта'));
  if (extras.campus_candidate?.lat && extras.campus_candidate?.lon) {
    const { lat, lon, display_name } = extras.campus_candidate;
    map.append(node('p', display_name));
    map.append(link('Открыть местоположение в OpenStreetMap', `https://www.openstreetmap.org/?mlat=${encodeURIComponent(lat)}&mlon=${encodeURIComponent(lon)}#map=15/${encodeURIComponent(lat)}/${encodeURIComponent(lon)}`));
    map.append(node('p', extras.campus_candidate.warning, 'hint'));
  } else map.append(node('p', 'Подтверждённая точка кампуса не найдена.'));
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
    for (const category of categories) {
      const row = node('tr'); row.append(node('td', labels[category]));
      for (const profile of data.profiles) row.append(node('td', `${profile.coverage[category] || 0} материалов`));
      table.append(row);
    }
    for(const [label,read] of [
      ['Город и страна',p=>[p.institution.city,p.institution.country].filter(Boolean).join(', ')],
      ['Обновлено',p=>new Date(p.generated_at*1000).toLocaleString('ru-RU')],
      ['Всего материалов',p=>String(p.asset_count)],
    ]){const row=node('tr');row.append(node('td',label));for(const profile of data.profiles)row.append(node('td',read(profile)));table.append(row);}
    $('compare-result').replaceChildren(table, node('p', data.profiles[0].caveat, 'hint'));
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
