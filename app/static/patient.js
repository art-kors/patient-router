'use strict';
// Кабинет получает область пациента только от сервера, параметры URL её не меняют.
const $ = id => document.getElementById(id);
let selectedId = '', busy = false;
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = presentText(text);
  if (className) element.className = className;
  return element;
}
const date = value => value ? new Date(value).toLocaleString('ru-RU', {timeZone:'Europe/Moscow', ...(String(value).length === 10 ? {year:'numeric',month:'long',day:'numeric'} : {})}) : 'Дата не согласована — уточните в клинике';
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(response.status === 401 ? 'Для открытия своего кабинета войдите с ролью пациента.' : response.status === 403 ? 'Этот кабинет недоступен вашей учётной записи.' : typeof data.detail === 'string' ? data.detail : 'Не удалось загрузить данные. Повторите действие.');
  return data;
}
async function run(action) {
  if (busy) return;
  busy = true;
  $('clinical-status').textContent = 'Загрузка…';
  try { await action(); if ($('clinical-status').textContent === 'Загрузка…') $('clinical-status').textContent = 'Кабинет обновлён.'; }
  catch(error) { $('clinical-status').textContent = presentText(error instanceof TypeError ? 'Нет связи с сервером. Проверьте подключение и нажмите «Обновить кабинет».' : error.message); }
  finally { busy = false; }
}
function empty(id, text) { $(id).replaceChildren(node('p', text)); }
function renderProtocols(items) {
  $('protocols').replaceChildren();
  if (!items.length) return empty('protocols', 'Исследований пока нет. После передачи подписанного протокола клиникой он появится здесь. Если ожидаете результат, свяжитесь с клиникой.');
  for (const item of items) {
    const detail = node('details'); detail.append(node('summary', `${date(item.date)} · ${item.type}`));
    detail.append(node('h3','Что найдено'), node('p', item.findings.length ? item.findings.join('; ') : 'Находки для автоматических рекомендаций не выделены. Уточните результаты у врача.'), node('h3','Что это значит'), node('p',item.meaning), node('h3','Куда и когда обратиться'));
    for (const recommendation of item.recommendations) detail.append(node('p',`${recommendation.specialty} · ${recommendation.clinic} · срок: ${date(recommendation.deadline)}`));
    if (!item.recommendations.length) detail.append(node('p','Обсудите результаты с врачом. Специалист и срок консультации пока не определены.'));
    $('protocols').append(detail);
  }
}
function renderRoutes(items) {
  $('patient-route').replaceChildren(); $('completed-routes').replaceChildren();
  if (!items.some(item => !item.completed)) empty('patient-route','Активных маршрутов нет. Если ожидаете рекомендации по исследованию, обратитесь в клинику.');
  if (!items.some(item => item.completed)) empty('completed-routes','Завершённых маршрутов пока нет. Пройденные планы появятся здесь.');
  for (const item of items) {
    const card = node('article', undefined, 'route-card');
    card.append(node('h3',item.finding),node('p',item.stage));
    if (!item.completed) {
      card.append(node('p',`${item.specialty} · ${item.clinic}`),node('p',`Рекомендованный срок: ${date(item.deadline)}`),node('h4','Что делать сейчас'),node('p',item.what_to_do),node('h4','Что произойдёт дальше'),node('p',item.what_next));
      const steps = node('details'); steps.append(node('summary','Пройденные этапы'));
      for (const step of item.steps) steps.append(node('p',`${date(step.date)} · ${step.text}`));
      if (!item.steps.length) steps.append(node('p','Следующие этапы появятся после обращения в клинику.'));
      card.append(steps);
      for (const [label, action] of [...(item.can_confirm ? [['Подтверждаю запись','confirmed_booking']] : []), ['Не смогу прийти','cannot_attend'],['Другой вопрос','question']]) {
        const button = node('button',label); button.type = 'button';
        button.onclick = () => run(async () => { const result = await api(`/api/v1/mock/lk/${selectedId}/banner/${item.route_id}/response`,{action}); await loadCabinet(); $('clinical-status').textContent = result.message; });
        card.append(button);
      }
      card.append(node('p','Ответ «Не смогу прийти» передаёт вопрос координатору. Для отмены записи свяжитесь с клиникой.'));
    }
    $(item.completed ? 'completed-routes' : 'patient-route').append(card);
  }
}
function renderMessages(items) {
  $('messages').replaceChildren();
  if (!items.length) return empty('messages','Уведомлений пока нет. Здесь появятся рекомендации и напоминания клиники. Проверьте свои маршруты.');
  for (const item of items) {
    const card = node('article',undefined,item.read ? 'message' : 'message unread');
    card.append(node('small',date(item.date)),node('p',item.text),node('strong',item.read ? 'Прочитано' : 'Новое · не прочитано'));
    if (!item.read) {
      const button = node('button','Отметить прочитанным'); button.type = 'button';
      button.onclick = () => run(async () => { await api(`/api/v1/mock/lk/${selectedId}/messages/${item.id}/read`,{}); await loadCabinet(); }); card.append(button);
    }
    $('messages').append(card);
  }
}
function renderAppointments(items) {
  $('appointments').replaceChildren();
  if (!items.length) return empty('appointments','Записей в медицинской системе пока нет. Если в маршруте рекомендован приём, пора записаться: свяжитесь с клиникой. После подтверждения клиникой запись появится здесь.');
  for (const item of items) {
    const card = node('article',undefined,'route-card');
    card.append(node('h3',item.type),node('p',date(item.date)),node('p',item.where),node('p',item.who),node('strong',item.state)); $('appointments').append(card);
  }
}
async function loadCabinet() {
  for (const id of ['protocols','patient-route','completed-routes','messages','appointments','unfinished-banner']) empty(id,'Загрузка данных своего кабинета…');
  const me = await api('/api/v1/auth/me');
  if (me.role !== 'patient') throw new Error('Войдите с ролью пациента на странице выбора роли.');
  selectedId = me.patient_ids[0];
  if (!selectedId) throw new Error('Кабинет ещё не связан с вашей учётной записью. Обратитесь в клинику.');
  const base = `/api/v1/mock/lk/${encodeURIComponent(selectedId)}`;
  const [data, messages, banner] = await Promise.all([api(base+'/cabinet'),api(base+'/messages'),api(base+'/banner')]);
  renderProtocols(data.protocols); renderRoutes(data.routes); renderMessages(messages); renderAppointments(data.appointments);
  $('unfinished-banner').replaceChildren(node('h2',banner.visible ? 'Есть следующие шаги' : 'Все планы завершены'),node('p',banner.visible ? 'Проверьте каждый маршрут ниже и ответьте на напоминание в нужной карточке.' : 'Если у вас появились вопросы по результатам, свяжитесь с клиникой.'));
}
$('refresh').onclick = () => run(loadCabinet);
$('logout').onclick = () => { sessionStorage.removeItem('access_token'); location.href = '/'; };
run(loadCabinet);
