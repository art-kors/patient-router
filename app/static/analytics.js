'use strict';
const el = id => document.getElementById(id);
const fmt = value => value == null ? 'Метрики посчитать пока нечем: добавьте разбор и проверку врача' : `${Math.round(100 * value)} из 100`;
let baseline;
const reasons = {negative_context:'отрицательный контекст', threshold_not_met:'порог не выполнен', study_type_mismatch:'другой профиль УЗИ', no_match:'находка не извлечена'};
async function api(path, options) {
  const response = await fetch(`/api/v1${path}`, options).catch(() => { throw new Error('Нет связи с локальным сервером. Проверьте, что он запущен, и нажмите «Обновить».'); });
  const body = await response.json().catch(() => { throw new Error('Сервер вернул непонятный ответ. Повторите действие.'); });
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Проверьте данные запроса');
  return body;
}
function node(tag, text) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  return element;
}
function table(target, headers, rows) {
  const t = node('table'), head = node('tr'), body = node('tbody');
  headers.forEach(h => head.append(node('th', h)));
  t.append(head, body);
  rows.forEach(row => {
    const tr = node('tr'); row.forEach(value => tr.append(node('td', value))); body.append(tr);
  });
  el(target).replaceChildren(rows.length ? t : node('p', 'Данных пока нет. Разберите протокол и сохраните оценку врача.'));
}
async function refresh() {
  el('status').textContent = 'Загрузка разборов и оценок…';
  baseline = null; el('compare').disabled = true;
  try {
    const [report, runs] = await Promise.all([api('/analytics/metrics'), api('/analytics/analyses')]);
    baseline = report; el('compare').disabled = false;
    el('comparison').textContent='';
    el('cards').replaceChildren(...[
      ['Сохранённых протоколов для проверки', report.analyses], ['Находим находки: проверено врачом', fmt(report.doctor_metrics.recall)],
      ['Верные сигналы: проверено врачом', fmt(report.doctor_metrics.precision)], ['Ложных находок среди оценённых врачом', fmt(report.rejected_fraction)],
      ['Оценок находок, сохранённых врачом', report.reviewed], ['Ответов для проверки, созданных автоматически', report.generated_labels],
      ['Находим находки: вся разметка', fmt(report.metrics.recall)], ['Верные сигналы: вся разметка', fmt(report.metrics.precision)]
    ].map(([title, value]) => {const c = node('article'); c.className='card'; c.append(node('small', title), node('strong', value)); return c;}));
    el('decoders').textContent = 'Способ извлечения находок из протокола: ' + (Object.entries(report.decoders).map(([k,v]) => `${k === 'rules' ? 'правила' : 'модель'}: ${v}`).join(', ') || 'разборов пока нет');
    table('triggers', ['Триггер', 'Встречаемость', 'Срабатывания', 'Подтверждено', 'Отклонено', 'Пропущено', 'Ошибки', 'Находим находки', 'Верные сигналы'], report.by_trigger.map(r => [r.display_name || r.trigger_id,r.occurrences,r.fired,r.confirmed,r.rejected,r.missed,fmt(r.error_rate),fmt(r.recall),fmt(r.precision)]));
    table('studies', ['Профиль', 'Находим находки', 'Верные сигналы'], report.by_study.map(r => [r.study_type,fmt(r.recall),fmt(r.precision)]));
    el('trend').replaceChildren();
    report.timeline.forEach(r => {
      const line = node('p', `${r.date}: находим находки ${fmt(r.recall)}, верные сигналы ${fmt(r.precision)}`);
      if (r.recall != null) { const meter = node('meter'); meter.min=0; meter.max=1; meter.value=r.recall; meter.setAttribute('aria-label', `Полнота за ${r.date}`); line.append(meter); }
      el('trend').append(line);
    });
    if (!report.timeline.length) el('trend').textContent = 'Метрики посчитать пока нечем: добавьте разбор и проверку врача';
    el('runs').replaceChildren();
    if (!runs.length) el('runs').append(node('p', 'Разборов пока нет. Введите протокол в форме выше, затем проверьте находки.'));
    runs.forEach(run => {
      const section = node('details');
      section.append(node('summary', `${new Date(run.created_at).toLocaleString('ru-RU')} · ${run.study_type || 'Профиль не указан'} · ${run.decoder_used === 'rules' ? 'правила' : 'модель'} · ${Math.round(run.duration_ms)} мс · ${run.id}`));
      if (run.fallback) section.append(node('p', 'Модель не ответила: использован резервный декодер'));
      run.matches.forEach(m => {
        const line = node('div', `${m.display_name || m.trigger_id} · версия ${m.version}: ${m.fired ? 'сработал' : 'не сработал'}${m.suppression_reason ? ` (${reasons[m.suppression_reason] || m.suppression_reason})` : ''} `);
        const select = node('select');
        const options = m.fired ? [['confirmed','Подтверждено'],['false_positive','Ложная находка']] : [['missed','Пропущена находка']];
        select.append(new Option('Выберите оценку', ''));
        options.forEach(([value,text]) => select.append(new Option(text,value)));
        select.value = m.feedback || '';
        const button = node('button','Сохранить отметку');
        button.onclick = async () => {
          if (!select.value) {el('status').textContent='Выберите оценку врача'; return;}
          button.disabled = true;
          try {await api(`/analytics/analyses/${run.id}/feedback/${encodeURIComponent(m.trigger_id)}`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({label:select.value})}); await refresh();}
          catch (error) {el('status').textContent=error.message;} finally {button.disabled=false;}
        };
        line.append(select,button); section.append(line);
      });
      el('runs').append(section);
    });
    el('status').textContent='Метрики пересчитаны' + (report.unlinked_labels ? `. Для ${report.unlinked_labels} меток нет сохранённого протокола` : '');
  } catch (error) {
    el('status').textContent=error.message + '. Нажмите «Обновить», чтобы повторить загрузку.';
    el('cards').replaceChildren(node('p', 'Оценки недоступны: данные не загружены. Это не означает нулевое качество.'));
    el('runs').replaceChildren(node('p', 'Список разборов не загружен. Повторите загрузку кнопкой «Обновить».'));
  }
}
el('refresh').onclick = refresh;
el('compare').onclick = async () => {
  if (!baseline) { el('status').textContent = 'Сначала загрузите исходную оценку кнопкой «Обновить».'; return; }
  el('comparison').textContent = 'Сравниваем правила на той же выборке…';
  try {
    const current = await api('/analytics/metrics?current=true');
    el('comparison').textContent = `На той же выборке: находим находки ${fmt(baseline.metrics.recall)} → ${fmt(current.metrics.recall)}; верные сигналы ${fmt(baseline.metrics.precision)} → ${fmt(current.metrics.precision)}. Для учёта изменений текста повторите разбор протокола. Пар, требующих нового разбора: ${current.reanalysis_required_pairs}.`;
  } catch (error) {el('status').textContent=error.message;}
};
el('analyze').onsubmit = async event => {
  event.preventDefault();
  const form = event.currentTarget, button = form.querySelector('button'); button.disabled = true;
  try {
    const data = new FormData(form);
    const response = await api('/analyze',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({text:data.get('text'),study_type:data.get('study_type') || null})});
    form.reset();
    el('analysis-result').textContent=`Разбор ${response.analysis_id}: ${response.triggered_count} срабатываний. Декодер: ${response.decoder_used === 'rules' ? 'правила' : response.decoder_used}`;
    await refresh();
  } catch (error) {el('status').textContent=error.message;} finally {button.disabled=false;}
};
refresh();

document.querySelectorAll('.site-nav a').forEach(link => { if (link.pathname === location.pathname) link.setAttribute('aria-current', 'page'); });
