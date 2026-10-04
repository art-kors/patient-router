/* Локальный интерфейс: данные врача выводятся как текст, без HTML-вставок. */
'use strict';
const $ = id => document.getElementById(id);
let active = 'metrics', items = [], selected = null, creating = false;
function node(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; }
function status(text, failure = false) { $('status').textContent = text; $('status').className = failure ? 'failure' : ''; }
async function api(path, method = 'GET', body) {
  const r = await fetch(path, {method, headers: {'Content-Type': 'application/json'}, ...(body === undefined ? {} : {body: JSON.stringify(body)})}).catch(() => { throw new Error('Нет связи с локальным сервером. Проверьте, что он запущен, и нажмите «Обновить».'); });
  const data = await r.json().catch(() => { throw new Error('Сервер вернул непонятный ответ. Повторите действие.'); });
  if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось выполнить действие. Проверьте заполненные поля и повторите.');
  return data;
}
function table(target, headers, rows) {
  const t = node('table'), head = node('thead'), tr = node('tr');
  headers.forEach(h => tr.append(node('th', h))); head.append(tr); t.append(head);
  const body = node('tbody'); rows.forEach(row => { const r = node('tr'); row.forEach(value => { const td = node('td'); value instanceof Node ? td.append(value) : td.textContent = value; r.append(td); }); body.append(r); });
  t.append(body); $(target).replaceChildren(rows.length ? t : node('p', 'Данных пока нет. Добавьте проверенные примеры или измените выборку и обновите страницу.'));
}
const percent = value => value == null ? 'Пока нечем посчитать' : `${Math.round(value * 100)} из 100`;
const metricValue = (m, key) => {
  const denominator = {recall:m.tp + m.fn, precision:m.tp + m.fp, fpr:m.fp + m.tn, f1:2 * m.tp + m.fp + m.fn}[key];
  return denominator ? percent(m[key]) : 'Пока нечем посчитать';
};
async function loadErrors() {
  const filter = $('error-filter').value;
  const errors = await api(`/api/v1/quality/errors?split=${$('split').value}&limit=100${filter ? '&trigger_id=' + encodeURIComponent(filter) : ''}`);
  $('errors').replaceChildren();
  if (!errors.length) $('errors').append(node('p', 'Расхождений в выбранной выборке нет.'));
  errors.forEach(error => {
    const box = node('article', undefined, 'error');
    box.append(node('strong', `${({fp:'Ложный сигнал', fn:'Пропущенная находка'}[error.type.toLowerCase()] || 'Расхождение')} · ${error.trigger_id}`), node('p', `Исследование ${error.study_id} · Правило: ${error.applied_rule}. Причина: ${({negative_context:'найдено отрицание', threshold_not_met:'размер ниже порога', study_type_mismatch:'другой вид исследования', no_match:'находка не распознана'}[error.suppression_reason] || 'условие правила сработало')}`), node('blockquote', error.quote || 'Цитата отсутствует: находка не распознана.'));
    $('errors').append(box);
  });
}
async function loadMetrics() {
  const split = $('split').value;
  const results = await Promise.allSettled([
    api(`/api/v1/quality/metrics?split=${split}`),
    api(`/api/v1/quality/metrics/by-trigger?split=${split}`),
    api(`/api/v1/quality/metrics/timeline?split=${split}`)
  ]);
  const failures = [];
  ['cards', 'confusion', 'trigger-metrics', 'timeline', 'errors'].forEach(id => $(id).replaceChildren());
  if (results[0].status === 'fulfilled') {
    const m = results[0].value;
    const count = m.tp + m.fp + m.fn + m.tn;
    $('quality-summary').textContent = !count ? 'Метрики посчитать пока нечем: нет разметки. Добавьте проверенные примеры для выбранной выборки.' : m.fp + m.fn ? `Нужна проверка: ${m.fn} пропущенных находок и ${m.fp} ложных сигналов на ${count} проверенных парах «исследование–правило». Откройте подробности и проверьте правила.` : `В этой выборке ошибок нет: проверено ${count} пар «исследование–правило». Для общей оценки нужна проверка на новых протоколах.`;
    [['recall', 'Находим реальные находки (полнота, recall)'], ['precision', 'Верные сигналы среди найденных (точность, precision)'], ['fpr', 'Ложные сигналы при отсутствии находки (FPR)'], ['f1', 'Баланс полноты и точности (F1, шкала до 100)']].forEach(([key, title]) => { const c = node('div', title, 'card'); c.append(node('strong', key === 'f1' ? metricValue(m, key).replace('из 100', 'баллов из 100') : metricValue(m, key))); $('cards').append(c); });
    [['tp', 'Верно найдено'], ['fp', 'Ложные срабатывания'], ['fn', 'Пропущено'], ['tn', 'Верная норма']].forEach(([key, title]) => { const c = node('div', title, 'card'); c.append(node('strong', count ? m[key] : 'Нет разметки')); $('confusion').append(c); });
  } else { $('quality-summary').textContent = 'Качество проверить не удалось. Нажмите «Обновить», чтобы повторить загрузку.'; failures.push(results[0].reason.message); }
  if (results[1].status === 'fulfilled') {
    const rows = results[1].value, old = $('error-filter').value;
    $('error-filter').replaceChildren(new Option('Все', ''));
    rows.forEach(r => $('error-filter').add(new Option(r.display_name, r.trigger_id)));
    $('error-filter').value = old;
    table('trigger-metrics', ['Правило', 'Находим находки', 'Верные сигналы', 'Ложные сигналы на норме', 'Баланс, баллы', 'Найдено / ложно / пропущено / норма', 'Проверенных пар'], rows.map(r => [r.display_name + (r.enabled === false ? ' (отключён)' : ''), ...['recall', 'precision', 'fpr', 'f1'].map(k => r.available ? metricValue(r, k) : 'Нет разметки'), r.available ? `${r.tp} / ${r.fp} / ${r.fn} / ${r.tn}` : 'Нет проверенных примеров', r.n_samples]));
  } else failures.push(results[1].reason.message);
  if (results[2].status === 'fulfilled') {
    table('timeline', ['Дата оценки', 'Версия правил', 'Способ разбора', 'Находим находки', 'Верные сигналы', 'Ложные сигналы на норме', 'Баланс, баллы'], results[2].value.points.map(p => [new Date(p.created_at).toLocaleString('ru'), p.matrix_version, p.decoder === 'rules' ? 'Правила' : 'Модель', ...['recall', 'precision', 'fpr', 'f1'].map(k => metricValue(p.metrics, k))]));
  } else { $('timeline').append(node('p', results[2].reason.message)); failures.push(results[2].reason.message); }
  try { await loadErrors(); } catch (e) { failures.push(e.message); }
  status(failures.length ? [...new Set(failures)].join('\n') : 'Метрики обновлены.', failures.length > 0);
}
const definitions = [
  ['trigger_id', 'Идентификатор', 'text'], ['display_name', 'Название', 'text'], ['source_study', 'Тип исследования', 'text'],
  ['synonyms', 'Синонимы — по одному на строку', 'list'], ['negative_contexts', 'Отрицания — по одному на строку', 'list'],
  ['thresholds', 'Ограничения размеров в формате данных JSON (min_size_mm — минимальный размер в мм, например {"min_size_mm": 10})', 'json'], ['specialty', 'Специальность', 'text'],
  ['potential_route', 'Маршрут', 'text'], ['department', 'Подразделение', 'text'], ['target_sla_days', 'Срок следующего шага (SLA), дней', 'number'],
  ['priority', 'Приоритет (1 — самый высокий)', 'number'], ['emergency_flag', 'Экстренный триггер', 'checkbox'], ['enabled', 'Включён', 'checkbox']
];
function edit(item, fresh = false) {
  creating = fresh; selected = item.trigger_id; $('editor').hidden = false; $('disable').hidden = fresh;
  $('editor-title').textContent = fresh ? 'Новый триггер' : item.display_name; $('fields').replaceChildren();
  const origin = item.provenance === 'clinician' ? 'Медицинский специалист' : 'Проектная гипотеза';
  $('fields').append(node('p', `Источник: ${origin}. Условия врача: ${item.threshold_text || 'не заданы'}`));
  if (item.threshold_text) $('fields').append(node('p', Object.keys(item.thresholds || {}).length ? `Формализованный порог: ${JSON.stringify(item.thresholds)}. Обязательные атрибуты требуют проверки специалистом.` : 'Текстовое условие: движок его автоматически не проверяет; требуется решение врача.'));
  if (item.required_attributes?.length) $('fields').append(node('p', `Атрибуты: ${item.required_attributes.join(', ')}`));
  if (item.evidence_phrases?.length) $('fields').append(node('p', `Фразы-доказательства: ${item.evidence_phrases.join('; ')}`));
  definitions.forEach(([key, title, type]) => {
    const label = node('label', title); const field = node(type === 'list' || type === 'json' ? 'textarea' : 'input');
    field.name = key;
    if (field.tagName === 'INPUT') field.type = type;
    if (type === 'checkbox') field.checked = item[key] ?? key === 'enabled';
    else field.value = type === 'list' ? (item[key] || []).join('\n') : type === 'json' ? JSON.stringify(item[key] || {}, null, 2) : item[key] ?? '';
    if (key === 'trigger_id') { field.readOnly = !fresh; field.required = true; field.pattern = '[a-zA-Z0-9_-]+'; }
    if (key === 'display_name') field.required = true;
    if (type === 'number') { field.min = 1; if (key === 'priority') field.max = 4; field.step = 1; field.required = true; }
    if (type === 'json' || type === 'list') label.className = 'wide';
    label.append(field); $('fields').append(label);
  });
  $('editor').elements.description.value = '';
}
async function loadTriggers() {
  items = await api('/api/v1/admin/triggers'); $('trigger-list').replaceChildren();
  if (!items.length) $('trigger-list').append(node('p', 'Правил пока нет. Нажмите «Добавить триггер», чтобы создать первое правило.'));
  // Условие, которое система не проверяет, видно сразу; подробности доступны в редакторе.
  items.forEach(item => {
    const threshold = Object.keys(item.thresholds || {}).length ? 'Числовой порог' : item.threshold_text ? 'Текстовое условие — нужно решение врача' : 'Без порога';
    const b = node('button', `${item.enabled === false ? 'Отключено:' : 'Включено:'} ${item.display_name} [${item.provenance === 'clinician' ? 'Врач' : 'Проект'} · ${threshold}]`);
    b.onclick = () => edit(item); $('trigger-list').append(b);
  });
  const validation = await api('/api/v1/admin/validate');
  $('warnings').replaceChildren(...(validation.warnings.length ? validation.warnings : ['Предупреждений нет.']).map(w => node('li', w)));
}
async function loadVersions() {
  const versions = await api('/api/v1/admin/versions');
  table('version-list', ['Версия', 'Дата', 'Автор', 'Описание', 'Действие'], versions.map(v => {
    const b = node('button', 'Откатить'); b.onclick = () => action(async () => { const author = $('rollback-author').value.trim(); if (!author) throw new Error('Укажите автора отката'); await api(`/api/v1/admin/versions/${v.version}/rollback`, 'POST', {author, description: $('rollback-description').value}); await loadVersions(); status('Откат сохранён новой версией и применён.'); });
    return [v.version, new Date(v.created_at).toLocaleString('ru'), v.author, v.description, b];
  }));
  if (!versions.length) $('version-list').append(node('p', 'Матрица пока загружена из файла. История появится при первом сохранении.'));
}
async function action(fn) {
  const buttons = [...document.querySelectorAll('button')]; buttons.forEach(b => b.disabled = true); status('Загрузка… Подождите, получаем данные локального сервера.');
  try { await fn(); } catch (e) { status(e instanceof SyntaxError ? 'Проверьте формат ограничений: нужен корректный объект данных JSON.' : e.message, true); } finally { buttons.forEach(b => b.disabled = false); }
}
async function refresh() { if (active === 'metrics') await loadMetrics(); else if (active === 'triggers') { await loadTriggers(); status('Правила загружены.'); } else { await loadVersions(); status('История загружена.'); } }
document.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => action(async () => { active = b.dataset.tab; ['metrics', 'triggers', 'versions'].forEach(id => $(id).hidden = id !== active); document.querySelectorAll('[data-tab]').forEach(t => t.setAttribute('aria-pressed', String(t === b))); await refresh(); }));
$('refresh').onclick = () => action(refresh); $('split').onchange = () => action(loadMetrics); $('error-filter').onchange = () => action(loadErrors);
$('add').onclick = () => edit({target_sla_days: 14, priority: 3, enabled: true}, true);
$('editor').onsubmit = event => { event.preventDefault(); action(async () => {
  const form = $('editor'), body = {author: form.elements.author.value, description: form.elements.description.value};
  definitions.forEach(([key, , type]) => { const field = form.elements[key]; body[key] = type === 'checkbox' ? field.checked : type === 'number' ? Number(field.value) : type === 'list' ? field.value.split('\n').map(s => s.trim()).filter(Boolean) : type === 'json' ? JSON.parse(field.value) : field.value; });
  const id = body.trigger_id; if (!creating) delete body.trigger_id;
  const result = await api(creating ? '/api/v1/admin/triggers' : `/api/v1/admin/triggers/${encodeURIComponent(id)}`, creating ? 'POST' : 'PUT', body);
  await loadTriggers(); edit(items.find(i => i.trigger_id === id)); status(`Версия ${result.version} сохранена и применена. Предупреждений: ${result.warnings.length}.`);
}); };
$('disable').onclick = () => action(async () => { const result = await api(`/api/v1/admin/triggers/${encodeURIComponent(selected)}`, 'PUT', {enabled: false, author: $('editor').elements.author.value, description: $('editor').elements.description.value || 'Отключение триггера'}); await loadTriggers(); edit(items.find(i => i.trigger_id === selected)); status(`Триггер отключён. Версия ${result.version}.`); });
$('reload').onclick = () => action(async () => { await api('/api/v1/admin/reload', 'POST'); status('Матрица применена.'); });
if (location.hash === '#triggers') document.querySelector('[data-tab="triggers"]').click();
else action(refresh);

document.querySelectorAll('.site-nav a').forEach(link => { if (link.pathname === location.pathname) link.setAttribute('aria-current', 'page'); });
