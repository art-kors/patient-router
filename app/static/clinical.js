'use strict';
// Все данные сервера вставляются как текст, без интерпретации HTML.
const $ = id => document.getElementById(id);
const page = document.body.dataset.page;
// Названия событий объясняют произошедшее, технические коды остаются в запросах.
const eventNames = {VisitStarted:'Начался приём', StudyProtocolSigned:'Протокол исследования подписан', StudyProtocolCorrected:'Протокол исправлен', StudyProtocolCancelled:'Протокол отменён', AppointmentBooked:'Пациент записан на приём', AppointmentCancelled:'Запись на приём отменена', VisitCompleted:'Приём завершён', VisitNoShow:'Пациент не пришёл', TacticsChosen:'Врач выбрал дальнейшие действия', HospitalizationScheduled:'Госпитализация запланирована', HospitalizationFactual:'Пациент госпитализирован', SurgeryPerformed:'Операция выполнена', Discharged:'Пациент выписан'};
let patients = [], selectedId = '', route = null, banner = null, delivery = null;
let busy = false, modelClock = false;
const date = value => value ? new Date(value).toLocaleString('ru-RU') : 'Дата не назначена';
function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = presentText(text);
  if (className) el.className = className;
  return el;
}
function show(id, text) { $(id).replaceChildren(node('p', text)); }
async function api(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(path, {
      signal: controller.signal,
      ...(body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
    });
    const data = await response.json().catch(() => { throw new Error('Сервер вернул непонятный ответ. Повторите действие.'); });
    if (!response.ok) {
      const detail = data.detail ?? data;
      throw new Error(typeof detail === 'string' ? detail : 'Не удалось выполнить действие. Проверьте данные и повторите.');
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('Сервер не ответил за 20 секунд. Проверьте состояние операции перед повтором.');
    if (error instanceof TypeError) throw new Error('Нет связи с сервером. Проверьте подключение и повторите действие.');
    throw error;
  } finally { clearTimeout(timeout); }
}
async function run(action) {
  if (busy) return;
  busy = true;
  const controls = [...document.querySelectorAll('button, select, input, textarea')];
  const disabled = controls.map(el => el.disabled);
  controls.forEach(el => { el.disabled = true; });
  $('clinical-status').className = '';
  $('clinical-status').textContent = 'Загрузка…';
  try { await action(); $('clinical-status').textContent = page === 'pulse' ? 'Данные загружены. Выберите событие и отправьте его или проверьте ленту ниже.' : selectedId ? 'Пациент открыт. Проверьте рекомендации и следующий шаг ниже.' : page === 'doctor' ? 'Выберите пациента и нажмите «Новый визит».' : 'Выберите пациента и нажмите «Открыть пациента».'; }
  catch (error) { $('clinical-status').className = 'failure'; $('clinical-status').textContent = error instanceof SyntaxError ? 'Проверьте дополнительные данные события: нужен корректный объект данных JSON.' : error.message; }
  finally {
    controls.forEach((el, i) => { el.disabled = disabled[i]; });
    document.querySelectorAll('[data-hours]').forEach(el => { el.disabled = !modelClock; });
    busy = false;
  }
}
function patientId() {
  const id = $('patient-id').value.trim() || $('patient-select').value;
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) throw new Error('Выберите пациента или введите идентификатор из каталога.');
  return id;
}
function renderBanner(value) {
  const el = $('unfinished-banner');
  el.classList.toggle('clear', !value?.visible);
  el.replaceChildren(node('h2', value?.visible ? 'Незавершённый клинический маршрут' : 'Незавершённых клинических маршрутов нет'));
  if (value?.visible) el.append(node('p', value.text || 'Уточните рекомендации пациента.'));
}
function renderRoute(value) {
  const el = $('patient-route');
  $('route-actions').replaceChildren();
  if (!value) { show('patient-route', 'План пока не создан. Если ожидаете рекомендации, обратитесь к врачу; в демонстрации координатор может передать подписанный протокол.'); return; }
  const dl = node('dl');
  for (const [title, text] of [['Что случилось', value.what_happened], ['Что делать дальше', value.what_to_do], ['Когда следующий шаг', `${value.next_step_text} ${value.next_step_at ? date(value.next_step_at) : ''}`]]) {
    dl.append(node('dt', title), node('dd', text));
  }
  el.replaceChildren(dl);
  if (value.history?.length) {
    const list = node('ol');
    value.history.forEach(step => list.append(node('li', `${date(step.date)} — ${step.text}`)));
    el.append(node('h3', 'Пройденные этапы'), list);
  }
  // При нескольких напоминаниях ответ относится к явно выбранному маршруту.
  const ids = banner?.route_ids?.length ? banner.route_ids : [value.route_id];
  let target = value.route_id;
  if (!ids.includes(target)) target = ids[0];
  if (ids.length > 1) {
    const label = node('label', 'Маршрут для ответа');
    const select = node('select');
    ids.forEach((id, i) => { const option = node('option', `Напоминание ${i + 1}`); option.value = id; select.append(option); });
    select.value = target;
    select.onchange = () => { target = select.value; };
    label.append(select); $('route-actions').append(label);
  }
  if (!banner?.visible) return;
  for (const [text, action] of [[page === 'doctor' ? 'Записать' : 'Записаться', 'wants_booking'], ['Уже обратился', 'already_attended'], ...(page === 'patient' ? [['Не планирую обращаться', 'decline']] : [])]) {
    const button = node('button', text);
    button.onclick = () => run(async () => {
      if (action === 'decline') {
        await api(`/api/v1/routes/${encodeURIComponent(target)}/transition`, {to_status: 'closed_by_patient', basis: 'Пациент не планирует обращаться: ответ в демонстрационном кабинете'});
      } else {
        await api(`/api/v1/mock/lk/${selectedId}/banner/${encodeURIComponent(target)}/response`, {action});
      }
      await loadPatient(selectedId);
    });
    $('route-actions').append(button);
  }
}
function renderMessages(messages) {
  const el = $('messages'); el.replaceChildren();
  if (!messages.length) { show('messages', 'Сообщений пока нет. Рекомендации и напоминания появятся после создания плана.'); return; }
  messages.forEach(message => {
    const item = node('article', undefined, 'message');
    item.append(node('small', date(message.date)), node('p', message.text), node('span', message.read ? 'Прочитано' : 'Не прочитано', 'badge'));
    if (!message.read) {
      const button = node('button', 'Прочитать');
      button.onclick = () => run(async () => { await api(`/api/v1/mock/lk/${selectedId}/messages/${message.id}/read`, {}); await loadPatient(selectedId); });
      item.append(document.createTextNode(' '), button);
    }
    el.append(item);
  });
}
async function loadPatient(id, visitBanner) {
  selectedId = id; route = null; banner = null;
  show('patient-route', 'Загрузка плана…'); $('route-actions').replaceChildren();
  if (page === 'patient') show('messages', 'Загрузка сообщений…');
  if (visitBanner === undefined) show('unfinished-banner', 'Проверяем рекомендации пациента…'); else renderBanner(visitBanner);
  const base = `/api/v1/mock/lk/${id}`;
  try {
    const results = await Promise.all([api(base + '/route'), visitBanner === undefined ? api(base + '/banner') : Promise.resolve(visitBanner), page === 'patient' ? api(base + '/messages') : Promise.resolve([])]);
    [route, banner] = results;
    renderBanner(banner); renderRoute(route);
    if (page === 'patient') renderMessages(results[2]);
  } catch (error) {
    show('patient-route', 'Не удалось загрузить план. Повторите открытие пациента.');
    if (page === 'patient') show('messages', 'Не удалось загрузить сообщения.');
    if (visitBanner === undefined) show('unfinished-banner', 'Не удалось проверить незавершённые маршруты.');
    throw error;
  }
}
function updateStudies() {
  const patient = patients.find(p => p.patient_id === $('patient-select').value);
  $('study-select').replaceChildren();
  (patient?.studies || []).forEach((study, i) => {
    const option = node('option', `${study.study_type} · ${study.study_date} · ${study.study_id}`);
    option.value = String(i); $('study-select').append(option);
  });
  if (!patient?.studies.length) $('study-select').append(node('option', 'Исследований нет'));
}
function currentStudy() {
  const patient = patients.find(p => p.patient_id === patientId());
  return patient?.studies[Number($('study-select').value)];
}
function resetDelivery() { delivery = null; $('delivery-id').textContent = 'Следующая отправка создаст новую доставку.'; }
function details(id, data, text) {
  const container = $(id); container.replaceChildren(node('p', text));
  const disclosure = node('details'); disclosure.append(node('summary', 'Подробности ответа сервера'), node('pre', JSON.stringify(data, null, 2))); container.append(disclosure);
}
async function refreshFeed() {
  let queue;
  try { queue = await api('/api/v1/mock/mis/queue'); }
  catch (error) { show('event-feed', 'Журнал недоступен. Нажмите «Обновить ленту», чтобы повторить загрузку.'); throw error; }
  const el = $('event-feed'); el.replaceChildren(node('p', `Документов к передаче: ${queue.ready_count}. Ожидают обработки: ${queue.unprocessed_events.length}.`));
  if (!queue.recent_events.length) el.append(node('p', 'Событий пока нет. Начните с подписанного протокола исследования.'));
  queue.recent_events.forEach(event => {
    const item = node('article', undefined, 'feed-item');
    item.append(node('strong', $('event-type').querySelector(`option[value="${event.event_type}"]`)?.textContent || 'Событие медицинской системы'), node('p', `${date(event.occurred_at)} · ${event.event_id}`), node('p', event.processed_at ? `Обработано: ${date(event.processed_at)}` : 'Ожидает обработки'));
    const raw = node('details'); raw.append(node('summary', 'Что пришло'), node('pre', JSON.stringify(event.payload, null, 2))); item.append(raw); el.append(item);
  });
}
async function refreshClock() {
  let clock;
  try { clock = await api('/api/v1/demo/clock'); }
  catch (error) { $('clock').textContent = 'Не удалось получить время сервера. Повторите загрузку страницы.'; modelClock = false; throw error; }
  $('clock').textContent = `${clock.is_mock ? 'Модельное' : 'Системное'} время: ${date(clock.now)}.${clock.is_mock ? '' : ' Прокрутка отключена: сервер использует реальное время. Для демонстрации включите модельные часы в настройках запуска.'}`;
  modelClock = clock.is_mock;
  document.querySelectorAll('[data-hours]').forEach(button => { button.disabled = !modelClock; });
}
async function initPulse() {
  const types = await api('/api/v1/mis/event-types');
  types.forEach(type => { const option = node('option', eventNames[type.event_type] || 'Событие медицинской системы'); option.value = type.event_type; option.dataset.description = type.event_type === 'StudyProtocolSigned' ? 'Система прочитает подписанный протокол и при необходимости создаст план дальнейших действий для пациента.' : type.description; $('event-type').append(option); });
  $('event-type').value = 'StudyProtocolSigned';
  const describe = () => { $('event-description').textContent = $('event-type').selectedOptions[0]?.dataset.description || ''; };
  describe(); updateStudies(); resetDelivery();
  ['event-type', 'study-select', 'event-payload', 'patient-id'].forEach(id => $(id).addEventListener('change', () => { resetDelivery(); describe(); }));
  $('new-event').onclick = resetDelivery;
  $('send-event').onclick = () => run(async () => {
    if (!delivery) {
      const clock = await api('/api/v1/demo/clock');
      const payload = JSON.parse($('event-payload').value);
      if (!payload || Array.isArray(payload) || typeof payload !== 'object') throw new Error('Дополнительные факты должны быть JSON-объектом.');
      const study = currentStudy();
      delivery = {type: $('event-type').value, body: {patient_id: patientId(), event_id: 'ui:' + crypto.randomUUID(), occurred_at: clock.now, payload}};
      if (delivery.type.startsWith('StudyProtocol')) {
        if (!study) { delivery = null; throw new Error('Выберите исследование из каталога этого пациента.'); }
        delivery.body.study_id = study.study_id;
      }
    }
    $('delivery-id').textContent = `Идентификатор доставки: ${delivery.body.event_id}. Повторная отправка использует тот же факт.`;
    const result = await api(`/api/v1/mock/mis/emit/${encodeURIComponent(delivery.type)}`, delivery.body);
    const text = result.duplicate ? 'Повторная доставка. Повторные действия не выполнялись.' : `Новая доставка. Обработка завершена. Проверьте план в кабинете пациента; технические действия перечислены в подробностях.`;
    details('delivery-result', result, text);
    $('session-feed').prepend(node('p', `${eventNames[delivery.type] || 'Событие медицинской системы'} · ${delivery.body.event_id} · ${text}`));
    await refreshFeed();
  });
  $('refresh-feed').onclick = () => run(refreshFeed);
  document.querySelectorAll('[data-hours]').forEach(button => { button.onclick = () => run(async () => {
    const result = await api('/api/v1/demo/clock/advance', {hours: Number(button.dataset.hours)});
    details('time-result', result, `${date(result.from)} → ${date(result.to)}. Сработало таймеров: ${result.fired.length}. Отправлено сообщений: ${result.notifications}. Выполнено за ${Math.round(result.elapsed_ms)} мс.${result.database === 'unavailable' ? ' База недоступна: эффекты не сохранены.' : ''}`);
    await refreshClock(); await refreshFeed();
  }); });
  $('analyze').onclick = () => run(async () => {
    const study = currentStudy(); if (!study) throw new Error('Выберите исследование пациента из каталога.');
    const result = await api('/api/v1/analyze', {text: study.text, study_type: study.study_type});
    const decoder = result.decoder_used ?? result.extractor;
    const rules = decoder && /rules|dictionary|regex/i.test(decoder);
    const source = rules ? 'Анализ выполнен правилами, без модели.' : decoder ? 'Анализ выполнен моделью.' : 'Сервер не сообщил источник анализа; работа модели не подтверждена.';
    details('analysis-result', result, `${source} Решение о маршрутизации принимают правила. ${result.llm_error ? 'Модель недоступна; проверьте источник результата в подробностях.' : ''} ${result.is_emergency ? result.emergency_notice : result.route_would_be_created ? 'Рекомендован маршрут: ' + result.winning_trigger : 'Автоматический маршрут не рекомендован.'}`);
  });
  const results = await Promise.allSettled([refreshFeed(), refreshClock(), api('/api/v1/demo/scenarios').then(items => {
    $('scenarios').replaceChildren(); if (!items.length) show('scenarios', 'Сценариев пока нет. Для проверки отправьте событие и прокрутите модельное время.'); items.forEach(item => { const article = node('article', undefined, 'feed-item'); article.append(node('h3', item.name), node('p', item.description), node('p', 'Прокрутка: ' + item.expected_advance)); $('scenarios').append(article); });
  }).catch(error => { show('scenarios', 'Не удалось загрузить сценарии. Повторите загрузку страницы.'); throw error; })]);
  const failures = results.filter(result => result.status === 'rejected');
  if (failures.length) throw new Error(failures.map(result => result.reason.message).join('\n'));
}
$('patient-select').onchange = () => {
  $('patient-id').value = ''; selectedId = ''; route = null; banner = null;
  if (page === 'pulse') { updateStudies(); resetDelivery(); }
  else { renderRoute(null); show('unfinished-banner', 'Откройте пациента или новый визит, чтобы проверить напоминание.'); if (page === 'patient') show('messages', 'Откройте выбранного пациента.'); }
};
$('patient-id').oninput = () => {
  selectedId = ''; route = null; banner = null;
  if (page === 'pulse') resetDelivery();
  else { $('route-actions').replaceChildren(); show('patient-route', 'Откройте пациента с новым идентификатором.'); show('unfinished-banner', 'Откройте пациента или новый визит, чтобы проверить напоминание.'); if (page === 'patient') show('messages', 'Откройте пациента с новым идентификатором.'); }
};
$('load-patient').onclick = () => run(async () => {
  const id = patientId();
  if (page === 'pulse') { selectedId = id; $('clinical-status').textContent = 'Пациент выбран.'; return; }
  if (page === 'doctor') { $('visit-status').textContent = 'Визит ещё не открыт для выбранного пациента.'; }
  await loadPatient(id);
});
if (page === 'doctor') $('start-visit').onclick = () => run(async () => {
  const id = patientId();
  selectedId = ''; route = null; banner = null;
  $('route-actions').replaceChildren();
  show('patient-route', 'Открываем новый визит…');
  show('unfinished-banner', 'Проверяем незавершённые рекомендации…');
  $('visit-status').textContent = 'Открываем визит…';
  let result;
  try {
    const clock = await api('/api/v1/demo/clock');
    result = await api('/api/v1/mis/events', {event_id: 'visit:' + crypto.randomUUID(), event_type: 'VisitStarted', occurred_at: clock.now, subject: {patient_id: id}, payload: {}});
  } catch (error) {
    $('visit-status').textContent = 'Не удалось подтвердить открытие визита.';
    show('unfinished-banner', 'Не удалось проверить незавершённые рекомендации. Повторите открытие визита.');
    show('patient-route', 'План пациента не загружен.');
    throw error;
  }
  $('visit-status').textContent = `Визит открыт. ${result.has_unfinished_routes ? 'Есть незавершённые рекомендации.' : 'Незавершённых рекомендаций нет.'}`;
  renderBanner(result.unfinished_routes_banner);
  await loadPatient(id, result.unfinished_routes_banner);
});
document.querySelectorAll('.site-nav a').forEach(link => { if (link.pathname === location.pathname) link.setAttribute('aria-current', 'page'); });
run(async () => {
  try {
    patients = await api('/api/v1/mock/mis/patients');
    $('patient-select').replaceChildren(node('option', 'Выберите пациента'));
    $('patient-select').firstChild.value = '';
    patients.forEach(patient => { const option = node('option', `${patient.name} · ${patient.age} лет · ${patient.external_id}`); option.value = patient.patient_id; $('patient-select').append(option); });
    if (!patients.length) $('patient-select').firstChild.textContent = 'Пациентов пока нет — запросите демонстрационные данные';
  } catch (error) {
    $('patient-select').replaceChildren(node('option', 'Каталог недоступен — повторите загрузку'));
    $('patient-select').firstChild.value = '';
    if (page !== 'pulse') throw error;
  }
  if (page === 'pulse') await initPulse();
});
