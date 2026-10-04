'use strict';
const el = id => document.getElementById(id);
const fmt = value => value == null ? 'недостаточно данных' : `${(100 * value).toFixed(1)}%`;
let baseline;
const reasons = {negative_context:'отрицательный контекст', threshold_not_met:'порог не выполнен', study_type_mismatch:'другой профиль УЗИ', no_match:'находка не извлечена'};
async function api(path, options) {
  const response = await fetch(`/api/v1${path}`, options);
  const body = await response.json();
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
  el(target).replaceChildren(t);
}
async function refresh() {
  try {
    const [report, runs] = await Promise.all([api('/analytics/metrics'), api('/analytics/analyses')]);
    baseline = report;
    el('comparison').textContent='';
    el('cards').replaceChildren(...[
      ['Разборов', report.analyses], ['Recall по врачу', fmt(report.doctor_metrics.recall)],
      ['Precision по врачу', fmt(report.doctor_metrics.precision)], ['Доля отклонённых находок', fmt(report.rejected_fraction)],
      ['Врачебных отметок', report.reviewed], ['Сгенерированных меток', report.generated_labels],
      ['Recall всей разметки', fmt(report.metrics.recall)], ['Precision всей разметки', fmt(report.metrics.precision)]
    ].map(([title, value]) => {const c = node('article'); c.className='card'; c.append(node('small', title), node('strong', value)); return c;}));
    el('decoders').textContent = 'Фактический декодер: ' + (Object.entries(report.decoders).map(([k,v]) => `${k === 'rules' ? 'правила' : k === 'llm' ? 'модель' : k}: ${v}`).join(', ') || 'разборов пока нет');
    table('triggers', ['Триггер', 'Встречаемость', 'Срабатывания', 'Подтверждено', 'Отклонено', 'Пропущено', 'Ошибки', 'Recall', 'Precision'], report.by_trigger.map(r => [r.display_name || r.trigger_id,r.occurrences,r.fired,r.confirmed,r.rejected,r.missed,fmt(r.error_rate),fmt(r.recall),fmt(r.precision)]));
    table('studies', ['Профиль', 'Recall', 'Precision'], report.by_study.map(r => [r.study_type,fmt(r.recall),fmt(r.precision)]));
    el('trend').replaceChildren();
    report.timeline.forEach(r => {
      const line = node('p', `${r.date}: recall ${fmt(r.recall)}, precision ${fmt(r.precision)}`);
      if (r.recall != null) { const meter = node('meter'); meter.min=0; meter.max=1; meter.value=r.recall; meter.setAttribute('aria-label', `Recall ${r.date}`); line.append(meter); }
      el('trend').append(line);
    });
    if (!report.timeline.length) el('trend').textContent = 'недостаточно данных';
    el('runs').replaceChildren();
    runs.forEach(run => {
      const section = node('details');
      section.append(node('summary', `${run.created_at} · ${run.study_type || 'Профиль не указан'} · ${run.decoder_used === 'rules' ? 'правила' : run.decoder_used} · ${run.duration_ms.toFixed(1)} мс · ${run.id}`));
      if (run.fallback) section.append(node('p', 'Модель не ответила: использован резервный декодер'));
      run.matches.forEach(m => {
        const line = node('div', `${m.display_name || m.trigger_id} · v${m.version}: ${m.fired ? 'сработал' : 'не сработал'}${m.suppression_reason ? ` (${reasons[m.suppression_reason] || m.suppression_reason})` : ''} `);
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
  } catch (error) {el('status').textContent=error.message;}
}
el('refresh').onclick = refresh;
el('compare').onclick = async () => {
  try {
    const current = await api('/analytics/metrics?current=true');
    el('comparison').textContent = `На той же выборке: recall ${fmt(baseline.metrics.recall)} → ${fmt(current.metrics.recall)}; precision ${fmt(baseline.metrics.precision)} → ${fmt(current.metrics.precision)}. ${current.message} Пар, требующих нового разбора: ${current.reanalysis_required_pairs}.`;
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
