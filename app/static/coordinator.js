/* Весь пользовательский текст создаётся через textContent: содержимое файла не становится HTML. */
const byId = id => document.getElementById(id);
let selectedPatient = null, previewTicket = null, uploadPatient = null;
let previewRevision = 0, patientRevision = 0;
function element(tag, text) { const node = document.createElement(tag); node.textContent = presentText(text); return node; }
async function api(path, options = {}) {
  const response = await fetch('/api/v1/coordinator' + path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(response.status === 401 ? 'Войдите через страницу выбора роли.' : typeof data.detail === 'string' ? data.detail : 'Не удалось выполнить запрос. Проверьте поля и повторите.');
  return data;
}
async function run(action) { try { await action(); byId('status').textContent = ''; } catch(error) { byId('status').textContent = error.message; } }
function invalidatePreview() { previewRevision++; previewTicket = null; byId('preview').hidden = true; byId('reviewed').checked = false; byId('confirm').disabled = true; }
function renderPatients(data) {
  byId('patients').replaceChildren(); byId('count').textContent = 'Найдено пациентов: ' + data.total;
  if (!data.items.length) byId('patients').append(element('p', data.message));
  for (const patient of data.items) {
    const button = element('button', `${patient.name} · карта ${patient.card} · ${patient.date} · протоколов: ${patient.protocol_count}`);
    button.type = 'button';
    button.onclick = () => run(() => openPatient(patient)); byId('patients').append(button);
  }
}
function renderProtocols(data) {
  const container = byId('protocols'); container.replaceChildren(element('p', 'Всего протоколов: ' + data.total));
  if (!data.items.length) container.append(element('p', 'Протоколов пока нет. Загрузите обезличенный документ для выбранного пациента.'));
  for (const protocol of data.items) {
    const detail = element('details', '');
    detail.append(element('summary', `${protocol.date} · ${protocol.study_type} · ${protocol.study_id}`));
    if (!protocol.findings.length) detail.append(element('p', 'Находок не обнаружено.'));
    for (const finding of protocol.findings) {
      if (presentText(finding.quote) !== finding.quote) detail.append(element('p', 'Часть формулировки скрыта на экране. В протоколе цитата сохранена дословно.'));
      detail.append(element('p', finding.finding), element('blockquote', finding.quote), element('p', `Смещения: ${finding.char_start}–${finding.char_end}`));
    }
    for (const rule of protocol.rules) detail.append(element('p', `Правило: ${rule.rule} · версия ${rule.version ?? 'указана в правиле'} · ${rule.fired ? 'сработало' : 'не сработало'}`));
    for (const route of protocol.routes) detail.append(element('p', `Маршрут: ${route.id} · этап ${route.status} · срок ${route.target_date || 'не указан'}`));
    if (!protocol.routes.length) detail.append(element('p', 'Маршрут отсутствует.'));
    container.append(detail);
  }
}
async function openPatient(patient) {
  const revision = ++patientRevision;
  invalidatePreview(); selectedPatient = patient.id;
  byId('patient-title').textContent = 'Карточка пациента: ' + patient.name;
  byId('upload-target').textContent = 'Протокол будет добавлен выбранному пациенту: ' + patient.name;
  byId('protocols').textContent = 'Загрузка протоколов…';
  const data = await api('/patients/' + encodeURIComponent(patient.id) + '/protocols');
  if (revision === patientRevision) renderProtocols(data);
}
async function searchPatients() { renderPatients(await api('/patients?q=' + encodeURIComponent(byId('query').value) + '&sort=' + byId('sort').value)); }
byId('search').onsubmit = event => { event.preventDefault(); run(searchPatients); };
byId('sort').onchange = () => run(searchPatients);
byId('new-patient').onclick = () => { patientRevision++; selectedPatient = null; invalidatePreview(); byId('patient-title').textContent = 'Карточка пациента'; byId('upload-target').textContent = 'Будет создан новый обезличенный пациент.'; byId('protocols').textContent = 'Выберите пациента, чтобы открыть все его протоколы.'; };
byId('upload').onchange = invalidatePreview;
byId('upload').onsubmit = event => {
  event.preventDefault(); invalidatePreview();
  const target = selectedPatient;
  const revision = previewRevision;
  run(async () => {
    const data = await api('/upload/preview', {method:'POST', body:new FormData(byId('upload'))});
    if (target !== selectedPatient || revision !== previewRevision) return;
    uploadPatient = target; previewTicket = data.ticket;
    byId('changes').replaceChildren();
    for (const change of data.changes) byId('changes').append(element('li', `${change.kind}: «${change.original}» → ${change.replacement} (${change.start}–${change.end})`));
    if (!data.changes.length) byId('changes').append(element('li', 'Распознанных персональных данных нет. Проверьте текст вручную.'));
    byId('clean-text').textContent = presentText(data.text); byId('notice').textContent = data.notice; byId('preview').hidden = false;
  });
};
byId('reviewed').onchange = () => { byId('confirm').disabled = !byId('reviewed').checked; };
byId('confirm').onclick = () => run(async () => {
  byId('confirm').disabled = true;
  try {
    const data = await api('/upload/confirm', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ticket:previewTicket, patient_id:uploadPatient, reviewed:byId('reviewed').checked})});
    invalidatePreview(); await searchPatients(); await openPatient({id:data.patient_id, name:'Выбранный пациент'});
  } finally { byId('confirm').disabled = !previewTicket || !byId('reviewed').checked; }
});
run(async () => {
  const types = await api('/study-types');
  for (const type of types) { const option = element('option', type); option.value = type; byId('study-type').append(option); }
  await searchPatients();
  const tasks = await api('/tasks');
  byId('tasks').replaceChildren();
  if (!tasks.length) byId('tasks').append(element('p', 'Открытых задач пока нет.'));
  for (const task of tasks) byId('tasks').append(element('p', `${task.task_type} · срок ${task.due_at} · ${task.status}`));
});
