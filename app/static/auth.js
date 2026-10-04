'use strict';
// Экранная замена не изменяет медицинские данные в базе.
function presentText(value) {
  return String(value ?? '').replace(/(?<![а-яё])(?:диагноз|лечени)[а-яё]*/gi, '[формулировка скрыта]').replace(/(?:история|карточка) пациента/gi, 'мои исследования');
}
const originalFetch = window.fetch.bind(window);
window.fetch = (path, options = {}) => {
  const headers = new Headers(options.headers || {});
  const token = sessionStorage.getItem('access_token');
  if (String(path).startsWith('/api/') && token) headers.set('Authorization', 'Bearer ' + token);
  return originalFetch(path, {...options, headers});
};
const loginForm = document.getElementById('login');
if (loginForm) {
  const destinations = {patient:'/patient',doctor:'/doctor',coordinator:'/coordinator',hospitalization_manager:'/pulse',admin:'/admin',manager:'/analytics'};
  const names = {patient:'Пациент',doctor:'Врач',coordinator:'Координатор',hospitalization_manager:'Менеджер госпитализации',admin:'Администратор',manager:'Руководитель'};
  loginForm.onsubmit = async event => {
    event.preventDefault();
    try {
      const response = await fetch('/api/v1/auth/login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:loginForm.elements.username.value,password:loginForm.elements.password.value})});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Не удалось войти');
      sessionStorage.setItem('access_token', data.access_token);
      location.href = destinations[data.role];
    } catch(error) { document.getElementById('login-status').textContent = error.message; }
  };
  fetch('/api/v1/auth/demo').then(response => response.json()).then(data => {
    for (const role of data.roles) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = names[role];
      button.onclick = () => { loginForm.elements.username.value = role; loginForm.elements.password.value = data.password; loginForm.onsubmit({preventDefault(){}}); };
      document.getElementById('demo-roles').append(button);
    }
  });
}
