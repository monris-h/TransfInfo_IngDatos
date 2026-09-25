const $ = (id) => document.getElementById(id);
const money = new Intl.NumberFormat('es-MX', {style: 'currency', currency: 'MXN'});
const selected = new Map();
const compatibility = new Map();
let expandedPrinterId = null;
let lastProducts = [];
let lastMeta = null;
let currentParams = null;
let requestNumber = 0;
let activeRequest;
let refreshInfo = null;
let refreshTimer;
let connectionIssue = false;
let timer;
let activeView = location.hash === '#filamentos' ? 'filamentos' : 'impresoras';
let presenceSessionId = sessionStorage.getItem('compare3d_session');
let presenceUsername = sessionStorage.getItem('compare3d_username');
let presenceTimer;
const sourceCatalog = [
  {name: 'Inovamarket', method: 'API', detail: 'Catálogo WooCommerce', url: 'https://www.inovamarket.com/'},
  {name: 'Shop3D', method: 'API', detail: 'Catálogo WooCommerce', url: 'https://shop3d.mx/'},
  {name: '3DCity', method: 'Scraping', detail: 'Resultados de búsqueda', url: 'https://www.3dcity.com.mx/'},
  {name: '3D Market', method: 'Scraping', detail: 'Categorías y fichas de producto', url: 'https://www.3dmarket.mx/'},
  {name: 'Creality México', method: 'Scraping', detail: 'Colecciones de productos', url: 'https://store.creality.com/mx/'},
  {name: 'Amazon México', method: 'Scraping', detail: 'Resultados de búsqueda', url: 'https://www.amazon.com.mx/'},
  {name: 'Mercado Libre', method: 'Scraping', detail: 'Resultados públicos de búsqueda', url: 'https://www.mercadolibre.com.mx/'},
];

function el(tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined) node.textContent = value;
  return node;
}

function showNotice(message, error = false, loading = false) {
  const node = $('notice');
  node.hidden = !message;
  node.textContent = message;
  node.classList.toggle('error', error);
  node.classList.toggle('loading', loading);
}

function renderSkeleton() {
  const host = $('results');
  host.replaceChildren();
  host.setAttribute('aria-busy', 'true');
  for (let i = 0; i < 4; i++) {
    const card = el('div', 'card skeleton-card');
    card.setAttribute('aria-hidden', 'true');
    card.append(el('div', 'skeleton-image'));
    const copy = el('div', 'skeleton-copy');
    copy.append(el('span', 'skeleton-line short'), el('span', 'skeleton-line'),
      el('span', 'skeleton-line medium'));
    card.append(copy);
    const price = el('div', 'skeleton-price');
    price.append(el('span', 'skeleton-line'));
    card.append(price);
    host.append(card);
  }
}

function renderSources(data) {
  const host = $('sources-list');
  host.replaceChildren();
  const updated = data.updated_at
    ? new Date(data.updated_at).toLocaleString('es-MX', {dateStyle: 'medium', timeStyle: 'short'})
    : null;
  $('sources-updated').textContent = updated ? `Última actualización: ${updated} · ${data.count} ofertas en el catálogo` : 'Todavía no se ha actualizado el catálogo.';
  for (const source of sourceCatalog) {
    const entries = (data.sources || []).filter(entry => entry.store === source.name);
    const count = entries.reduce((sum, entry) => sum + (entry.count || 0), 0);
    const statuses = new Set(entries.map(entry => entry.status));
    const failed = entries.filter(entry => entry.status !== 'ok');
    let state = 'Sin consultar';
    let stateClass = '';
    if (statuses.has('ok') && failed.length) { state = 'Parcial · datos previos'; stateClass = 'stale'; }
    else if (statuses.has('ok')) { state = 'Disponible'; stateClass = 'ok'; }
    else if (statuses.has('stale')) { state = 'Datos previos'; stateClass = 'stale'; }
    else if (statuses.has('blocked')) { state = count ? 'Bloqueado · datos previos' : 'Acceso bloqueado'; stateClass = 'blocked'; }
    else if (statuses.has('auth_required')) { state = 'Pendiente de actualizar'; stateClass = ''; }
    else if (statuses.has('error')) { state = 'Error de consulta'; stateClass = 'error'; }
    const row = el('div', 'source-row');
    const main = el('div', 'source-row-main');
    const link = el('a', '', source.name + ' ↗');
    link.href = source.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    main.append(link, el('span', 'source-method', source.method));
    row.append(main, el('small', '', `${source.detail}${count ? ` · ${count} registros obtenidos` : ''}${failed.length ? ` · ${failed.length} ${failed.length === 1 ? 'consulta fallida' : 'consultas fallidas'}` : ''}`),
      el('span', `source-state ${stateClass}`, state));
    host.append(row);
  }
}

async function openSources() {
  const dialog = $('sources-dialog');
  dialog.showModal();
  $('sources-updated').textContent = 'Consultando el estado de las fuentes…';
  $('sources-list').replaceChildren();
  try {
    const response = await fetch('/api/status');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (dialog.open) renderSources(data);
  } catch (error) {
    if (dialog.open) $('sources-updated').textContent = `No se pudo consultar el estado: ${error.message}`;
  }
}

function updatePresence(data) {
  $('active-count').textContent = data.count;
  $('current-user').textContent = presenceUsername;
  $('presence-avatar').textContent = presenceUsername.slice(0, 1).toLocaleUpperCase('es-MX');
  $('presence-button').hidden = false;
  if ($('users-dialog').open) renderUsers(data);
}

function renderUsers(data) {
  $('users-summary').textContent = `${data.count} ${data.count === 1 ? 'persona conectada' : 'personas conectadas'}`;
  const host = $('users-list');
  host.replaceChildren();
  if (!data.users.length) {
    host.append(el('p', 'users-empty', 'No hay usuarios conectados en este momento.'));
    return;
  }
  for (const user of data.users) {
    const row = el('div', 'user-row');
    row.append(el('span', 'user-avatar', user.username.slice(0, 1).toLocaleUpperCase('es-MX')));
    const info = el('div', 'user-info');
    info.append(el('strong', '', user.username));
    info.append(el('small', '', `Activo desde ${new Date(user.joined_at).toLocaleTimeString('es-MX', {hour: '2-digit', minute: '2-digit'})}`));
    row.append(info, el('span', 'online-indicator', 'En línea'));
    host.append(row);
  }
}

async function presenceRequest(path, payload) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw Object.assign(new Error(data.detail || `HTTP ${response.status}`), {status: response.status});
  return data;
}

async function joinPresence(username) {
  const data = await presenceRequest('/api/presence/join', {username, session_id: presenceSessionId});
  presenceUsername = data.username;
  presenceSessionId = data.session_id;
  sessionStorage.setItem('compare3d_username', presenceUsername);
  sessionStorage.setItem('compare3d_session', presenceSessionId);
  updatePresence(data);
  if ($('login-dialog').open) $('login-dialog').close();
  clearInterval(presenceTimer);
  presenceTimer = setInterval(heartbeatPresence, 15000);
}

async function heartbeatPresence() {
  if (!presenceSessionId) return;
  try {
    const data = await presenceRequest('/api/presence/heartbeat', {session_id: presenceSessionId});
    updatePresence(data);
  } catch (error) {
    if (error.status === 404 && presenceUsername) {
      try { await joinPresence(presenceUsername); } catch (_) {}
    }
  }
}

async function openUsers() {
  $('users-dialog').showModal();
  $('users-list').replaceChildren(el('p', 'users-empty', 'Actualizando usuarios…'));
  try {
    const response = await fetch('/api/presence');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if ($('users-dialog').open) renderUsers(data);
  } catch (error) {
    if ($('users-dialog').open) $('users-list').replaceChildren(el('p', 'users-empty', `No se pudo cargar la lista: ${error.message}`));
  }
}

function showLogin() {
  $('login-name').value = presenceUsername || '';
  $('login-error').hidden = true;
  if (!$('login-dialog').open) $('login-dialog').showModal();
}

async function leavePresence() {
  clearInterval(presenceTimer);
  const sessionId = presenceSessionId;
  presenceSessionId = null;
  presenceUsername = null;
  sessionStorage.removeItem('compare3d_session');
  sessionStorage.removeItem('compare3d_username');
  $('presence-button').hidden = true;
  $('users-dialog').close();
  if (sessionId) {
    try { await presenceRequest('/api/presence/leave', {session_id: sessionId}); } catch (_) {}
  }
  showLogin();
}

function imageFor(item) {
  if (!item.image) return el('div', 'product-image no-image', 'Sin imagen');
  const wrap = el('div', 'product-image');
  const img = document.createElement('img');
  img.src = item.image;
  img.alt = item.name;
  img.loading = 'lazy';
  img.referrerPolicy = 'no-referrer';
  img.addEventListener('error', () => wrap.replaceChildren(el('span', 'no-image', 'Sin imagen')));
  wrap.append(img);
  return wrap;
}

function cardFor(item) {
  const card = el('article', 'card');
  card.append(imageFor(item));
  const info = el('div', 'card-info');
  const store = el('div', 'store-line');
  store.append(el('span', 'store-name', item.store), el('span', 'method-badge', item.method));
  info.append(store, el('h3', '', item.name));
  const meta = el('div', 'card-meta');
  meta.append(el('span', item.available === null ? 'stock unknown' : item.available ? 'stock yes' : 'stock no',
    item.available === null ? (item.availability_note || 'Existencia por confirmar') : item.available ? '● Disponible' : '○ Sin existencias'));
  if (item.review_count > 0 && item.rating) meta.append(el('span', 'rating', `★ ${item.rating.toFixed(1)} · ${item.review_count} reseñas`));
  info.append(meta);
  const choose = el('button', 'choose', selected.has(item.id) ? '✓ En comparación' : '+ Comparar');
  choose.type = 'button';
  choose.setAttribute('aria-pressed', selected.has(item.id));
  choose.addEventListener('click', () => {
    if (selected.has(item.id)) selected.delete(item.id);
    else if (selected.size >= 3) return showNotice('Puedes comparar hasta tres ofertas.', true);
    else if (selected.size && [...selected.values()][0].category !== item.category) return showNotice('Compara impresoras entre sí o filamentos del mismo material.', true);
    else selected.set(item.id, item);
    showNotice('');
    renderCompare();
    renderResults();
  });
  info.append(choose);
  if (item.category === 'impresora') {
    const matchButton = el('button', 'compatibility-toggle', expandedPrinterId === item.id ? 'Ocultar filamentos compatibles' : 'Ver filamentos compatibles');
    matchButton.type = 'button';
    matchButton.setAttribute('aria-expanded', expandedPrinterId === item.id);
    matchButton.addEventListener('click', () => toggleCompatibility(item));
    info.append(matchButton);
  }
  const price = el('div', 'card-price');
  price.append(el('strong', '', money.format(item.price)));
  if (item.price_note) price.append(el('small', '', item.price_note));
  const link = el('a', 'shop-link', 'Ver en tienda ↗');
  link.href = item.url;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  price.append(link);
  card.append(info, price);
  if (expandedPrinterId === item.id) card.append(compatibilityPanel(item));
  return card;
}

function compatibilityPanel(item) {
  const panel = el('div', 'compatibility-panel');
  const result = compatibility.get(item.id);
  if (!result || result.loading) return el('div', 'compatibility-panel', 'Consultando la descripción de la tienda…');
  if (result.error) return el('div', 'compatibility-panel', `No se pudo revisar la ficha: ${result.error}`);
  if (!result.materials.length) return el('div', 'compatibility-panel', 'La ficha de esta impresora no indica materiales compatibles de forma clara. Revisa las especificaciones en la tienda.');
  panel.append(el('strong', '', 'Materiales declarados por la tienda'));
  const tags = el('div', 'material-tags');
  for (const material of result.materials) tags.append(el('span', '', material));
  panel.append(tags);
  if (result.evidence) panel.append(el('p', 'compatibility-evidence', `“${result.evidence}”`));
  panel.append(el('p', 'compatibility-note', 'Coincidencias por material. Confirma diámetro, temperatura y requisitos de la bobina antes de comprar.'));
  panel.append(el('strong', '', `${result.total} filamentos del catálogo coinciden`));
  for (const material of result.materials) {
    const entries = result.filaments.filter(filament => filament.category === material);
    if (!entries.length) continue;
    const group = el('div', 'compatible-group');
    group.append(el('span', 'compatible-group-title', `${material} · ${result.material_counts[material]} ofertas`));
    const list = el('div', 'compatible-list');
    for (const filament of entries.slice(0, result.visiblePerMaterial || 3)) {
      const link = el('a', 'compatible-item');
      link.href = filament.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.append(el('span', '', filament.name), el('small', '', `${filament.store} · ${money.format(filament.price)}`));
      list.append(link);
    }
    group.append(list);
    panel.append(group);
  }
  if (result.materials.some(material => result.filaments.filter(f => f.category === material).length > (result.visiblePerMaterial || 3))) {
    const more = el('button', 'compatibility-more', 'Mostrar más filamentos');
    more.type = 'button';
    more.addEventListener('click', () => {result.visiblePerMaterial = (result.visiblePerMaterial || 3) + 10; renderResults();});
    panel.append(more);
  }
  return panel;
}

async function toggleCompatibility(item) {
  expandedPrinterId = expandedPrinterId === item.id ? null : item.id;
  renderResults();
  if (expandedPrinterId !== item.id || compatibility.has(item.id)) return;
  compatibility.set(item.id, {loading: true});
  renderResults();
  try {
    const response = await fetch(`/api/compatibility/${encodeURIComponent(item.id)}`);
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);
    compatibility.set(item.id, result);
  } catch (error) {compatibility.set(item.id, {error: error.message});}
  renderResults();
}

function fillSection(host, items, type, otherCount) {
  host.replaceChildren();
  if (!items.length) {
    const empty = el('div', 'empty');
    empty.append(el('strong', '', `Sin ${type} para esta búsqueda.`), el('p', '', 'Prueba otro modelo o quita un filtro.'));
    if (otherCount) {
      const switchButton = el('button', 'switch-view', `Ver ${otherCount} ${activeView === 'impresoras' ? 'filamentos' : 'impresoras'} encontrados`);
      switchButton.type = 'button';
      switchButton.addEventListener('click', () => {setView(activeView === 'impresoras' ? 'filamentos' : 'impresoras'); load();});
      empty.append(switchButton);
    }
    host.append(empty);
    return;
  }
  for (const item of items) host.append(cardFor(item));
}

function renderResults() {
  const total = lastMeta?.total || 0;
  const otherCount = activeView === 'impresoras' ? lastMeta?.filament_count : lastMeta?.printer_count;
  $('result-count').textContent = `${total} ${total === 1 ? 'oferta' : 'ofertas'}`;
  fillSection($('results'), lastProducts, activeView, otherCount || 0);
  $('results').setAttribute('aria-busy', 'false');
  $('show-more').hidden = lastProducts.length >= total;
}

function setView(view) {
  activeView = view;
  for (const tab of document.querySelectorAll('.tab')) {
    const active = tab.dataset.view === view;
    tab.setAttribute('aria-selected', active);
    tab.tabIndex = active ? 0 : -1;
  }
  $('results').setAttribute('aria-labelledby', `tab-${view}`);
  $('material-field').hidden = view !== 'filamentos';
  $('search-label').textContent = view === 'impresoras' ? 'Buscar modelo o marca' : 'Buscar material, marca o color';
  $('search').placeholder = view === 'impresoras' ? 'Ej. Creality K1 Max' : 'Ej. PETG, ABS o Polymaker';
  $('view-hint').textContent = view === 'impresoras'
    ? 'Compara equipos completos de distintas tiendas.'
    : 'Explora todos los materiales disponibles y revisa el peso de cada bobina.';
  if (location.hash !== `#${view}`) history.replaceState(null, '', `#${view}`);
  lastProducts = [];
  lastMeta = null;
  $('results').replaceChildren();
  $('show-more').hidden = true;
  $('result-count').textContent = 'Cargando ofertas…';
}

function renderCompare() {
  const host = $('compare-content');
  const items = [...selected.values()];
  $('compare').hidden = !items.length;
  host.replaceChildren();
  $('compare-count').textContent = `${items.length} de 3 ofertas seleccionadas`;
  if (!items.length) return;
  const cheapest = Math.min(...items.map(p => p.price));
  for (const item of items) {
    const column = el('div', 'compare-item');
    column.append(el('small', '', item.store), el('strong', '', item.name), el('span', 'compare-price', money.format(item.price)));
    column.append(el('span', 'compare-diff', item.price === cheapest ? 'Menor precio seleccionado' : `+${money.format(item.price - cheapest)} frente al menor`));
    const link = el('a', '', 'Ver en tienda ↗');
    link.href = item.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    column.append(link); host.append(column);
  }
}

function applyProducts(data, append = false) {
  if (append) {
    lastProducts.push(...data.products);
    lastMeta = data;
    for (const product of data.products) $('results').append(cardFor(product));
    $('show-more').hidden = !data.products.length || lastProducts.length >= data.total;
    return;
  }
  lastProducts = data.products;
  lastMeta = data;
  const materialSelect = $('material');
  const chosenMaterial = materialSelect.value;
  const materials = data.materials.sort((a, b) => a === 'OTRO' ? 1 : b === 'OTRO' ? -1 : a.localeCompare(b, 'es'));
  const currentOptions = [...materialSelect.options].slice(1).map(option => option.value);
  if (materials.join('|') !== currentOptions.join('|')) {
    materialSelect.replaceChildren(new Option('Todos los materiales', 'todos'), ...materials.map(value => new Option(value === 'OTRO' ? 'Otro / sin especificar' : value, value)));
    materialSelect.value = materials.includes(chosenMaterial) ? chosenMaterial : 'todos';
  }
  $('printer-count').textContent = data.printer_count;
  $('filament-count').textContent = data.filament_count;
  renderResults();
  const timestamp = data.updated_at;
  $('metric-date').textContent = timestamp ? `Actualización: ${new Date(timestamp).toLocaleString('es-MX', {dateStyle: 'short', timeStyle: 'short'})}` : 'Sin datos todavía';
  const stores = new Set(data.sources.filter(s => s.status === 'ok' || s.status === 'stale').map(s => s.store));
  $('source-count').textContent = stores.size ? `${stores.size} ${stores.size === 1 ? 'tienda consultada' : 'tiendas consultadas'}` : '';
  if (data.failures.length) showNotice(failureMessage(data.failures, data.sources), true);
  else if (!data.updated_at) showNotice('Pulsa “Actualizar datos” para iniciar la ingesta.');
  else showNotice('');
  if (refreshInfo?.status === 'running') showNotice(refreshMessage(refreshInfo), false, true);
}

function failureMessage(failures, sources) {
  const stores = [...new Set(failures.map(failure => failure.store))];
  const blocked = [...new Set(sources.filter(source => source.status === 'blocked').map(source => source.store))];
  const summary = `${failures.length} ${failures.length === 1 ? 'consulta falló' : 'consultas fallaron'} en ${stores.length} ${stores.length === 1 ? 'tienda' : 'tiendas'}: ${stores.join(' y ')}.`;
  return `${summary}${blocked.length ? ` ${blocked.join(' y ')} ${blocked.length === 1 ? 'bloqueó' : 'bloquearon'} el acceso automatizado.` : ''} Se muestran datos previos donde existen.`;
}

function refreshMessage(state) {
  return state.total_stores
    ? `Actualizando tiendas: ${state.completed_stores} de ${state.total_stores} completadas${state.last_store ? ` · última: ${state.last_store}` : ''}. Puedes seguir consultando.`
    : 'Iniciando actualización. Puedes seguir consultando el catálogo actual.';
}

function showRefreshState(state) {
  refreshInfo = state;
  const running = state.status === 'running';
  $('refresh').disabled = running;
  $('refresh').querySelector('span').textContent = running && state.total_stores
    ? `Actualizando ${state.completed_stores}/${state.total_stores}`
    : running ? 'Actualizando…' : '↻  Actualizar datos';
  if (running) showNotice(refreshMessage(state), false, true);
}

async function pollRefresh() {
  clearTimeout(refreshTimer);
  try {
    const response = await fetch('/api/refresh/status');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const state = await response.json();
    const wasRunning = refreshInfo?.status === 'running';
    const reconnected = connectionIssue;
    connectionIssue = false;
    showRefreshState(state);
    if (state.status === 'running') refreshTimer = setTimeout(pollRefresh, 1000);
    else if (wasRunning && state.status === 'complete') {
      await load();
      if ($('sources-dialog').open) {
        try {
          const response = await fetch('/api/status');
          if (response.ok) renderSources(await response.json());
        } catch (_) {}
      }
      showNotice(state.failures.length
        ? failureMessage(state.failures, lastMeta?.sources || [])
        : `Catálogo actualizado: ${state.count} ofertas.`, Boolean(state.failures.length));
    } else if (state.status === 'error') showNotice(`No se pudo actualizar: ${state.error}`, true);
    else if (reconnected) {
      showNotice('');
      load();
    }
  } catch (error) {
    connectionIssue = true;
    refreshTimer = setTimeout(pollRefresh, 3000);
    if (refreshInfo?.status !== 'running') {
      $('refresh').disabled = false;
      $('refresh').querySelector('span').textContent = '↻  Actualizar datos';
    }
    showNotice('Sin conexión con el servidor. Reintentando…', true);
  }
}

function filterParams() {
  const category = activeView === 'impresoras' ? 'impresora' : $('material').value === 'todos' ? 'filamento' : $('material').value;
  return new URLSearchParams({q: $('search').value.trim(), category, store: $('store').value, sort: $('sort').value,
    available: $('available').checked, context: activeView === 'impresoras' ? 'impresora' : 'filamento'});
}

async function load() {
  const currentRequest = ++requestNumber;
  activeRequest?.abort();
  const controller = new AbortController();
  activeRequest = controller;
  const params = filterParams();
  params.set('limit', 16);
  params.set('offset', 0);
  currentParams = params;
  document.querySelector('.workspace').classList.add('is-loading');
  if (!lastProducts.length) renderSkeleton();
  try {
    const response = await fetch(`/api/products?${params}`, {signal: controller.signal});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (currentRequest !== requestNumber) return;
    applyProducts(data);
  } catch (error) {
    if (currentRequest === requestNumber && error.name !== 'AbortError') {
      renderResults();
      showNotice(`No se pudo consultar el catálogo: ${error.message}`, true);
    }
  } finally {
    if (currentRequest === requestNumber) {
      document.querySelector('.workspace').classList.remove('is-loading');
      $('results').setAttribute('aria-busy', 'false');
    }
  }
}

async function loadMore() {
  if (!currentParams || !lastMeta || lastProducts.length >= lastMeta.total) return;
  const currentRequest = requestNumber;
  const params = new URLSearchParams(currentParams);
  params.set('offset', lastProducts.length);
  $('show-more').disabled = true;
  try {
    const response = await fetch(`/api/products?${params}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (currentRequest === requestNumber) {
      if (data.updated_at !== lastMeta.updated_at) load();
      else applyProducts(data, true);
    }
  } catch (error) {
    if (currentRequest === requestNumber) showNotice(`No se pudieron cargar más ofertas: ${error.message}`, true);
  } finally {$('show-more').disabled = false;}
}

function updateSearchRefresh() {
  $('search-refresh').hidden = $('search').value.trim().length < 2;
}

async function refreshSearch() {
  const query = $('search').value.trim();
  if (query.length < 2) return;
  const button = $('search-refresh');
  button.disabled = true;
  showNotice(`Consultando precios actuales para “${query}”…`, false, true);
  try {
    const response = await fetch('/api/search/refresh', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({query, context: activeView === 'impresoras' ? 'impresora' : 'filamento', store: $('store').value}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    await load();
    const count = data.saved;
    const failures = data.failures.length;
    showNotice(`${count} ${count === 1 ? 'oferta guardada' : 'ofertas guardadas'} para “${query}” en el catálogo local.${failures ? ` ${failures} ${failures === 1 ? 'fuente no respondió' : 'fuentes no respondieron'}; se conservaron los datos anteriores.` : ''}`, Boolean(failures));
  } catch (error) {
    showNotice(`No se pudo actualizar “${query}”: ${error.message}`, true);
  } finally {
    button.disabled = false;
    updateSearchRefresh();
  }
}

for (const id of ['search', 'store', 'sort', 'available']) {
  $(id).addEventListener(id === 'search' ? 'input' : 'change', () => {
    if (id === 'search') updateSearchRefresh();
    clearTimeout(timer);
    timer = setTimeout(load, id === 'search' ? 300 : 50);
  });
}
$('material').addEventListener('change', load);
$('show-more').addEventListener('click', loadMore);
$('export-csv').addEventListener('click', () => {
  $('export-csv').href = `/api/export.csv?${filterParams()}`;
});
$('search-refresh').addEventListener('click', refreshSearch);
$('sources-button').addEventListener('click', openSources);
$('sources-close').addEventListener('click', () => $('sources-dialog').close());
$('sources-dialog').addEventListener('click', event => {
  if (event.target === $('sources-dialog')) $('sources-dialog').close();
});
$('presence-button').addEventListener('click', openUsers);
$('users-close').addEventListener('click', () => $('users-dialog').close());
$('users-dialog').addEventListener('click', event => {
  if (event.target === $('users-dialog')) $('users-dialog').close();
});
$('logout').addEventListener('click', leavePresence);
$('login-dialog').addEventListener('cancel', event => event.preventDefault());
$('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = $('login-continue');
  button.disabled = true;
  $('login-error').hidden = true;
  try {
    await joinPresence($('login-name').value);
  } catch (error) {
    $('login-error').textContent = error.message;
    $('login-error').hidden = false;
  } finally {button.disabled = false;}
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) heartbeatPresence();
});
window.addEventListener('pagehide', () => {
  if (presenceSessionId) navigator.sendBeacon('/api/presence/leave',
    new Blob([JSON.stringify({session_id: presenceSessionId})], {type: 'application/json'}));
});
for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => {if (activeView !== tab.dataset.view) {setView(tab.dataset.view); load();}});
  tab.addEventListener('keydown', event => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const other = tab.dataset.view === 'impresoras' ? 'filamentos' : 'impresoras';
    setView(other);
    load();
    $(`tab-${other}`).focus();
  });
}
window.addEventListener('hashchange', () => {setView(location.hash === '#filamentos' ? 'filamentos' : 'impresoras'); load();});
$('clear-compare').addEventListener('click', () => {selected.clear(); renderCompare(); renderResults();});
$('refresh').addEventListener('click', async () => {
  $('refresh').disabled = true;
  $('refresh').querySelector('span').textContent = 'Iniciando…';
  try {
    const response = await fetch('/api/refresh', {method: 'POST'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    showRefreshState(data);
    refreshTimer = setTimeout(pollRefresh, 1000);
  } catch (error) {
    $('refresh').disabled = false;
    $('refresh').querySelector('span').textContent = '↻  Actualizar datos';
    if (error instanceof TypeError) {
      connectionIssue = true;
      showNotice('Sin conexión con el servidor. Reintentando…', true);
      refreshTimer = setTimeout(pollRefresh, 3000);
    } else showNotice(`Error de ingesta: ${error.message}`, true);
  }
});
setView(activeView);
updateSearchRefresh();
load();
pollRefresh();
if (presenceUsername) joinPresence(presenceUsername).catch(showLogin);
else showLogin();
