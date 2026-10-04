// Минимальный DOM: запускаем целые скрипты и проверяем реально созданный текст.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.value = ''; this.dataset = {}; this.hidden = false; this.disabled = false; this.text = ''; this.classList = {toggle(){}}; }
  set textContent(value) { this.text = String(value ?? ''); this.children = []; }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(' '); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.text = ''; this.children = children; }
  setAttribute() {}
  addEventListener(type, fn) { this['on' + type] = fn; }
  get firstChild() { return this.children[0]; }
  get selectedOptions() { return this.children.filter(c => c.value === this.value); }
  querySelector() { return this.children[0] || null; }
}
const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const pending = () => new Promise(resolve => setImmediate(resolve));
async function check(page, script) {
  const ids = new Map();
  const get = id => { if (!ids.has(id)) ids.set(id, new Element()); return ids.get(id); };
  const document = {body: {dataset: {page}}, getElementById: get, createElement: tag => new Element(tag), querySelectorAll: () => []};
  get('sort').value = 'date';
  const fieldFor = name => {
    const find = element => element.name === name ? element : element.children.map(find).find(Boolean);
    return find(get('fields')) || get('field-' + name);
  };
  get('editor').elements = new Proxy({}, {get:(_,name) => fieldFor(name)});
  get('login').elements = {username: new Element(), password: new Element()};
  if (page !== 'index') document.getElementById = id => id === 'login' ? null : get(id);
  const calls = [];
  const responseFor = path => {
    if (path.includes('/quality/metrics/by-trigger') || path.includes('/quality/metrics/timeline')) return [];
    if (path.includes('/quality/metrics')) return {tp:1,fn:0,fp:0,tn:1,recall:1,precision:1,fpr:0,f1:1};
    if (path.includes('/quality/errors')) return [{type:'fp',trigger_id:'test',study_id:'s',quote:fixture.raw}];
    if (path.includes('/admin/triggers')) return [fixture.trigger];
    if (path.includes('/admin/validate')) return {warnings:[]};
    if (path.includes('/analytics/metrics')) return {analyses:1,doctor_metrics:{recall:1,precision:1},metrics:{recall:1,precision:1},decoders:{rules:1},by_trigger:[],by_study:[],timeline:[]};
    if (path.includes('/analytics/analyses')) return [{id:'r',matches:[{display_name:'Полип',fired:true,version:1}],study_type:'УЗИ',decoder_used:'rules',created_at:'2026-08-26',duration_ms:1}];
    if (path.includes('/auth/me')) return {role:'patient',patient_ids:['own']};
    if (path.endsWith('/cabinet')) return {protocols:[],routes:[],appointments:[]};
    if (path.endsWith('/messages')) return [];
    if (path.endsWith('/banner')) return {visible:false};
    if (path.includes('/auth/demo')) return {roles:['coordinator','doctor','patient','hospitalization_manager','admin','manager'],password:'transient',enabled:true};
    if (path.includes('/coordinator/study-types')) return ['УЗИ органов малого таза'];
    if (path.includes('/coordinator/patients')) return fixture.patients;
    if (path.includes('/coordinator/tasks')) return [];
    if (path.includes('/mock/mis/patients')) return fixture.catalog;
    if (path.includes('/mock/mis/queue')) return {ready_count:0,unprocessed_events:[],recent_events:[{event_type:'StudyProtocolSigned',payload:{text:fixture.raw},occurred_at:'2026-08-26'}]};
    if (path.includes('/demo/clock')) return {now:'2026-08-26T00:00:00Z', is_mock:true};
    if (path.includes('/demo/scenarios')) return [];
    if (path.includes('/mis/event-types')) return [{event_type:'StudyProtocolSigned',description:'Протокол подписан'}];
    throw new Error('Неожиданный путь ' + path);
  };
  const context = vm.createContext({document, Node:Element, Headers, location:{pathname:'/' + page}, sessionStorage:{getItem:()=> 'token',setItem(){},removeItem(){}}, console, AbortController, crypto:require('crypto').webcrypto, setTimeout,clearTimeout, Option:class extends Element {constructor(text,value){super('option');this.textContent=text;this.value=value;}}, fetch:async (path,options) => { calls.push({path,options}); return {ok:true,status:200,json:async () => responseFor(path)}; }});
  context.window = context;
  vm.runInContext(fs.readFileSync('app/static/auth.js','utf8'), context);
  if (script) vm.runInContext(fs.readFileSync('app/static/' + script,'utf8'), context);
  for (let i=0;i<15;i++) await pending();
  if (page === 'admin') {
    context.triggerFixture = fixture.trigger;
    vm.runInContext('edit(triggerFixture)', context);
    assert(fieldFor('potential_route').value.includes('[формулировка скрыта]'));
    assert.strictEqual(fieldFor('potential_route').originalValue, fixture.trigger.potential_route);
  }
  if (page === 'coordinator') {
    context.data = fixture.protocols;
    vm.runInContext('renderProtocols(data)',context);
    assert.strictEqual(get('protocols').children.filter(c=>c.tagName === 'details').length,15);
    assert(get('protocols').textContent.includes(fixture.quote));
    context.empty = {items:[],total:0,message:'Пациенты не найдены. Измените запрос или загрузите протокол.'};
    vm.runInContext('renderPatients(empty)',context);
    assert(get('patients').textContent.includes('Измените запрос'));
  }
  if (page === 'doctor') {
    context.routeFixture = {route_id:'r',what_happened:'Находка из протокола',what_to_do:'Обратитесь к специалисту',next_step_text:'Запись на приём',history:[{date:'2026-08-26',text:'Маршрут создан'}]};
    vm.runInContext('renderRoute(routeFixture); renderBanner({visible:true,text:"Есть незавершённые рекомендации"})',context);
    assert(get('patient-route').textContent.includes('Обратитесь к специалисту'));
  }
  if (page === 'patient') {
    context.fixtureData = {protocols:[{date:'2026-08-26',type:'УЗИ',findings:['Находка'],meaning:'Требует внимания',recommendations:[]}], routes:[1,2].map(i=>({route_id:'secret-'+i,finding:'Находка '+i,specialty:'Гинеколог',clinic:'Клиника',stage:'Ждём записи на приём',what_to_do:'Свяжитесь с клиникой',what_next:'Появятся рекомендации',steps:[],completed:false})), appointments:[{date:'2026-08-26',type:'Контроль',where:'Клиника',who:'Врач',state:'Запись ещё не подтверждена'}]};
    vm.runInContext('renderProtocols(fixtureData.protocols); renderRoutes(fixtureData.routes); renderAppointments(fixtureData.appointments); renderMessages([{id:"hidden",date:"2026-08-26",text:"Новое сообщение",read:false}])',context);
    assert.strictEqual(get('patient-route').children.filter(c=>c.tagName==='article').length,2);
    assert(get('messages').textContent.includes('не прочитано'));
    assert(get('appointments').textContent.includes('ещё не подтверждена'));
    assert(!get('patient-route').textContent.includes('secret-'));
    assert(!calls.some(c=>c.path.includes('/mock/mis/patients')));
  }
  const visible = e => e.textContent + ' ' + e.value + ' ' + e.children.map(visible).join(' ');
  let output = [...ids.values()].map(visible).join(' ');
  if (page === 'doctor') output = output.replaceAll('Оперативное лечение показано','');
  assert(!/(?<![а-яё])(?:диагноз|лечени)[а-яё]*|история пациента/i.test(output), `${page}: запрещённые слова в выводе: ${output.match(/.{0,45}(?<![а-яё])(?:диагноз|лечени|история пациента).{0,45}/i)?.[0]}`);
  assert(calls.filter(c=>c.path.startsWith('/api/')).every(c=>c.options.headers.get('Authorization')==='Bearer token'));
  if (page === 'index') assert.strictEqual(get('demo-roles').children.length,6);
  assert(!/Неожиданный путь/.test(output));
}
(async()=> {await check('index'); await check('coordinator','coordinator.js'); await check('admin','dashboard.js'); await check('analytics','analytics.js'); await check('patient','patient.js'); for (const page of ['doctor','pulse']) await check(page,'clinical.js'); console.log('Проверен вывод скриптов: вход, координатор, пациент, врач, события МИС');})().catch(e=>{console.error(e);process.exitCode=1;});
