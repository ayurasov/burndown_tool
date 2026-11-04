(function(){
"use strict";

/* ════════════════════════════════════════════════════════════════
   STATE
════════════════════════════════════════════════════════════════ */
var currentUser = null;
var projects = [];
var currentProjectId = null;
var currentProjectRole = null; /* 'admin', 'manager', 'user' */
var STATE = { services:[], entries:[], stageTargets:{}, settings:{}, jira:null, jiraComp:null };
var currentDataServiceId = null;
var burnRangeOverride = null;
var stageBurnRangeOverride = null;
var selectedAdminUser = null;
function localIsoDate(d){
  return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
}
/* «Сегодня» вычисляется при каждом обращении — вкладка может быть открыта после полуночи */
function todayIso(){ return localIsoDate(new Date()); }

var dashboardFilter = { hideDone:false, epics:null }; /* epics: {id:bool} выбор эпиков для дашборда; null = видны все */
var stageShowDone = false; /* показывать сданные эпики в своде по этапам */
var viewStateProject = null; /* проект, чей личный вид восстановлен */

var DEFAULT_SETTINGS = { forecastMethod:'linear', riskBufferDays:3, burnrateMethod:'all', burnrateWindowDays:14,
  rangeConfidence:70, workdays:false, holidays:[], extraWorkdays:[], orgHolidays:[], orgExtraWorkdays:[], units:'hours', hoursPerDay:8,
  staleDays:10, scopeThreshold:10, slipThreshold:7, chanceThreshold:50, insightWindowDays:28 };
var charts = {};
var currentView = 'dashboard';

/* ════════════════════════════════════════════════════════════════
   API helpers
════════════════════════════════════════════════════════════════ */
function api(url, opts){
  opts = opts || {};
  if(opts.method && opts.method !== 'GET') invalidateInsights();  /* данные меняются — выводы пересчитываются */
  opts.headers = opts.headers || {};
  opts.headers['Content-Type'] = 'application/json';
  return fetch(url, Object.assign({credentials:'same-origin'}, opts)).catch(function(){
    toast('Нет связи с сервером — изменения не сохранены');
    var err = new Error('network');
    err.handled = true;
    throw err;
  }).then(function(r){
    if(r.status === 401){ window.location.href = '/login' + window.location.hash; throw new Error('Unauthorized'); }
    return r.json().catch(function(){ return {}; }).then(function(data){
      if(!r.ok){
        var msg = (data && data.error) || ('Ошибка сервера (' + r.status + ')');
        toast(msg);
        var err = new Error(msg);
        err.handled = true;
        throw err;
      }
      return data;
    });
  });
}

/* Подсказка из title недоступна на телефоне и с клавиатуры: касание, щелчок или Enter показывают её внизу экрана */
document.addEventListener('click', function(e){
  var el = e.target.closest && e.target.closest('[data-tip]');
  if(el) toast(el.dataset.tip.replace(/\n/g, ' · '));
});
document.addEventListener('keydown', function(e){
  if(e.key === 'Enter' && e.target.dataset && e.target.dataset.tip) toast(e.target.dataset.tip.replace(/\n/g, ' · '));
});

/* Ошибки API уже показаны пользователю в toast — не засоряем консоль */
window.addEventListener('unhandledrejection', function(e){ if(e.reason && e.reason.handled) e.preventDefault(); });
function apiPost(url, data){ return api(url, {method:'POST', body:JSON.stringify(data||{})}); }
function apiPut(url, data){ return api(url, {method:'PUT', body:JSON.stringify(data||{})}); }
function apiDelete(url){ return api(url, {method:'DELETE'}); }

/* ════════════════════════════════════════════════════════════════
   UI helpers
════════════════════════════════════════════════════════════════ */
var toastEl = document.getElementById('toast');
var toastTimer = null;
function toast(msg){
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function(){ toastEl.classList.remove('show'); }, 2200);
}

var confirmModal = document.getElementById('confirmModal');
var confirmTitle = document.getElementById('confirmTitle');
var confirmBody = document.getElementById('confirmBody');
var confirmOk = document.getElementById('confirmOk');
var confirmCancel = document.getElementById('confirmCancel');
var modalOkHandler = null;
/* Универсальное модальное окно: подтверждение (text) или форма (html).
   onOk может вернуть false, чтобы окно осталось открытым (ошибка валидации). */
function openModal(opts){
  confirmTitle.textContent = opts.title || '';
  if(opts.html !== undefined) confirmBody.innerHTML = opts.html;
  else confirmBody.textContent = opts.text || '';
  confirmOk.textContent = opts.okLabel || 'OK';
  confirmOk.className = 'btn ' + (opts.danger ? 'btn-danger' : 'btn-primary');
  confirmModal.classList.toggle('modal-wide', !!opts.wide);
  confirmModal.classList.toggle('modal-xl', opts.size === 'xl');
  confirmCancel.hidden = !!opts.noCancel;
  modalOkHandler = opts.onOk || null;
  if(!confirmModal.classList.contains('show')) modalReturnFocus = document.activeElement;
  confirmModal.classList.add('show');
  document.getElementById('appRoot').inert = true;  /* страница под окном недоступна с клавиатуры и для экранного диктора */
  var first = confirmBody.querySelector('input:not([type=file]):not([disabled]),select,textarea');
  /* в подтверждении удаления фокус — на «Отмена», чтобы Enter не удалял по ошибке */
  (first || (opts.danger && !opts.noCancel ? confirmCancel : confirmOk)).focus();
}
var modalReturnFocus = null;
function closeModal(){
  confirmOk.disabled = false;
  confirmModal.classList.remove('show');
  modalOkHandler = null;
  document.getElementById('appRoot').inert = false;
  if(modalReturnFocus && document.contains(modalReturnFocus)) modalReturnFocus.focus();
  modalReturnFocus = null;
}
function modalFocusables(){
  return Array.prototype.filter.call(confirmModal.querySelectorAll('a[href],button,input,select,textarea,[tabindex]:not([tabindex="-1"])'),
    function(el){ return !el.disabled && !el.hidden && el.type !== 'hidden' && el.offsetParent !== null; });
}
function askConfirm(title, body, cb, okLabel){
  openModal({title:title, text:body, okLabel:okLabel || 'Удалить', danger:true, onOk:cb});
}
confirmOk.addEventListener('click', function(){
  var h = modalOkHandler;
  if(h && h() === false) return;
  closeModal();
});
confirmCancel.addEventListener('click', closeModal);
confirmModal.addEventListener('mousedown', function(e){ if(e.target === confirmModal) closeModal(); });
document.addEventListener('keydown', function(e){
  if(!confirmModal.classList.contains('show')) return;
  if(e.key === 'Escape'){ closeModal(); return; }
  if(e.key === 'Tab'){  /* фокус не уходит из окна */
    var items = modalFocusables();
    if(!items.length) return;
    var firstEl = items[0], lastEl = items[items.length - 1];
    if(e.shiftKey && document.activeElement === firstEl){ lastEl.focus(); e.preventDefault(); }
    else if(!e.shiftKey && document.activeElement === lastEl){ firstEl.focus(); e.preventDefault(); }
    return;
  }
  /* Enter в поле формы — как «Сохранить» (кроме многострочных полей и полей выбора файла) */
  var t = e.target;
  if(e.key === 'Enter' && modalOkHandler && !confirmOk.disabled && confirmBody.contains(t) && t.tagName === 'INPUT' &&
     ['checkbox', 'radio', 'file', 'button', 'submit'].indexOf(t.type) === -1 && !e.isComposing){
    e.preventDefault();
    confirmOk.click();
  }
});

function fmtNum(v){
  if(v === null || v === undefined || isNaN(v)) return '—';
  var r = Math.round(v*100)/100;
  return (r === 0 ? 0 : r).toLocaleString('ru-RU', {maximumFractionDigits:2}); /* без «-0» */
}
/* Единицы отображения (настройки проекта): часы или человеко-дни. Замеры хранятся и вводятся в часах. */
function unitDiv(){ var st = STATE.settings || {}; return st.units === 'days' ? (st.hoursPerDay || 8) : 1; }
function unitSuffix(){ return (STATE.settings || {}).units === 'days' ? 'чел.-дн.' : 'ч'; }
function rateUnit(){ return unitSuffix() + '/' + ((STATE.settings || {}).workdays ? 'раб. день' : 'день'); }
function unitAxisTitle(what){ return ((STATE.settings || {}).units === 'days' ? 'Человеко-дни' : 'Часы') + (what ? ' (' + what + ')' : ''); }
function fmtWorkNum(v){ return fmtNum(Math.round(v / unitDiv() * 100) / 100); }
function hoursToStr(v){
  if(v === null || v === undefined || isNaN(v)) return '—';
  return fmtWorkNum(v) + ' ' + unitSuffix();
}
function signedWork(v){ return (v > 0 ? '+' : '') + fmtWorkNum(v) + ' ' + unitSuffix(); }
function escapeHtml(str){
  var d = document.createElement('div');
  d.textContent = str == null ? '' : String(str);
  return d.innerHTML;
}
function escapeAttr(str){
  return escapeHtml(str).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

/* ── Sorting helpers ─────────────────────────────────────────── */
var tableSortState = {};
var STATUS_RANK = {danger:0, risk:1, ok:2, notarget:3, unknown:4};
function compareValues(av, bv, dir){
  /* Пустые значения — всегда в конце; числа сравниваются как числа */
  var aEmpty = av === null || av === undefined || av === '';
  var bEmpty = bv === null || bv === undefined || bv === '';
  if(aEmpty || bEmpty) return aEmpty === bEmpty ? 0 : (aEmpty ? 1 : -1);
  var res;
  if(typeof av === 'number' && typeof bv === 'number') res = av - bv;
  else res = String(av).localeCompare(String(bv), 'ru', {numeric:true, sensitivity:'base'});
  return dir === 'desc' ? -res : res;
}
function sortValue(row, field){
  return field === 'status' ? STATUS_RANK[row.status] : row[field];
}
function getSortedServices(){
  var list = STATE.services.slice();
  var sort = tableSortState['servicesTable'];
  if(!sort){ list.sort(function(a,b){ return (a.sortOrder||0)-(b.sortOrder||0); }); return list; }
  list.sort(function(a,b){
    var av = sort.field === 'count' ? entriesForService(a.id).length : a[sort.field];
    var bv = sort.field === 'count' ? entriesForService(b.id).length : b[sort.field];
    return compareValues(av, bv, sort.dir);
  });
  return list;
}
function initTableSort(table, renderFn){
  var key = table.id;
  var sort = tableSortState[key];
  table.querySelectorAll('th.sortable').forEach(function(th){
    if(sort && sort.field === th.dataset.sort) th.classList.add(sort.dir === 'desc' ? 'sorted-desc' : 'sorted-asc');
    th.addEventListener('click', function(){
      var field = th.dataset.sort;
      if(tableSortState[key] && tableSortState[key].field === field){
        tableSortState[key].dir = tableSortState[key].dir === 'asc' ? 'desc' : 'asc';
      } else {
        tableSortState[key] = {field: field, dir: 'asc'};
      }
      saveTableState();
      renderFn();
    });
  });
}
/* ── Фильтр таблицы: поиск по всем колонкам и «Фильтр по полям» — условие под каждой колонкой
   (text — содержит, num — «>0», «5–20», «пусто», list — выбор из значений). Состояние хранится по id таблицы
   и переживает перерисовку; строки «Итого» не скрываются. ── */
var tableFilterState = {};
function tableFilter(table, cols){
  if(!table || !table.tHead || !table.id) return;
  var st = tableFilterState[table.id] || (tableFilterState[table.id] = {open: false, search: '', values: {}});
  table._tfCols = cols;
  var wrap = table.closest('.table-scroll') || table;
  var bar = wrap.previousElementSibling;
  if(!bar || !bar.classList.contains('tf-bar')){
    bar = document.createElement('div');
    bar.className = 'tf-bar';
    bar.innerHTML = '<input type="search" class="filter-input tf-search" placeholder="Найти в таблице" aria-label="Найти в таблице">'+
      '<button type="button" class="btn btn-ghost btn-sm tf-toggle" aria-expanded="false">Фильтр по полям</button>'+
      '<span class="tf-count muted-cell" aria-live="polite"></span><button type="button" class="link-btn tf-reset" hidden>Сбросить фильтр</button>';
    wrap.parentNode.insertBefore(bar, wrap);
    bar.querySelector('.tf-search').addEventListener('input', function(e){ st.search = e.target.value; applyTableFilter(table); });
    bar.querySelector('.tf-toggle').addEventListener('click', function(){ st.open = !st.open; tableFilter(table, table._tfCols); });
    bar.querySelector('.tf-reset').addEventListener('click', function(){ st.search = ''; st.values = {}; tableFilter(table, table._tfCols); });
  }
  bar.querySelector('.tf-search').value = st.search;
  var toggle = bar.querySelector('.tf-toggle');
  toggle.setAttribute('aria-expanded', st.open ? 'true' : 'false');
  toggle.classList.toggle('active', st.open);
  var old = table.tHead.querySelector('tr.tf-row');
  if(old) old.remove();
  var rows = tableFilterRows(table);
  var row = document.createElement('tr');
  row.className = 'tf-row';
  row.hidden = !st.open;
  row.innerHTML = cols.map(function(kind, i){
    var v = st.values[i] || '';
    if(kind === 'text') return '<td><input type="search" class="tf-input" data-col="'+i+'" value="'+escapeAttr(v)+'" placeholder="содержит…" aria-label="Фильтр: '+escapeAttr(tableHeadText(table, i))+'"></td>';
    if(kind === 'num') return '<td><input type="text" class="tf-input" data-col="'+i+'" value="'+escapeAttr(v)+'" placeholder="&gt;0, 5–20" inputmode="decimal" title="Число: «&gt;10», «&lt;=5», «=0», «5–20» или «пусто»" aria-label="Фильтр: '+escapeAttr(tableHeadText(table, i))+'"></td>';
    if(kind === 'list'){
      var seen = {};
      rows.forEach(function(tr){ if(tr.cells[i]) cellValues(tr.cells[i]).forEach(function(x){ seen[x] = true; }); });
      var opts = Object.keys(seen).sort(function(a, b){ return a.localeCompare(b, 'ru'); });
      if(v && !seen[v]) opts.push(v);
      return '<td><select class="tf-input" data-col="'+i+'" aria-label="Фильтр: '+escapeAttr(tableHeadText(table, i))+'"><option value="">все</option>'+
        opts.map(function(x){ return '<option value="'+escapeAttr(x)+'"'+(x === v ? ' selected' : '')+'>'+escapeHtml(x || '(пусто)')+'</option>'; }).join('')+'</select></td>';
    }
    return '<td></td>';
  }).join('');
  table.tHead.appendChild(row);
  row.querySelectorAll('.tf-input').forEach(function(el){
    el.addEventListener(el.tagName === 'SELECT' ? 'change' : 'input', function(){ st.values[el.dataset.col] = el.value; applyTableFilter(table); });
  });
  bindDomSort(table, st);
  applyTableSortDom(table);
  applyTableFilter(table);
}
/* Сортировка по щелчку на заголовок у таблиц, которые не сортируются по данным (нет th[data-sort]): строки
   переставляются на месте, «Итого» — внизу, пустые значения — в конце; повторный щелчок меняет направление */
function bindDomSort(table, st){
  var head = table.tHead.rows[0];
  if(!head || head.querySelector('th[data-sort]')) return;
  Array.prototype.forEach.call(head.cells, function(th, i){
    if(!table._tfCols[i] || th.colSpan > 1) return;
    var dir = st.sort && st.sort.col === i ? st.sort.dir : null;
    th.classList.add('sortable');
    th.classList.toggle('sorted-asc', dir === 'asc');
    th.classList.toggle('sorted-desc', dir === 'desc');
    th.setAttribute('aria-sort', dir === 'asc' ? 'ascending' : dir === 'desc' ? 'descending' : 'none');
    if(th._tfSort) return;  /* шапка та же (сброс фильтра) — обработчик уже есть */
    th._tfSort = true;
    th.tabIndex = 0;
    var toggle = function(){
      st.sort = {col: i, dir: st.sort && st.sort.col === i && st.sort.dir === 'asc' ? 'desc' : 'asc'};
      tableFilter(table, table._tfCols);
    };
    th.addEventListener('click', toggle);
    th.addEventListener('keydown', function(e){ if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); toggle(); } });
  });
}
function applyTableSortDom(table){
  var st = tableFilterState[table.id], body = table.tBodies[0];
  if(!st || !st.sort || !body) return;
  var i = st.sort.col, kind = (table._tfCols || [])[i];
  var rows = tableFilterRows(table);
  var key = function(tr){
    var td = tr.cells[i];
    if(!td) return null;
    if(kind === 'num') return cellNumber(td);
    var t = kind === 'list' ? cellValues(td)[0] : cellText(td);
    var d = t.match(/^(\d{2})\.(\d{2})\.(\d{4})/);  /* дата ДД.ММ.ГГГГ — по времени */
    return d ? d[3] + '-' + d[2] + '-' + d[1] : (t === '—' ? '' : t);
  };
  var keyed = rows.map(function(tr, idx){ return {tr: tr, k: key(tr), idx: idx}; });
  keyed.sort(function(a, b){ return compareValues(a.k, b.k, st.sort.dir) || a.idx - b.idx; });
  var rest = Array.prototype.filter.call(body.rows, function(tr){ return rows.indexOf(tr) === -1; });
  keyed.forEach(function(x){ body.appendChild(x.tr); });
  rest.forEach(function(tr){ body.appendChild(tr); });
}
function tableHeadText(table, i){
  var cell = table.tHead.rows[0] && table.tHead.rows[0].cells[i];
  return cell ? cell.textContent.trim() : '';
}
function tableFilterRows(table){
  return Array.prototype.filter.call(table.tBodies[0] ? table.tBodies[0].rows : [], function(tr){
    return !tr.classList.contains('total-row') && !tr.querySelector('.empty-cell');
  });
}
function cellText(td){
  var control = td.querySelector('input, select, textarea');
  if(control) return control.type === 'checkbox' ? (control.checked ? 'да' : 'нет') : control.value;
  return td.dataset.v !== undefined ? td.dataset.v : td.textContent.replace(/\s+/g, ' ').trim();
}
/* значения для списка в фильтре: без значков в начале («✓ Успеваем» → «Успеваем») */
function cellValues(td){
  return td.dataset.values !== undefined ? td.dataset.values.split('|') : [cellText(td).replace(/^[^0-9A-Za-zА-Яа-яЁё(]+/, '')];
}
function cellNumber(td){
  if(td.dataset.num !== undefined) return td.dataset.num === '' ? null : parseFloat(td.dataset.num);
  var m = cellText(td).match(/[-−+]?\d[\d\s  ]*(?:[.,]\d+)?/);
  return m ? parseFloat(m[0].replace(/[\s  ]/g, '').replace(',', '.').replace('−', '-')) : null;
}
/* «>10», «>=10», «<5», «=0», «10» — равно, «5–20» (или 5..20) — диапазон, «пусто»; иначе false — условие не разобрано */
function parseNumFilter(expr){
  var s = String(expr).trim().replace(/\s+/g, '').replace(/,/g, '.').replace(/−/g, '-').replace(/≥/g, '>=').replace(/≤/g, '<=');
  if(!s) return null;
  if(/^(пусто|нет|—|-)$/i.test(s)) return function(v){ return v === null; };
  var m = s.match(/^(>=|<=|>|<|=)?(-?\d+(?:\.\d+)?)$/);
  if(m){
    var n = parseFloat(m[2]), op = m[1] || '=';
    return function(v){
      if(v === null) return false;
      return op === '>' ? v > n : op === '<' ? v < n : op === '>=' ? v >= n : op === '<=' ? v <= n : Math.abs(v - n) < 1e-9;
    };
  }
  m = s.match(/^(-?\d+(?:\.\d+)?)(?:\.\.|–|—|-)(-?\d+(?:\.\d+)?)$/);
  if(m){
    var a = Math.min(parseFloat(m[1]), parseFloat(m[2])), b = Math.max(parseFloat(m[1]), parseFloat(m[2]));
    return function(v){ return v !== null && v >= a && v <= b; };
  }
  return false;
}
function applyTableFilter(table){
  var st = tableFilterState[table.id];
  if(!st) return;
  var cols = table._tfCols || [], tests = [];
  Object.keys(st.values).forEach(function(k){
    var v = st.values[k], i = parseInt(k, 10);
    var input = table.tHead.querySelector('.tf-input[data-col="'+i+'"]');
    if(input) input.classList.remove('tf-invalid');
    if(v === '' || v === undefined) return;
    if(cols[i] === 'num'){
      var f = parseNumFilter(v);
      if(!f){ if(input && f === false) input.classList.add('tf-invalid'); return; }
      tests.push(function(cells){ return !!cells[i] && f(cellNumber(cells[i])); });
    } else if(cols[i] === 'list'){
      tests.push(function(cells){ return !!cells[i] && cellValues(cells[i]).indexOf(v) >= 0; });
    } else {
      var needle = v.toLowerCase();
      tests.push(function(cells){ return !!cells[i] && cellText(cells[i]).toLowerCase().indexOf(needle) >= 0; });
    }
  });
  var search = st.search.trim().toLowerCase();
  if(table.id !== 'jiTable') saveTableState();
  var rows = tableFilterRows(table), shown = 0, total = 0;
  rows.forEach(function(tr){
    var ok = tests.every(function(t){ return t(tr.cells); }) &&
      (!search || Array.prototype.some.call(tr.cells, function(td){ return cellText(td).toLowerCase().indexOf(search) >= 0; }));
    tr.classList.toggle('tf-hidden', !ok);
    if(!tr.hidden) total++;
    if(ok && !tr.hidden) shown++;
  });
  var active = !!search || tests.length > 0;
  var wrap = table.closest('.table-scroll') || table;
  var bar = wrap.previousElementSibling;
  if(bar && bar.classList.contains('tf-bar')){
    bar.querySelector('.tf-count').textContent = active ? 'Показано ' + shown + ' из ' + total : '';
    bar.querySelector('.tf-reset').hidden = !active && !Object.keys(st.values).some(function(k){ return st.values[k]; });
  }
}
function applyTableSort(key, rows){
  var sort = tableSortState[key];
  if(!sort) return rows;
  return rows.slice().sort(function(a, b){
    return compareValues(sortValue(a, sort.field), sortValue(b, sort.field), sort.dir);
  });
}
function getCss(varName){
  return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
}
function colorPalette(){
  return [getCss('--color-primary'), getCss('--color-blue'), getCss('--color-orange'), getCss('--color-error'), getCss('--color-success'), getCss('--color-warning')];
}
/* номер дня даты ГГГГ-ММ-ДД (дни от 1970-01-01, UTC); разбор даты запоминается — расчёты прогноза обращаются
   к одним и тем же датам тысячи раз */
var dayNumCache = new Map();
function isoOrdinal(d){
  var v = dayNumCache.get(d);
  if(v === undefined){
    v = Math.round(Date.parse(d + 'T00:00:00Z') / 86400000);
    if(dayNumCache.size > 50000) dayNumCache.clear();
    dayNumCache.set(d, v);
  }
  return v;
}
function daysBetween(d1, d2){
  return isoOrdinal(d2) - isoOrdinal(d1);
}
function addDays(dateStr, days){
  var d = new Date(dateStr + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0,10);
}
/* Календарь расчёта сроков — тот же, что в forecast.py (calendar_for): календарные дни или рабочие (пн–пт без
   праздников проекта, с перенесёнными рабочими днями). span(a, b) — длина периода (a, b] в днях расчёта,
   shift(a, n) — дата через n дней расчёта. */
var MC_HORIZON_WORKDAYS = 3 * 261;
var CALENDAR_DAYS = {workdays: false, signature: '', horizon: 3 * 365, span: daysBetween, shift: addDays};
var calCache = {key: null, cal: null};
function ordinalIso(o){ return new Date(o * 86400000).toISOString().slice(0, 10); }
function workCalendar(){
  var st = STATE.settings || {};
  if(!st.workdays) return CALENDAR_DAYS;
  /* общий производственный календарь (администратор) плюс даты проекта — как calendar_for в forecast.py */
  var uniq = function(a, b){ var m = {}; (a || []).concat(b || []).forEach(function(d){ m[d] = true; }); return Object.keys(m).sort(); };
  var holidays = uniq(st.orgHolidays, st.holidays), extra = uniq(st.orgExtraWorkdays, st.extraWorkdays);
  var signature = 'w:' + holidays.join(',') + '|' + extra.join(',');
  if(calCache.key === signature) return calCache.cal;
  var on = {}, off = {}, onList = [], offList = [];
  extra.forEach(function(d){ var o = isoOrdinal(d); if(!on[o]){ on[o] = true; onList.push(o); } });
  holidays.forEach(function(d){ var o = isoOrdinal(d); if(!on[o] && !off[o]){ off[o] = true; offList.push(o); } });
  var weekday = function(o){ return (o + 3) % 7 < 5; };  /* 01.01.1970 — четверг; пн = 0 */
  var isWork = function(o){ return on[o] ? true : (off[o] ? false : weekday(o)); };
  var count = function(oa, ob){
    var n = ob - oa, full = Math.floor(n / 7), c = full * 5, k;
    for(k = oa + full * 7 + 1; k <= ob; k++) if(weekday(k)) c++;
    for(k = 0; k < offList.length; k++) if(offList[k] > oa && offList[k] <= ob && weekday(offList[k])) c--;
    for(k = 0; k < onList.length; k++) if(onList[k] > oa && onList[k] <= ob && !weekday(onList[k])) c++;
    return c;
  };
  var cal = {workdays: true, signature: signature, horizon: MC_HORIZON_WORKDAYS,
    span: function(a, b){ var oa = isoOrdinal(a), ob = isoOrdinal(b); return ob >= oa ? count(oa, ob) : -count(ob, oa); },
    shift: function(a, n){
      if(n <= 0) return addDays(a, n);
      var o = isoOrdinal(a), c = 0;
      while(c < n){ o++; if(isWork(o)) c++; }
      return ordinalIso(o);
    }};
  calCache = {key: signature, cal: cal};
  return cal;
}
var MC_QUANTILES = {50: [0.25, 0.75], 70: [0.15, 0.85], 80: [0.1, 0.9], 90: [0.05, 0.95]};
function rangeConfidence(){ var c = (STATE.settings || {}).rangeConfidence; return MC_QUANTILES[c] ? c : 70; }
function fmtDate(d){
  if(!d) return '—';
  return d.split('-').reverse().join('.');
}
/* «04.12.2026 — 18.12.2026»; одинаковые границы — одна дата; нет даты — «не сгорает» */
function fmtDateRange(a, b){
  var fa = a ? fmtDate(a) : 'не сгорает', fb = b ? fmtDate(b) : 'не сгорает';
  return fa === fb ? fa : fa + ' — ' + fb;
}
function fmtShortDate(d){ return d.slice(5).split('-').reverse().join('.'); }
function isNum(v){ return v !== null && v !== undefined && !isNaN(v); }
/* Ячейка burn rate: отрицательное значение = остаток вырос */
function rateCell(v, withUnit){
  if(!isNum(v)) return '<td class="num">—</td>';
  var neg = Math.round(v*100) < 0;
  return '<td class="num'+(neg?' neg':'')+'"'+(neg?' title="Остаток вырос"':'')+'>'+fmtWorkNum(v)+(withUnit ? ' ' + unitSuffix() : '')+'</td>';
}
/* Ячейка изменения оценки: рост объёма подсвечивается */
function scopeCell(v){
  if(!isNum(v)) return '<td class="num">—</td>';
  var r = Math.round(v*100)/100;
  return '<td class="num'+(r>0?' scope-up':'')+'"'+(r>0?' title="Оценка выросла с первого замера"':'')+'>'+signedWork(r)+'</td>';
}
function forecastText(row){
  return row.forecastZero ? fmtDate(row.forecastZero) : (row.tooFar ? '> 3 лет' : (row.noBurn ? 'не сгорает' : '—'));
}
var ROLE_LABELS = {admin:'Администратор', manager:'Руководитель', user:'Пользователь'};
var ICON_EDIT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>';
var ICON_DELETE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0l-1 14a2 2 0 01-2 2H7a2 2 0 01-2-2L4 6h16z"/></svg>';
function actionBtn(action, title, icon, extra){
  return '<button class="icon-btn'+(action === 'delete' ? ' icon-btn-danger' : '')+'" data-action="'+action+'" title="'+title+'" aria-label="'+title+'"'+(extra||'')+'>'+icon+'</button>';
}

/* ════════════════════════════════════════════════════════════════
   Theme
════════════════════════════════════════════════════════════════ */
var root = document.documentElement;
var themeToggle = document.getElementById('themeToggle');
function setThemeIcon(mode){
  themeToggle.innerHTML = mode === 'dark'
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
}
var storedTheme = localStorage.getItem('burndown-theme');
var themeMode = storedTheme || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
root.setAttribute('data-theme', themeMode);
setThemeIcon(themeMode);
themeToggle.addEventListener('click', function(){
  themeMode = themeMode === 'dark' ? 'light' : 'dark';
  root.setAttribute('data-theme', themeMode);
  localStorage.setItem('burndown-theme', themeMode);
  setThemeIcon(themeMode);
  refreshCharts();
  renderProjectsNav();
});

/* ════════════════════════════════════════════════════════════════
   Иконки (линейные, 24×24, в духе lucide): <i data-icon="имя"> → svg
════════════════════════════════════════════════════════════════ */
var ICON_PATHS = {
  chevronLeft: '<path d="m15 18-6-6 6-6"/>', chevronRight: '<path d="m9 18 6-6-6-6"/>', chevronDown: '<path d="m6 9 6 6 6-6"/>',
  portfolio: '<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/>',
  dashboard: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
  stages: '<path d="m12 2 10 5-10 5L2 7l10-5z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
  data: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18M8 14h.01M12 14h.01M16 14h.01M8 18h.01M12 18h.01"/>',
  epics: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  settings: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  history: '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>',
  plug: '<path d="M12 22v-5M9 8V2M15 8V2M18 8v5a6 6 0 0 1-12 0V8z"/>',
  calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18M9 16l2 2 4-4"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/><path d="M4 19.5V21h16"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
  trend: '<path d="m3 17 6-6 4 4 8-8"/><path d="M14 7h7v7"/>',
  bulb: '<path d="M9 18h6M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.7V17h8v-2.3A7 7 0 0 0 12 2z"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>'
};
function iconSvg(name, size){
  return '<svg class="icon" viewBox="0 0 24 24" width="'+(size || 16)+'" height="'+(size || 16)+'" fill="none" stroke="currentColor" '+
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+(ICON_PATHS[name] || '')+'</svg>';
}
function renderIcons(root){
  (root || document).querySelectorAll('i[data-icon]').forEach(function(el){
    var tmp = document.createElement('span');
    tmp.innerHTML = iconSvg(el.dataset.icon);
    el.replaceWith(tmp.firstChild);
  });
}
renderIcons();

/* ════════════════════════════════════════════════════════════════
   Sidebar: на компьютере — кнопка «‹» сворачивает меню в узкую полосу с иконками (запоминается),
   группы разделов раскрываются и сворачиваются; на телефоне — меню выезжает по кнопке ☰
════════════════════════════════════════════════════════════════ */
var appRoot = document.getElementById('appRoot');
var sidebarToggle = document.getElementById('sidebarToggle');
var sidebarCollapse = document.getElementById('sidebarCollapse');
var sidebarScrim = document.getElementById('sidebarScrim');
var sidebarEl = document.querySelector('.sidebar');
var mobileLayout = window.matchMedia('(max-width: 900px)');
var SIDEBAR_KEY = 'burndown-sidebar', NAV_GROUPS_KEY = 'burndown-nav-groups';
function setSidebarCollapsed(collapsed){
  appRoot.classList.toggle('collapsed', collapsed);
  sidebarEl.inert = collapsed;  /* скрытое меню не попадает в обход клавишей Tab */
  sidebarToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
}
function setSidebarRail(rail){
  appRoot.classList.toggle('rail', rail);
  sidebarCollapse.title = rail ? 'Развернуть меню' : 'Свернуть меню';
  sidebarCollapse.setAttribute('aria-label', sidebarCollapse.title);
  sidebarCollapse.setAttribute('aria-expanded', rail ? 'false' : 'true');
  sidebarCollapse.innerHTML = iconSvg(rail ? 'chevronRight' : 'chevronLeft', 14);
  try { localStorage.setItem(SIDEBAR_KEY, rail ? 'rail' : 'full'); } catch(e){}
}
sidebarCollapse.addEventListener('click', function(){
  if(mobileLayout.matches){ setSidebarCollapsed(true); sidebarToggle.focus(); return; }
  setSidebarRail(!appRoot.classList.contains('rail'));
});
try { setSidebarRail(localStorage.getItem(SIDEBAR_KEY) === 'rail'); } catch(e){ setSidebarRail(false); }
/* группы меню: «Обзор», «Проекты», «Данные проекта», «Администрирование» — раскрыты, пока их не свернули */
var navGroupState = {};
try { navGroupState = JSON.parse(localStorage.getItem(NAV_GROUPS_KEY) || '{}') || {}; } catch(e){ navGroupState = {}; }
function setNavGroupOpen(group, open){
  var nav = document.querySelector('.nav-group[data-group="'+group+'"]');
  if(!nav) return;
  nav.classList.toggle('closed', !open);
  nav.querySelector('.nav-label').setAttribute('aria-expanded', open ? 'true' : 'false');
}
document.querySelectorAll('.nav-group[data-group]').forEach(function(nav){
  var group = nav.dataset.group;
  setNavGroupOpen(group, navGroupState[group] !== false);
  nav.querySelector('.nav-label').addEventListener('click', function(){
    var open = nav.classList.contains('closed');
    setNavGroupOpen(group, open);
    navGroupState[group] = open;
    try { localStorage.setItem(NAV_GROUPS_KEY, JSON.stringify(navGroupState)); } catch(e){}
  });
});
sidebarToggle.addEventListener('click', function(){
  var open = appRoot.classList.contains('collapsed');
  setSidebarCollapsed(!open);
  if(open && mobileLayout.matches){  /* на телефоне фокус — в открытое меню */
    var target = sidebarEl.querySelector('.nav-item.active') || sidebarEl.querySelector('.nav-item');
    if(target) target.focus();
  }
});
sidebarScrim.addEventListener('click', function(){ setSidebarCollapsed(true); });
document.addEventListener('keydown', function(e){
  if(e.key === 'Escape' && mobileLayout.matches && !appRoot.classList.contains('collapsed') &&
     !confirmModal.classList.contains('show')){
    setSidebarCollapsed(true);
    sidebarToggle.focus();
  }
});
setSidebarCollapsed(mobileLayout.matches);
if(mobileLayout.addEventListener) mobileLayout.addEventListener('change', function(){ setSidebarCollapsed(mobileLayout.matches); });

/* ════════════════════════════════════════════════════════════════
   Вкладки разделов (настройки, интеграции, Jira): .tabs[data-tabs="раздел"] и [data-tab-panel="ключ …"] внутри
   раздела; выбранная вкладка — в адресе страницы (…&tab=…), стрелки ← → переключают вкладки
════════════════════════════════════════════════════════════════ */
var tabState = {};
function tabBar(scope){ return document.querySelector('[data-tabs="'+scope+'"]'); }
function visibleTabs(scope){
  var bar = tabBar(scope);
  return bar ? Array.prototype.filter.call(bar.querySelectorAll('[data-tab]'), function(b){ return !b.hidden; })
    .map(function(b){ return b.dataset.tab; }) : [];
}
function selectTab(scope, key){
  var bar = tabBar(scope);
  if(!bar) return;
  var keys = visibleTabs(scope);
  if(keys.indexOf(key) === -1) key = keys[0];
  tabState[scope] = key;
  bar.querySelectorAll('[data-tab]').forEach(function(b){
    var on = b.dataset.tab === key;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
    b.tabIndex = on ? 0 : -1;
  });
  document.querySelectorAll('#view-'+scope+' [data-tab-panel]').forEach(function(p){
    p.hidden = p.dataset.tabPanel.split(' ').indexOf(key) === -1;
  });
}
document.querySelectorAll('[data-tabs]').forEach(function(bar){
  var scope = bar.dataset.tabs;
  bar.addEventListener('click', function(e){
    var b = e.target.closest('[data-tab]');
    if(!b) return;
    selectTab(scope, b.dataset.tab);
    syncRoute(false);
  });
  bar.addEventListener('keydown', function(e){
    if(e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    var keys = visibleTabs(scope), i = keys.indexOf(tabState[scope]);
    var next = keys[(i + (e.key === 'ArrowRight' ? 1 : keys.length - 1)) % keys.length];
    selectTab(scope, next);
    bar.querySelector('[data-tab="'+next+'"]').focus();
    syncRoute(false);
  });
});

document.getElementById('btnLogout').addEventListener('click', function(){
  apiPost('/api/auth/logout').then(function(){ window.location.href = '/login'; });
});

/* ════════════════════════════════════════════════════════════════
   Data helpers (adapted from original)
════════════════════════════════════════════════════════════════ */
function serviceById(id){ return STATE.services.find(function(s){ return s.id === id; }); }
function isSvcDone(s){ return !!(s && s.isDone); }
function visibleDashboardServices(){
  /* Эпики, видимые на дашборде: с учётом «Скрыть сданные» и галочек выбора эпиков */
  return STATE.services.filter(function(s){
    if(dashboardFilter.hideDone && isSvcDone(s)) return false;
    if(dashboardFilter.epics && dashboardFilter.epics[s.id] === false) return false;
    return true;
  });
}
function stageScopeServices(){
  /* Сданные эпики не считаются в рамках этапа, пока не включён показ сданных */
  return STATE.services.filter(function(s){ return stageShowDone || !isSvcDone(s); });
}
function entriesForService(id){
  return STATE.entries.filter(function(e){ return e.service_id === id; }).slice().sort(function(a,b){
    return a.date < b.date ? -1 : a.date > b.date ? 1 : 0;
  });
}

function stageKeyOf(s){
  /* Ключ группы в «Своде по этапам»: явная группа эпика, иначе этап без подсетапа */
  var sg = (s.stageGroup || '').trim();
  if(sg) return sg;
  var etap = (s.etap || '').trim();
  var match = etap.match(/^(Этап\s+\d+)(\.\d+)?$/i);
  return match ? match[1] : (etap || 'Без этапа');
}

function computeStageGroups(){
  /* groups[имя группы] = [id эпиков]; сданные эпики не считаются, пока не включён их показ */
  var groups = {};
  stageScopeServices().forEach(function(s){
    var key = stageKeyOf(s);
    if(!groups[key]) groups[key] = [];
    if(groups[key].indexOf(s.id) === -1) groups[key].push(s.id);
  });
  return groups;
}

function servicesInStage(stageName, stageGroups){
  var sg = stageGroups || computeStageGroups();
  var ids = sg[stageName] || [];
  return STATE.services.filter(function(s){ return ids.indexOf(s.id) !== -1; });
}

/* ════════════════════════════════════════════════════════════════
   Burn rate / forecast calculations
════════════════════════════════════════════════════════════════ */
function avgBurnRate(dates, remainingVals){
  var pts = dates.map(function(d,i){ return {d:d, y:remainingVals[i]}; }).filter(function(p){ return p.y !== null && p.y !== undefined; });
  if(pts.length < 2) return null;
  if(STATE.settings && STATE.settings.burnrateMethod === 'rolling'){
    var lastDate = pts[pts.length-1].d;
    var windowDays = STATE.settings.burnrateWindowDays || 14;
    var cutoff = addDays(lastDate, -windowDays);
    var windowed = pts.filter(function(p){ return p.d >= cutoff; });
    if(windowed.length < 2) windowed = pts.slice(-2);
    pts = windowed;
  }
  var first = pts[0], last = pts[pts.length-1];
  var days = workCalendar().span(first.d, last.d);
  if(days <= 0) return null;
  return (first.y - last.y) / days;
}

function linearForecast(points){
  var pts = points.filter(function(p){ return p.y !== null && !isNaN(p.y); });
  if(pts.length < 2) return null;
  var n = pts.length;
  var sumX = pts.reduce(function(s,p){ return s+p.x; }, 0);
  var sumY = pts.reduce(function(s,p){ return s+p.y; }, 0);
  var sumXY = pts.reduce(function(s,p){ return s+p.x*p.y; }, 0);
  var sumXX = pts.reduce(function(s,p){ return s+p.x*p.x; }, 0);
  var denom = (n*sumXX - sumX*sumX);
  if(denom === 0) return null;
  var slope = (n*sumXY - sumX*sumY) / denom;
  var intercept = (sumY - slope*sumX) / n;
  return {slope:slope, intercept:intercept};
}

/* Сумма по этапу на каждую дату замеров — та же, что aggregate_series на сервере (forecast.py).
   Для эпика без замера в эту дату берётся его последнее известное значение, а до первого замера — первое:
   замеры в разные дни не дают ни ложных провалов, ни ложного «роста объёма». Дата, на которую ни у одного
   эпика нет значения показателя (пустая «+ Новая дата»), для этого показателя пропускается. */
function aggregateStageSeries(stageName, stageGroups){
  return aggregateSeries(servicesInStage(stageName, stageGroups).map(function(s){ return entriesForService(s.id); }));
}
function aggregateSeries(rowsBySvc){
  var fields = ['remaining', 'spent', 'estimate'];
  var dateSet = {};
  rowsBySvc.forEach(function(rows){
    rows.forEach(function(e){ if(fields.some(function(f){ return isNum(e[f]); })) dateSet[e.date] = true; });
  });
  var dates = Object.keys(dateSet).sort();
  var series = {remaining:[], spent:[], estimate:[]};
  var first = rowsBySvc.map(function(rows){
    var res = {};
    fields.forEach(function(f){ var r = rows.find(function(e){ return isNum(e[f]); }); res[f] = r ? r[f] : null; });
    return res;
  });
  var last = rowsBySvc.map(function(){ return {remaining:null, spent:null, estimate:null}; });
  var pos = rowsBySvc.map(function(){ return 0; });
  dates.forEach(function(date){
    var measured = {remaining:false, spent:false, estimate:false};
    rowsBySvc.forEach(function(rows, i){
      while(pos[i] < rows.length && rows[pos[i]].date <= date){
        var row = rows[pos[i]++];
        fields.forEach(function(f){
          if(isNum(row[f])){ last[i][f] = row[f]; if(row.date === date) measured[f] = true; }
        });
      }
    });
    fields.forEach(function(f){
      if(!measured[f]){ series[f].push(null); return; }
      var sum = 0;
      last.forEach(function(l, i){ var v = l[f] !== null ? l[f] : first[i][f]; if(v !== null) sum += v; });
      series[f].push(sum);
    });
  });
  return {dates:dates, remaining:series.remaining, spent:series.spent, estimate:series.estimate};
}

/* ── Прогноз диапазоном (Монте-Карло) ─────────────────────────────
   Берём интервалы между замерами (скорость сгорания и длина интервала) и много раз «проигрываем» будущее,
   выбирая интервалы случайно с весом по длине. Получаем распределение даты обнуления остатка:
   границы диапазона (уровень доверия — в настройках проекта: 50/70/80/90 %), медиану P50 и вероятность успеть к цели.
   Генератор детерминирован — цифры не «прыгают» между перерисовками. */
var MC_RUNS = 2000, MC_HORIZON_DAYS = 3 * 365;
var mcCache = {};
function seededRandom(seed){
  return function(){
    seed |= 0; seed = seed + 0x6D2B79F5 | 0;
    var t = Math.imul(seed ^ seed >>> 15, 1 | seed);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}
function hashString(str){
  var h = 2166136261;
  for(var i = 0; i < str.length; i++){ h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}
/* Частые замеры (ежедневное автозаполнение) — недельными блоками, как block_points в forecast.py: сценарии опираются
   на недельный темп, а не на дневной шум, и считаются в разы быстрее */
var MC_BLOCK_AFTER = 30;
function rawIntervals(pts, cal){
  var list = [];
  for(var i = 1; i < pts.length; i++){
    var len = cal.span(pts[i-1].d, pts[i].d);
    if(len > 0) list.push({end: pts[i].d, len: len, rate: (pts[i-1].y - pts[i].y) / len});
  }
  return list;
}
function blockPoints(pts, cal){
  var block = cal.workdays ? 5 : 7, out = [pts[0]];
  for(var i = 1; i < pts.length; i++) if(cal.span(out[out.length-1].d, pts[i].d) >= block) out.push(pts[i]);
  if(out[out.length-1] !== pts[pts.length-1]) out.push(pts[pts.length-1]);
  return out;
}
function burnIntervals(pts){
  var cal = workCalendar();
  var list = rawIntervals(pts, cal);
  if(list.length > MC_BLOCK_AFTER) list = rawIntervals(blockPoints(pts, cal), cal);
  if(STATE.settings && STATE.settings.burnrateMethod === 'rolling' && list.length){
    var cutoff = addDays(pts[pts.length-1].d, -(STATE.settings.burnrateWindowDays || 14));
    var windowed = list.filter(function(it){ return it.end > cutoff; });
    list = windowed.length >= 2 ? windowed : list.slice(-2);
  }
  return list;
}
/* Дата, когда остаток обнулился: начало последней серии замеров с остатком ≤ 0 (null, если остаток не нулевой).
   Нулевые замеры после завершения не сдвигают ни прогноз, ни статус. */
function zeroReachedDate(pts){
  if(!pts.length || pts[pts.length-1].y > 0) return null;
  var i = pts.length - 1;
  while(i > 0 && pts[i-1].y <= 0) i--;
  return pts[i].d;
}
var SLOPE_EPS = 1e-9;  /* наклон меньше — «не сгорает»: шум округления на ровном остатке, а не скорость */
function monteCarloForecast(dates, remainingVals, targetDate){
  var pts = dates.map(function(d, i){ return {d: d, y: remainingVals[i]}; }).filter(function(p){ return isNum(p.y); });
  if(pts.length < 3) return null;
  var intervals = burnIntervals(pts);
  if(intervals.length < 2) return null;
  var last = pts[pts.length-1];
  var doneDate = zeroReachedDate(pts);
  var cal = workCalendar(), confidence = rangeConfidence(), q = MC_QUANTILES[confidence];
  /* ключ (и зерно генератора) — те же, что у сервера (forecast.py mc_key): цифры в письмах совпадают с дашбордом */
  var key = intervals.map(function(it){ return it.end + ':' + it.len + ':' + it.rate.toFixed(6); }).join(';') +
    '|' + last.d + ':' + last.y.toFixed(6) + '|' + (targetDate || '');
  var cacheKey = key + '|' + cal.signature + '|' + confidence;
  if(mcCache[cacheKey]) return mcCache[cacheKey];
  var res;
  if(doneDate){  /* остаток уже обнулён — дата завершения известна */
    res = {lo: doneDate, p50: doneDate, hi: doneDate, confidence: confidence,
           probability: targetDate ? (doneDate <= targetDate ? 1 : 0) : null, intervals: intervals.length};
  } else {
    var cum = [], total = 0, maxRate = -Infinity, maxLen = 0;
    intervals.forEach(function(it){ total += it.len; cum.push(total); if(it.rate > maxRate) maxRate = it.rate; if(it.len > maxLen) maxLen = it.len; });
    var rnd = seededRandom(hashString(key));
    var days = [];
    for(var r = 0; r < (maxRate > 0 ? MC_RUNS : 0); r++){  /* нигде не сгорало — ни один сценарий не завершится */
      var rem = last.y, day = 0;
      while(day < cal.horizon){
        if(rem > maxRate * (cal.horizon - day + maxLen)) break;  /* уже не успеть до горизонта даже на самой высокой скорости */
        var pick = rnd() * total, lo = 0, hi = cum.length - 1;
        while(lo < hi){ var mid = (lo + hi) >> 1; if(cum[mid] < pick) lo = mid + 1; else hi = mid; }  /* с весом по длине */
        var it = intervals[lo];
        if(it.rate > 0 && rem <= it.rate * it.len){ day += rem / it.rate; rem = 0; break; }
        rem -= it.rate * it.len;
        day += it.len;
      }
      days.push(rem <= 0 ? day : Infinity);
    }
    if(maxRate <= 0) for(var z = 0; z < MC_RUNS; z++) days.push(Infinity);
    days.sort(function(a, b){ return a - b; });
    var at = function(qq){ var d = days[Math.floor(qq * (days.length - 1))]; return isFinite(d) ? cal.shift(last.d, Math.ceil(d)) : null; };
    var probability = null;
    if(targetDate){
      var limit = cal.span(last.d, targetDate);
      probability = days.filter(function(d){ return Math.ceil(d) <= limit; }).length / days.length;
    }
    res = {lo: at(q[0]), p50: at(0.5), hi: at(q[1]), confidence: confidence, probability: probability, intervals: intervals.length};
  }
  mcCache[cacheKey] = res;
  return res;
}
function probabilityCell(mc){
  if(!mc) return '<td class="num muted-cell" title="Нужно не меньше трёх замеров остатка">—</td>';
  var range = 'Диапазон '+mc.confidence+'%: '+fmtDateRange(mc.lo, mc.hi)+
    '\nМедиана (P50): '+(mc.p50 ? fmtDate(mc.p50) : 'не сгорает')+'\nПо '+mc.intervals+' интервалам между замерами';
  if(mc.probability === null) return '<td class="num muted-cell" title="'+escapeAttr('Нет цели. '+range)+'">—</td>';
  var pct = Math.round(mc.probability * 100);
  var cls = pct >= 80 ? 'prob-high' : (pct >= 50 ? 'prob-mid' : 'prob-low');
  return '<td class="num nowrap"><span class="prob '+cls+'" title="'+escapeAttr(range)+'" data-tip="'+escapeAttr(range)+'" tabindex="0">'+pct+'%</span></td>';
}

/* Изменение оценки (объёма работ) от первого до последнего заполненного значения */
function scopeChange(values){
  var filled = values.filter(isNum);
  return filled.length >= 2 ? filled[filled.length-1] - filled[0] : null;
}

function computeStatus(dates, remainingVals, targetDate){
  var empty = {status:'unknown', forecastZeroDate:null, noBurn:false, tooFar:false, finished:false, burnRate:null, lastRemaining:null,
               reg:null, zeroX:null, baseDate:null, lastDate:null, mc:null};
  if(!dates.length) return empty;
  var cal = workCalendar();
  var baseDate = dates[0];
  var lastDate = dates[dates.length-1];
  var pts = dates.map(function(d,i){ return {x:cal.span(baseDate, d), y:remainingVals[i], d:d}; }).filter(function(p){ return p.y !== null && p.y !== undefined; });
  var burnRate = avgBurnRate(dates, remainingVals);
  /* Последний заполненный замер: пустая строка «+ Новая дата» не обнуляет остаток */
  var lastRemaining = pts.length ? pts[pts.length-1].y : null;
  var lastX = pts.length ? pts[pts.length-1].x : null;
  if(lastX !== null) lastDate = pts[pts.length-1].d;
  var targetX = targetDate ? cal.span(baseDate, targetDate) : null;
  /* есть замеры, но нет цели — отдельный статус; «нет данных» — только когда нечего прогнозировать */
  var status = (pts.length && targetX === null) ? 'notarget' : 'unknown';
  var buffer = (STATE.settings && STATE.settings.riskBufferDays) || 0;
  var byDay = function(day){ return day <= targetX ? 'ok' : (day <= targetX + buffer ? 'risk' : 'danger'); };
  var mc = monteCarloForecast(dates, remainingVals, targetDate);
  var doneDate = zeroReachedDate(pts);
  if(doneDate){  /* работы завершены: прогноз — фактическая дата обнуления */
    var doneX = cal.span(baseDate, doneDate);
    return Object.assign(empty, {status: targetX === null ? 'notarget' : byDay(doneX), forecastZeroDate: doneDate, finished: true,
      burnRate: burnRate, lastRemaining: lastRemaining, zeroX: doneX, baseDate: baseDate, lastDate: lastDate, mc: mc});
  }
  var reg = null;
  var method = (STATE.settings && STATE.settings.forecastMethod) || 'linear';
  if(method === 'average'){
    if(burnRate !== null && lastRemaining !== null && lastX !== null){
      var slope = -burnRate;
      var intercept = lastRemaining - slope*lastX;
      reg = {slope:slope, intercept:intercept};
    }
  } else {
    reg = linearForecast(pts);
  }
  var forecastZeroDate = null, zeroX = null, tooFar = false;
  var noBurn = !!(reg && reg.slope >= -SLOPE_EPS);
  if(reg && reg.slope < -SLOPE_EPS){
    zeroX = -reg.intercept / reg.slope;
    /* линия регрессии пересекла ноль раньше последнего замера, а работа осталась — считаем от последнего замера */
    if(lastRemaining > 0 && zeroX < lastX) zeroX = lastX + lastRemaining / -reg.slope;
    if(zeroX - lastX > cal.horizon){
      tooFar = true;  /* дальше трёх лет — не дата, а «> 3 лет» */
      if(targetX !== null) status = 'danger';
    } else {
      var zeroDay = Math.round(zeroX);  /* статус — по той же дате, что показана */
      forecastZeroDate = cal.shift(baseDate, zeroDay);
      if(forecastZeroDate < lastDate) forecastZeroDate = lastDate;  /* замер в выходной: не раньше последнего замера */
      if(targetX !== null) status = byDay(zeroDay);
    }
  } else if(noBurn && targetX !== null){
    status = 'danger';
  }
  return {status:status, forecastZeroDate:forecastZeroDate, noBurn:noBurn, tooFar:tooFar, finished:false, burnRate:burnRate,
          lastRemaining:lastRemaining, reg:reg, zeroX:zeroX, baseDate:baseDate, lastDate:lastDate, mc:mc};
}

function computeServiceStatus(serviceId){
  var svc = serviceById(serviceId);
  var rows = entriesForService(serviceId);
  var dates = rows.map(function(r){ return r.date; });
  var remVals = rows.map(function(r){ return r.remaining; });
  return Object.assign(computeStatus(dates, remVals, svc ? svc.targetDate : null),
    {scope: scopeChange(rows.map(function(r){ return r.estimate; }))});
}

function computeStageStatus(stageName, stageGroups){
  var agg = aggregateStageSeries(stageName, stageGroups);
  var targetDate = STATE.stageTargets[stageName];
  return Object.assign(computeStatus(agg.dates, agg.remaining, targetDate), {agg:agg, scope:scopeChange(agg.estimate)});
}

function statusBadge(status){
  if(status === 'ok') return '<span class="badge badge-ok">✓ Успеваем</span>';
  if(status === 'risk') return '<span class="badge badge-risk">⚠ Риск</span>';
  if(status === 'danger') return '<span class="badge badge-danger">✗ Не успеваем</span>';
  if(status === 'notarget') return '<span class="badge badge-muted" title="Укажите целевую дату, чтобы оценить сроки">Нет цели</span>';
  return '<span class="badge badge-muted">Нет данных</span>';
}

/* ════════════════════════════════════════════════════════════════
   Interpolation helper for enhanced tooltips
════════════════════════════════════════════════════════════════ */
function interpolateValue(dataPoints, targetDate){
  if(!dataPoints || dataPoints.length === 0) return null;
  var sorted = dataPoints.slice().sort(function(a,b){ return a.x < b.x ? -1 : a.x > b.x ? 1 : 0; });
  if(sorted.length === 1) return sorted[0].y;
  if(targetDate <= sorted[0].x){
    var p1 = sorted[0], p2 = sorted[1];
    var d = daysBetween(p1.x, p2.x);
    if(d === 0) return p1.y;
    var slope = (p2.y - p1.y) / d;
    return p1.y + slope * daysBetween(p1.x, targetDate);
  }
  if(targetDate >= sorted[sorted.length-1].x){
    var lp1 = sorted[sorted.length-2], lp2 = sorted[sorted.length-1];
    var ld = daysBetween(lp1.x, lp2.x);
    if(ld === 0) return lp2.y;
    var lslope = (lp2.y - lp1.y) / ld;
    return lp2.y + lslope * daysBetween(lp2.x, targetDate);
  }
  for(var i = 0; i < sorted.length - 1; i++){
    if(targetDate >= sorted[i].x && targetDate <= sorted[i+1].x){
      var q1 = sorted[i], q2 = sorted[i+1];
      var qd = daysBetween(q1.x, q2.x);
      if(qd === 0) return q1.y;
      var t = daysBetween(q1.x, targetDate) / qd;
      return q1.y + (q2.y - q1.y) * t;
    }
  }
  return null;
}

/* ════════════════════════════════════════════════════════════════
   Chart helpers
════════════════════════════════════════════════════════════════ */
function destroyChart(key){ if(charts[key]){ charts[key].destroy(); delete charts[key]; } }
function destroyChartsByPrefix(prefix){
  Object.keys(charts).forEach(function(k){ if(k.indexOf(prefix) === 0){ charts[k].destroy(); delete charts[k]; } });
}

/* Плотные данные (ежедневные замеры за месяцы, десятки эпиков): без анимации и маркеров точек — Chart.js тогда
   использует общие параметры элементов и рисует график в разы быстрее; при наведении точка по-прежнему видна */
var DENSE_SERIES_POINTS = 60, DENSE_CHART_POINTS = 1200;
function tuneChartConfig(cfg){
  var datasets = (cfg.data && cfg.data.datasets) || [], total = 0;
  datasets.forEach(function(ds){
    var n = (ds.data || []).length;
    total += n;
    if(n > DENSE_SERIES_POINTS && !ds.isRange && !ds.isEpicTarget){
      ds.pointRadius = 0;
      ds.pointHoverRadius = Math.max(3, ds.pointHoverRadius || 0);
      ds.tension = 0;
      delete ds.cubicInterpolationMode;
    }
  });
  if(total > DENSE_CHART_POINTS){
    cfg.options = cfg.options || {};
    cfg.options.animation = false;
  }
  return cfg;
}
function makeChart(el, cfg){ return new Chart(el, tuneChartConfig(cfg)); }
/* подписи графиков — тем же шрифтом, что интерфейс */
if(window.Chart){ Chart.defaults.font.family = "Inter, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif"; Chart.defaults.font.size = 11; }

function baseChartOptions(yTitle, extraTooltipCtx){
  var opts = {
    responsive:true, maintainAspectRatio:false,
    interaction:{mode:'nearest', axis:'x', intersect:false},
    plugins:{
      legend:{display:false},
      tooltip:{
        filter: function(item){
          if(item.dataset && item.dataset.isRange) return false;
          /* для линий целей эпиков — только одна точка, без верхних/нижних значений */
          if(item.dataset && item.dataset.isEpicTarget) return item.dataIndex === 0;
          return true;
        },
        callbacks:{
          title: function(items){
            if(!items.length) return '';
            var raw = items[0].raw;
            return raw && raw.x ? fmtDate(raw.x) : '';
          },
          label: function(ctx){
            var ds = ctx.dataset;
            /* линии целей эпиков: короткий текст с датой необходимого завершения */
            if(ds.isEpicTarget){
              return (ds.epicName || '') + ': завершение ' + fmtDate(ds.targetDate);
            }
            var val = ctx.parsed.y;
            var lines = [(ds.label || '') + ': ' + hoursToStr(val)];
            /* For actual datasets, add deviation info using interpolation at this point's date */
            if(ds.isActual && extraTooltipCtx){
              var date = ctx.raw ? ctx.raw.x : null;
              if(date){
                if(extraTooltipCtx.avgPts){
                  var avgVal = interpolateValue(extraTooltipCtx.avgPts, date);
                  if(avgVal !== null && !isNaN(avgVal)){
                    var dev = val - avgVal;
                    lines.push('Средняя динамика: ' + hoursToStr(avgVal) + ' (откл.: ' + signedWork(dev) + ')');
                  }
                }
                if(extraTooltipCtx.forecastPts){
                  var fcVal = interpolateValue(extraTooltipCtx.forecastPts, date);
                  if(fcVal !== null && !isNaN(fcVal)){
                    var dev2 = val - fcVal;
                    lines.push('Прогноз: ' + hoursToStr(fcVal) + ' (откл.: ' + signedWork(dev2) + ')');
                  }
                }
              }
            }
            return lines;
          }
        }
      }
    },
    scales:{
      x:{ type:'time', time:{unit:'day', tooltipFormat:'dd.MM.yyyy', displayFormats:{day:'dd.MM'}}, grid:{color:getCss('--color-divider')}, ticks:{color:getCss('--color-text-muted')} },
      y:{ title:{display:true, text:yTitle, color:getCss('--color-text-muted')}, grid:{color:getCss('--color-divider')},
          ticks:{color:getCss('--color-text-muted'), callback: function(v){ return fmtWorkNum(v); }}, beginAtZero:true }
    }
  };
  return opts;
}

function renderLegend(containerId, datasets){
  var el = document.getElementById(containerId);
  if(!el) return;
  el.innerHTML = datasets.filter(function(d){ return !d.hideInLegend && d.label; }).map(function(d){
    return '<span><span class="legend-dot'+(d.isRange ? ' legend-range' : '')+'" style="background:'+(d.isRange ? d.backgroundColor : d.borderColor)+'"></span>'+escapeHtml(d.label)+'</span>';
  }).join('');
}

/* Диапазон прогноза (Монте-Карло, уровень доверия из настроек): две линии от последнего остатка к нулю с заливкой между ними */
function rangeDatasets(stat){
  var mc = stat.mc;
  if(!mc || !mc.lo || !mc.hi || !isNum(stat.lastRemaining) || stat.lastRemaining <= 0) return [];
  var color = getCss('--color-orange');
  var rem = stat.lastRemaining, start = stat.lastDate;
  /* обе границы — в одних и тех же точках по оси X, иначе заливка между ними рисуется с разрывом */
  var span = Math.max(1, daysBetween(start, mc.hi));
  var upperAtLo = rem * (1 - daysBetween(start, mc.lo) / span);
  return [
    {label: 'Диапазон прогноза ' + mc.confidence + '%: ' + fmtDateRange(mc.lo, mc.hi),
     data: [{x: start, y: rem}, {x: mc.lo, y: 0}, {x: mc.hi, y: 0}],
     borderColor: color + '66', backgroundColor: color + '1f', borderWidth: 1, pointRadius: 0, tension: 0, isRange: true},
    {label: '', data: [{x: start, y: rem}, {x: mc.lo, y: Math.max(0, upperAtLo)}, {x: mc.hi, y: 0}],
     borderColor: color + '66', backgroundColor: color + '1f', borderWidth: 1, pointRadius: 0, tension: 0, fill: '-1',
     isRange: true, hideInLegend: true}
  ];
}

function avgLineDataset(dates, burnRate, lastRemaining, lastDate){
  if(!dates.length || burnRate === null || burnRate === undefined || lastRemaining === null) return null;
  var baseDate = dates[0];
  lastDate = lastDate || dates[dates.length-1];
  var daysSpan = Math.max(1, workCalendar().span(baseDate, lastDate));
  var startVal = lastRemaining + burnRate * daysSpan;
  return {
    label: 'Средняя динамика (' + fmtWorkNum(burnRate) + ' ' + rateUnit() + ')',
    data: [{x: baseDate, y: Math.max(0,startVal)}, {x: lastDate, y: Math.max(0,lastRemaining)}],
    /* нейтральный серый: зелёный совпадал с цветом одного из эпиков на графике этапа */
    borderColor: getCss('--color-text-muted'), borderDash: [6,4], pointRadius: 0, borderWidth: 1.5, tension: 0
  };
}

function targetLineDataset(lastDate, lastValue, targetDate, label){
  if(!targetDate || lastValue === null || lastValue === undefined) return null;
  var startDate = lastDate;
  if(daysBetween(startDate, targetDate) <= 0) return null;
  return {
    label: label || 'Целевая линия',
    data: [{x: startDate, y: lastValue}, {x: targetDate, y: 0}],
    borderColor: getCss('--color-blue'), borderDash: [8,4], pointRadius: [0,4], borderWidth: 2, tension: 0
  };
}

function forecastDataset(stat, firstDate, lastRow, targetDate){
  var reg = stat.reg, baseDate = stat.baseDate;
  if(!reg || stat.finished) return null;
  var endDate = targetDate || addDays(lastRow.date, 21);
  /* цель уже прошла или прогноз «0» позже неё — линия доводится до прогнозной даты (не дальше полугода) */
  var cap = addDays(lastRow.date, 180);
  var zeroDate = stat.forecastZeroDate || (stat.tooFar ? cap : null);
  if(zeroDate && zeroDate > endDate) endDate = zeroDate < cap ? zeroDate : (cap > endDate ? cap : endDate);
  var cal = workCalendar();
  var startX = cal.span(baseDate, firstDate);
  var endX = Math.max(cal.span(baseDate, endDate), startX + 1);
  /* прогноз от последнего замера (регрессия пересекла ноль раньше него) — прямая из последней точки в дату «0» */
  var lastX = cal.span(baseDate, stat.lastDate);
  var anchored = stat.zeroX !== null && reg.slope < 0 && -reg.intercept / reg.slope < lastX;
  var lineY = function(x){
    if(anchored) return x <= lastX ? reg.slope*x + reg.intercept : stat.lastRemaining * (1 - (x - lastX) / (stat.zeroX - lastX));
    return reg.slope*x + reg.intercept;
  };
  var forecastPts = [];
  var steps = 16;
  for(var i=0; i<=steps; i++){
    var x = startX + (endX-startX)*i/steps;
    forecastPts.push({x: cal.shift(baseDate, Math.round(x)), y: Math.max(0, lineY(x))});
  }
  if(stat.forecastZeroDate && stat.forecastZeroDate <= endDate){  /* линия доходит до нуля в показанную дату */
    forecastPts = forecastPts.filter(function(p){ return p.x < stat.forecastZeroDate; });
    forecastPts.push({x: stat.forecastZeroDate, y: 0});
  }
  var methodLabel = (STATE.settings.forecastMethod === 'average') ? 'Прогноз (по burn rate)' : 'Прогноз (линейн.)';
  return { label: methodLabel, data: forecastPts, borderColor: getCss('--color-orange'), borderDash:[3,3], pointRadius:0, borderWidth:2, tension:0 };
}

function buildDatasetsForMetric(metric, serviceIds){
  var palette = colorPalette();
  return serviceIds.map(function(sid, i){
    var svc = serviceById(sid);
    var rows = entriesForService(sid);
    var data = rows.map(function(r){ return {x: r.date, y: r[metric]}; });
    return { label: svc ? svc.name : '', data: data, borderColor: palette[i % palette.length], backgroundColor: palette[i % palette.length]+'22', spanGaps:true, tension:0.25, cubicInterpolationMode:'monotone', pointRadius:3, borderWidth:2 };
  });
}

/* ════════════════════════════════════════════════════════════════
   Chart rendering
════════════════════════════════════════════════════════════════ */
/* Пустой график: вместо осей 0–1 без линий — пояснение, что показать нечего и почему. message = null — график есть */
function setChartEmpty(canvasId, message){
  var canvas = document.getElementById(canvasId);
  var wrap = canvas.parentElement;
  var note = wrap.querySelector('.chart-empty');
  canvas.hidden = !!message;
  if(!message){ if(note) note.remove(); return false; }
  if(!note){ note = document.createElement('div'); note.className = 'chart-empty'; wrap.appendChild(note); }
  note.textContent = message;
  return true;
}
function noEpicsMessage(){
  return STATE.services.length ? 'Не выбран ни один эпик — отметьте эпики в фильтре «Эпики на дашборде»'
    : 'В проекте пока нет эпиков — добавьте их в разделе «Эпики (блоки задач)»';
}
function noValuesMessage(rows, metric){
  if(!rows.length) return 'По эпику ещё нет замеров — внесите их в разделе «Данные по датам»';
  if(!rows.some(function(r){ return isNum(r[metric]); })) return 'Значения «' + HOUR_LABELS[metric] + '» по эпику не вносились';
  return null;
}
var HOUR_LABELS = {remaining: 'Остаток', spent: 'Затрачено', estimate: 'Оценка'};

function renderEpicCharts(){
  renderEpicRemainingChart();
  renderEpicSpentChart();
  renderEpicEstimateChart();
  renderDriftChart();
  renderBurnupChart();
}

function renderEpicRemainingChart(){
  destroyChart('epicRemaining');
  var sid = parseInt(document.getElementById('chartServiceSelect').value, 10);
  var svc = serviceById(sid);
  if(!svc){ document.getElementById('legendRemaining').innerHTML = ''; setChartEmpty('chartRemaining', noEpicsMessage()); return; }
  var rows = entriesForService(sid);
  if(setChartEmpty('chartRemaining', noValuesMessage(rows, 'remaining'))){ document.getElementById('legendRemaining').innerHTML = ''; return; }
  var stat = computeServiceStatus(sid);
  var palette = colorPalette();

  var actualData = rows.map(function(r){ return {x: r.date, y: r.remaining}; });
  var dates = rows.map(function(r){ return r.date; });
  var remVals = rows.map(function(r){ return r.remaining; });

  var datasets = [
    { label: 'Факт: остаток', data: actualData, borderColor: palette[0], backgroundColor: palette[0]+'33', borderWidth: 2.5, pointRadius:4, tension:0.2, cubicInterpolationMode:'monotone', fill:false, spanGaps:true, isActual:true }
  ];

  var avgLine = avgLineDataset(dates, stat.burnRate, stat.lastRemaining, stat.lastDate);
  if(avgLine) datasets.push(avgLine);

  var fcData = null, avgPts = null;
  if(rows.length){
    var firstRow = rows[0];
    var lastRow = rows[rows.length-1];
    var fc = forecastDataset(stat, firstRow.date, lastRow, svc.targetDate);
    if(fc){ datasets.push(fc); fcData = fc.data; }
    var tl = targetLineDataset(stat.lastDate, stat.lastRemaining, svc.targetDate, 'Целевая линия (эпик)');
    if(tl) datasets.push(tl);
    if(avgLine) avgPts = avgLine.data;
    rangeDatasets(stat).forEach(function(d){ datasets.push(d); });
  }

  renderLegend('legendRemaining', datasets);
  var ctx = document.getElementById('chartRemaining');
  var tooltipCtx = { avgPts: avgPts, forecastPts: fcData };
  charts.epicRemaining = makeChart(ctx, {type:'line', data:{datasets:datasets}, options: baseChartOptions(unitAxisTitle('ост. время'), tooltipCtx)});
}

function renderEpicSpentChart(){
  destroyChart('epicSpent');
  var sid = parseInt(document.getElementById('chartServiceSelect').value, 10);
  var svc = serviceById(sid);
  var rows = svc ? entriesForService(sid) : [];
  if(setChartEmpty('chartSpentEpic', svc ? noValuesMessage(rows, 'spent') : noEpicsMessage())){
    document.getElementById('legendSpentEpic').innerHTML = '';
    return;
  }
  var palette = colorPalette();
  var data = rows.map(function(r){ return {x:r.date, y:r.spent}; });
  var datasets = [{ label: svc ? svc.name : '', data: data, borderColor: palette[1], backgroundColor: palette[1]+'33', spanGaps:true, tension:0.25, cubicInterpolationMode:'monotone', pointRadius:3, borderWidth:2 }];
  renderLegend('legendSpentEpic', datasets);
  var ctx = document.getElementById('chartSpentEpic');
  charts.epicSpent = makeChart(ctx, {type:'line', data:{datasets:datasets}, options: baseChartOptions(unitAxisTitle())});
}

function renderEpicEstimateChart(){
  destroyChart('epicEstimate');
  var sid = parseInt(document.getElementById('chartServiceSelect').value, 10);
  var svc = serviceById(sid);
  var rows = svc ? entriesForService(sid) : [];
  if(setChartEmpty('chartEstimateEpic', svc ? noValuesMessage(rows, 'estimate') : noEpicsMessage())){
    document.getElementById('legendEstimateEpic').innerHTML = '';
    return;
  }
  var palette = colorPalette();
  var data = rows.map(function(r){ return {x:r.date, y:r.estimate}; });
  var datasets = [{ label: svc ? svc.name : '', data: data, borderColor: palette[2], backgroundColor: palette[2]+'33', spanGaps:true, tension:0.25, cubicInterpolationMode:'monotone', pointRadius:3, borderWidth:2 }];
  renderLegend('legendEstimateEpic', datasets);
  var ctx = document.getElementById('chartEstimateEpic');
  charts.epicEstimate = makeChart(ctx, {type:'line', data:{datasets:datasets}, options: baseChartOptions(unitAxisTitle())});
}

var allEpicsBlock = document.getElementById('allEpicsBlock');
try { if(localStorage.getItem('burndown-all-epics-open') === '0') allEpicsBlock.open = false; } catch(e){}
allEpicsBlock.addEventListener('toggle', function(){
  try { localStorage.setItem('burndown-all-epics-open', allEpicsBlock.open ? '1' : '0'); } catch(e){}
  if(allEpicsBlock.open && currentView === 'dashboard') renderAllEpicsCharts();
});

function renderAllEpicsCharts(){
  ['allRemaining','allSpent','allEstimate'].forEach(destroyChart);
  if(!allEpicsBlock.open) return;  /* свёрнутый блок не рисуем — графики построятся при раскрытии */
  var ids = visibleDashboardServices().map(function(s){ return s.id; });
  [['remaining', 'allRemaining', 'chartRemainingAll', 'legendRemainingAll', unitAxisTitle('ост. время')],
   ['spent', 'allSpent', 'chartSpent', 'legendSpent', unitAxisTitle()],
   ['estimate', 'allEstimate', 'chartEstimate', 'legendEstimate', unitAxisTitle()]].forEach(function(c){
    var hasValues = ids.some(function(sid){ return entriesForService(sid).some(function(r){ return isNum(r[c[0]]); }); });
    var empty = !ids.length ? noEpicsMessage() : (hasValues ? null : 'Значения «' + HOUR_LABELS[c[0]] + '» по выбранным эпикам не вносились');
    if(setChartEmpty(c[2], empty)){ document.getElementById(c[3]).innerHTML = ''; return; }
    var datasets = buildDatasetsForMetric(c[0], ids);
    renderLegend(c[3], datasets);
    charts[c[1]] = makeChart(document.getElementById(c[2]), {type:'line', data:{datasets:datasets}, options: baseChartOptions(c[4])});
  });
}

function refreshCharts(){
  if(document.getElementById('view-dashboard').classList.contains('active')){
    renderEpicCharts();
    renderAllEpicsCharts();
  }
  if(document.getElementById('view-stages').classList.contains('active')){
    renderStageCharts();
  }
}

/* ════════════════════════════════════════════════════════════════
   KPIs & Summary tables
════════════════════════════════════════════════════════════════ */
function kpiCard(label, value, note, color){
  return '<div class="kpi-card"><div class="kpi-label">'+label+'</div><div class="kpi-value"'+(color?' style="color:var('+color+')"':'')+'>'+value+'</div><div class="kpi-note">'+note+'</div></div>';
}

function renderKpis(){
  var vis = visibleDashboardServices();
  var okCount=0, riskCount=0, dangerCount=0, totalRemaining=0;
  vis.forEach(function(s){
    var stat = computeServiceStatus(s.id);
    if(stat.status==='ok') okCount++; else if(stat.status==='risk') riskCount++; else if(stat.status==='danger') dangerCount++;
    totalRemaining += stat.lastRemaining || 0;
  });
  document.getElementById('kpiGrid').innerHTML =
    kpiCard('Всего эпиков', vis.length, 'Выбрано на дашборде') +
    kpiCard('Успевают', okCount, 'В рамках целевой даты', '--color-success') +
    kpiCard('Риск срыва', riskCount, 'В пределах буфера риска', '--color-warning') +
    kpiCard('Не успевают', dangerCount, 'Прогноз позже цели', '--color-error') +
    kpiCard('Остаток по проекту', hoursToStr(totalRemaining), 'Сумма по выбранным эпикам');
}

function renderSummaryTable(){
  var table = document.getElementById('summaryTable');
  var html = '<thead><tr>'+
    '<th class="sortable" data-sort="etap">Этап</th>'+
    '<th class="sortable" data-sort="name">Эпик</th>'+
    '<th class="sortable num" data-sort="remaining">Остаток</th>'+
    '<th class="sortable num" data-sort="burnRate">Burn rate, '+rateUnit()+'</th>'+
    '<th class="sortable num" data-sort="scope" title="Изменение первоначальной оценки с первого замера">Δ оценки</th>'+
    '<th class="sortable" data-sort="targetDate">Цель</th>'+
    '<th class="sortable" data-sort="forecastZero">Прогноз «0»</th>'+
    '<th class="sortable num" data-sort="probability" title="Вероятность обнулить остаток к цели по моделированию Монте-Карло">Шанс успеть</th>'+
    '<th class="sortable" data-sort="status">Статус</th>'+
    '</tr></thead><tbody>';
  var totalRemaining = 0, totalBurnRate = 0, totalScope = 0, hasRemaining=false, hasBurn=false, hasScope=false;
  var rows = visibleDashboardServices().map(function(s){
    var stat = computeServiceStatus(s.id);
    if(isNum(stat.lastRemaining)){ totalRemaining += stat.lastRemaining; hasRemaining = true; }
    if(isNum(stat.burnRate)){ totalBurnRate += stat.burnRate; hasBurn = true; }
    if(isNum(stat.scope)){ totalScope += stat.scope; hasScope = true; }
    return {etap:s.etap, name:s.name, isDone:s.isDone, remaining:stat.lastRemaining, burnRate:stat.burnRate, scope:stat.scope, targetDate:s.targetDate||'', forecastZero:stat.forecastZeroDate||'', noBurn:stat.noBurn, tooFar:stat.tooFar, status:stat.status,
            mc:stat.mc, probability:stat.mc ? stat.mc.probability : null};
  });
  if(!rows.length){
    html += '<tr><td colspan="9" class="empty-cell">'+(STATE.services.length ? 'Нет эпиков для отображения — проверьте фильтр выше.' : 'В проекте пока нет эпиков — добавьте их в разделе «Эпики (блоки задач)».')+'</td></tr>';
  }
  applyTableSort('summaryTable', rows).forEach(function(r){
    html += '<tr'+(r.isDone?' class="done-row"':'')+'><td class="nowrap">'+escapeHtml(r.etap)+'</td><td>'+escapeHtml(r.name)+'</td><td class="num">'+hoursToStr(r.remaining)+'</td>'+rateCell(r.burnRate, true)+scopeCell(r.scope)+'<td class="mono nowrap">'+fmtDate(r.targetDate)+'</td><td class="mono nowrap">'+forecastText(r)+'</td>'+probabilityCell(r.mc)+'<td class="nowrap">'+statusBadge(r.status)+'</td></tr>';
  });
  if(rows.length){
    html += '<tr class="total-row"><td colspan="2">Итого</td><td class="num">'+(hasRemaining?hoursToStr(totalRemaining):'—')+'</td>'+rateCell(hasBurn?totalBurnRate:null, true)+scopeCell(hasScope?totalScope:null)+'<td colspan="4"></td></tr>';
  }
  html += '</tbody>';
  table.innerHTML = html;
  initTableSort(table, renderSummaryTable);
  tableFilter(table, ['list', 'text', 'num', 'num', 'num', 'text', 'text', 'num', 'list']);
}

/* ════════════════════════════════════════════════════════════════
   Burn rate daily tables
════════════════════════════════════════════════════════════════ */
/* Даты замеров остатка только тех эпиков, что показаны в таблице (фильтр, «Скрыть сданные») — как в PDF */
function unionOfDates(serviceIds){
  var set = {}, ids = {};
  serviceIds.forEach(function(id){ ids[id] = true; });
  STATE.entries.forEach(function(e){ if(ids[e.service_id] && isNum(e.remaining)) set[e.date] = true; });
  return Object.keys(set).sort();
}

function burnRateDates(range, serviceIds){
  var allDates = unionOfDates(serviceIds);
  if(range && (range.from || range.to)){
    return allDates.filter(function(d){ return (!range.from || d >= range.from) && (!range.to || d <= range.to); });
  }
  return allDates.slice(-5);
}

/* Дневной burn rate эпика на каждую из дат: (предыдущий заполненный остаток − текущий) / дней между замерами */
function serviceDailyRates(serviceId, dates){
  var rows = entriesForService(serviceId).filter(function(r){ return isNum(r.remaining); });
  return dates.map(function(d){
    var idx = -1;
    for(var i=0;i<rows.length;i++){ if(rows[i].date === d){ idx = i; break; } }
    if(idx <= 0) return null;
    var days = workCalendar().span(rows[idx-1].date, d);
    return days > 0 ? (rows[idx-1].remaining - rows[idx].remaining) / days : null;
  });
}

function sumRates(rateLists, len){
  var res = [];
  for(var i=0;i<len;i++){
    var sum = 0, has = false;
    rateLists.forEach(function(list){ if(isNum(list[i])){ sum += list[i]; has = true; } });
    res.push(has ? sum : null);
  }
  return res;
}

function burnRateTableHtml(firstHeader, dates, rows){
  /* rows: [{label, rates}] — последняя строка «Итого по проекту» добавляется автоматически */
  var rateCells = function(rates){ return rates.map(function(r){ return rateCell(r); }).join(''); };
  var html = '<thead><tr><th>'+firstHeader+'</th>' + dates.map(function(d){ return '<th class="num">'+fmtShortDate(d)+'</th>'; }).join('') + '</tr></thead><tbody>';
  rows.forEach(function(r){ html += '<tr><td>'+escapeHtml(r.label)+'</td>'+rateCells(r.rates)+'</tr>'; });
  var totals = sumRates(rows.map(function(r){ return r.rates; }), dates.length);
  html += '<tr class="total-row"><td>Итого по проекту</td>'+rateCells(totals)+'</tr></tbody>';
  return html;
}

function emptyTable(table, title, text){
  table.innerHTML = '<thead><tr><th>'+title+'</th></tr></thead><tbody><tr><td class="empty-cell">'+text+'</td></tr></tbody>';
}

function renderBurnRateTable(){
  var table = document.getElementById('burnRateTable');
  var dates = burnRateDates(burnRangeOverride, visibleDashboardServices().map(function(s){ return s.id; }));
  if(!dates.length){ emptyTable(table, 'Нет данных', 'Добавьте замеры в разделе «Данные по датам».'); return; }
  var rows = visibleDashboardServices().map(function(s){
    return {label: s.etap+' — '+s.name, rates: serviceDailyRates(s.id, dates)};
  });
  table.innerHTML = burnRateTableHtml('Эпик', dates, rows);
  tableFilter(table, ['text'].concat(dates.map(function(){ return 'num'; })));
}

/* ════════════════════════════════════════════════════════════════
   Stages view
════════════════════════════════════════════════════════════════ */
function renderStageKpis(){
  var grid = document.getElementById('stageKpiGrid');
  var stageGroups = computeStageGroups();
  var stageNames = Object.keys(stageGroups);
  if(!stageNames.length){
    grid.innerHTML = '<div class="empty-state panel" style="grid-column:1/-1;"><p>Нет этапов в этом проекте. Добавьте эпики в разделе «Эпики (блоки задач)».</p></div>';
    return;
  }
  grid.innerHTML = stageNames.map(function(name){
    var stat = computeStageStatus(name, stageGroups);
    return kpiCard(escapeHtml(name), hoursToStr(stat.lastRemaining), 'Цель: '+fmtDate(STATE.stageTargets[name])+' '+statusBadge(stat.status));
  }).join('');
}

function renderStageSummaryTable(){
  var table = document.getElementById('stageSummaryTable');
  var stageGroups = computeStageGroups();
  var stageNames = Object.keys(stageGroups);
  if(!stageNames.length){ emptyTable(table, 'Нет данных', 'Добавьте эпики для отображения этапов.'); return; }
  var html = '<thead><tr>'+
    '<th class="sortable" data-sort="name">Этап</th>'+
    '<th class="sortable num" data-sort="count">Эпиков</th>'+
    '<th class="sortable num" data-sort="remaining">Остаток (сумма)</th>'+
    '<th class="sortable num" data-sort="burnRate">Burn rate, '+rateUnit()+'</th>'+
    '<th class="sortable num" data-sort="scope" title="Изменение суммарной оценки с первого замера">Δ оценки</th>'+
    '<th class="sortable" data-sort="targetDate">Цель</th>'+
    '<th class="sortable" data-sort="forecastZero">Прогноз «0»</th>'+
    '<th class="sortable num" data-sort="probability" title="Вероятность обнулить остаток к цели по моделированию Монте-Карло">Шанс успеть</th>'+
    '<th class="sortable" data-sort="status">Статус</th>'+
    '</tr></thead><tbody>';
  var rows = stageNames.map(function(name){
    var stat = computeStageStatus(name, stageGroups);
    return {name:name, count:stageGroups[name].length, remaining:stat.lastRemaining, burnRate:stat.burnRate, scope:stat.scope, targetDate:STATE.stageTargets[name]||'', forecastZero:stat.forecastZeroDate||'', noBurn:stat.noBurn, tooFar:stat.tooFar, status:stat.status, mc:stat.mc, probability:stat.mc ? stat.mc.probability : null};
  });
  applyTableSort('stageSummaryTable', rows).forEach(function(r){
    html += '<tr><td>'+escapeHtml(r.name)+'</td><td class="num">'+r.count+'</td><td class="num">'+hoursToStr(r.remaining)+'</td>'+rateCell(r.burnRate, true)+scopeCell(r.scope)+'<td class="mono nowrap">'+fmtDate(r.targetDate)+'</td><td class="mono nowrap">'+forecastText(r)+'</td>'+probabilityCell(r.mc)+'<td class="nowrap">'+statusBadge(r.status)+'</td></tr>';
  });
  html += '</tbody>';
  table.innerHTML = html;
  initTableSort(table, renderStageSummaryTable);
  tableFilter(table, ['text', 'num', 'num', 'num', 'num', 'text', 'text', 'num', 'list']);
}

function renderStageBurnRateTable(){
  var table = document.getElementById('stageBurnRateTable');
  var stageGroups = computeStageGroups();
  var dates = burnRateDates(stageBurnRangeOverride, [].concat.apply([], Object.keys(stageGroups).map(function(k){ return stageGroups[k]; })));
  if(!dates.length){ emptyTable(table, 'Нет данных', 'Добавьте замеры для отображения burn rate.'); return; }
  var stageNames = Object.keys(stageGroups);
  if(!stageNames.length){ emptyTable(table, 'Нет данных', 'Нет этапов для отображения.'); return; }
  /* Burn rate этапа — сумма дневных burn rate его эпиков (согласовано с таблицей на дашборде) */
  var rows = stageNames.map(function(name){
    return {label: name, rates: sumRates(stageGroups[name].map(function(id){ return serviceDailyRates(id, dates); }), dates.length)};
  });
  table.innerHTML = burnRateTableHtml('Этап', dates, rows);
  tableFilter(table, ['text'].concat(dates.map(function(){ return 'num'; })));
}

function buildStagePanelsSkeleton(){
  var container = document.getElementById('stageChartsContainer');
  var stageGroups = computeStageGroups();
  var stageNames = Object.keys(stageGroups);
  if(!stageNames.length){ container.innerHTML = ''; return; }
  container.innerHTML = stageNames.map(function(name, idx){
    var safeId = 'st'+idx;
    return '<div class="panel"><div class="panel-head"><div><div class="panel-title">'+escapeHtml(name)+'</div><div class="section-note">Сумма остатка по эпикам этапа. Вертикальный пунктир — целевые даты эпиков.</div></div><div class="panel-controls"><label class="small" for="stageTarget_'+safeId+'">Цель этапа</label><input type="date" id="stageTarget_'+safeId+'" data-stage="'+escapeAttr(name)+'"></div></div><div class="legend-row" id="legendStage_'+safeId+'"></div><div class="chart-wrap"><canvas id="chartStage_'+safeId+'"></canvas></div></div>';
  }).join('');

  container.querySelectorAll('input[type=date][data-stage]').forEach(function(inp){
    var stageName = inp.dataset.stage;
    inp.value = STATE.stageTargets[stageName] || '';
    inp.addEventListener('change', function(e){
      var prev = STATE.stageTargets[stageName];
      var refresh = function(){ renderStageCharts(); renderStageKpis(); renderStageSummaryTable(); };
      STATE.stageTargets[stageName] = e.target.value;
      apiPut('/api/projects/'+currentProjectId+'/stage_targets', {stageName:stageName, targetDate:e.target.value}).then(function(){
        toast('Целевая дата «'+stageName+'» обновлена');
      }).catch(function(){
        STATE.stageTargets[stageName] = prev;
        inp.value = prev || '';
        refresh();
      });
      refresh();
    });
  });
  applyReadonlyToView('view-stages');
}

/* Подписи дат завершения эпиков у вертикальных линий целей — видны без наведения */
var epicTargetLabelPlugin = {
  id: 'epicTargetLabels',
  afterDatasetsDraw: function(chart){
    var ctx = chart.ctx;
    var area = chart.chartArea;
    if(!area) return;
    ctx.save();
    ctx.font = '600 10px Inter, Arial, sans-serif';
    ctx.textBaseline = 'top';
    var rows = []; /* занятые диапазоны подписей по строкам, чтобы не наползали */
    chart.data.datasets.forEach(function(ds){
      if(!ds.isEpicTarget || !ds.data || !ds.data.length) return;
      var text = fmtDate(ds.targetDate) + ' — ' + (ds.epicName || '');
      if(text.length > 36) text = text.slice(0, 35) + '…';
      var w = ctx.measureText(text).width;
      var x = chart.scales.x.getPixelForValue(ds.targetDate);
      var tx = x + 5;
      if(tx + w > area.right - 2) tx = x - w - 5;
      var y = area.top + 4;
      for(var r = 0; r < 6; r++){
        var rowY = area.top + 4 + r * 13;
        var overlap = rows.some(function(row){ return row.r === r && tx < row.x2 && tx + w > row.x1; });
        if(!overlap){ y = rowY; rows.push({r:r, x1:tx, x2:tx+w}); break; }
      }
      ctx.fillStyle = ds.borderColor;
      ctx.fillText(text, tx, y);
    });
    ctx.restore();
  }
};

/* Вертикальная отметка «Сегодня» на всех графиках с осью времени (если сегодня попадает в диапазон оси) */
var todayLinePlugin = {
  id: 'todayLine',
  afterDatasetsDraw: function(chart){
    var x = chart.scales && chart.scales.x;
    var area = chart.chartArea;
    if(!x || !area || x.type !== 'time') return;
    var t = Date.parse(todayIso() + 'T00:00:00');
    if(t < x.min || t > x.max) return;
    var px = x.getPixelForValue(t);
    var ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = getCss('--color-text-faint');
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(px, area.top); ctx.lineTo(px, area.bottom); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = getCss('--color-text-muted');
    ctx.font = '600 10px Inter, Arial, sans-serif';
    ctx.textBaseline = 'bottom';
    var label = 'Сегодня';
    var w = ctx.measureText(label).width;
    ctx.fillText(label, Math.min(px + 4, area.right - w), area.bottom - 4);
    ctx.restore();
  }
};
if(window.Chart) Chart.register(todayLinePlugin);

function renderStageCharts(){
  destroyChartsByPrefix('stage_');
  var stageGroups = computeStageGroups();
  Object.keys(stageGroups).forEach(function(name, idx){
    var safeId = 'st'+idx;
    var canvas = document.getElementById('chartStage_'+safeId);
    if(!canvas) return;
    var stat = computeStageStatus(name, stageGroups);
    var agg = stat.agg;
    var palette = colorPalette();
    var targetDate = STATE.stageTargets[name];

    var actualData = agg.dates.map(function(d,i){ return {x:d, y: agg.remaining[i]}; });
    var datasets = [
      { label: name+': остаток (сумма)', data: actualData, borderColor: palette[1], backgroundColor: palette[1]+'33', borderWidth: 2.5, pointRadius:4, tension:0.2, cubicInterpolationMode:'monotone', spanGaps:true, isActual:true }
    ];

    var avgLine = avgLineDataset(agg.dates, stat.burnRate, stat.lastRemaining, stat.lastDate);
    var avgPts = null;
    if(avgLine){ datasets.push(avgLine); avgPts = avgLine.data; }

    var fcData = null;
    if(agg.dates.length){
      var firstDate = agg.dates[0];
      var lastRow = {date: agg.dates[agg.dates.length-1]};
      var fc = forecastDataset(stat, firstDate, lastRow, targetDate);
      if(fc){ datasets.push(fc); fcData = fc.data; }
      var tl = targetLineDataset(stat.lastDate, stat.lastRemaining, targetDate, 'Целевая линия (этап)');
      if(tl) datasets.push(tl);
    }

    /* Целевые даты эпиков этапа — вертикальные отметки на графике */
    var yMax = 0;
    agg.remaining.forEach(function(v){ if(v !== null && v !== undefined && v > yMax) yMax = v; });
    if(yMax > 0){
      servicesInStage(name, stageGroups).forEach(function(svc, idx){
        if(!svc.targetDate) return;
        datasets.push({
          label: 'Цель эпика: ' + svc.name,
          data: [{x: svc.targetDate, y: 0}, {x: svc.targetDate, y: yMax}],
          borderColor: palette[(idx + 3) % palette.length],
          borderDash: [2, 4], pointRadius: 0, pointHoverRadius: 0, borderWidth: 1.5, tension: 0,
          isEpicTarget: true, epicName: svc.name, targetDate: svc.targetDate
        });
      });
    }

    renderLegend('legendStage_'+safeId, datasets);
    var tooltipCtx = { avgPts: avgPts, forecastPts: fcData };
    charts['stage_'+safeId] = makeChart(canvas, {type:'line', data:{datasets:datasets}, options: baseChartOptions(unitAxisTitle('суммарный остаток'), tooltipCtx), plugins:[epicTargetLabelPlugin]});
  });
}

/* ════════════════════════════════════════════════════════════════
   Data entry view
════════════════════════════════════════════════════════════════ */
function serviceOption(s){
  return '<option value="'+s.id+'">'+escapeHtml(s.etap)+' — '+escapeHtml(s.name)+(s.isDone?' (сдано)':'')+'</option>';
}
function populateServiceSelects(){
  var chartSel = document.getElementById('chartServiceSelect');
  var dataSel = document.getElementById('dataServiceSelect');
  var vis = visibleDashboardServices();
  var prevChart = parseInt(chartSel.value, 10);
  chartSel.innerHTML = vis.length ? vis.map(serviceOption).join('') : '<option value="">Нет выбранных эпиков</option>';
  if(vis.some(function(s){ return s.id === prevChart; })) chartSel.value = prevChart;
  dataSel.innerHTML = STATE.services.length ? STATE.services.map(serviceOption).join('') : '<option value="">Нет эпиков</option>';
  if(currentDataServiceId) dataSel.value = currentDataServiceId;
  document.getElementById('navServiceCount').textContent = STATE.services.length;
}

function saveService(svc){
  return apiPut('/api/services/'+svc.id, {etap:svc.etap, name:svc.name, targetDate:svc.targetDate, isDone:svc.isDone, stageGroup:svc.stageGroup});
}

/* Enter / Shift+Enter — переход к тому же полю в следующей / предыдущей строке таблицы */
function enableEnterNavigation(table){
  if(table.dataset.enterNav) return;  /* таблица перерисовывается, а обработчик на ней — один */
  table.dataset.enterNav = '1';
  table.addEventListener('keydown', function(e){
    if(e.key !== 'Enter' || e.target.tagName !== 'INPUT') return;
    e.preventDefault();
    var field = e.target.dataset.field;
    var rows = Array.from(table.querySelectorAll('tbody tr'));
    var idx = rows.indexOf(e.target.closest('tr')) + (e.shiftKey ? -1 : 1);
    var next = rows[idx] && rows[idx].querySelector('input[data-field="'+field+'"]');
    if(next){ next.focus(); if(next.select) next.select(); }
    else e.target.blur();  /* последняя строка — фиксируем значение */
  });
}

function refreshDashboardParts(){
  refreshCharts();
  renderJiraComposition();
  renderSummaryTable();
  renderKpis();
  renderBurnRateTable();
}

/* Перетаскивание строк только за «ручку», чтобы не мешать выделению текста в полях */
function enableRowDrag(table, onDrop){
  var dragRow = null;
  table.querySelectorAll('tbody tr[data-id]').forEach(function(tr){
    var handle = tr.querySelector('.drag-handle');
    if(!handle) return;
    handle.addEventListener('mousedown', function(){ tr.setAttribute('draggable', 'true'); });
    handle.addEventListener('mouseup', function(){ tr.removeAttribute('draggable'); });
    tr.addEventListener('dragstart', function(e){ dragRow = tr; tr.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; });
    tr.addEventListener('dragend', function(){ tr.classList.remove('dragging'); tr.removeAttribute('draggable'); dragRow = null; });
    tr.addEventListener('dragover', function(e){
      e.preventDefault();
      if(!dragRow || dragRow === tr) return;
      var rect = tr.getBoundingClientRect();
      tr.parentNode.insertBefore(dragRow, e.clientY < rect.top + rect.height/2 ? tr : tr.nextSibling);
    });
    tr.addEventListener('drop', function(e){
      e.preventDefault();
      if(dragRow) onDrop(Array.from(table.querySelectorAll('tbody tr[data-id]')));
    });
  });
}

function renderDataView(){
  populateServiceSelects();
  var rows = currentDataServiceId ? entriesForService(currentDataServiceId) : [];
  rows = applyTableSort('entriesTable', rows);
  var table = document.getElementById('entriesTable');
  document.getElementById('dataEmptyHint').style.display = rows.length ? 'none' : '';
  table.style.display = rows.length ? '' : 'none';

  var numInput = function(r, field){ return '<input type="number" step="0.01" min="0" value="'+(r[field] ?? '')+'" data-field="'+field+'" aria-label="'+HOUR_LABELS[field]+', ч, '+fmtDate(r.date)+'">'; };
  var html = '<colgroup><col class="col-date"><col class="col-num"><col class="col-num"><col class="col-num"><col class="col-actions"></colgroup>'+
    '<thead><tr><th class="sortable" data-sort="date">Дата</th><th class="sortable" data-sort="remaining">Остаток, ч</th><th class="sortable" data-sort="spent">Затрачено, ч</th><th class="sortable" data-sort="estimate">Оценка, ч</th><th class="actions-col"></th></tr></thead><tbody>';
  rows.forEach(function(r){
    html += '<tr data-id="'+r.id+'"><td data-label="Дата"><input type="date" value="'+r.date+'" data-field="date" required aria-label="Дата замера"></td>'+
      '<td data-label="Остаток, ч">'+numInput(r,'remaining')+'</td><td data-label="Затрачено, ч">'+numInput(r,'spent')+'</td>'+
      '<td data-label="Оценка, ч">'+numInput(r,'estimate')+'</td><td class="row-actions">'+actionBtn('delete','Удалить',ICON_DELETE)+'</td></tr>';
  });
  html += '</tbody>';
  table.innerHTML = html;
  initTableSort(table, renderDataView);
  tableFilter(table, ['text', 'num', 'num', 'num', null]);

  table.querySelectorAll('input').forEach(function(inp){
    inp.addEventListener('change', function(){
      var id = parseInt(inp.closest('tr').dataset.id, 10);
      var entry = STATE.entries.find(function(e){ return e.id === id; });
      if(!entry) return;
      var field = inp.dataset.field;
      if(field === 'date'){
        var dup = STATE.entries.some(function(e){ return e.id !== id && e.service_id === entry.service_id && e.date === inp.value; });
        if(!inp.value || dup){
          toast(inp.value ? 'Замер на эту дату уже есть' : 'Укажите дату');
          inp.value = entry.date;
          return;
        }
      }
      var newVal = field === 'date' ? inp.value : (inp.value === '' ? null : parseFloat(inp.value));
      if(field !== 'date' && newVal !== null && (isNaN(newVal) || newVal < 0)){
        toast('Введите число не меньше 0');
        inp.value = entry[field] ?? '';
        return;
      }
      var prev = entry[field];
      entry[field] = newVal;
      apiPut('/api/entries/'+id, {date:entry.date, remaining:entry.remaining, spent:entry.spent, estimate:entry.estimate}).then(function(){
        toast('Сохранено');
      }).catch(function(){
        entry[field] = prev;   /* сервер не принял изменение — возвращаем прежнее значение */
        if(currentView === 'data') renderDataView();
        refreshDashboardParts();
      });
      if(field === 'date') renderDataView();
      refreshDashboardParts();
    });
  });
  enableEnterNavigation(table);
  table.querySelectorAll('[data-action="delete"]').forEach(function(btn){
    btn.addEventListener('click', function(){
      var id = parseInt(btn.closest('tr').dataset.id, 10);
      askConfirm('Удалить запись?', 'Строка данных будет удалена без возможности восстановления.', function(){
        apiDelete('/api/entries/'+id).then(function(){
          STATE.entries = STATE.entries.filter(function(e){ return e.id !== id; });
          renderDataView();
          refreshDashboardParts();
          toast('Строка удалена');
        });
      });
    });
  });
  applyReadonlyToView('view-data');
}

/* ════════════════════════════════════════════════════════════════
   Services view
════════════════════════════════════════════════════════════════ */
function renderServicesView(){
  var table = document.getElementById('servicesTable');
  var groupHint = 'Группа для «Свода по этапам»; пусто — группировка по этапу';
  var html = '<colgroup><col class="col-drag"><col class="col-etap"><col class="col-name"><col class="col-target"><col class="col-done"><col class="col-group"><col class="col-count"><col class="col-actions"></colgroup>'+
    '<thead><tr><th class="drag-col"></th><th class="sortable" data-sort="etap">Этап</th><th class="sortable" data-sort="name">Название эпика</th><th class="sortable" data-sort="targetDate">Целевая дата</th><th class="sortable center" data-sort="isDone" title="Сданный функционал не учитывается в рамках этапа">Сдано</th><th class="sortable" data-sort="stageGroup" title="'+groupHint+'">Группа в своде</th><th class="sortable num" data-sort="count">Замеров</th><th class="actions-col"></th></tr></thead><tbody>';
  var services = getSortedServices();
  if(!services.length){
    html += '<tr><td colspan="8" class="empty-cell">Эпиков пока нет — добавьте первый.</td></tr>';
  }
  services.forEach(function(s){
    html += '<tr data-id="'+s.id+'"'+(s.isDone?' class="done-row"':'')+'>'+
      '<td class="drag-handle" title="Перетащите для изменения порядка">⠿</td>'+
      '<td><input type="text" value="'+escapeAttr(s.etap)+'" data-field="etap"></td>'+
      '<td class="col-name-cell"><input type="text" value="'+escapeAttr(s.name)+'" data-field="name"></td>'+
      '<td><input type="date" value="'+(s.targetDate||'')+'" data-field="targetDate"></td>'+
      '<td class="center"><input type="checkbox" data-field="isDone" title="Функционал сдан"'+(s.isDone?' checked':'')+'></td>'+
      '<td><input type="text" value="'+escapeAttr(s.stageGroup||'')+'" placeholder="по этапу" data-field="stageGroup" title="'+groupHint+'"></td>'+
      '<td class="num">'+entriesForService(s.id).length+'</td>'+
      '<td class="row-actions">'+actionBtn('delete','Удалить',ICON_DELETE)+'</td></tr>';
  });
  html += '</tbody>';
  table.innerHTML = html;
  initTableSort(table, renderServicesView);
  tableFilter(table, [null, 'text', 'text', 'text', 'list', 'text', 'num', null]);

  if(canEdit()){
    enableRowDrag(table, function(rows){
      var orders = rows.map(function(r, i){ return {id: parseInt(r.dataset.id, 10), sortOrder: i+1}; });
      apiPost('/api/projects/'+currentProjectId+'/services/reorder', {orders: orders}).then(function(){
        orders.forEach(function(o){ var svc = serviceById(o.id); if(svc) svc.sortOrder = o.sortOrder; });
        STATE.services.sort(function(a,b){ return (a.sortOrder||0) - (b.sortOrder||0); });
        delete tableSortState['servicesTable'];
        renderServicesView();
        populateServiceSelects();
        toast('Порядок обновлён');
      });
    });
  }

  table.querySelectorAll('input').forEach(function(inp){
    inp.addEventListener('change', function(){
      var id = parseInt(inp.closest('tr').dataset.id, 10);
      var svc = serviceById(id);
      if(!svc) return;
      var field = inp.dataset.field;
      var prev = svc[field];
      if(field === 'isDone') svc.isDone = inp.checked;
      else if(field === 'etap' || field === 'name') svc[field] = inp.value.trim();
      else svc[field] = inp.value;
      saveService(svc).then(function(){
        toast('Сохранено');
      }).catch(function(){
        svc[field] = prev;
        renderServicesView();
        populateServiceSelects();
        renderDashboardFilterBar();
      });
      if(field === 'isDone') inp.closest('tr').classList.toggle('done-row', svc.isDone);
      populateServiceSelects();
      renderDashboardFilterBar();
    });
  });
  table.querySelectorAll('[data-action="delete"]').forEach(function(btn){
    btn.addEventListener('click', function(){
      var id = parseInt(btn.closest('tr').dataset.id, 10);
      askConfirm('Удалить эпик?', 'Все связанные замеры данных также будут удалены.', function(){
        apiDelete('/api/services/'+id).then(function(){
          STATE.services = STATE.services.filter(function(s){ return s.id !== id; });
          STATE.entries = STATE.entries.filter(function(e){ return e.service_id !== id; });
          if(currentDataServiceId === id) currentDataServiceId = STATE.services.length ? STATE.services[0].id : null;
          renderServicesView();
          populateServiceSelects();
          renderDashboardFilterBar();
          toast('Эпик удалён');
        });
      });
    });
  });
  applyReadonlyToView('view-services');
}

/* ════════════════════════════════════════════════════════════════
   Settings view
════════════════════════════════════════════════════════════════ */
/* Списки дат календаря: «01.01.2026», «2026-01-01», диапазоны «01.01.2026 – 08.01.2026»; через запятую или с новой строки */
function parseDateToken(t){
  var m = t.match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})$/), iso;
  if(m) iso = m[3] + '-' + ('0' + m[2]).slice(-2) + '-' + ('0' + m[1]).slice(-2);
  else if(/^\d{4}-\d{2}-\d{2}$/.test(t)) iso = t;
  else return null;
  var d = new Date(iso + 'T00:00:00Z');
  return !isNaN(d) && d.toISOString().slice(0, 10) === iso ? iso : null;
}
function parseDateList(text){
  var out = {}, bad = [];
  text.split(/[\n,;]+/).map(function(x){ return x.trim(); }).filter(Boolean).forEach(function(item){
    var parts = item.split(/\s*(?:–|—|\.\.|\s-\s)\s*/);
    var a = parseDateToken(parts[0]), b = parts.length > 1 ? parseDateToken(parts[1]) : a;
    if(!a || !b || parts.length > 2 || b < a || daysBetween(a, b) > 366){ bad.push(item); return; }
    for(var d = a; d <= b; d = addDays(d, 1)) out[d] = true;
  });
  return {dates: Object.keys(out).sort(), bad: bad};
}
/* обратно в текст: подряд идущие даты — диапазоном */
function formatDateList(dates){
  var lines = [], i = 0;
  while(i < dates.length){
    var j = i;
    while(j + 1 < dates.length && addDays(dates[j], 1) === dates[j + 1]) j++;
    lines.push(j > i ? fmtDate(dates[i]) + ' – ' + fmtDate(dates[j]) : fmtDate(dates[i]));
    i = j + 1;
  }
  return lines.join('\n');
}
function renderSettingsView(){
  var st = STATE.settings;
  var burnMethod = st.burnrateMethod || 'all';
  var opt = function(value, label, current){ return '<option value="'+value+'"'+(String(current)===String(value)?' selected':'')+'>'+label+'</option>'; };
  var num = function(id, label, value, min, max, hint){
    return '<div class="setting-item"><label class="small" for="'+id+'">'+label+'</label><input type="number" id="'+id+'" min="'+min+'" max="'+max+'" step="1" value="'+value+'">'+
      (hint ? '<div class="setting-hint">'+hint+'</div>' : '')+'</div>';
  };
  /* группы — по вкладкам раздела (прогноз, календарь, единицы, выводы) */
  document.getElementById('settingsGrid').innerHTML =
    '<div class="settings-grid" data-tab-panel="forecast"><div class="settings-divider">Прогноз</div>'+
    '<div class="setting-item"><label class="small" for="setForecastMethod">Метод прогноза</label><select id="setForecastMethod">'+opt('linear','Линейная регрессия',st.forecastMethod)+opt('average','Средняя скорость сгорания (burn rate)',st.forecastMethod)+'</select></div>'+
    num('setRiskBuffer', 'Буфер риска, дней', st.riskBufferDays ?? 3, 0, 365, 'Прогноз позже цели не больше чем на столько дней — «Риск», а не «Не успеваем»')+
    '<div class="setting-item"><label class="small" for="setConfidence">Диапазон прогноза</label><select id="setConfidence">'+
      [50, 70, 80, 90].map(function(c){ return opt(c, c + '% сценариев', st.rangeConfidence); }).join('')+'</select>'+
      '<div class="setting-hint">Между оптимистичной и пессимистичной датой — указанная доля сценариев Монте-Карло</div></div>'+
    '<div class="settings-divider">Расчёт среднего burn rate</div>'+
    '<div class="setting-item"><label class="small" for="setBurnrateMethod">Метод расчёта</label><select id="setBurnrateMethod">'+opt('all','По всей истории',burnMethod)+opt('rolling','Скользящее среднее',burnMethod)+'</select></div>'+
    '<div class="setting-item"><label class="small" for="setBurnrateWindow">Окно скользящего среднего, дней</label><input type="number" id="setBurnrateWindow" min="1" step="1" value="'+(st.burnrateWindowDays || 14)+'"'+(burnMethod!=='rolling'?' disabled':'')+'></div>'+
    '</div><div class="settings-grid" data-tab-panel="calendar"><div class="settings-divider">Календарь</div>'+
    '<div class="setting-item setting-wide"><label class="checkbox-row"><input type="checkbox" id="setWorkdays"'+(st.workdays?' checked':'')+'> Считать сроки в рабочих днях</label>'+
      '<div class="setting-hint">Прогноз, burn rate и шанс успеть — без выходных и праздников: работа идёт с понедельника по пятницу, кроме праздников, плюс рабочие выходные.</div></div>'+
    '<div class="setting-item setting-wide setting-hint">Общий производственный календарь задаёт администратор: '+
      ((st.orgHolidays || []).length ? 'нерабочих будней — ' + st.orgHolidays.length + ', рабочих выходных — ' + (st.orgExtraWorkdays || []).length + '. Ниже — дополнительные даты только этого проекта.' :
        'пока пуст. Ниже — даты только этого проекта.')+'</div>'+
    '<div class="setting-item"><label class="small" for="setHolidays">Дополнительные праздники проекта</label><textarea id="setHolidays" rows="5" placeholder="01.01.2026 – 08.01.2026&#10;23.02.2026"'+(st.workdays?'':' disabled')+'>'+escapeHtml(formatDateList(st.holidays || []))+'</textarea>'+
      '<div class="setting-hint">Даты или диапазоны, по одному в строке</div></div>'+
    '<div class="setting-item"><label class="small" for="setExtraWorkdays">Дополнительные рабочие выходные проекта</label><textarea id="setExtraWorkdays" rows="5" placeholder="27.12.2025"'+(st.workdays?'':' disabled')+'>'+escapeHtml(formatDateList(st.extraWorkdays || []))+'</textarea>'+
      '<div class="setting-hint">Суббота или воскресенье, которые объявлены рабочими</div></div>'+
    '</div><div class="settings-grid" data-tab-panel="units"><div class="settings-divider">Единицы</div>'+
    '<div class="setting-item"><label class="small" for="setUnits">Показывать объём работ</label><select id="setUnits">'+opt('hours','В часах',st.units)+opt('days','В человеко-днях',st.units)+'</select>'+
      '<div class="setting-hint">Только отображение: замеры вводятся и хранятся в часах</div></div>'+
    '<div class="setting-item"><label class="small" for="setHoursPerDay">Часов в человеко-дне</label><input type="number" id="setHoursPerDay" min="1" max="24" step="0.5" value="'+(st.hoursPerDay || 8)+'"'+(st.units!=='days'?' disabled':'')+'></div>'+
    '</div><div class="settings-grid" data-tab-panel="insights"><div class="settings-divider">Выводы</div>'+
    num('setStaleDays', 'Замер устарел через, дней', st.staleDays || 10, 1, 365, 'Нет замера дольше — вывод «данные могли устареть»')+
    num('setScopeThreshold', 'Рост объёма, %', st.scopeThreshold || 10, 1, 1000, 'Объём вырос сильнее за период сравнения — вывод о росте объёма')+
    num('setSlipThreshold', 'Сдвиг прогноза, дней', st.slipThreshold || 7, 1, 365, 'Прогноз сдвинулся сильнее за период сравнения — вывод о сдвиге')+
    num('setChanceThreshold', 'Порог шанса успеть, %', st.chanceThreshold || 50, 1, 99, 'Прогноз в срок, но шанс ниже — вывод о неустойчивом темпе')+
    num('setInsightWindow', 'Период сравнения, дней', st.insightWindowDays || 28, 7, 365, 'За какой срок сравниваются темп, объём и прогноз')+
    '</div>';

  var bind = function(id, apply, event){
    document.getElementById(id).addEventListener(event || 'change', function(e){
      var prev = Object.assign({}, st);
      if(apply(e.target) === false) return;
      saveSettings(prev);
      renderSettingsView();
    });
  };
  var intIn = function(el, low, high, dflt){ var v = parseInt(el.value, 10); return isNaN(v) ? dflt : Math.min(high, Math.max(low, v)); };
  var dateList = function(key){ return function(el){
    var res = parseDateList(el.value);
    if(res.bad.length){ toast('Не распознаны даты: ' + res.bad.slice(0, 3).join(', ') + ' — формат ДД.ММ.ГГГГ или диапазон через «–»'); return false; }
    st[key] = res.dates;
  }; };
  bind('setForecastMethod', function(el){ st.forecastMethod = el.value; });
  bind('setRiskBuffer', function(el){ st.riskBufferDays = intIn(el, 0, 365, 0); });
  bind('setConfidence', function(el){ st.rangeConfidence = parseInt(el.value, 10); });
  bind('setBurnrateMethod', function(el){ st.burnrateMethod = el.value; });
  bind('setBurnrateWindow', function(el){ st.burnrateWindowDays = intIn(el, 1, 365, 14); });
  bind('setWorkdays', function(el){ st.workdays = el.checked; });
  bind('setHolidays', dateList('holidays'));
  bind('setExtraWorkdays', dateList('extraWorkdays'));
  bind('setUnits', function(el){ st.units = el.value; });
  bind('setHoursPerDay', function(el){ var v = parseFloat(el.value); st.hoursPerDay = isNaN(v) ? 8 : Math.min(24, Math.max(1, v)); });
  bind('setStaleDays', function(el){ st.staleDays = intIn(el, 1, 365, 10); });
  bind('setScopeThreshold', function(el){ st.scopeThreshold = intIn(el, 1, 1000, 10); });
  bind('setSlipThreshold', function(el){ st.slipThreshold = intIn(el, 1, 365, 7); });
  bind('setChanceThreshold', function(el){ st.chanceThreshold = intIn(el, 1, 99, 50); });
  bind('setInsightWindow', function(el){ st.insightWindowDays = intIn(el, 7, 365, 28); });
  applyReadonlyToView('view-settings');
  selectTab('settings', tabState.settings);
}

var SETTINGS_KEYS = ['forecastMethod', 'riskBufferDays', 'burnrateMethod', 'burnrateWindowDays', 'rangeConfidence', 'workdays',
  'holidays', 'extraWorkdays', 'units', 'hoursPerDay', 'staleDays', 'scopeThreshold', 'slipThreshold', 'chanceThreshold', 'insightWindowDays'];
function saveSettings(prevSettings){
  if(!canEdit()) return;
  var body = {};
  SETTINGS_KEYS.forEach(function(k){ body[k] = STATE.settings[k]; });
  apiPut('/api/projects/'+currentProjectId+'/settings', body).then(function(r){
    /* на месте: экран настроек держит ссылку на этот объект — следующее изменение не должно потеряться */
    if(r && r.settings) Object.assign(STATE.settings, r.settings);
    toast('Настройки сохранены');
  }).catch(function(){
    if(prevSettings){
      STATE.settings = prevSettings;
      if(currentView === 'settings') renderSettingsView();
    }
  });
}

/* ════════════════════════════════════════════════════════════════
   Admin: User management
════════════════════════════════════════════════════════════════ */
var allUsersCache = [];
function field(label, inputHtml){
  var id = /\bid="([^"]+)"/.exec(inputHtml);  /* подпись связана с полем — щелчок по ней и экранный диктор находят поле */
  return '<div class="setting-item"><label class="small"'+(id ? ' for="'+id[1]+'"' : '')+'>'+label+'</label>'+inputHtml+'</div>';
}
function roleOptions(current){
  return ['user','manager','admin'].map(function(r){
    var label = r === 'manager' ? 'Руководитель проекта' : ROLE_LABELS[r];
    return '<option value="'+r+'"'+(current===r?' selected':'')+'>'+label+'</option>';
  }).join('');
}
function roleBadge(role){ return '<span class="role-badge '+escapeAttr(role)+'">'+(ROLE_LABELS[role] || escapeHtml(role))+'</span>'; }

function loadUsers(){
  return api('/api/users').then(function(users){ allUsersCache = users; renderUsersTable(); });
}

function renderUsersTable(){
  var table = document.getElementById('usersTable');
  var search = document.getElementById('userSearch').value.toLowerCase().trim();
  var filtered = search ? allUsersCache.filter(function(u){
    return (u.username||'').toLowerCase().indexOf(search)!==-1 || (u.displayName||'').toLowerCase().indexOf(search)!==-1;
  }) : allUsersCache;
  filtered = applyTableSort('usersTable', filtered);
  var html = '<thead><tr><th class="sortable" data-sort="username">Логин</th><th class="sortable" data-sort="displayName">Имя</th><th class="sortable" data-sort="email">Email</th><th class="sortable" data-sort="position">Должность</th><th class="sortable" data-sort="role">Роль</th><th></th></tr></thead><tbody>';
  if(!filtered.length){
    html += '<tr><td colspan="6" class="empty-cell">Ничего не найдено</td></tr>';
  }
  filtered.forEach(function(u){
    html += '<tr class="clickable'+(u.id === selectedAdminUser ? ' selected' : '')+'" data-uid="'+u.id+'"><td>'+escapeHtml(u.username)+'</td><td>'+escapeHtml(u.displayName||'')+'</td><td>'+escapeHtml(u.email||'')+'</td><td>'+escapeHtml(u.position||'')+'</td><td class="nowrap">'+roleBadge(u.role)+'</td><td class="row-actions">'+actionBtn('edit','Редактировать',ICON_EDIT)+actionBtn('delete','Удалить',ICON_DELETE)+'</td></tr>';
  });
  html += '</tbody>';
  table.innerHTML = html;
  initTableSort(table, renderUsersTable);

  table.querySelectorAll('tr[data-uid]').forEach(function(tr){
    tr.addEventListener('click', function(){
      selectedAdminUser = parseInt(tr.dataset.uid, 10);
      renderUsersTable();
      renderUserProjectsMgmt();
    });
  });
  table.querySelectorAll('[data-action="delete"]').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.stopPropagation();
      var uid = parseInt(btn.closest('tr').dataset.uid, 10);
      askConfirm('Удалить пользователя?', 'Пользователь и все его назначения будут удалены.', function(){
        apiDelete('/api/users/'+uid).then(function(){
          if(selectedAdminUser === uid) selectedAdminUser = null;
          loadUsers();
          renderUserProjectsMgmt();
          toast('Пользователь удалён');
        });
      });
    });
  });
  table.querySelectorAll('[data-action="edit"]').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.stopPropagation();
      showEditUserModal(parseInt(btn.closest('tr').dataset.uid, 10));
    });
  });
}

function showEditUserModal(uid){
  var u = allUsersCache.find(function(x){ return x.id === uid; });
  if(!u) return;
  openModal({
    title: 'Редактирование пользователя', okLabel: 'Сохранить', wide: true,
    html: '<div class="form-grid">'+
      field('Логин', '<input type="text" id="editUsername" value="'+escapeAttr(u.username||'')+'">')+
      field('Отображаемое имя', '<input type="text" id="editDisplayName" value="'+escapeAttr(u.displayName||'')+'">')+
      field('Email', '<input type="email" id="editEmail" value="'+escapeAttr(u.email||'')+'">')+
      field('Должность', '<input type="text" id="editPosition" value="'+escapeAttr(u.position||'')+'">')+
      field('Роль', '<select id="editRole">'+roleOptions(u.role)+'</select>')+
      field('Новый пароль', '<input type="password" id="editPassword" placeholder="Не менять" autocomplete="new-password">')+
      '</div>',
    onOk: function(){
      var data = {
        username: document.getElementById('editUsername').value.trim(),
        displayName: document.getElementById('editDisplayName').value,
        email: document.getElementById('editEmail').value,
        position: document.getElementById('editPosition').value,
        role: document.getElementById('editRole').value
      };
      if(!data.username){ toast('Укажите логин'); return false; }
      var pw = document.getElementById('editPassword').value;
      if(pw) data.password = pw;
      apiPut('/api/users/'+uid, data).then(function(){
        toast('Пользователь обновлён');
        if(currentUser && currentUser.id === uid){
          Object.assign(currentUser, {username:data.username, displayName:data.displayName, email:data.email, position:data.position, role:data.role});
          updateRoleUI();
        }
        loadUsers();
        renderUserProjectsMgmt();
      });
    }
  });
}

function renderUserProjectsMgmt(){
  var container = document.getElementById('userProjectsMgmt');
  if(!selectedAdminUser){
    container.innerHTML = '<p class="muted-text">Выберите пользователя в таблице, чтобы настроить ему видимость проектов.</p>';
    return;
  }
  var user = allUsersCache.find(function(u){ return u.id === selectedAdminUser; });
  if(user && user.role === 'admin'){
    container.innerHTML = '<p class="muted-text">«'+escapeHtml(user.displayName || user.username)+'» — администратор и видит все проекты с полными правами.</p>';
    return;
  }
  Promise.all([
    api('/api/users/'+selectedAdminUser+'/projects'),
    api('/api/projects')
  ]).then(function(results){
    var assignedMap = {};
    results[0].forEach(function(a){ assignedMap[a.projectId] = a.role; });
    var html = user ? '<p class="muted-text">Пользователь: <b>'+escapeHtml(user.displayName || user.username)+'</b></p>' : '';
    if(!results[1].length) html += '<p class="muted-text">Проектов пока нет.</p>';
    results[1].forEach(function(p){
      var role = assignedMap[p.id] || null;
      html += '<div class="assign-row">'+
        '<span class="assign-name">'+escapeHtml(p.name)+'</span>'+
        '<select data-pid="'+p.id+'"'+(role?'':' disabled')+'>'+
        '<option value="user"'+(role==='user'?' selected':'')+'>Пользователь (только просмотр)</option>'+
        '<option value="manager"'+(role==='manager'?' selected':'')+'>Руководитель (редактирование)</option>'+
        '</select>'+
        '<button class="btn btn-sm '+(role?'btn-danger':'btn-primary')+'" data-toggle="'+p.id+'" data-assigned="'+(role?1:0)+'">'+(role?'Убрать':'Назначить')+'</button>'+
        '</div>';
    });
    container.innerHTML = html;

    container.querySelectorAll('select[data-pid]').forEach(function(sel){
      sel.addEventListener('change', function(){
        apiPost('/api/users/'+selectedAdminUser+'/projects', {projectId:parseInt(sel.dataset.pid,10), role:sel.value}).then(function(){
          toast('Доступ обновлён');
          renderUserProjectsMgmt();
        });
      });
    });
    container.querySelectorAll('[data-toggle]').forEach(function(btn){
      btn.addEventListener('click', function(){
        var pid = parseInt(btn.dataset.toggle, 10);
        var req = btn.dataset.assigned === '1'
          ? apiDelete('/api/users/'+selectedAdminUser+'/projects/'+pid)
          : apiPost('/api/users/'+selectedAdminUser+'/projects', {projectId:pid, role:'user'});
        req.then(function(){
          toast(btn.dataset.assigned === '1' ? 'Доступ удалён' : 'Доступ добавлен');
          renderUserProjectsMgmt();
        });
      });
    });
  });
}

function projectFormHtml(p){
  p = p || {};
  return '<div class="form-stack">'+
    field('Название', '<input type="text" id="projectName" value="'+escapeAttr(p.name||'')+'" placeholder="Введите название проекта">')+
    field('Описание', '<textarea id="projectDesc" rows="3" placeholder="Описание проекта (необязательно)">'+escapeHtml(p.description||'')+'</textarea>')+
    '</div>';
}
function openProjectModal(p){
  openModal({
    title: p ? 'Редактирование проекта' : 'Новый проект', okLabel: p ? 'Сохранить' : 'Создать',
    html: projectFormHtml(p),
    onOk: function(){
      var data = {name: document.getElementById('projectName').value.trim(), description: document.getElementById('projectDesc').value};
      if(!data.name){ toast('Введите название проекта'); return false; }
      (p ? apiPut('/api/projects/'+p.id, data) : apiPost('/api/projects', data)).then(function(){
        toast(p ? 'Проект обновлён' : 'Проект добавлен');
        loadProjects();
        renderProjectsMgmtTable();
      });
    }
  });
}

function renderProjectsMgmtTable(){
  api('/api/projects').then(function(projs){
    var table = document.getElementById('projectsMgmtTable');
    var html = '<thead><tr><th class="sortable" data-sort="name">Название</th><th class="sortable" data-sort="description">Описание</th><th></th></tr></thead><tbody>';
    if(!projs.length) html += '<tr><td colspan="3" class="empty-cell">Проектов пока нет.</td></tr>';
    applyTableSort('projectsMgmtTable', projs).forEach(function(p){
      html += '<tr data-pid="'+p.id+'"><td>'+escapeHtml(p.name)+'</td><td>'+escapeHtml(p.description||'')+'</td><td class="row-actions">'+actionBtn('edit','Редактировать',ICON_EDIT)+actionBtn('delete','Удалить',ICON_DELETE)+'</td></tr>';
    });
    html += '</tbody>';
    table.innerHTML = html;
    initTableSort(table, renderProjectsMgmtTable);

    var projectOf = function(btn){ var pid = parseInt(btn.closest('tr').dataset.pid, 10); return projs.find(function(x){ return x.id === pid; }); };
    table.querySelectorAll('[data-action="delete"]').forEach(function(btn){
      btn.addEventListener('click', function(){
        var p = projectOf(btn);
        askConfirm('Удалить проект «'+p.name+'»?', 'Проект и все его данные (эпики, замеры, настройки) будут удалены безвозвратно.', function(){
          apiDelete('/api/projects/'+p.id).then(function(){
            toast('Проект удалён');
            loadProjects();
            renderProjectsMgmtTable();
          });
        });
      });
    });
    table.querySelectorAll('[data-action="edit"]').forEach(function(btn){
      btn.addEventListener('click', function(){ openProjectModal(projectOf(btn)); });
    });
  });
}

/* ════════════════════════════════════════════════════════════════
   Admin view render
════════════════════════════════════════════════════════════════ */
function renderUsersView(){
  loadUsers().then(renderUserProjectsMgmt);
}

/* ════════════════════════════════════════════════════════════════
   Role-based UI
════════════════════════════════════════════════════════════════ */
function canEdit(){ return currentProjectRole === 'admin' || currentProjectRole === 'manager'; }
function isGlobalAdmin(){ return currentUser && currentUser.role === 'admin'; }

/* Режим просмотра: поля редактирования блокируются, фильтры (.filter-input и быстрые фильтры) остаются доступны */
function applyReadonlyToView(viewId){
  var view = document.getElementById(viewId);
  if(!view) return;
  var readonly = !canEdit();
  view.classList.toggle('readonly-overlay', readonly);
  if(!readonly) return;
  view.querySelectorAll('input, select, textarea').forEach(function(el){
    if(el.closest('.quick-filter') || el.classList.contains('filter-input')) return;
    if(el.type === 'text' || el.type === 'number') el.setAttribute('readonly', true);
    else el.setAttribute('disabled', true);
  });
}

function userAvatarHtml(user, cls){
  if(user.photo) return '<img src="'+escapeAttr(user.photo)+'" class="'+cls+' has-photo" alt="">';
  return '<div class="'+cls+'">'+escapeHtml((user.displayName || user.username || '?').charAt(0).toUpperCase())+'</div>';
}

function updateRoleUI(){
  var admin = isGlobalAdmin();
  document.getElementById('readonlyBanner').style.display = (!canEdit() && currentProjectId) ? '' : 'none';
  document.getElementById('adminNav').style.display = admin ? '' : 'none';
  ['navUsers','navProjects','navAudit','navIntegrations','navCalendar','navHealth','btnExport'].forEach(function(id){ document.getElementById(id).style.display = admin ? '' : 'none'; });
  document.getElementById('navSettings').style.display = canEdit() ? '' : 'none';
  document.getElementById('navJira').style.display = canEdit() ? '' : 'none';
  document.getElementById('navAutofill').style.display = canEdit() ? '' : 'none';
  document.getElementById('sectionsNav').style.display = currentProjectId ? '' : 'none';
  document.getElementById('defaultPwWarning').hidden = !(currentUser && currentUser.defaultPassword);
  if(currentUser){
    document.getElementById('userInfo').innerHTML = userAvatarHtml(currentUser, 'avatar')+
      '<div class="user-info-text"><span class="user-info-name">'+escapeHtml(currentUser.displayName||currentUser.username)+'</span>'+roleBadge(admin || !currentProjectRole ? currentUser.role : currentProjectRole)+'</div>';  /* роль в открытом проекте */
  }
}

/* ════════════════════════════════════════════════════════════════
   Navigation
════════════════════════════════════════════════════════════════ */
var navItems = document.querySelectorAll('.nav-item[data-view]');
var pageTitle = document.getElementById('pageTitle');
var pageSub = document.getElementById('pageSub');
var ADMIN_VIEWS = ['users', 'projects', 'audit', 'integrations', 'calendar', 'health'];
var GLOBAL_VIEWS = ADMIN_VIEWS.concat(['portfolio', 'guide']);  /* не относятся к выбранному проекту */
var PDF_VIEWS = ['dashboard', 'stages', 'data'];
var titles = {
  portfolio: ['Портфель проектов', 'Сводка по всем доступным проектам'],
  dashboard: ['Дашборд сгорания', 'Свод по эпикам и этапам'],
  stages: ['Свод по этапам', 'Агрегированное сгорание по укрупнённым этапам'],
  data: ['Данные по датам', 'Внесение и редактирование замеров'],
  services: ['Эпики (блоки задач)', 'Список эпиков проекта и целевых дат'],
  settings: ['Настройки проекта', 'Прогноз, календарь, единицы и пороги выводов'],
  jira: ['Jira и Confluence', 'Кнопки среза, прогноз в задачах Jira, отчёт в Confluence'],
  autofill: ['Автозаполнение', 'Срез из Jira по расписанию'],
  notify: ['Уведомления', 'Дайджест и оповещения по проекту'],
  integrations: ['Интеграции', 'Jira, почта, Telegram-бот и журнал отправок'],
  users: ['Управление пользователями', 'Пользователи, роли и доступ к проектам'],
  projects: ['Управление проектами', 'Создание, редактирование и удаление проектов'],
  audit: ['Реестр изменений', 'Журнал всех изменений с фильтрами'],
  calendar: ['Производственный календарь', 'Праздники и рабочие выходные для расчёта в рабочих днях'],
  health: ['Состояние системы', 'Работа планировщика, Jira, автозаполнения и рассылок; оповещения администратору'],
  guide: ['Руководство', 'Как работать с Burndown']
};

function projectTitleSuffix(){
  var p = projects.find(function(x){ return x.id === currentProjectId; });
  return p ? ' · ' + p.name : '';
}

function navigate(view){
  var isAdminView = ADMIN_VIEWS.indexOf(view) !== -1;
  if(isAdminView && !isGlobalAdmin()) view = 'dashboard';
  if(['settings', 'jira', 'autofill'].indexOf(view) !== -1 && !canEdit()) view = 'dashboard';
  if(GLOBAL_VIEWS.indexOf(view) === -1 && !currentProjectId && isGlobalAdmin()) view = 'projects';
  currentView = view;
  navItems.forEach(function(n){ n.classList.toggle('active', n.dataset.view === view); });
  var viewId = (!currentProjectId && GLOBAL_VIEWS.indexOf(view) === -1) ? 'view-empty' : 'view-'+view;
  document.querySelectorAll('.view').forEach(function(v){ v.classList.toggle('active', v.id === viewId); });
  if(titles[view]){
    pageTitle.textContent = titles[view][0];
    pageSub.textContent = titles[view][1] + (GLOBAL_VIEWS.indexOf(view) === -1 ? projectTitleSuffix() : '');
  }
  /* эпик «Данных по датам» из адреса (F5, «Назад» в другой проект) — до отрисовки раздела */
  if(pendingRoute && pendingRoute.view === view && view === 'data' && pendingRoute.epic && serviceById(pendingRoute.epic)){
    currentDataServiceId = pendingRoute.epic;
  }
  var renderers = {dashboard:renderDashboard, stages:renderStagesView, data:renderDataView, services:renderServicesView,
    settings:renderSettingsView, jira:renderJiraView, autofill:renderAutofillView, notify:renderNotifyView,
    integrations:renderIntegrationsView, users:renderUsersView, projects:renderProjectsMgmtTable, audit:function(){ renderAuditTable(); },
    portfolio:renderPortfolioView, calendar:renderCalendarView, health:renderHealthView, guide:renderGuideView};
  if(renderers[view] && (currentProjectId || GLOBAL_VIEWS.indexOf(view) !== -1)) renderers[view]();
  if(tabBar(view)) selectTab(view, tabState[view]);
  var reportView = PDF_VIEWS.indexOf(view) !== -1 && currentProjectId;
  document.getElementById('btnPdf').style.display = reportView ? '' : 'none';
  document.getElementById('btnPrint').style.display = (reportView && view !== 'data') ? '' : 'none';
  if(window.matchMedia('(max-width: 900px)').matches) setSidebarCollapsed(true);
  applyPendingRoute();
  syncRoute(true);
}

/* ── Адрес страницы: раздел, проект и эпик в «#/раздел?project=…&epic=…» — после F5 открывается та же страница
   (и прокрутка), «Назад» и «Вперёд» браузера переключают разделы ── */
var pendingRoute = null, routeReplaceNext = true;
var ROUTE_SCROLL_KEY = 'burndown-scroll';
function parseRoute(){
  var m = (window.location.hash || '').match(/^#\/([a-z]+)(?:\?(.*))?$/);
  if(!m || !(titles[m[1]] || m[1] === 'guide')) return null;
  var q = new URLSearchParams(m[2] || '');
  var num = function(k){ var n = parseInt(q.get(k), 10); return n > 0 ? n : null; };
  return {view: m[1], project: num('project'), epic: num('epic'), doc: q.get('doc'), section: q.get('s'), tab: q.get('tab')};
}
function routeEpic(){
  if(currentView === 'dashboard') return parseInt(document.getElementById('chartServiceSelect').value, 10) || null;
  if(currentView === 'data') return currentDataServiceId;
  return null;
}
function routeHash(){
  var q = [];
  if(currentProjectId) q.push('project=' + currentProjectId);  /* и в общих разделах — выбранный проект остаётся тем же */
  var epic = routeEpic();
  if(epic) q.push('epic=' + epic);
  var tabs = visibleTabs(currentView);
  if(tabs.length && tabState[currentView] && tabState[currentView] !== tabs[0]) q.push('tab=' + encodeURIComponent(tabState[currentView]));
  if(currentView === 'guide') q = q.concat(guideRouteParams());
  return '#/' + currentView + (q.length ? '?' + q.join('&') : '');
}
/* push — новая запись в истории браузера (переход пользователя); иначе адрес заменяется (выбор эпика, первый показ) */
function syncRoute(push){
  if(!currentUser) return;
  var hash = routeHash();
  var replace = routeReplaceNext || !push;
  routeReplaceNext = false;
  if(window.location.hash === hash) return;
  try { window.history[replace ? 'replaceState' : 'pushState'](null, '', window.location.pathname + window.location.search + hash); } catch(e){}
}
/* после загрузки раздела из адреса: эпик и прокрутка, как были до обновления страницы */
function applyPendingRoute(){
  var r = pendingRoute;
  if(!r) return;
  pendingRoute = null;
  if(r.view !== currentView){ restoreScroll(true); return; }
  if(r.epic && currentView === 'dashboard'){
    var sel = document.getElementById('chartServiceSelect');
    if(Array.prototype.some.call(sel.options, function(o){ return o.value === String(r.epic); }) && sel.value !== String(r.epic)){
      sel.value = String(r.epic);
      sel.dispatchEvent(new Event('change'));
    }
  }
  restoreScroll(currentView === 'guide' && r.section);  /* в руководстве — переход к разделу из адреса */
}
function restoreScroll(forgetOnly){
  var saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(ROUTE_SCROLL_KEY) || 'null'); sessionStorage.removeItem(ROUTE_SCROLL_KEY); } catch(e){}
  if(forgetOnly || !saved || saved.hash !== window.location.hash || !(saved.y > 0)) return;
  /* графики и выводы догружаются — прокручиваем, пока страница не станет достаточно длинной, если читатель не начал сам */
  var stop = false, tries = 0;
  var cancel = function(){ stop = true; };
  ['wheel', 'touchstart', 'keydown', 'mousedown'].forEach(function(ev){ window.addEventListener(ev, cancel, {once: true, passive: true}); });
  (function attempt(){
    if(stop) return;
    window.scrollTo({top: saved.y, behavior: 'instant'});
    if(Math.abs(window.scrollY - saved.y) > 2 && ++tries < 40) setTimeout(attempt, 150);  /* до 6 с */
  })();
}
window.addEventListener('pagehide', function(){
  try { sessionStorage.setItem(ROUTE_SCROLL_KEY, JSON.stringify({hash: window.location.hash, y: window.scrollY})); } catch(e){}
});
window.addEventListener('popstate', function(){
  var r = parseRoute();
  if(!r || !currentUser) return;
  routeReplaceNext = true;
  if(r.tab || tabBar(r.view)) tabState[r.view] = r.tab || null;
  if(r.project && r.project !== currentProjectId && projects.some(function(p){ return p.id === r.project; })){
    pendingRoute = r;
    currentView = r.view;
    switchProject(r.project);
  } else {
    pendingRoute = r;
    if(r.view === 'data' && r.epic && serviceById(r.epic)) currentDataServiceId = r.epic;
    navigate(r.view);
  }
});

navItems.forEach(function(n){
  n.addEventListener('click', function(e){
    e.preventDefault();
    navigate(n.dataset.view);
  });
});

/* ════════════════════════════════════════════════════════════════
   Онлайн-руководство: тот же документ, что в PDF и DOCX, — с оглавлением, поиском по тексту разделов,
   переходом по разделам (раздел — в адресе страницы) и увеличением иллюстраций
════════════════════════════════════════════════════════════════ */
var guideState = {doc: 'user-guide', data: {}, loading: {}, section: null, pinned: null, sectionText: {}};
var GUIDE_PIN_MS = 4000;
function guideRouteParams(){
  var q = [];
  if(guideState.doc && guideState.doc !== 'user-guide') q.push('doc=' + encodeURIComponent(guideState.doc));
  if(guideState.section) q.push('s=' + encodeURIComponent(guideState.section));
  return q;
}
function renderGuideView(){
  var r = pendingRoute && pendingRoute.view === 'guide' ? pendingRoute : null;
  if(r){ guideState.doc = r.doc || 'user-guide'; guideState.section = r.section || null; }  /* адрес — сразу с разделом */
  loadGuide(guideState.doc, r ? r.section : null);
}
function loadGuide(doc, section){
  doc = doc || 'user-guide';
  var got = guideState.data[doc] ? Promise.resolve(guideState.data[doc]) :
    (guideState.loading[doc] || (guideState.loading[doc] = api('/api/guide/' + encodeURIComponent(doc)).then(function(d){
      guideState.data[doc] = d;
      return d;
    }).then(function(d){ delete guideState.loading[doc]; return d; }, function(err){ delete guideState.loading[doc]; throw err; })));
  return got.then(function(d){
    if(currentView === 'guide') showGuide(d, section);
  }).catch(function(){
    if(doc !== 'user-guide') return loadGuide('user-guide');
    document.getElementById('guideContent').innerHTML = '<p class="insight-empty">Онлайн-руководство недоступно — '+
      '<a href="/manuals/user-guide.pdf" target="_blank" rel="noopener">откройте PDF</a>.</p>';
  });
}
function guideHeadings(){
  return Array.prototype.slice.call(document.querySelectorAll('#guideContent h1[id], #guideContent h2[id], #guideContent h3[id]'));
}
function guideHeadingText(h){
  var num = h.querySelector('.header-section-number');
  var text = h.textContent.trim();
  return {num: num ? num.textContent.trim() : '', text: num ? text.slice(num.textContent.length).trim() : text};
}
function showGuide(d, section){
  var content = document.getElementById('guideContent');
  guideState.doc = d.id;
  pageTitle.textContent = d.title;
  pageSub.textContent = 'Burndown — версия ' + d.version + ' · тот же текст, что в PDF и Word';
  document.getElementById('guideDocs').innerHTML = d.guides.length > 1 ? d.guides.map(function(g){
    return '<button type="button" class="btn btn-ghost btn-sm'+(g.id === d.id ? ' active' : '')+'" data-guide="'+escapeAttr(g.id)+'">'+escapeHtml(g.title)+'</button>';
  }).join('') : '';
  var has = function(ext){ return d.formats.indexOf(ext) !== -1; };
  document.getElementById('guideActions').innerHTML =
    (has('pdf') ? '<a class="btn btn-ghost btn-sm" href="/manuals/'+d.id+'.pdf" target="_blank" rel="noopener">Открыть PDF</a>'+
                  '<a class="btn btn-primary btn-sm" href="/manuals/'+d.id+'.pdf?download=1" download>Скачать PDF</a>' : '')+
    (has('docx') ? '<a class="btn btn-ghost btn-sm" href="/manuals/'+d.id+'.docx" download>Скачать Word</a>' : '');
  document.querySelectorAll('#guideDocs [data-guide]').forEach(function(btn){
    btn.addEventListener('click', function(){
      guideState.section = null;
      loadGuide(btn.dataset.guide).then(function(){ window.scrollTo({top: 0, behavior: 'instant'}); syncRoute(true); });
    });
  });
  if(content.dataset.doc !== d.id){
    content.dataset.doc = d.id;
    /* документ приложения (собран pandoc из docs/manuals), иллюстрации подгружаются по мере прокрутки */
    content.innerHTML = d.html.replace(/<img /g, '<img loading="lazy" ');
    var heads = guideHeadings();
    guideState.sectionText = {};
    heads.forEach(function(h){
      var text = '';
      for(var n = h.nextElementSibling; n && !/^H[1-3]$/.test(n.tagName); n = n.nextElementSibling) text += ' ' + n.textContent;
      guideState.sectionText[h.id] = text.toLowerCase();
    });
    document.getElementById('guideTocMobile').innerHTML = heads.filter(function(h){ return h.tagName !== 'H3'; }).map(function(h){
      var t = guideHeadingText(h);
      return '<option value="'+escapeAttr(h.id)+'">'+(h.tagName === 'H2' ? '— ' : '')+escapeHtml((t.num ? t.num + ' ' : '') + t.text)+'</option>';
    }).join('');
    renderGuideToc();
  }
  if(section) guideGo(section, true);
}
function renderGuideToc(){
  var query = document.getElementById('guideSearch').value.trim().toLowerCase();
  var heads = guideHeadings().filter(function(h){
    if(!query) return h.tagName !== 'H3';
    return guideHeadingText(h).text.toLowerCase().indexOf(query) !== -1 || (guideState.sectionText[h.id] || '').indexOf(query) !== -1;
  });
  document.getElementById('guideTocEmpty').hidden = !query || heads.length > 0;
  document.getElementById('guideToc').innerHTML = heads.map(function(h){
    var t = guideHeadingText(h);
    return '<li class="toc-'+h.tagName.toLowerCase()+'"><a href="#" data-section="'+escapeAttr(h.id)+'">'+
      (t.num ? '<span class="toc-num">'+escapeHtml(t.num)+'</span>' : '')+escapeHtml(t.text)+'</a></li>';
  }).join('');
  markGuideToc();
}
/* подсвечивается раздел оглавления, в который входит текущий подраздел */
function markGuideToc(){
  var heads = guideHeadings(), active = null;
  var idx = heads.findIndex(function(h){ return h.id === guideState.section; });
  for(var i = idx; i >= 0; i--){ if(heads[i].tagName !== 'H3'){ active = heads[i].id; break; } }
  document.querySelectorAll('#guideToc a').forEach(function(a){ a.parentNode.classList.toggle('active', a.dataset.section === active); });
  var sel = document.getElementById('guideTocMobile');
  if(active && sel.value !== active) sel.value = active;
}
/* текущий раздел — последний заголовок, поднявшийся в верхнюю треть экрана; раздел запоминается в адресе
   (F5 вернёт к нему). Сразу после перехода к разделу держится он, пока догружаются иллюстрации выше */
var guideScrollQueued = false;
function trackGuideSection(){
  guideScrollQueued = false;
  if(currentView !== 'guide') return;
  var pin = guideState.pinned, current = null;
  if(pin && Date.now() <= pin.until) current = pin.id;
  else {
    var limit = window.innerHeight * 0.3;
    guideHeadings().some(function(h){
      if(h.getBoundingClientRect().top > limit) return true;
      current = h.id;
      return false;
    });
  }
  if(!current || current === guideState.section) return;
  guideState.section = current;
  markGuideToc();
  syncRoute(false);
}
window.addEventListener('scroll', function(){
  if(currentView !== 'guide' || guideScrollQueued) return;
  guideScrollQueued = true;
  window.requestAnimationFrame(trackGuideSection);
}, {passive: true});
function guideGo(id, initial){
  var target = document.getElementById(id);
  if(!target || !document.getElementById('guideContent').contains(target)) return;
  target.scrollIntoView({block: 'start', behavior: 'instant'});
  /* иллюстрации выше раздела догружаются и сдвигают текст — пока читатель не прокрутил сам, раздел держится на месте */
  guideState.pinned = {id: id, until: Date.now() + GUIDE_PIN_MS};
  guideState.section = id;
  markGuideToc();
  syncRoute(!initial);
}
function guideUnpin(){ guideState.pinned = null; }
document.getElementById('guideSearch').addEventListener('input', renderGuideToc);
document.getElementById('guideToc').addEventListener('click', function(e){
  var a = e.target.closest('a[data-section]');
  if(!a) return;
  e.preventDefault();
  guideGo(a.dataset.section);
});
document.getElementById('guideTocMobile').addEventListener('change', function(e){ guideGo(e.target.value); });
document.getElementById('guideContent').addEventListener('click', function(e){
  var link = e.target.closest('a[href^="#"]');
  if(link){ e.preventDefault(); guideGo(decodeURIComponent(link.getAttribute('href').slice(1))); return; }
  var img = e.target.closest('figure img');
  if(img){
    var zoom = document.getElementById('guideZoom');
    zoom.querySelector('img').src = img.getAttribute('src');
    zoom.querySelector('img').alt = img.alt;
    zoom.querySelector('p').textContent = img.alt;
    zoom.setAttribute('aria-label', img.alt);
    zoom.hidden = false;
  }
});
document.getElementById('guideContent').addEventListener('load', function(e){
  var pin = guideState.pinned;
  if(e.target.tagName !== 'IMG' || !pin) return;
  if(Date.now() > pin.until){ guideState.pinned = null; return; }
  var target = document.getElementById(pin.id);
  if(target) target.scrollIntoView({block: 'start', behavior: 'instant'});
}, true);
['wheel', 'touchmove'].forEach(function(ev){ document.getElementById('view-guide').addEventListener(ev, guideUnpin, {passive: true}); });
document.getElementById('guideZoom').addEventListener('click', function(){ this.hidden = true; });
document.addEventListener('keydown', function(e){
  if(currentView !== 'guide') return;
  if(e.key === 'Escape') document.getElementById('guideZoom').hidden = true;
  if(['PageUp', 'PageDown', 'ArrowUp', 'ArrowDown', 'Home', 'End', ' '].indexOf(e.key) !== -1) guideUnpin();
});
document.getElementById('lnkManual').addEventListener('click', function(e){
  e.preventDefault();
  guideState.section = null;
  navigate('guide');
  window.scrollTo({top: 0, behavior: 'instant'});
});

/* ════════════════════════════════════════════════════════════════
   Быстрый поиск в шапке (как в «По Делу»): эпик, этап, раздел или проект — «/» или Ctrl+K, стрелки и Enter
════════════════════════════════════════════════════════════════ */
var quickSearch = document.getElementById('quickSearch');
var quickResults = document.getElementById('quickSearchResults');
var quickItems = [], quickActive = 0;
function quickSearchItems(query){
  var q = query.trim().toLowerCase(), out = [];
  var hit = function(){ for(var i = 0; i < arguments.length; i++){ if(String(arguments[i] || '').toLowerCase().indexOf(q) !== -1) return true; } return false; };
  if(!q) return out;
  STATE.services.forEach(function(sv){
    if(hit(sv.name, sv.etap)) out.push({kind: 'Эпик', title: sv.name, meta: sv.etap + (sv.isDone ? ' · сдан' : ''), run: function(){ showEpicOnDashboard(sv.id); }});
  });
  Object.keys(computeStageGroups()).forEach(function(name){
    if(hit(name)) out.push({kind: 'Этап', title: name, meta: 'Свод по этапам', run: function(){ showStageInsight(name); }});
  });
  Array.prototype.forEach.call(navItems, function(n){
    var title = (n.querySelector('.nav-text') || n).textContent.trim();
    if(n.style.display !== 'none' && !n.closest('[hidden]') && n.closest('.nav-group').style.display !== 'none' && hit(title, (titles[n.dataset.view] || [])[1]))
      out.push({kind: 'Раздел', title: title, meta: (titles[n.dataset.view] || [])[1] || '', run: function(){ navigate(n.dataset.view); }});
  });
  projects.forEach(function(p){
    if(hit(p.name, p.description)) out.push({kind: 'Проект', title: p.name, meta: p.description || '', run: function(){ switchProject(p.id); }});
  });
  if(hit('руководство', 'справка', 'помощь')) out.push({kind: 'Раздел', title: 'Руководство', meta: 'Как работать с Burndown', run: function(){ navigate('guide'); }});
  return out.slice(0, 12);
}
function renderQuickResults(){
  quickItems = quickSearchItems(quickSearch.value);
  quickActive = Math.min(quickActive, Math.max(quickItems.length - 1, 0));
  quickResults.hidden = !quickSearch.value.trim();
  quickResults.innerHTML = quickItems.length ? quickItems.map(function(it, i){
    return '<button type="button" class="search-result'+(i === quickActive ? ' active' : '')+'" role="option" aria-selected="'+(i === quickActive)+'" data-i="'+i+'">'+
      '<span class="search-kind">'+escapeHtml(it.kind)+'</span><span class="search-title">'+escapeHtml(it.title)+'</span>'+
      (it.meta ? '<span class="search-meta">'+escapeHtml(it.meta)+'</span>' : '')+'</button>';
  }).join('') : '<div class="search-empty">Ничего не найдено по «'+escapeHtml(quickSearch.value.trim())+'»</div>';
}
function runQuickResult(i){
  var it = quickItems[i];
  if(!it) return;
  quickSearch.value = '';
  quickResults.hidden = true;
  quickSearch.blur();
  it.run();
}
quickSearch.addEventListener('input', function(){ quickActive = 0; renderQuickResults(); });
quickSearch.addEventListener('focus', function(){ if(quickSearch.value.trim()) renderQuickResults(); });
quickSearch.addEventListener('keydown', function(e){
  if(e.key === 'ArrowDown' || e.key === 'ArrowUp'){
    e.preventDefault();
    if(!quickItems.length) return;
    quickActive = (quickActive + (e.key === 'ArrowDown' ? 1 : quickItems.length - 1)) % quickItems.length;
    renderQuickResults();
  } else if(e.key === 'Enter'){
    e.preventDefault();
    runQuickResult(quickActive);
  } else if(e.key === 'Escape'){
    quickSearch.value = '';
    quickResults.hidden = true;
    quickSearch.blur();
  }
});
quickResults.addEventListener('mousedown', function(e){
  var b = e.target.closest('[data-i]');
  if(!b) return;
  e.preventDefault();  /* до потери фокуса полем */
  runQuickResult(parseInt(b.dataset.i, 10));
});
/* после ухода из поля список прячется с задержкой (щелчок по результату успевает сработать), но не если поле
   уже снова в фокусе — иначе старая задержка скрыла бы новые результаты */
quickSearch.addEventListener('blur', function(){
  setTimeout(function(){ if(document.activeElement !== quickSearch) quickResults.hidden = true; }, 120);
});
document.addEventListener('keydown', function(e){
  var t = e.target, typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
  if(((e.key === '/' && !typing) || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k')) && !confirmModal.classList.contains('show')){
    e.preventDefault();
    if(mobileLayout.matches && appRoot.classList.contains('collapsed')) quickSearch.scrollIntoView({block: 'nearest'});
    quickSearch.focus();
    quickSearch.select();
  }
});

/* ── Сворачиваемые панели дашборда и свода (запоминаются в браузере) ── */
var PANELS_KEY = 'burndown-collapsed-panels';
var collapsedPanels = {};
try { collapsedPanels = JSON.parse(localStorage.getItem(PANELS_KEY) || '{}') || {}; } catch(e){ collapsedPanels = {}; }
document.querySelectorAll('.view .panel[data-block] > .panel-head').forEach(function(head){
  var panel = head.parentNode, view = panel.closest('.view').id.replace('view-', ''), key = view + ':' + panel.dataset.block;
  var title = (head.querySelector('.panel-title') || {}).textContent || '';
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'panel-collapse';
  btn.innerHTML = iconSvg('chevronDown', 14);
  var apply = function(collapsed){
    panel.classList.toggle('panel-collapsed', collapsed);
    btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    btn.title = (collapsed ? 'Развернуть' : 'Свернуть') + ' блок' + (title ? ' «' + title.trim() + '»' : '');
    btn.setAttribute('aria-label', btn.title);
  };
  btn.addEventListener('click', function(){
    var collapsed = !panel.classList.contains('panel-collapsed');
    apply(collapsed);
    if(collapsed) collapsedPanels[key] = true; else delete collapsedPanels[key];
    try { localStorage.setItem(PANELS_KEY, JSON.stringify(collapsedPanels)); } catch(e){}
  });
  head.appendChild(btn);
  apply(!!collapsedPanels[key]);
});

/* шапка раздела закреплена: при прокрутке — тень, чтобы отделить её от содержимого */
var topbarEl = document.querySelector('.topbar');
window.addEventListener('scroll', function(){ topbarEl.classList.toggle('scrolled', window.scrollY > 4); }, {passive: true});

/* ════════════════════════════════════════════════════════════════
   Dashboard render
════════════════════════════════════════════════════════════════ */
function renderStagesView(){
  applyBlockPrefs();
  updateUnitLabels();
  renderStageInsights();
  document.getElementById('stageShowDone').checked = stageShowDone;
  buildStagePanelsSkeleton();
  renderStageKpis();
  renderStageSummaryTable();
  renderStageCharts();
  renderStageBurnRateTable();
  renderStageJira();
}

function updateUnitLabels(){
  document.querySelectorAll('.rate-unit').forEach(function(el){ el.textContent = rateUnit(); });
}
function renderDashboard(){
  applyBlockPrefs();
  updateUnitLabels();
  renderInsightsPanel();
  renderDashboardFilterBar();
  populateServiceSelects();
  updateEpicTargetInput();
  renderKpis();
  renderJiraComposition();
  renderSummaryTable();
  renderEpicCharts();
  renderAllEpicsCharts();
  renderBurnRateTable();
}

/* ════════════════════════════════════════════════════════════════
   Выводы: проект → этапы → эпики (расчёт на сервере, insights.py) и портфель проектов
════════════════════════════════════════════════════════════════ */
var insightsCache = {pid: null, data: null, promise: null, stamp: 0};
function invalidateInsights(){
  insightsCache.pid = null; insightsCache.data = null; insightsCache.promise = null; insightsCache.stamp++;
  if(typeof changesCache !== 'undefined') changesCache = {pid: null, data: null};
}
function loadInsights(){
  var pid = currentProjectId;
  if(!pid) return Promise.resolve(null);
  if(insightsCache.pid === pid && insightsCache.data) return Promise.resolve(insightsCache.data);
  if(insightsCache.pid === pid && insightsCache.promise) return insightsCache.promise;
  var stamp = ++insightsCache.stamp;
  insightsCache.pid = pid;
  insightsCache.promise = api('/api/projects/' + pid + '/insights').then(function(d){
    if(stamp === insightsCache.stamp){ insightsCache.data = d; insightsCache.promise = null; }
    return d;
  }, function(err){ if(stamp === insightsCache.stamp) invalidateInsights(); throw err; });
  return insightsCache.promise;
}
var SEVERITY_LABELS = {danger: 'Не успеваем', risk: 'Риск', info: 'К сведению', ok: 'Хорошо'};
function findingHtml(f, withSubject){
  var subject = '';
  if(withSubject && f.subject && f.ref){
    subject = '<button type="button" class="insight-subject" data-level="' + f.ref.level + '" data-ref="' +
      escapeAttr(f.ref.level === 'epic' ? f.ref.id : f.ref.name) + '">' + escapeHtml(f.subject) + '</button> ';
  } else if(withSubject && f.subject){
    subject = '<b class="insight-subject-plain">' + escapeHtml(f.subject) + '</b> ';  /* эпик уже не в работе — без ссылки */
  }
  return '<li class="insight insight-' + f.severity + '"><span class="insight-dot" aria-hidden="true"></span>' +
    '<span class="sr-only">' + SEVERITY_LABELS[f.severity] + ': </span><span class="insight-text">' + subject + escapeHtml(f.text) + '</span></li>';
}
function findingsList(items, withSubject, empty){
  if(!items || !items.length) return empty ? '<p class="insight-empty">' + empty + '</p>' : '';
  return '<ul class="insight-list">' + items.map(function(f){ return findingHtml(f, withSubject); }).join('') + '</ul>';
}
function seriesMeta(x){
  var parts = ['прогноз ' + (x.forecast ? fmtDate(x.forecast) : (x.tooFar ? '> 3 лет' : (x.noBurn ? 'не сгорает' : '—')))];
  if(x.target) parts.push('цель ' + fmtDate(x.target));
  if(x.probability !== null && x.probability !== undefined) parts.push('шанс ' + Math.round(x.probability * 100) + '%');
  if(isNum(x.remaining)) parts.push('остаток ' + hoursToStr(x.remaining));
  return parts.join(' · ');
}
function insightsHeadHtml(summary){
  return '<div class="insight-head insight-head-' + summary.status + '">' +
    '<div class="insight-headline">' + escapeHtml(summary.headline) + '</div>' +
    (summary.facts.length ? '<div class="insight-facts">' + summary.facts.map(function(f){
      return '<div class="insight-fact"><div class="insight-fact-label">' + escapeHtml(f.label) + '</div><div class="insight-fact-value">' +
        escapeHtml(f.value) + '</div>' + (f.hint ? '<div class="insight-fact-hint">' + escapeHtml(f.hint) + '</div>' : '') + '</div>';
    }).join('') + '</div>' : '') + '</div>';
}
function epicDetailHtml(e){
  return '<details class="insight-epic" data-epic="' + e.id + '"><summary>' + statusBadge(e.status) + ' <span class="insight-epic-name">' +
    escapeHtml(e.name) + '</span><span class="insight-epic-head">' + escapeHtml(e.headline ? e.headline.text : '') + '</span></summary>' +
    '<div class="insight-meta">' + seriesMeta(e) + '</div>' + findingsList(e.findings) + '</details>';
}
function stageDetailHtml(st, epics, index){
  return '<div class="insight-stage" id="stageInsight-' + index + '"><div class="insight-stage-head">' + statusBadge(st.status) +
    ' <b>' + escapeHtml(st.name) + '</b><span class="insight-meta">' + seriesMeta(st) + '</span></div>' +
    findingsList(st.findings) + '<div class="insight-epics">' + epics.map(epicDetailHtml).join('') + '</div></div>';
}
function stageEpics(d, st){ return d.epics.filter(function(e){ return e.stage === st.name; }); }
function insightsHtml(d){
  var s = d.summary;
  var html = insightsHeadHtml(s) + '<div class="insight-cols"><div><div class="insight-col-title">Главные риски</div>' +
    findingsList(s.top, true, 'Рисков по срокам не видно.') + '</div><div>';
  if(s.wins.length) html += '<div class="insight-col-title">Хорошие новости</div>' + findingsList(s.wins, true);
  html += '<div class="insight-col-title">Данные</div>' + findingsList(s.health, false, 'Замеры свежие, цели заданы, ошибок автозаполнения нет.') + '</div></div>';
  html += '<div class="insight-changes" id="insightChanges"><div class="insight-col-title">Что изменилось за неделю</div>' +
    '<p class="insight-empty">Сравниваем с прошлой неделей…</p></div>';
  if(d.stages.length){
    html += '<details class="insight-details"><summary>Подробно по этапам и эпикам</summary>' +
      d.stages.map(function(st, i){ return stageDetailHtml(st, stageEpics(d, st), i); }).join('') + '</details>';
  }
  return html;
}
/* «Что изменилось за неделю» и лента по неделям: сравнение ежедневных снимков выводов (insight_changes на сервере) */
var changesCache = {pid: null, data: null};
var CHANGES_SHOWN = 6;
function loadInsightChanges(){
  var pid = currentProjectId;
  if(changesCache.pid === pid && changesCache.data) return Promise.resolve(changesCache.data);
  return api('/api/projects/' + pid + '/insights/changes').then(function(d){
    if(pid === currentProjectId) changesCache = {pid: pid, data: d};
    return d;
  });
}
function plural(n, one, few, many){
  n = Math.abs(Math.trunc(n));
  if(n % 10 === 1 && n % 100 !== 11) return one;
  return (n % 10 >= 2 && n % 10 <= 4 && !(n % 100 >= 12 && n % 100 <= 14)) ? few : many;
}
/* первые CHANGES_SHOWN изменений сразу, остальные — под «Ещё N» */
function changesListHtml(items, empty){
  var html = findingsList(items.slice(0, CHANGES_SHOWN), true, empty);
  if(items.length > CHANGES_SHOWN){
    html += '<details class="insight-more"><summary>Ещё ' + (items.length - CHANGES_SHOWN) + '</summary>' +
      findingsList(items.slice(CHANGES_SHOWN), true) + '</details>';
  }
  return html;
}
function changesWeekTitle(w){ return fmtDate(w.from) + ' — ' + fmtDate(w.to); }
function insightChangesHtml(d){
  var weeks = d.weeks || [];
  var html = '<div class="insight-col-title">Что изменилось за неделю' + (weeks.length ? ' <span class="insight-changes-period">' +
    changesWeekTitle(weeks[0]) + '</span>' : '') + '</div>';
  if(!weeks.length) return html + '<p class="insight-empty">Сравнивать пока не с чем: замеры появились меньше недели назад.</p>';
  html += changesListHtml(weeks[0].items, 'Существенных изменений в статусах и прогнозах нет.');
  if(weeks.length > 1){
    html += '<details class="insight-feed"><summary>Лента изменений по неделям</summary>' + weeks.slice(1).map(function(w){
      return '<div class="insight-feed-week"><div class="insight-feed-title">' + changesWeekTitle(w) + '<span class="insight-feed-count">' +
        (w.items.length ? w.items.length + ' ' + plural(w.items.length, 'изменение', 'изменения', 'изменений') : 'без изменений') +
        '</span></div>' + changesListHtml(w.items) + '</div>';
    }).join('') + '</details>';
  }
  return html;
}
function renderInsightChanges(){
  var box = document.getElementById('insightChanges');
  if(!box) return;
  var pid = currentProjectId;
  loadInsightChanges().then(function(d){
    var el = document.getElementById('insightChanges');
    if(!el || pid !== currentProjectId) return;
    el.innerHTML = insightChangesHtml(d);
    bindInsightLinks(el);
  }).catch(function(){
    var el = document.getElementById('insightChanges');
    if(el) el.innerHTML = '<div class="insight-col-title">Что изменилось за неделю</div><p class="insight-empty">Не удалось загрузить изменения.</p>';
  });
}
function bindInsightLinks(root){
  root.querySelectorAll('.insight-subject').forEach(function(btn){
    btn.addEventListener('click', function(){
      if(btn.dataset.level === 'epic') return showEpicOnDashboard(parseInt(btn.dataset.ref, 10));
      showStageInsight(btn.dataset.ref);
    });
  });
}
function showEpicOnDashboard(sid){
  var go = function(){
    var sel = document.getElementById('chartServiceSelect');
    if(!Array.prototype.some.call(sel.options, function(o){ return parseInt(o.value, 10) === sid; })){
      /* эпик скрыт фильтром дашборда — показываем его */
      if(dashboardFilter.epics) dashboardFilter.epics[sid] = true;
      if(dashboardFilter.hideDone && isSvcDone(serviceById(sid))) dashboardFilter.hideDone = false;
      saveViewState();
      renderDashboard();
    }
    sel.value = String(sid);
    sel.dispatchEvent(new Event('change'));
    var panel = document.querySelector('#view-dashboard [data-block="epic"]');
    if(panel && panel.scrollIntoView) panel.scrollIntoView({behavior: 'smooth', block: 'start'});
  };
  if(currentView !== 'dashboard'){ navigate('dashboard'); setTimeout(go, 0); } else go();
}
function showStageInsight(name){
  var go = function(){
    loadInsights().then(function(d){
      var i = d ? d.stages.findIndex(function(st){ return st.name === name; }) : -1;
      var el = document.getElementById('stageInsight-' + i);
      if(el && el.scrollIntoView){ el.scrollIntoView({behavior: 'smooth', block: 'start'}); el.classList.add('insight-flash'); setTimeout(function(){ el.classList.remove('insight-flash'); }, 1500); }
    });
  };
  if(currentView !== 'stages'){ navigate('stages'); setTimeout(go, 0); } else go();
}
function insightsFailed(body){
  body.classList.remove('insight-loading');
  body.innerHTML = '<p class="insight-empty">Не удалось рассчитать выводы — обновите страницу.</p>';
}
/* пока выводы пересчитываются, прежние показаны приглушёнными */
function markInsightsLoading(body){
  if(!(insightsCache.pid === currentProjectId && insightsCache.data)) body.classList.add('insight-loading');
}
function renderInsightsPanel(){
  var body = document.getElementById('insightsBody');
  if(blockHidden('dashboard', 'insights')) return;
  var pid = currentProjectId;
  markInsightsLoading(body);
  loadInsights().then(function(d){
    if(!d || pid !== currentProjectId) return;
    body.classList.remove('insight-loading');
    body.innerHTML = insightsHtml(d);
    bindInsightLinks(body);
    renderInsightChanges();
    body.querySelectorAll('.insight-epic').forEach(function(det){
      det.querySelector('summary').addEventListener('dblclick', function(){ showEpicOnDashboard(parseInt(det.dataset.epic, 10)); });
    });
    renderEpicInsights();
  }).catch(function(){ insightsFailed(body); });
}
/* «Почему такой прогноз» под графиком выбранного эпика */
function renderEpicInsights(){
  var box = document.getElementById('epicInsights');
  var sid = parseInt(document.getElementById('chartServiceSelect').value, 10);
  var d = insightsCache.pid === currentProjectId ? insightsCache.data : null;
  var e = d ? d.epics.find(function(x){ return x.id === sid; }) : null;
  box.hidden = !e;
  if(!e){
    if(!d && sid && !isSvcDone(sid)) loadInsights().then(function(x){ if(x) renderEpicInsights(); }).catch(function(){});
    return;
  }
  box.innerHTML = '<div class="insight-col-title">Почему такой прогноз</div>' + findingsList(e.findings);
}
function renderStageInsights(){
  var body = document.getElementById('stageInsightsBody');
  if(blockHidden('stages', 'stageInsights')) return;
  var pid = currentProjectId;
  markInsightsLoading(body);
  loadInsights().then(function(d){
    if(!d || pid !== currentProjectId) return;
    body.classList.remove('insight-loading');
    body.innerHTML = d.stages.length ? d.stages.map(function(st, i){
      var epics = stageEpics(d, st);
      return '<div class="insight-stage" id="stageInsight-' + i + '"><div class="insight-stage-head">' + statusBadge(st.status) +
        ' <b>' + escapeHtml(st.name) + '</b><span class="insight-meta">' + seriesMeta(st) + '</span></div>' + findingsList(st.findings) +
        '<div class="insight-epic-chips">' + epics.map(function(e){
          return '<button type="button" class="insight-chip insight-chip-' + e.status + '" data-epic="' + e.id + '" title="' +
            escapeAttr(e.headline ? e.headline.text : '') + '">' + escapeHtml(e.name) + '</button>';
        }).join('') + '</div></div>';
    }).join('') : '<p class="insight-empty">В проекте нет несданных эпиков.</p>';
    body.querySelectorAll('.insight-chip').forEach(function(b){
      b.addEventListener('click', function(){ showEpicOnDashboard(parseInt(b.dataset.epic, 10)); });
    });
  }).catch(function(){ insightsFailed(body); });
}

/* ── Портфель проектов ───────────────────────────────────────── */
function portfolioFact(p, label){
  var f = (p.summary.facts || []).find(function(x){ return x.label === label; });
  return f ? f.value : '—';
}
function renderPortfolioView(){
  var body = document.getElementById('portfolioBody');
  api('/api/portfolio').then(function(d){
    if(currentView !== 'portfolio') return;
    var status = d.counts.danger ? 'danger' : (d.counts.risk ? 'risk' : (d.counts.ok ? 'ok' : 'unknown'));
    var html = '<div class="insight-head insight-head-' + status + '"><div class="insight-headline">' + escapeHtml(d.headline) + '</div>' +
      '<div class="insight-facts">' + [['Не успевают', d.counts.danger], ['С рисками', d.counts.risk], ['В порядке', d.counts.ok], ['Нет данных', d.counts.unknown]].map(function(c){
        return '<div class="insight-fact"><div class="insight-fact-label">' + c[0] + '</div><div class="insight-fact-value">' + c[1] + '</div></div>';
      }).join('') + '</div></div>';
    html += '<div class="table-scroll"><table class="data-table portfolio-table"><thead><tr><th>Проект</th><th>Статус</th><th>Главное</th>' +
      '<th>Прогноз завершения</th><th class="num">Остаток</th><th>Первоочередные риски</th></tr></thead><tbody>' +
      d.projects.map(function(p){
        var top = p.summary.top.slice(0, 2).map(function(f){ return '<div class="portfolio-risk insight-' + f.severity + '"><b>' + escapeHtml(f.subject) + ':</b> ' + escapeHtml(f.text) + '</div>'; }).join('');
        return '<tr class="clickable-row" data-project="' + p.id + '" tabindex="0"><td><b>' + escapeHtml(p.name) + '</b><div class="muted-cell">' +
          p.epics + ' ' + (p.epics % 10 === 1 && p.epics % 100 !== 11 ? 'эпик' : (p.epics % 10 >= 2 && p.epics % 10 <= 4 && (p.epics % 100 < 12 || p.epics % 100 > 14) ? 'эпика' : 'эпиков')) +
          '</div></td><td>' + statusBadge(p.summary.status) + '</td><td>' + escapeHtml(p.summary.headline) + '</td><td class="nowrap">' +
          escapeHtml(portfolioFact(p, 'Прогноз завершения')) + '</td><td class="num nowrap">' + escapeHtml(portfolioFact(p, 'Остаток')) + '</td><td>' +
          (top || '<span class="muted-cell">—</span>') + '</td></tr>';
      }).join('') + '</tbody></table></div>';
    body.innerHTML = d.projects.length ? html : '<p class="insight-empty">Нет доступных проектов.</p>';
    body.querySelectorAll('tr[data-project]').forEach(function(tr){
      var open = function(){ switchProject(parseInt(tr.dataset.project, 10)); };
      tr.addEventListener('click', open);
      tr.addEventListener('keydown', function(e){ if(e.key === 'Enter') open(); });
    });
  }).catch(function(){ body.innerHTML = '<p class="insight-empty">Не удалось загрузить портфель.</p>'; });
}
document.getElementById('portfolioRefresh').addEventListener('click', function(){
  document.getElementById('portfolioBody').innerHTML = '<p class="insight-empty">Считаем выводы по проектам…</p>';
  renderPortfolioView();
});

/* ── Вид дашборда и свода: какие блоки показывать (личная настройка) ── */
var VIEW_BLOCKS = {
  dashboard: [['insights', 'Выводы'], ['kpis', 'Ключевые показатели'], ['epic', 'Динамика по выбранному эпику'],
    ['epicExtra', 'Затраченное время и оценка эпика'], ['drift', 'Дрейф прогноза'], ['burnup', 'Объём и выполнение по задачам Jira (эпик)'],
    ['allEpics', 'Графики по всем выбранным эпикам'], ['composition', 'Задачи Jira по эпикам'], ['summary', 'Сводка по всем эпикам'],
    ['burnRate', 'Burn rate по эпикам']],
  stages: [['stageInsights', 'Выводы по этапам'], ['stageKpis', 'Карточки этапов'], ['stageCharts', 'Графики этапов'],
    ['stageSummary', 'Сводка по этапам'], ['stageJira', 'Задачи Jira по этапам'], ['stageBurnup', 'Объём и выполнение по задачам Jira (этап)'],
    ['stageBurnRate', 'Burn rate по этапам']]
};
function hiddenBlocks(view){
  var prefs = (currentUser && currentUser.prefs) || {};
  return ((prefs.hiddenBlocks || {})[view]) || [];
}
function blockHidden(view, key){ return hiddenBlocks(view).indexOf(key) !== -1; }
function applyBlockPrefs(){
  Object.keys(VIEW_BLOCKS).forEach(function(view){
    document.querySelectorAll('#view-' + view + ' [data-block]').forEach(function(el){
      el.classList.toggle('block-off', blockHidden(view, el.dataset.block));
    });
  });
}
function openBlocksModal(view){
  var hidden = hiddenBlocks(view);
  openModal({title: view === 'dashboard' ? 'Вид дашборда' : 'Вид свода по этапам', okLabel: 'Сохранить',
    html: '<p class="bulk-intro">Отметьте блоки, которые нужно показывать. Настройка личная: другие пользователи видят свой вид.</p>' +
      '<div class="blocks-list">' + VIEW_BLOCKS[view].map(function(b){
        return '<label class="checkbox-row"><input type="checkbox" data-block-key="' + b[0] + '"' + (hidden.indexOf(b[0]) === -1 ? ' checked' : '') + '> ' + b[1] + '</label>';
      }).join('') + '</div>',
    onOk: function(){
      var all = {};
      Object.keys(VIEW_BLOCKS).forEach(function(v){ all[v] = hiddenBlocks(v).slice(); });
      all[view] = Array.prototype.filter.call(confirmBody.querySelectorAll('[data-block-key]'), function(cb){ return !cb.checked; })
        .map(function(cb){ return cb.dataset.blockKey; });
      apiPut('/api/profile/prefs', {hiddenBlocks: all}).then(function(r){
        currentUser.prefs = r.prefs;
        closeModal();
        applyBlockPrefs();
        navigate(currentView);
      }).catch(function(){});
    }});
}
document.querySelectorAll('.view-blocks-btn').forEach(function(btn){
  btn.addEventListener('click', function(){ openBlocksModal(btn.dataset.blocks); });
});

/* ── Личный вид проекта: переживает обновление страницы и повторный вход (хранится на сервере у пользователя) ── */
var viewStateTimer = null;
function currentViewState(){
  var off = [];
  if(dashboardFilter.epics) Object.keys(dashboardFilter.epics).forEach(function(id){ if(dashboardFilter.epics[id] === false) off.push(parseInt(id, 10)); });
  return {hideDone: !!dashboardFilter.hideDone, epicsOff: off, stageShowDone: !!stageShowDone,
          burnRange: burnRangeOverride, stageBurnRange: stageBurnRangeOverride};
}
function saveViewState(){
  if(!currentUser || !currentProjectId) return;
  var pid = currentProjectId, state = currentViewState();
  currentUser.prefs = currentUser.prefs || {};
  currentUser.prefs.views = currentUser.prefs.views || {};
  currentUser.prefs.views[pid] = state;
  clearTimeout(viewStateTimer);
  viewStateTimer = setTimeout(function(){
    apiPut('/api/profile/view-state', {projectId: pid, state: state}).catch(function(){});
  }, 400);
}
/* при открытии проекта — его сохранённый вид; эпики, которых нет в проекте, игнорируются */
function restoreViewState(pid){
  var st = ((currentUser && currentUser.prefs && currentUser.prefs.views) || {})[pid] || {};
  dashboardFilter = {hideDone: !!st.hideDone, epics: null};
  (st.epicsOff || []).forEach(function(id){
    if(!dashboardFilter.epics) dashboardFilter.epics = {};
    dashboardFilter.epics[id] = false;
  });
  stageShowDone = !!st.stageShowDone;
  burnRangeOverride = st.burnRange || null;
  stageBurnRangeOverride = st.stageBurnRange || null;
  [['burnFrom', burnRangeOverride, 'from'], ['burnTo', burnRangeOverride, 'to'], ['stageBurnFrom', stageBurnRangeOverride, 'from'],
   ['stageBurnTo', stageBurnRangeOverride, 'to']].forEach(function(x){ document.getElementById(x[0]).value = (x[1] && x[1][x[2]]) || ''; });
  restoreTableState(pid);
}
/* сортировка и фильтры таблиц — в этом браузере, у каждого пользователя и проекта свои */
function tableStateKey(pid){ return 'burndown-tables:' + (currentUser ? currentUser.id : 0) + ':' + pid; }
function restoreTableState(pid){
  var saved = null;
  try { saved = JSON.parse(localStorage.getItem(tableStateKey(pid)) || 'null'); } catch(e){}
  Object.keys(tableSortState).forEach(function(k){ delete tableSortState[k]; });
  Object.keys(tableFilterState).forEach(function(k){ delete tableFilterState[k]; });
  if(saved && saved.sort) Object.assign(tableSortState, saved.sort);
  if(saved && saved.filter) Object.keys(saved.filter).forEach(function(k){
    var f = saved.filter[k] || {};
    tableFilterState[k] = {open: !!f.open, search: f.search || '', values: f.values || {}, sort: f.sort || null};
  });
}
function saveTableState(){
  if(!currentProjectId) return;
  var filter = {};
  Object.keys(tableFilterState).forEach(function(k){ if(k !== 'jiTable') filter[k] = tableFilterState[k]; });
  try { localStorage.setItem(tableStateKey(currentProjectId), JSON.stringify({sort: tableSortState, filter: filter})); } catch(e){}
}

/* ── Быстрые фильтры дашборда и свода ────────────────────────── */
function renderDashboardFilterBar(){
  var box = document.getElementById('dashEpicChecks');
  document.getElementById('dashHideDone').checked = dashboardFilter.hideDone;
  box.innerHTML = STATE.services.map(function(s){
    var checked = !(dashboardFilter.epics && dashboardFilter.epics[s.id] === false);
    return '<label class="epic-check'+(s.isDone?' done':'')+'" title="'+escapeAttr(s.etap+' — '+s.name)+'"><input type="checkbox" data-sid="'+s.id+'"'+(checked?' checked':'')+'><span>'+escapeHtml(s.name)+'</span></label>';
  }).join('') || '<span class="muted-text">Эпиков нет</span>';
  box.querySelectorAll('input[type=checkbox]').forEach(function(inp){
    inp.addEventListener('change', function(){
      if(!dashboardFilter.epics) dashboardFilter.epics = {};
      dashboardFilter.epics[parseInt(inp.dataset.sid, 10)] = inp.checked;
      saveViewState();
      renderDashboard();
    });
  });
}

document.getElementById('dashHideDone').addEventListener('change', function(e){
  dashboardFilter.hideDone = e.target.checked;
  saveViewState();
  renderDashboard();
});
document.getElementById('dashEpicAll').addEventListener('click', function(){
  dashboardFilter.epics = null;
  saveViewState();
  renderDashboard();
});
document.getElementById('dashEpicNone').addEventListener('click', function(){
  dashboardFilter.epics = {};
  STATE.services.forEach(function(s){ dashboardFilter.epics[s.id] = false; });
  saveViewState();
  renderDashboard();
});
document.getElementById('stageShowDone').addEventListener('change', function(e){
  stageShowDone = e.target.checked;
  saveViewState();
  renderStagesView();
});

/* ════════════════════════════════════════════════════════════════
   Event handlers (epic select, target date, add row/service, etc)
════════════════════════════════════════════════════════════════ */
document.getElementById('chartServiceSelect').addEventListener('change', function(){
  updateEpicTargetInput();
  renderEpicCharts();
  renderEpicInsights();
  syncRoute(false);
});
document.getElementById('epicTargetDate').addEventListener('change', function(e){
  var sid = parseInt(document.getElementById('chartServiceSelect').value, 10);
  var svc = serviceById(sid);
  if(!svc || !canEdit()) return;
  var prev = svc.targetDate;
  var refresh = function(){ renderEpicCharts(); renderSummaryTable(); renderKpis(); };
  svc.targetDate = e.target.value;
  saveService(svc).then(function(){
    toast('Целевая дата обновлена');
  }).catch(function(){
    svc.targetDate = prev;
    e.target.value = prev || '';
    refresh();
  });
  refresh();
});
document.getElementById('dataServiceSelect').addEventListener('change', function(e){
  currentDataServiceId = parseInt(e.target.value, 10) || null;
  renderDataView();
  syncRoute(false);
});

function updateEpicTargetInput(){
  var svc = serviceById(parseInt(document.getElementById('chartServiceSelect').value, 10));
  var inp = document.getElementById('epicTargetDate');
  inp.value = svc ? (svc.targetDate || '') : '';
  inp.disabled = !canEdit() || !svc;
}

document.getElementById('btnAddRow').addEventListener('click', function(){
  if(!canEdit()){ toast('Нет прав на редактирование'); return; }
  if(!currentDataServiceId){ toast('Сначала добавьте эпик'); return; }
  var rows = entriesForService(currentDataServiceId);
  /* сегодняшняя дата, если она позже последнего замера; иначе — следующий день после последнего */
  var lastDate = rows.length ? rows[rows.length-1].date : null;
  var today = todayIso();
  var newDate = (!lastDate || today > lastDate) ? today : addDays(lastDate, 1);
  var sid = currentDataServiceId;
  apiPost('/api/services/'+sid+'/entries', {date:newDate, remaining:null, spent:null, estimate:null}).then(function(data){
    STATE.entries.push({id:data.id, service_id:sid, date:newDate, remaining:null, spent:null, estimate:null});
    renderDataView();
    toast('Добавлена новая дата');
  });
});

/* Замер на одну дату сразу по всем эпикам проекта */
function lastValuesBefore(serviceId, date){
  var res = {remaining:null, spent:null, estimate:null};
  entriesForService(serviceId).forEach(function(e){
    if(e.date >= date) return;
    Object.keys(res).forEach(function(f){ if(isNum(e[f])) res[f] = e[f]; });
  });
  return res;
}

/* opts (необязательно): title, intro, services — список эпиков, prefill — {serviceId: {remaining, spent, estimate}}
   (готовые значения, например срез из Jira), notes — {serviceId: {text, warnings[]}} под названием эпика */
function openBulkMeasurement(opts){
  opts = opts || {};
  if(!canEdit()){ toast('Нет прав на редактирование'); return; }
  var services = opts.services || STATE.services.filter(function(s){ return !s.isDone; });
  if(!opts.services && !services.length) services = STATE.services.slice();
  if(!services.length){ toast('Сначала добавьте эпик'); return; }
  var prefill = opts.prefill || null, notes = opts.notes || {};
  var noteHtml = function(sid){
    var n = notes[sid];
    if(!n) return '';
    return '<div class="bulk-row-note">'+escapeHtml(n.text || '')+'</div>'+
      (n.warnings || []).map(function(w){ return '<div class="bulk-row-note warn">⚠ '+escapeHtml(w)+'</div>'; }).join('');
  };
  var fields = ['remaining', 'spent', 'estimate'];
  var FIELD_LABELS = {remaining:'Остаток, ч', spent:'Затрачено, ч', estimate:'Оценка, ч'};
  openModal({
    title: opts.title || 'Замер по всем эпикам', okLabel: 'Сохранить', wide: true,
    html:
      '<div class="bulk-head">'+
        '<label class="small" for="bulkDate">Дата замера</label><input type="date" id="bulkDate" value="'+todayIso()+'">'+
        '<button type="button" class="btn btn-ghost btn-sm" id="bulkFillPrev" title="Заполнить пустые поля значениями предыдущего замера">Подставить прошлые значения</button>'+
      '</div>'+
      (opts.intro ? '<p class="bulk-note bulk-intro">'+opts.intro+'</p>' : '')+
      '<p class="bulk-note">Пустые строки не сохраняются. Если замер на эту дату уже есть, он будет обновлён; пустое поле не меняет уже внесённое значение. Серым показаны значения предыдущего замера'+(opts.services ? '' : '; сданные эпики не выводятся')+'.</p>'+
      '<div class="table-scroll"><table class="data-table compact-table bulk-table" id="bulkTable"><thead><tr><th>Эпик</th><th class="num">Остаток, ч</th><th class="num">Затрачено, ч</th><th class="num">Оценка, ч</th></tr></thead><tbody>'+
      services.map(function(s){
        return '<tr data-sid="'+s.id+'"><td><span class="bulk-etap">'+escapeHtml(s.etap)+'</span> '+escapeHtml(s.name)+noteHtml(s.id)+'</td>'+
          fields.map(function(f){ return '<td data-label="'+FIELD_LABELS[f]+'"><input type="number" step="0.01" min="0" data-field="'+f+'" aria-label="'+FIELD_LABELS[f]+'"></td>'; }).join('')+'</tr>';
      }).join('')+
      '</tbody></table></div>',
    onOk: function(){
      var date = document.getElementById('bulkDate').value;
      if(!date){ toast('Укажите дату замера'); return false; }
      var items = [], invalid = false;
      document.querySelectorAll('#bulkTable tbody tr').forEach(function(tr){
        var item = {serviceId: parseInt(tr.dataset.sid, 10)}, filled = false;
        tr.querySelectorAll('input').forEach(function(inp){
          var v = inp.value === '' ? null : parseFloat(inp.value);
          if(v !== null && (isNaN(v) || v < 0)) invalid = true;
          if(v !== null) filled = true;
          item[inp.dataset.field] = v;
        });
        if(filled) items.push(item);
      });
      if(invalid){ toast('Значения должны быть числами не меньше 0'); return false; }
      if(!items.length){ toast('Заполните хотя бы одно значение'); return false; }
      confirmOk.disabled = true;
      apiPost('/api/projects/'+currentProjectId+'/measurements', {date:date, items:items}).then(function(data){
        data.entries.forEach(function(saved){
          var existing = STATE.entries.find(function(e){ return e.id === saved.id; });
          if(existing) Object.assign(existing, saved); else STATE.entries.push(saved);
        });
        closeModal();
        renderDataView();
        refreshDashboardParts();
        toast('Сохранено замеров: ' + data.entries.length);
      }).catch(function(){}).then(function(){ confirmOk.disabled = false; });
      return false;
    }
  });

  var dateEl = document.getElementById('bulkDate');
  /* при смене даты подставляем уже существующие значения на эту дату и подсказки с прошлого замера;
     готовые значения (prefill) задаются один раз и при смене даты не перезаписываются */
  var fill = function(initial){
    var date = dateEl.value;
    document.querySelectorAll('#bulkTable tbody tr').forEach(function(tr){
      var sid = parseInt(tr.dataset.sid, 10);
      var existing = STATE.entries.find(function(e){ return e.service_id === sid && e.date === date; });
      var prev = lastValuesBefore(sid, date || '9999-12-31');
      var source = prefill ? prefill[sid] : existing;
      tr.querySelectorAll('input').forEach(function(inp){
        var f = inp.dataset.field;
        if(!prefill || initial) inp.value = source && isNum(source[f]) ? source[f] : '';
        inp.placeholder = isNum(prev[f]) ? (Math.round(prev[f]*100)/100).toLocaleString('ru-RU', {maximumFractionDigits:2, useGrouping:false}) : '';
        inp.dataset.prev = isNum(prev[f]) ? prev[f] : '';
      });
    });
  };
  dateEl.addEventListener('change', function(){ fill(false); });
  fill(true);
  document.getElementById('bulkFillPrev').addEventListener('click', function(){
    document.querySelectorAll('#bulkTable input[data-field]').forEach(function(inp){
      if(inp.value === '' && inp.dataset.prev !== '') inp.value = inp.dataset.prev;
    });
  });
  enableEnterNavigation(document.getElementById('bulkTable'));
}
document.getElementById('btnBulkMeasure').addEventListener('click', function(){ openBulkMeasurement(); });
/* ════════════════════════════════════════════════════════════════
   Связь с Jira: подключение, кнопки среза из Structure
════════════════════════════════════════════════════════════════ */
var JIRA_TYPE_TEXTS = {
  server: {login: 'Логин (необязательно)', token: 'Personal Access Token', loginPh: '', tokenPh: 'Вставьте PAT',
           hint: 'PAT создаётся в профиле Jira: «Профиль → Personal Access Tokens» (Jira Server/DC 8.14 и новее).'},
  server_basic: {login: 'Логин (username)', token: 'Пароль', loginPh: 'Ваш логин в Jira', tokenPh: 'Пароль от Jira',
                 hint: '⚠ Basic Auth передаёт пароль в base64 — используйте только по HTTPS.'},
  cloud: {login: 'Email', token: 'API-токен', loginPh: 'you@company.com', tokenPh: 'Вставьте API-токен',
          hint: 'API-токен создаётся на id.atlassian.com → Security → API tokens.'}
};

function loadJiraConfig(){
  STATE.jira = null;
  renderJiraSnapshotButtons();
  if(!canEdit() || !currentProjectId) return Promise.resolve();
  var pid = currentProjectId;
  return api('/api/projects/'+pid+'/jira').then(function(data){
    if(pid !== currentProjectId) return;
    STATE.jira = data;
    renderJiraSnapshotButtons();
    if(currentView === 'jira') renderJiraView();
    if(currentView === 'autofill') renderAutofillView();
  }).catch(function(){});
}

/* Кнопки среза в разделе «Данные по датам» */
function renderJiraSnapshotButtons(){
  var box = document.getElementById('jiraSnapshotButtons');
  var buttons = (canEdit() && STATE.jira && STATE.jira.connected) ? STATE.jira.buttons : [];
  box.innerHTML = buttons.map(function(b){
    return '<button type="button" class="btn btn-ghost btn-sm" data-jira-snapshot="'+b.id+'" title="Забрать срез из Jira'+(b.structureName ? ' (структура «'+escapeAttr(b.structureName)+'»)' : '')+'">⇣ '+escapeHtml(b.name)+'</button>';
  }).join('');
  box.querySelectorAll('[data-jira-snapshot]').forEach(function(btn){
    btn.addEventListener('click', function(){ runJiraSnapshot(parseInt(btn.dataset.jiraSnapshot, 10), btn); });
  });
}

function runJiraSnapshot(bid, btn){
  var label = btn ? btn.textContent : '';
  if(btn){ btn.disabled = true; btn.textContent = 'Загрузка из Jira…'; }
  return apiPost('/api/jira/buttons/'+bid+'/snapshot', {date: todayIso()}).then(function(data){
    var prefill = {}, notes = {}, services = [];
    data.items.forEach(function(it){
      prefill[it.serviceId] = it;
      var count = it.issueCount;
      notes[it.serviceId] = {text: 'Задач Jira учтено: ' + count, warnings: it.warnings};
    });
    /* порядок эпиков — как в проекте */
    STATE.services.forEach(function(s){ if(prefill[s.id]) services.push(s); });
    var b = data.button;
    loadJiraComposition();
    var before = jiraButtonById(b.id);
    replaceJiraButton(b);
    if(b.lastSuccessAt && before && !before.lastSuccessAt)
      toast('Первый срез получен — для кнопки доступно автозаполнение по расписанию');
    openBulkMeasurement({
      title: 'Срез из Jira: ' + b.name,
      services: services, prefill: prefill, notes: notes,
      intro: 'Значения получены из Jira' + (b.structureName ? ' (структура «' + escapeHtml(b.structureName) + '»)' : '') +
        '. Проверьте их и при необходимости поправьте — замер будет сохранён на выбранную дату.'
    });
  }).catch(function(){}).then(function(){
    if(btn){ btn.disabled = false; btn.textContent = label; }
  });
}

function jiraButtonById(id){
  return STATE.jira ? STATE.jira.buttons.find(function(b){ return b.id === id; }) : null;
}
function replaceJiraButton(saved){
  if(!STATE.jira) return;
  STATE.jira.buttons = STATE.jira.buttons.map(function(x){ return x.id === saved.id ? saved : x; });
  if(currentView === 'jira') renderJiraButtonsTable();
  if(currentView === 'autofill') renderAutofillView();
}

/* «Связь с Jira» проекта: подключение общее (его настраивает администратор), здесь — только факт подключения и кнопки среза */
function renderJiraView(){
  var cfg = STATE.jira;
  var connected = !!(cfg && cfg.connected);
  var st = document.getElementById('jiraProjectStatus');
  st.className = 'jira-status' + (connected ? ' ok' : ' error');
  st.textContent = connected ? '✓ Подключение к Jira настроено'
    : 'Подключение к Jira не настроено' + (isGlobalAdmin() ? ' — настройте его в разделе «Интеграции».' : ' — обратитесь к администратору.');
  document.getElementById('jiraOpenIntegrations').hidden = !isGlobalAdmin();
  document.getElementById('jiraWizardCallout').hidden = !connected;
  renderWritebackPanel();
  renderConfluencePanel();
  if(!cfg){ document.getElementById('jiraButtonsTable').innerHTML = ''; return; }
  renderJiraButtonsTable();
}

/* ── Прогноз в Jira: какие показатели в какие поля задач эпиков записывать ── */
var wbState = {data: null, fields: null};
function renderWritebackPanel(){
  var pid = currentProjectId;
  api('/api/projects/' + pid + '/jira/writeback').then(function(d){
    if(pid !== currentProjectId) return;
    wbState.data = d;
    fillWriteback();
  }).catch(function(){});
}
function wbKind(kind){ return wbState.data.kinds.find(function(k){ return k.kind === kind; }); }
function wbFieldOptions(kind){
  var saved = wbState.data.fields[kind], types = wbKind(kind).types;
  var list = (wbState.fields || []).filter(function(f){ return types.indexOf(f.type) !== -1; });
  if(saved && !list.some(function(f){ return f.id === saved.id; })) list.unshift(saved);
  return '<option value="">— не записывать —</option>' + list.map(function(f){
    return '<option value="' + escapeAttr(f.id) + '" data-type="' + escapeAttr(f.type) + '" data-name="' + escapeAttr(f.name) + '"' +
      (saved && saved.id === f.id ? ' selected' : '') + '>' + escapeHtml(f.name) + ' (' + escapeHtml(f.id) + ')</option>';
  }).join('');
}
function wbValueText(kind, v){
  if(v === null || v === undefined || v === '') return '<span class="muted-cell">пусто</span>';
  if(kind === 'forecast' && /^\d{4}-\d{2}-\d{2}$/.test(v)) return fmtDate(v);
  if(kind === 'chance' && typeof v === 'number') return v + '%';
  return escapeHtml(String(v));
}
function fillWriteback(){
  var d = wbState.data;
  var blocked = !d.allowed || !d.connected;
  var st = document.getElementById('wbStatus');
  st.className = 'jira-status' + (blocked ? '' : (d.lastStatus === 'error' ? ' error' : (d.lastStatus === 'ok' ? ' ok' : '')));
  st.textContent = !d.connected ? 'Подключение к Jira не настроено.' :
    (!d.allowed ? 'Запись в Jira не разрешена администратором' + (isGlobalAdmin() ? ' — разрешите её в разделе «Интеграции».' : '.') :
      (d.lastAt ? 'Последняя запись: ' + fmtDateTime(d.lastAt) + ' — ' + (d.lastMessage || '') : 'В Jira ещё ничего не записывалось.'));
  var enabled = document.getElementById('wbEnabled');
  enabled.checked = d.enabled;
  enabled.disabled = blocked;
  document.getElementById('wbFields').innerHTML = d.kinds.map(function(k){
    return '<div class="setting-item"><label class="small" for="wbField-' + k.kind + '">' + escapeHtml(k.label) + '</label>' +
      '<select id="wbField-' + k.kind + '" data-kind="' + k.kind + '"' + (blocked ? ' disabled' : '') + '>' + wbFieldOptions(k.kind) + '</select></div>';
  }).join('');
  ['wbLoadFields', 'wbRun', 'wbSave'].forEach(function(id){ document.getElementById(id).disabled = blocked; });
  var kinds = d.kinds.filter(function(k){ return d.fields[k.kind]; });
  var table = document.getElementById('wbTable');
  if(!kinds.length){
    table.innerHTML = '<tbody><tr><td class="muted-cell">Выберите поля Jira — здесь появится, что и в какие задачи будет записано.</td></tr></tbody>';
    return;
  }
  table.innerHTML = '<thead><tr><th>Эпик</th><th>Задача Jira</th>' + kinds.map(function(k){ return '<th>' + escapeHtml(k.label) + '</th>'; }).join('') +
    '<th>В Jira</th></tr></thead><tbody>' + d.epics.map(function(e){
      if(!e.issue) return '<tr><td>' + escapeHtml(e.epic) + '</td><td colspan="' + (kinds.length + 2) + '" class="muted-cell">нет задачи — эпик не привязан к кнопке среза</td></tr>';
      return '<tr><td>' + escapeHtml(e.epic) + '</td><td class="mono">' + escapeHtml(e.issue) + '</td>' + kinds.map(function(k){
        return '<td>' + wbValueText(k.kind, e.values[k.kind]) + '</td>';
      }).join('') + '<td>' + (e.changed ? '<span class="prob-mid">будет записано</span>' : '<span class="prob-high">актуально</span>') + '</td></tr>';
    }).join('') + '</tbody>';
}
function writebackBody(){
  var fields = {};
  document.querySelectorAll('#wbFields select').forEach(function(sel){
    var opt = sel.options[sel.selectedIndex];
    if(sel.value) fields[sel.dataset.kind] = {id: sel.value, type: opt.dataset.type, name: opt.dataset.name};
  });
  return {enabled: document.getElementById('wbEnabled').checked, fields: fields};
}
function saveWriteback(message){
  return apiPut('/api/projects/' + currentProjectId + '/jira/writeback', writebackBody()).then(function(d){
    wbState.data = d;
    fillWriteback();
    if(message) toast(message);
    return d;
  });
}
document.getElementById('wbLoadFields').addEventListener('click', function(){
  var btn = this, current = writebackBody();
  btn.disabled = true;
  api('/api/projects/' + currentProjectId + '/jira/writable-fields').then(function(list){
    wbState.fields = list;
    wbState.data.fields = current.fields;  /* выбор, ещё не сохранённый, не теряется */
    fillWriteback();
    toast(list.length ? 'Поля Jira загружены: ' + list.length : 'В Jira нет подходящих пользовательских полей');
  }).catch(function(){}).then(function(){ btn.disabled = false; });
});
document.getElementById('wbSave').addEventListener('click', function(){ saveWriteback('Настройки записи в Jira сохранены').catch(function(){}); });
document.getElementById('wbEnabled').addEventListener('change', function(e){
  saveWriteback(e.target.checked ? 'Автоматическая запись в Jira включена' : 'Автоматическая запись в Jira выключена').catch(function(){
    e.target.checked = !e.target.checked;
  });
});
document.getElementById('wbRun').addEventListener('click', function(){
  var btn = this;
  btn.disabled = true;
  saveWriteback().then(function(){
    return apiPost('/api/projects/' + currentProjectId + '/jira/writeback/run');
  }).then(function(d){
    wbState.data = d;
    fillWriteback();
    toast(d.result.message);
  }).catch(function(){}).then(function(){ btn.disabled = false; });
});

/* ── Отчёт в Confluence ── */
function renderConfluencePanel(){
  var pid = currentProjectId;
  api('/api/projects/' + pid + '/confluence').then(function(d){ if(pid === currentProjectId) fillConfluence(d); }).catch(function(){});
}
function fillConfluence(d){
  var st = document.getElementById('crStatus');
  st.className = 'jira-status' + (!d.connected ? '' : (d.lastStatus === 'error' ? ' error' : (d.lastStatus === 'ok' ? ' ok' : '')));
  st.textContent = !d.connected ? 'Подключение к Confluence не настроено' + (isGlobalAdmin() ? ' — настройте его в разделе «Интеграции».' : ' — обратитесь к администратору.') :
    (d.lastAt ? 'Последняя публикация: ' + fmtDateTime(d.lastAt) + ' — ' + (d.lastMessage || '') : 'Отчёт ещё не публиковался.');
  document.getElementById('crEnabled').checked = d.enabled;
  document.getElementById('crSpace').value = d.spaceKey;
  document.getElementById('crParent').value = d.parentId;
  document.getElementById('crTitle').value = d.title;
  document.getElementById('crMode').value = d.mode;
  document.getElementById('crWeekday').value = String(d.weekday);
  document.getElementById('crTime').value = d.time;
  var zones = TIMEZONES.slice();
  if(zones.indexOf(d.timezone) === -1) zones.push(d.timezone);
  document.getElementById('crTz').innerHTML = zones.map(function(z){
    return '<option value="' + escapeAttr(z) + '"' + (z === d.timezone ? ' selected' : '') + '>' + escapeHtml(z) + '</option>';
  }).join('');
  var open = document.getElementById('crOpen');
  open.hidden = !d.pageUrl;
  if(d.pageUrl) open.href = d.pageUrl;
  document.querySelectorAll('#crPanel input, #crPanel select, #crPanel button').forEach(function(el){ el.disabled = !d.connected; });
}
function confluenceBody(){
  return {enabled: document.getElementById('crEnabled').checked, spaceKey: document.getElementById('crSpace').value.trim(),
          parentId: document.getElementById('crParent').value.trim(), title: document.getElementById('crTitle').value.trim(),
          mode: document.getElementById('crMode').value, weekday: parseInt(document.getElementById('crWeekday').value, 10),
          time: document.getElementById('crTime').value, timezone: document.getElementById('crTz').value};
}
function saveConfluence(message){
  return apiPut('/api/projects/' + currentProjectId + '/confluence', confluenceBody()).then(function(d){
    fillConfluence(d);
    if(message) toast(message);
    return d;
  });
}
document.getElementById('crSave').addEventListener('click', function(){ saveConfluence('Настройки отчёта сохранены').catch(function(){}); });
document.getElementById('crEnabled').addEventListener('change', function(e){
  saveConfluence(e.target.checked ? 'Еженедельная публикация включена' : 'Еженедельная публикация выключена').catch(function(){
    e.target.checked = !e.target.checked;
  });
});
document.getElementById('crPublish').addEventListener('click', function(){
  var btn = this;
  btn.disabled = true;
  saveConfluence().then(function(){
    return apiPost('/api/projects/' + currentProjectId + '/confluence/publish');
  }).then(function(d){
    fillConfluence(d);
    toast(d.result.message);
  }).catch(function(){ renderConfluencePanel(); }).then(function(){ btn.disabled = false; });
});
document.getElementById('jiraOpenIntegrations').addEventListener('click', function(){ navigate('integrations'); });

/* «Интеграции» (администратор): общее подключение к Jira, почта и Telegram-бот */
var adminJira = null;
function renderIntegrationsView(){
  api('/api/admin/jira-connection').then(function(c){ adminJira = c; fillJiraForm(); }).catch(function(){});
  api('/api/admin/jira-options').then(fillJiraOptions).catch(function(){});
  api('/api/admin/confluence-connection').then(fillConfForm).catch(function(){});
  renderNotifyAdminView();
}

/* Запись в Jira (разрешение) и вебхук Jira */
function fillJiraOptions(o){
  document.getElementById('joWriteback').checked = o.writebackAllowed;
  var w = o.webhook, last = w.last;
  var st = document.getElementById('joWebhookStatus');
  st.className = 'jira-status' + (w.enabled ? ' ok' : '');
  st.textContent = !w.enabled ? 'Вебхук выключен: замеры обновляются только по расписанию и вручную.' :
    'Вебхук включён. ' + (last ? 'Последнее событие: ' + fmtDateTime(last.at) + (last.event ? ' (' + last.event + ')' : '') +
      (last.keys && last.keys.length ? ', задачи ' + last.keys.join(', ') : '') + ', кнопок в очереди: ' + last.buttons + '.' : 'Событий от Jira ещё не было.');
  document.getElementById('joWebhookUrlRow').hidden = !w.enabled;
  document.getElementById('joWebhookUrl').value = w.url;
  document.getElementById('joWebhookOn').hidden = w.enabled;
  document.getElementById('joWebhookOff').hidden = !w.enabled;
  document.getElementById('joWebhookRotate').hidden = !w.enabled;
}
function saveJiraOptions(body, message){
  return apiPut('/api/admin/jira-options', body).then(function(o){ fillJiraOptions(o); if(message) toast(message); return o; });
}
document.getElementById('joWriteback').addEventListener('change', function(e){
  saveJiraOptions({writebackAllowed: e.target.checked}, e.target.checked ? 'Проекты могут записывать прогноз в Jira' : 'Запись в Jira запрещена')
    .catch(function(){ e.target.checked = !e.target.checked; });
});
document.getElementById('joWebhookOn').addEventListener('click', function(){
  saveJiraOptions({webhook: true}, 'Вебхук включён — добавьте адрес в настройках Jira').catch(function(){});
});
document.getElementById('joWebhookRotate').addEventListener('click', function(){
  askConfirm('Новый адрес вебхука?', 'Прежний адрес перестанет работать — его нужно будет заменить в настройках Jira.', function(){
    saveJiraOptions({webhook: true, rotate: true}, 'Адрес вебхука заменён').catch(function(){});
  }, 'Заменить');
});
document.getElementById('joWebhookOff').addEventListener('click', function(){
  askConfirm('Выключить вебхук?', 'События Jira перестанут приниматься; замеры будут обновляться только по расписанию.', function(){
    saveJiraOptions({webhook: false}, 'Вебхук выключен').catch(function(){});
  }, 'Выключить');
});
document.getElementById('joWebhookCopy').addEventListener('click', function(){
  var input = document.getElementById('joWebhookUrl');
  var done = function(){ toast('Адрес скопирован'); };
  if(navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(input.value).then(done, function(){ input.select(); });
  else { input.select(); try { document.execCommand('copy'); done(); } catch(e){} }
});

/* Подключение к Confluence */
var adminConf = null;
function fillConfForm(c){
  adminConf = c;
  document.getElementById('confUrl').value = c ? c.baseUrl : '';
  document.getElementById('confType').value = c ? c.instanceType : 'server';
  document.getElementById('confLogin').value = c ? c.login : '';
  document.getElementById('confInsecure').checked = !!(c && c.acceptInvalidCerts);
  var token = document.getElementById('confToken');
  token.value = '';
  token.placeholder = c && c.hasToken ? 'Сохранён — оставьте пустым, чтобы не менять' : '';
  var st = document.getElementById('confStatus');
  st.className = 'jira-status' + (c ? ' ok' : '');
  st.textContent = c ? 'Подключение сохранено' : 'Подключение не настроено';
  document.getElementById('confDisconnect').hidden = !c;
}
function confFormBody(){
  return {baseUrl: document.getElementById('confUrl').value.trim(), instanceType: document.getElementById('confType').value,
          login: document.getElementById('confLogin').value.trim(), token: document.getElementById('confToken').value,
          acceptInvalidCerts: document.getElementById('confInsecure').checked};
}
document.getElementById('confForm').addEventListener('submit', function(e){
  e.preventDefault();
  apiPut('/api/admin/confluence-connection', confFormBody()).then(function(c){ fillConfForm(c); toast('Подключение к Confluence сохранено'); }).catch(function(){});
});
document.getElementById('confTest').addEventListener('click', function(){
  var st = document.getElementById('confStatus');
  st.className = 'jira-status';
  st.textContent = 'Проверяем…';
  apiPost('/api/admin/confluence-connection/test', confFormBody()).then(function(r){
    st.className = 'jira-status ok';
    st.textContent = '✓ Подключение работает' + (r.user ? ': ' + r.user : '');
  }).catch(function(){ st.className = 'jira-status error'; st.textContent = 'Проверка не прошла — подробности в сообщении выше'; });
});
document.getElementById('confDisconnect').addEventListener('click', function(){
  askConfirm('Отключить Confluence?', 'Подключение и сохранённый токен будут удалены; отчёты проектов перестанут публиковаться.', function(){
    apiDelete('/api/admin/confluence-connection').then(function(){ fillConfForm(null); toast('Confluence отключён'); });
  }, 'Отключить');
});
function fillJiraForm(){
  var c = adminJira;
  document.getElementById('jiraUrl').value = c ? c.baseUrl : '';
  document.getElementById('jiraType').value = c ? c.instanceType : 'server_basic';
  document.getElementById('jiraLogin').value = c ? c.login : '';
  document.getElementById('jiraToken').value = '';
  document.getElementById('jiraInsecure').checked = !!(c && c.acceptInvalidCerts);
  document.getElementById('jiraDisconnect').hidden = !c;
  setJiraStatus(c ? 'Подключение сохранено' + (c.hasToken ? '' : ' (без пароля или токена)') : 'Подключение не настроено', c ? 'ok' : '');
  updateJiraFormLabels();
}

function setJiraStatus(text, kind){
  var el = document.getElementById('jiraStatus');
  el.textContent = text || '';
  el.className = 'jira-status' + (kind ? ' ' + kind : '');
}

function updateJiraFormLabels(){
  var t = JIRA_TYPE_TEXTS[document.getElementById('jiraType').value];
  var c = adminJira;
  document.getElementById('jiraLoginLabel').textContent = t.login;
  document.getElementById('jiraTokenLabel').textContent = t.token;
  document.getElementById('jiraLogin').placeholder = t.loginPh;
  document.getElementById('jiraToken').placeholder = (c && c.hasToken) ? 'Сохранён — оставьте пустым, чтобы не менять' : t.tokenPh;
  document.getElementById('jiraHint').textContent = t.hint;
}

function jiraFormData(){
  return {
    baseUrl: document.getElementById('jiraUrl').value.trim().replace(/\/+$/, ''),
    instanceType: document.getElementById('jiraType').value,
    login: document.getElementById('jiraLogin').value.trim(),
    token: document.getElementById('jiraToken').value,
    acceptInvalidCerts: document.getElementById('jiraInsecure').checked
  };
}

document.getElementById('jiraType').addEventListener('change', updateJiraFormLabels);
document.getElementById('jiraUrl').addEventListener('change', function(e){
  /* как в jira_mass_logger: адрес *.atlassian.net — это Jira Cloud */
  if(/atlassian\.net/i.test(e.target.value)) { document.getElementById('jiraType').value = 'cloud'; updateJiraFormLabels(); }
});
document.getElementById('jiraTest').addEventListener('click', function(){
  var btn = this;
  btn.disabled = true;
  setJiraStatus('Проверка подключения…');
  apiPost('/api/admin/jira-connection/test', jiraFormData()).then(function(data){
    setJiraStatus('✓ Соединение установлено' + (data.user ? ': ' + data.user : ''), 'ok');
  }).catch(function(e){ setJiraStatus(e.message, 'error'); }).then(function(){ btn.disabled = false; });
});
document.getElementById('jiraForm').addEventListener('submit', function(e){
  e.preventDefault();
  apiPut('/api/admin/jira-connection', jiraFormData()).then(function(conn){
    adminJira = conn;
    fillJiraForm();
    loadJiraConfig();  /* кнопки среза текущего проекта становятся доступны */
    toast('Подключение к Jira сохранено');
  }).catch(function(err){ setJiraStatus(err.message, 'error'); });
});
document.getElementById('jiraDisconnect').addEventListener('click', function(){
  askConfirm('Отключить Jira?', 'Подключение и сохранённый пароль или токен будут удалены. Кнопки среза во всех проектах сохранятся, но срезы и автозаполнение не будут работать до нового подключения.', function(){
    apiDelete('/api/admin/jira-connection').then(function(){
      adminJira = null;
      fillJiraForm();
      loadJiraConfig();
      toast('Jira отключена');
    });
  }, 'Отключить');
});

function renderJiraButtonsTable(){
  var table = document.getElementById('jiraButtonsTable');
  var buttons = STATE.jira ? STATE.jira.buttons : [];
  var connected = !!(STATE.jira && STATE.jira.connected);
  document.getElementById('jiraAddButton').disabled = !STATE.services.length;
  if(!buttons.length){
    emptyTable(table, 'Кнопок пока нет', connected ? 'Запустите мастер подключения — он создаст эпики и кнопку сам — или создайте кнопку вручную.' : 'Кнопки заработают, когда администратор настроит подключение к Jira.');
    return;
  }
  var serviceName = function(id){ var s = serviceById(id); return s ? s.name : '—'; };
  table.innerHTML = '<thead><tr><th>Кнопка</th><th>Структура</th><th>Эпики и задачи</th><th>Поля Jira</th><th></th></tr></thead><tbody>' +
    buttons.map(function(b){
      var auto = b.schedule && b.schedule.enabled ? '<div class="muted-cell">автозаполнение: ' + escapeHtml(scheduleSummary(b.schedule)) + '</div>' : '';
      if(b.history) auto += '<div class="muted-cell">история: ' + escapeHtml(historyJobSummary(b.history)) + '</div>';
      if(b.epicSync && (b.epicSync.enabled || b.epicSync.lastAt)) auto += '<div class="muted-cell">эпики из Jira: ' + (b.epicSync.enabled ? 'перед автозаполнением' : 'вручную') +
        (b.epicSync.lastMessage ? ' · ' + escapeHtml(b.epicSync.lastMessage.replace(/^Эпики из Jira — /, '')) : '') + '</div>';
      return '<tr data-bid="'+b.id+'"><td><b>'+escapeHtml(b.name)+'</b>'+auto+'</td>'+
        '<td>'+(b.structureId ? escapeHtml(b.structureName || ('Структура ' + b.structureId)) + ' <span class="muted-cell">#'+b.structureId+'</span>' : '<span class="muted-cell">без структуры</span>')+'</td>'+
        '<td>'+b.mappings.map(function(m){ return '<div class="jira-map-line"><span>'+escapeHtml(serviceName(m.serviceId))+'</span> ← <span class="mono">'+escapeHtml(m.issueKeys)+'</span>'+(m.includeSubtree ? ' <span class="muted-cell">с вложенными</span>' : '')+'</div>'; }).join('')+'</td>'+
        '<td>'+fieldMapSummary(b.fieldMap)+'</td>'+
        '<td class="row-actions"><button type="button" class="btn btn-ghost btn-sm" data-action="snapshot"'+(connected ? '' : ' disabled title="Нет подключения к Jira"')+'>⇣ Срез</button>'+
          '<button type="button" class="btn btn-ghost btn-sm" data-action="epics"'+(connected ? ' title="Эпики, цели и «Сдано» — из Jira"' : ' disabled title="Нет подключения к Jira"')+'>⇄ Эпики</button>'+
          '<button type="button" class="btn btn-ghost btn-sm" data-action="history"'+(!connected ? ' disabled title="Нет подключения к Jira"' :
            (b.lastSuccessAt ? ' title="Восстановить замеры на прошлые даты по журналу изменений Jira"' : ' disabled title="Доступно после первого успешного среза"'))+'>↺ История</button>'+
          actionBtn('edit','Редактировать',ICON_EDIT)+actionBtn('delete','Удалить',ICON_DELETE)+'</td></tr>';
    }).join('') + '</tbody>';
  var buttonOf = function(el){ var bid = parseInt(el.closest('tr').dataset.bid, 10); return buttons.find(function(b){ return b.id === bid; }); };
  table.querySelectorAll('[data-action="snapshot"]').forEach(function(el){
    el.addEventListener('click', function(){ runJiraSnapshot(buttonOf(el).id, el); });
  });
  table.querySelectorAll('[data-action="edit"]').forEach(function(el){
    el.addEventListener('click', function(){ openJiraButtonEditor(buttonOf(el)); });
  });
  table.querySelectorAll('[data-action="history"]').forEach(function(el){
    el.addEventListener('click', function(){ openHistoryRestore(buttonOf(el)); });
  });
  table.querySelectorAll('[data-action="epics"]').forEach(function(el){
    el.addEventListener('click', function(){ openEpicSync(buttonOf(el)); });
  });
  table.querySelectorAll('[data-action="delete"]').forEach(function(el){
    el.addEventListener('click', function(){
      var b = buttonOf(el);
      askConfirm('Удалить кнопку «'+b.name+'»?', 'Ассоциации эпиков с задачами Jira для этой кнопки будут удалены. Замеры не затрагиваются.', function(){
        apiDelete('/api/jira/buttons/'+b.id).then(function(){
          STATE.jira.buttons = STATE.jira.buttons.filter(function(x){ return x.id !== b.id; });
          renderJiraButtonsTable();
          renderJiraSnapshotButtons();
          toast('Кнопка удалена');
        });
      });
    });
  });
}

document.getElementById('jiraAddButton').addEventListener('click', function(){ openJiraButtonEditor(null); });

/* Редактор кнопки: структура + ассоциации «эпик ← задачи Jira» */
var JIRA_METRICS = [['remaining', 'Остаток'], ['spent', 'Затрачено'], ['estimate', 'Оценка']];
var JIRA_TIME_FIELDS = [{id: 'timeestimate', name: 'Оставшаяся оценка', time: true},
  {id: 'timespent', name: 'Затраченное время', time: true},
  {id: 'timeoriginalestimate', name: 'Первоначальная оценка', time: true}];
/* пустое поле Jira у показателя из колонки Structure: по задачам — та же колонка без суммы */
var FIELD_EMPTY_LABEL = '— не заполнять —', FIELD_FROM_STRUCTURE_LABEL = '— как колонка Structure, по самой задаче —';
var JIRA_DEFAULT_FIELD_MAP = {remaining: {field: 'timeestimate', unit: 'seconds', structure: null}, spent: {field: 'timespent', unit: 'seconds', structure: null},
  estimate: {field: 'timeoriginalestimate', unit: 'seconds', structure: null}};
var JIRA_FIELD_UNITS = [['seconds', 'секунды'], ['hours', 'часы'], ['minutes', 'минуты'], ['days', 'дни (8 ч)'], ['ms', 'миллисекунды']];

/* ── Колонки Structure как источник показателей (значение колонки у строки эпика — как в структуре) ── */
var STRUCTURE_UNITS = [['ms', 'миллисекунды'], ['seconds', 'секунды'], ['minutes', 'минуты'], ['hours', 'часы'], ['days', 'дни (8 ч)']];
var FORMULA_ALIASES = {remaining: 'timeestimate', remaining_estimate: 'timeestimate', remainingestimate: 'timeestimate', timeestimate: 'timeestimate',
  original_estimate: 'timeoriginalestimate', originalestimate: 'timeoriginalestimate', estimate: 'timeoriginalestimate', orig: 'timeoriginalestimate',
  original: 'timeoriginalestimate', timeoriginalestimate: 'timeoriginalestimate', time_spent: 'timespent', timespent: 'timespent', spent: 'timespent',
  status: 'status', duedate: 'duedate', due_date: 'duedate', due: 'duedate'};
var FORMULA_WORDS = ['if', 'then', 'else', 'with', 'and', 'or', 'not', 'case', 'true', 'false', 'null', 'undefined', 'end', 'in',
  'sum', 'min', 'max', 'avg', 'count', 'distinct', 'none', 'all', 'any'];
function unitOptions(units, current){
  return units.map(function(u){ return '<option value="'+u[0]+'"'+(u[0] === current ? ' selected' : '')+'>'+u[1]+'</option>'; }).join('');
}
/* Формат значений переменной формулы и поля колонки — как в Structure */
var STRUCTURE_FORMATS = [['duration', 'длительность'], ['number', 'число'], ['text', 'текст'], ['time', 'дата']];
var STRUCTURE_UNIT_HOURS = {ms: 1 / 3600000, seconds: 1 / 3600, minutes: 1 / 60, hours: 1, days: 8};
var FORMULA_FIELD_DEFAULTS = [{id: 'timeoriginalestimate', name: 'Первоначальная оценка', format: 'duration'},
  {id: 'timespent', name: 'Затраченное время', format: 'duration'}, {id: 'timeestimate', name: 'Оставшаяся оценка', format: 'duration'},
  {id: 'status', name: 'Статус', format: 'text'}, {id: 'duedate', name: 'Срок исполнения', format: 'time'}];
var formulaFieldCache = {pid: null, list: null};
function loadFormulaFields(){
  if(formulaFieldCache.pid === currentProjectId && formulaFieldCache.list) return Promise.resolve(formulaFieldCache.list);
  return api('/api/projects/' + currentProjectId + '/jira/formula-fields').then(function(list){
    formulaFieldCache = {pid: currentProjectId, list: list};
    return list;
  }).catch(function(){ return null; });
}
function formulaFieldList(){
  return formulaFieldCache.pid === currentProjectId && formulaFieldCache.list ? formulaFieldCache.list : FORMULA_FIELD_DEFAULTS;
}
function formulaFieldById(id){ return formulaFieldList().find(function(f){ return f.id === id; }) || null; }
function formulaFieldOptions(current, placeholder){
  var list = formulaFieldList().slice();
  if(current && !list.some(function(f){ return f.id === current; })) list.unshift({id: current, name: current, format: ''});
  return (placeholder ? '<option value="">— выберите поле —</option>' : '') + list.map(function(f){
    return '<option value="'+escapeAttr(f.id)+'"'+(f.id === current ? ' selected' : '')+'>'+escapeHtml(f.name)+(f.name !== f.id ? ' ('+escapeHtml(f.id)+')' : '')+'</option>';
  }).join('');
}
function structureFormatOptions(current){
  return STRUCTURE_FORMATS.map(function(x){ return '<option value="'+x[0]+'"'+(x[0] === current ? ' selected' : '')+'>'+x[1]+'</option>'; }).join('');
}
/* Поле для переменной формулы по её имени: orig → первоначальная оценка, spent → затрачено, status → статус… */
function guessVariable(name){
  var id = FORMULA_ALIASES[name.toLowerCase()];
  var f = id ? formulaFieldById(id) || FORMULA_FIELD_DEFAULTS.find(function(x){ return x.id === id; }) : null;
  if(!f){
    var low = name.toLowerCase().replace(/_/g, ' ');
    f = formulaFieldList().find(function(x){ return x.name.toLowerCase() === low || x.id.toLowerCase() === name.toLowerCase(); }) || null;
  }
  return f ? {field: f.id, format: f.format || 'number'} : {field: '', format: 'number'};
}
function describeEditor(ed){
  var agg = ed.sum ? (ed.distinct !== false ? ', Σ без дубликатов' : ', Σ с дубликатами') : '';
  if(ed.mode === 'field'){
    var f = formulaFieldById(ed.variables ? ed.variables.value.field : ed.field);
    return 'Поле «' + (f ? f.name : (ed.field || (ed.variables && ed.variables.value.field))) + '»' + agg;
  }
  return 'Формула: ' + String(ed.formula || '').replace(/\s+/g, ' ').slice(0, 70) + agg;
}
/* Колонка из прежних версий (формула или Σ формулы в виде спецификации) → настройки редактора */
function editorFromAttribute(spec){
  var sum = spec && spec.id === 'sum';
  var expr = sum ? (spec.params || {}).attribute : spec;
  if(!expr || expr.id !== 'expr' || !(expr.params || {}).formula) return null;
  var vars = {};
  Object.keys(expr.params.variables || {}).forEach(function(n){
    var v = expr.params.variables[n];
    vars[n] = {field: v.id === 'customfield' ? 'customfield_' + (v.params || {}).fieldId : v.id, format: v.format || 'number'};
  });
  return {mode: 'formula', formula: expr.params.formula, variables: vars, sum: sum, distinct: false};
}
/* Фильтр доски Structure у кнопки — для колонок «После фильтрации» (After filtering) */
function boardFilterHtml(bf){
  var mode = bf ? bf.mode : 'none';
  return '<details class="jb-board-filter" id="jbBoardFilterBox"'+(bf ? ' open' : '')+'><summary>Фильтр доски Structure <span class="muted-cell">— для колонок «После фильтрации»</span></summary>'+
    '<div class="setting-hint">Если на доске Structure включён фильтр и у колонок отмечено «After filtering», укажите здесь тот же фильтр: такие колонки Burndown посчитает только по видимым с ним задачам.</div>'+
    '<div class="seg-filter" role="radiogroup">'+[['none', 'Без фильтра'], ['jql', 'JQL'], ['json', 'Трансформации из Structure (JSON)']].map(function(x){
      return '<label class="checkbox-row"><input type="radio" name="jbBfMode" value="'+x[0]+'"'+(mode === x[0] ? ' checked' : '')+'> '+x[1]+'</label>';
    }).join('')+'</div>'+
    '<div id="jbBfJql"'+(mode === 'jql' ? '' : ' hidden')+'><label class="small" for="jbBfJqlText">JQL фильтра — как на доске</label>'+
      '<textarea id="jbBfJqlText" rows="2" placeholder="status not in (Done, Closed)">'+escapeHtml(bf && bf.mode === 'jql' ? bf.jql : '')+'</textarea>'+
      '<label class="checkbox-row"><input type="checkbox" id="jbBfSub"'+(bf && bf.showSubItems ? ' checked' : '')+'> Показывать вложенные задачи совпавших <span class="muted-cell">(Show sub-items)</span></label></div>'+
    '<div id="jbBfJson"'+(mode === 'json' ? '' : ' hidden')+'><label class="small" for="jbBfJsonText">Трансформации доски: forestSpec из запроса Structure (F12 → «Сеть», запрос value или forest) или его transformations</label>'+
      '<textarea id="jbBfJsonText" rows="3" placeholder="{&quot;structureId&quot;:…,&quot;transformations&quot;:[…]}">'+escapeHtml(bf && bf.mode === 'json' ? JSON.stringify(bf.transformations) : '')+'</textarea></div>'+
  '</details>';
}
function readBoardFilter(){
  var mode = (document.querySelector('input[name="jbBfMode"]:checked') || {}).value || 'none';
  if(mode === 'jql') return {mode: 'jql', jql: document.getElementById('jbBfJqlText').value.trim(), showSubItems: document.getElementById('jbBfSub').checked};
  if(mode === 'json') return {mode: 'json', transformations: document.getElementById('jbBfJsonText').value.trim()};
  return null;
}
function bindBoardFilter(){
  document.querySelectorAll('input[name="jbBfMode"]').forEach(function(r){
    r.addEventListener('change', function(){
      var mode = document.querySelector('input[name="jbBfMode"]:checked').value;
      document.getElementById('jbBfJql').hidden = mode !== 'jql';
      document.getElementById('jbBfJson').hidden = mode !== 'json';
    });
  });
}
/* Раздел структуры у кнопки: если эпик стоит в структуре несколько раз, строка берётся из отмеченных разделов */
function scopeHtml(scope){
  var list = scope || [];
  return '<details class="jb-board-filter" id="jbScopeBox"'+(list.length ? ' open' : '')+'><summary>Раздел структуры <span class="muted-cell">— если эпик стоит в структуре несколько раз</span></summary>'+
    '<div class="setting-hint">Одна и та же задача может стоять в структуре в нескольких местах, и под каждой строкой у неё своё поддерево и свои суммы. Отметьте разделы, где вы смотрите цифры на доске (например, «4.1», «4.2», «4.3»), или сами эпики под ними: строка эпика берётся из отмеченного места. Без отметок берётся первая строка в структуре.</div>'+
    '<div id="jbScopeList" class="jb-scope-list">'+list.map(function(x){ return scopeItemHtml(x, 0, true); }).join('')+'</div>'+
    '<div class="form-actions"><button type="button" class="btn btn-ghost btn-sm" id="jbScopeLoad">Загрузить разделы</button></div></details>';
}
/* Раздел — строка структуры: один и тот же эпик может стоять в нескольких разделах, поэтому сохраняется и строка (row),
   а в сохраняемом названии эпика второго уровня — раздел над ним («4.1 → SRM-4687 …»); label — текст в списке */
function scopeItemHtml(x, depth, checked, note){
  var row = typeof x.row === 'number' ? x.row : (typeof x.rowId === 'number' ? x.rowId : null);
  return '<label class="checkbox-row jb-scope-item" style="padding-left:'+(depth * 1.25)+'rem"><input type="checkbox" data-scope-item="'+escapeAttr(x.item)+'"'+
    (row !== null ? ' data-scope-row="'+row+'"' : '')+' data-scope-title="'+escapeAttr(x.title || '')+'"'+(checked ? ' checked' : '')+'> '+
    escapeHtml(x.label || x.title || x.item)+(note ? ' <span class="muted-cell">'+escapeHtml(note)+'</span>' : '')+'</label>';
}
function readScope(){
  var out = [], seen = {};
  document.querySelectorAll('#jbScopeList [data-scope-item]:checked').forEach(function(el){
    var row = el.dataset.scopeRow ? parseInt(el.dataset.scopeRow, 10) : null;
    var key = el.dataset.scopeItem + '|' + row;
    if(seen[key]) return;
    seen[key] = true;
    var x = {item: el.dataset.scopeItem, title: el.dataset.scopeTitle};
    if(row !== null) x.row = row;
    out.push(x);
  });
  return out.length ? out : null;
}
function bindScope(structureId){
  var btn = document.getElementById('jbScopeLoad');
  btn.addEventListener('click', function(){
    var sid = structureId();
    if(!sid){ toast('Сначала выберите структуру'); return; }
    var saved = readScope() || [];
    btn.disabled = true;
    api('/api/projects/'+currentProjectId+'/jira/structure-sections?structureId='+encodeURIComponent(sid)).then(function(list){
      // отмечено по строке; раздел без строки (сохранён до 1.9.3) или со строкой, которой больше нет, — по элементу
      var rows = {}, items = {};
      saved.forEach(function(x){
        var here = typeof x.row === 'number' && list.some(function(s){ return s.rowId === x.row && s.item === x.item; });
        if(here) rows[x.row] = true; else items[x.item] = true;
      });
      document.getElementById('jbScopeList').innerHTML = list.length ? list.map(function(x){
        var title = x.parentTitle ? x.parentTitle + ' → ' + x.title : x.title;
        return scopeItemHtml({item: x.item, row: x.rowId, title: title, label: x.title}, x.depth, rows[x.rowId] || items[x.item], 'строк: ' + x.rows);
      }).join('') : '<p class="insight-empty">В структуре нет разделов с вложенными строками.</p>';
      toast('Разделов загружено: ' + list.length);
    }).catch(function(){}).then(function(){ btn.disabled = false; });
  });
}
/* Сверка: все места эпика в структуре и значение колонки у каждой строки */
function placesHtml(values, hours){
  var multi = values.filter(function(v){ return v.places && v.places.length > 1; });
  if(!multi.length) return '';
  return '<div class="jb-places"><div class="field-label">Задача стоит в структуре несколько раз — у каждой строки своё поддерево</div>'+multi.map(function(v){
    return '<div class="jb-places-item"><span class="mono">'+escapeHtml(v.key)+'</span><ul>'+v.places.map(function(p){
      return '<li'+(p.chosen ? ' class="jb-place-chosen"' : '')+'>'+escapeHtml(p.path.filter(Boolean).join(' → ') || 'верхний уровень')+
        ' — '+hours(typeof p.raw === 'number' ? p.raw : null)+(p.chosen ? ' <b>(взята)</b>' : '')+'</li>';
    }).join('')+'</ul></div>';
  }).join('')+'<div class="setting-hint">Если на доске другое число — отметьте у кнопки нужный «Раздел структуры».</div></div>';
}
/* Сверка: значения переменных формулы у выбранной строки — видно, какое поле Structure не отдаёт */
function varsHtml(values, editor, k){
  if(!editor || !values.some(function(v){ return v.vars; })) return '';
  var names = Object.keys(values[0].vars || {});
  if(!names.length) return '';
  var vars = editor.mode === 'field' ? {value: {field: editor.field, format: editor.format}} : editor.variables;
  var show = function(name, x){
    if(x === null || x === undefined || x === '') return '<span class="muted-cell">пусто</span>';
    var fmt = (vars[name] || {}).format;
    if(fmt === 'duration' && typeof x === 'number') return fmtNum(Math.round(x * k * 100) / 100) + ' ч';
    return escapeHtml(String(x));
  };
  return '<div class="table-scroll"><table class="data-table compact-table jb-str-check"><thead><tr><th>Переменная</th><th>Поле</th>'+
    values.map(function(v){ return '<th class="num">'+escapeHtml(v.key)+'</th>'; }).join('')+'</tr></thead><tbody>'+names.map(function(n){
      var f = formulaFieldById((vars[n] || {}).field);
      return '<tr><td class="mono">'+escapeHtml(n)+'</td><td>'+escapeHtml(f ? f.name : (vars[n] || {}).field || '')+'</td>'+
        values.map(function(v){ return '<td class="num">'+show(n, (v.vars || {})[n])+'</td>'; }).join('')+'</tr>';
    }).join('')+'</tbody></table></div><div class="setting-hint">Значения переменных у строки эпика (без суммы по вложенным). Пусто — Structure не отдаёт это поле: выберите другое поле или формат.</div>';
}
function downloadJson(name, data){
  var blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(function(){ URL.revokeObjectURL(a.href); a.remove(); }, 0);
}
function APP_VERSION_TEXT(){
  var el = document.getElementById('btnVersions');
  return el ? el.textContent.trim() : '';
}
function describeAttribute(spec){
  if(!spec) return '';
  var p = spec.params || {};
  if(spec.id === 'expr') return 'Формула: ' + String(p.formula || '').replace(/\s+/g, ' ').slice(0, 90);
  if(spec.id === 'sum') return 'Σ по вложенным: ' + describeAttribute(p.attribute || p.value);
  return 'Атрибут «' + spec.id + '»' + (p.fieldId ? ' (' + p.fieldId + ')' : '');
}
/* Спецификации колонок из вставленного текста: запрос или ответ /value из инструментов разработчика, массив или одна спецификация */
function extractAttributeSpecs(text){
  var data = JSON.parse(text), out = [], seen = {};
  var add = function(spec){
    if(spec && typeof spec === 'object' && typeof spec.id === 'string'){
      var key = JSON.stringify(spec);
      if(!seen[key]){ seen[key] = true; out.push(spec); }
    }
  };
  var walk = function(node){
    if(Array.isArray(node)){ node.forEach(walk); return; }
    if(!node || typeof node !== 'object') return;
    if(node.requests) walk(node.requests);
    if(node.responses) walk(node.responses);
    if(node.attributes) (node.attributes || []).forEach(add);
    if(node.data && Array.isArray(node.data)) node.data.forEach(function(d){ add(d && d.attribute); });
    if(typeof node.id === 'string' && !node.requests && !node.responses) add(node);
  };
  walk(data);
  return out;
}
function formulaVariables(formula){
  var text = String(formula || '').replace(/"[^"]*"|'[^']*'/g, ' ');
  var locals = {};
  text.replace(/\bWITH\s+([A-Za-z_][A-Za-z0-9_]*)\s*=/gi, function(_m, n){ locals[n.toLowerCase()] = true; return ''; });
  var names = [], re = /[A-Za-z_][A-Za-z0-9_]*/g, m;
  while((m = re.exec(text))){
    var name = m[0], rest = text.slice(re.lastIndex).replace(/^\s+/, '');
    if(rest[0] === '(' || rest[0] === '{' || FORMULA_WORDS.indexOf(name.toLowerCase()) !== -1 || locals[name.toLowerCase()]) continue;
    if(names.indexOf(name) === -1) names.push(name);
  }
  return names;
}

function fieldMapSummary(fm){
  fm = fm || JIRA_DEFAULT_FIELD_MAP;
  return JIRA_METRICS.map(function(m){
    var f = fm[m[0]] || {};
    if(f.structure){
      var label = f.structure.label || describeAttribute(f.structure.attribute);
      if(f.structure.afterFiltering && label.indexOf('после фильтрации') === -1) label += ', после фильтрации';
      return '<div class="jira-map-line"><span>'+m[1]+'</span> ← <span class="badge badge-info">Structure</span> '+escapeHtml(label)+'</div>';
    }
    var known = JIRA_TIME_FIELDS.find(function(t){ return t.id === f.field; });
    return '<div class="jira-map-line"><span>'+m[1]+'</span> ← '+(f.field ? '<span class="mono">'+escapeHtml(known ? known.name : f.field)+'</span>'+(f.unit !== 'seconds' ? ' <span class="muted-cell">'+escapeHtml(({ms: 'мс', minutes: 'мин', hours: 'ч', days: 'дн'})[f.unit] || '')+'</span>' : '') : '<span class="muted-cell">не заполняется</span>')+'</div>';
  }).join('');
}

function fieldOptions(fields, selected, emptyLabel){
  var opts = '<option value="">'+escapeHtml(emptyLabel || FIELD_EMPTY_LABEL)+'</option>';
  var seen = !selected;
  fields.forEach(function(f){
    if(f.id === selected) seen = true;
    opts += '<option value="'+escapeAttr(f.id)+'" data-time="'+(f.time ? 1 : 0)+'"'+(f.id === selected ? ' selected' : '')+'>'+escapeHtml(f.name)+(f.time ? '' : ' ('+escapeHtml(f.id)+')')+'</option>';
  });
  if(!seen) opts += '<option value="'+escapeAttr(selected)+'" data-time="0" selected>'+escapeHtml(selected)+'</option>';
  return opts;
}

function openJiraButtonEditor(button){
  var b = button || {name: '', structureId: null, structureName: '', mappings: [], fieldMap: JIRA_DEFAULT_FIELD_MAP};
  var fieldMap = b.fieldMap || JIRA_DEFAULT_FIELD_MAP;
  var mapOf = {};
  b.mappings.forEach(function(m){ mapOf[m.serviceId] = m; });
  var structureOptions = function(list, selectedId){
    var opts = '<option value="">— без структуры: задача и её подзадачи —</option>';
    var seen = false;
    (list || []).forEach(function(st){
      if(st.id === selectedId) seen = true;
      opts += '<option value="'+st.id+'" data-name="'+escapeAttr(st.name)+'"'+(st.id === selectedId ? ' selected' : '')+'>'+escapeHtml(st.name)+' (#'+st.id+')</option>';
    });
    if(selectedId && !seen) opts += '<option value="'+selectedId+'" data-name="'+escapeAttr(b.structureName || '')+'" selected>'+escapeHtml(b.structureName || 'Структура')+' (#'+selectedId+')</option>';
    return opts + '<option value="__manual">Указать ID вручную…</option>';
  };
  openModal({
    title: button ? 'Кнопка среза: ' + b.name : 'Новая кнопка среза', okLabel: 'Сохранить', size: 'xl', wide: true,
    html:
      '<div class="form-grid">'+
        field('Название кнопки', '<input type="text" id="jbName" value="'+escapeAttr(b.name)+'" placeholder="Например: Срез из структуры релиза">')+
        '<div class="setting-item"><label class="small" for="jbStructure">Структура Jira</label>'+
          '<div class="jira-structure-row"><select id="jbStructure">'+structureOptions([], b.structureId)+'</select>'+
          '<button type="button" class="btn btn-ghost btn-sm" id="jbLoadStructures">Загрузить из Jira</button></div>'+
          '<input type="number" id="jbStructureId" min="1" placeholder="ID структуры" hidden></div>'+
      '</div>'+
      scopeHtml(b.structureScope)+
      boardFilterHtml(b.boardFilter)+
      '<div class="jira-fields"><div class="jira-fields-head"><span class="field-label">Откуда брать показатели замера</span>'+
        '<button type="button" class="btn btn-ghost btn-sm" id="jbLoadFields">Загрузить поля из Jira</button></div>'+
        '<div class="jb-metrics" id="jbFields">'+JIRA_METRICS.map(function(m){
          var f = fieldMap[m[0]] || {field: '', unit: 'seconds'};
          var st = f.structure;
          return '<div class="jb-metric" data-metric="'+m[0]+'"><div class="jb-metric-head"><b>'+m[1]+'</b>'+
            '<select id="jbSrc_'+m[0]+'" aria-label="Источник: '+m[1]+'"><option value="jira"'+(st ? '' : ' selected')+'>Поле Jira — сумма по задачам эпика</option>'+
              '<option value="structure"'+(st ? ' selected' : '')+'>Колонка Structure — значение как в структуре</option></select></div>'+
            '<div class="jb-structure" id="jbStr_'+m[0]+'"'+(st ? '' : ' hidden')+'><span class="jb-str-label" id="jbStrLabel_'+m[0]+'">'+
              escapeHtml(st ? (st.label || describeAttribute(st.attribute)) : 'Колонка не выбрана')+'</span>'+
              '<button type="button" class="btn btn-ghost btn-sm" data-str-edit="'+m[0]+'">Выбрать колонку…</button>'+
              '<label class="small" for="jbStrUnit_'+m[0]+'">Значение колонки в</label><select id="jbStrUnit_'+m[0]+'">'+unitOptions(STRUCTURE_UNITS, st ? st.unit : 'hours')+'</select></div>'+
            '<div class="jb-jira-row"><label class="small" for="jbField_'+m[0]+'" id="jbFieldLabel_'+m[0]+'">'+(st ? 'Поле Jira для состава задач и истории' : 'Поле Jira')+'</label>'+
              '<select id="jbField_'+m[0]+'" data-metric="'+m[0]+'">'+fieldOptions(JIRA_TIME_FIELDS, f.field, st ? FIELD_FROM_STRUCTURE_LABEL : null)+'</select>'+
              '<select id="jbUnit_'+m[0]+'" aria-label="Единица: '+m[1]+'">'+unitOptions(JIRA_FIELD_UNITS, f.unit || 'seconds')+'</select></div></div>';
        }).join('')+'</div>'+
        '<div class="jb-str-panel" id="jbStrPanel" hidden></div>'+
        '<p class="bulk-note">Поле Jira — значения суммируются по задачам эпика (учёт времени Jira, в том числе Σ-поля, — всегда в секундах). Колонка Structure — берётся значение колонки у строки задачи эпика: поле, сумма по вложенным или формула, ровно как в структуре; часы отдельных задач в составе эпика — та же колонка по самой задаче, если не выбрано поле Jira.</p></div>'+
      '<p class="bulk-note">Укажите для эпиков задачи Jira (ключи через запятую). «С вложенными»: со структурой — все задачи под указанными в дереве структуры, без структуры — задача вместе с подзадачами. Эпики без задач в срез не попадают.</p>'+
      '<div class="table-scroll"><table class="data-table compact-table jira-map-table" id="jbTable"><thead><tr><th>Эпик</th><th>Задачи Jira</th><th class="center">С вложенными</th></tr></thead><tbody>'+
      STATE.services.map(function(s){
        var m = mapOf[s.id];
        return '<tr data-sid="'+s.id+'"'+(s.isDone ? ' class="done-row"' : '')+'><td><span class="bulk-etap">'+escapeHtml(s.etap)+'</span> '+escapeHtml(s.name)+'</td>'+
          '<td><input type="text" data-field="keys" value="'+escapeAttr(m ? m.issueKeys : '')+'" placeholder="ABC-123, ABC-124" aria-label="Задачи Jira"></td>'+
          '<td class="center"><input type="checkbox" data-field="subtree"'+(!m || m.includeSubtree ? ' checked' : '')+' aria-label="С вложенными"></td></tr>';
      }).join('')+
      '</tbody></table></div>',
    onOk: function(){
      var sel = document.getElementById('jbStructure');
      var structureId = null, structureName = '';
      if(sel.value === '__manual'){
        structureId = document.getElementById('jbStructureId').value.trim() || null;
      } else if(sel.value){
        structureId = parseInt(sel.value, 10);
        structureName = sel.selectedOptions[0].dataset.name || '';
      }
      var mappings = [];
      document.querySelectorAll('#jbTable tbody tr').forEach(function(tr){
        var keys = tr.querySelector('[data-field="keys"]').value.trim();
        if(keys) mappings.push({serviceId: parseInt(tr.dataset.sid, 10), issueKeys: keys,
                                includeSubtree: tr.querySelector('[data-field="subtree"]').checked});
      });
      var fm = {}, missing = null;
      JIRA_METRICS.forEach(function(m){
        var structure = null;
        if(document.getElementById('jbSrc_'+m[0]).value === 'structure'){
          if(!strSpecs[m[0]]) missing = missing || m[1];
          else structure = {attribute: strSpecs[m[0]].attribute, editor: strSpecs[m[0]].editor || null, label: strSpecs[m[0]].label,
                            unit: document.getElementById('jbStrUnit_'+m[0]).value, afterFiltering: !!strSpecs[m[0]].afterFiltering};
        }
        fm[m[0]] = {field: document.getElementById('jbField_'+m[0]).value, unit: document.getElementById('jbUnit_'+m[0]).value, structure: structure};
      });
      var body = {name: document.getElementById('jbName').value.trim(), structureId: structureId,
                  structureName: structureName, mappings: mappings, fieldMap: fm, boardFilter: readBoardFilter(),
                  structureScope: readScope()};
      if(!body.name){ toast('Укажите название кнопки'); return false; }
      if(missing){ toast('Выберите колонку Structure для показателя «' + missing + '»'); return false; }
      if(!structureId && JIRA_METRICS.some(function(m){ return fm[m[0]].structure; })){ toast('Колонки Structure можно выбрать только у кнопки со структурой'); return false; }
      if(!JIRA_METRICS.some(function(m){ return fm[m[0]].field || fm[m[0]].structure; })){ toast('Сопоставьте с полями Jira или колонками Structure хотя бы один показатель'); return false; }
      if(!mappings.length){ toast('Укажите задачи Jira хотя бы для одного эпика'); return false; }
      confirmOk.disabled = true;
      (button ? apiPut('/api/jira/buttons/'+button.id, body) : apiPost('/api/projects/'+currentProjectId+'/jira/buttons', body)).then(function(saved){
        if(button) replaceJiraButton(saved);
        else STATE.jira.buttons.push(saved);
        closeModal();
        renderJiraButtonsTable();
        renderJiraSnapshotButtons();
        toast(button ? 'Кнопка обновлена' : 'Кнопка создана');
      }).catch(function(){}).then(function(){ confirmOk.disabled = false; });
      return false;
    }
  });
  JIRA_METRICS.forEach(function(m){
    /* поле учёта времени Jira — всегда в секундах; своё числовое поле обычно ведут в часах */
    document.getElementById('jbField_'+m[0]).addEventListener('change', function(e){
      var opt = e.target.selectedOptions[0];
      if(opt && opt.value) document.getElementById('jbUnit_'+m[0]).value = opt.dataset.time === '1' ? 'seconds' : 'hours';
    });
    document.getElementById('jbSrc_'+m[0]).addEventListener('change', function(e){
      var structure = e.target.value === 'structure';
      document.getElementById('jbStr_'+m[0]).hidden = !structure;
      document.getElementById('jbFieldLabel_'+m[0]).textContent = structure ? 'Поле Jira для состава задач и истории' : 'Поле Jira';
      document.getElementById('jbField_'+m[0]).options[0].textContent = structure ? FIELD_FROM_STRUCTURE_LABEL : FIELD_EMPTY_LABEL;
      if(structure && !strSpecs[m[0]]) openStructureColumnPanel(m[0], m[1]);
    });
  });
  bindBoardFilter();
  bindScope(function(){ return editorStructureId(); });
  var strSpecs = {};
  JIRA_METRICS.forEach(function(m){
    var st = (fieldMap[m[0]] || {}).structure;
    if(!st) return;
    var editor = st.editor || editorFromAttribute(st.attribute);  /* формула из прежних версий — тоже в редакторе */
    strSpecs[m[0]] = {attribute: st.attribute, editor: editor, label: st.label || (editor ? describeEditor(editor) : describeAttribute(st.attribute)),
                      afterFiltering: !!st.afterFiltering};
  });
  var jiraFieldList = JIRA_TIME_FIELDS.slice();
  confirmBody.querySelectorAll('[data-str-edit]').forEach(function(btn){
    btn.addEventListener('click', function(){ var m = JIRA_METRICS.find(function(x){ return x[0] === btn.dataset.strEdit; }); openStructureColumnPanel(m[0], m[1]); });
  });
  function editorStructureId(){
    var v = document.getElementById('jbStructure').value;
    return v === '__manual' ? document.getElementById('jbStructureId').value.trim() : v;
  }
  function editorKeys(){
    var keys = [];
    document.querySelectorAll('#jbTable [data-field="keys"]').forEach(function(inp){ if(inp.value.trim() && keys.length < 5) keys.push(inp.value.trim()); });
    return keys.join(', ');
  }
  function openStructureColumnPanel(metric, title){
    var panel = document.getElementById('jbStrPanel');
    var cur = strSpecs[metric] || null;
    var ed = cur && cur.editor ? JSON.parse(JSON.stringify(cur.editor)) : null;
    var startMode = ed ? ed.mode : (cur ? 'copy' : 'field');
    var fieldValue = ed && ed.mode === 'field' ? (ed.variables ? ed.variables.value : {field: ed.field, format: ed.format}) : {field: 'timespent', format: 'duration'};
    var aggSum = ed ? ed.sum : true, aggDistinct = ed ? ed.distinct !== false : true;
    panel.hidden = false;
    panel.innerHTML = '<div class="jb-str-panel-head"><b>Колонка Structure для показателя «'+escapeHtml(title)+'»</b>'+
        '<div class="setting-hint">Задайте колонку так же, как она настроена в Structure (Edit column): тип, поле или формула и галочки суммы.</div></div>'+
      '<div class="seg-filter" role="radiogroup">'+
        [['field', 'Поле'], ['formula', 'Формула'], ['copy', 'Спецификация из Structure (JSON)']].map(function(x){
          return '<label class="checkbox-row"><input type="radio" name="jbStrMode" value="'+x[0]+'"'+(startMode === x[0] ? ' checked' : '')+'> '+x[1]+'</label>';
        }).join('')+'</div>'+
      '<div id="jbStrField"><div class="jb-str-var"><span class="field-label">Поле Jira</span><select id="jbStrFieldSel"></select><select id="jbStrFieldFmt">'+
        structureFormatOptions(fieldValue.format)+'</select></div></div>'+
      '<div id="jbStrFormula"><label class="small" for="jbStrFormulaText">Формула (как в колонке Structure)</label>'+
        '<textarea id="jbStrFormulaText" rows="6" placeholder="WITH rest = orig - spent : if(rest < 0, 0, rest)">'+escapeHtml(ed && ed.mode === 'formula' ? ed.formula : '')+'</textarea>'+
        '<div id="jbStrVars" class="jb-str-vars"></div></div>'+
      '<div id="jbStrAgg">'+
        '<label class="checkbox-row"><input type="checkbox" id="jbStrSum"'+(aggSum ? ' checked' : '')+'> Суммировать по вложенным <span class="muted-cell">(Sum over sub-items)</span></label>'+
        '<label class="checkbox-row"><input type="checkbox" id="jbStrDistinct"'+(aggDistinct ? ' checked' : '')+'> Исключать дубликаты <span class="muted-cell">(Exclude duplicates)</span></label></div>'+
      '<div id="jbStrFilterOpt"><label class="checkbox-row"><input type="checkbox" id="jbStrAfter"'+(cur && cur.afterFiltering ? ' checked' : '')+'> После фильтрации <span class="muted-cell">(After filtering)</span></label>'+
        '<div class="setting-hint">Отмечено — колонка считается с фильтром доски (задаётся у кнопки выше, «Фильтр доски Structure»): задачи, скрытые фильтром, в сумму не входят, как в Structure.</div></div>'+
      '<div id="jbStrCopy"><ol class="plan-list"><li>Откройте структуру в Jira с нужной колонкой, нажмите F12 → «Сеть» (Network) и обновите страницу.</li>'+
        '<li>В фильтре запросов введите <span class="mono">structure</span> и найдите запрос <span class="mono">value</span>; на вкладке «Полезные данные» (Payload) скопируйте его содержимое.</li>'+
        '<li>Вставьте ниже. Если такого запроса нет — задайте колонку как «Поле» или «Формула» с теми же настройками.</li></ol>'+
        '<textarea id="jbStrPaste" rows="4" placeholder="{&quot;requests&quot;:[{&quot;forestSpec&quot;:…,&quot;attributes&quot;:[…]}]}">'+(cur && !ed ? escapeHtml(JSON.stringify(cur.attribute)) : '')+'</textarea>'+
        '<select id="jbStrPick" hidden aria-label="Колонка"></select></div>'+
      '<div class="form-actions"><button type="button" class="btn btn-ghost btn-sm" id="jbStrCheck">Проверить на эпиках</button>'+
        '<button type="button" class="btn btn-ghost btn-sm" id="jbStrCancel">Отмена</button><button type="button" class="btn btn-primary btn-sm" id="jbStrApply">Применить</button></div>'+
      '<div id="jbStrPreview"></div>';
    var mode = function(){ return (panel.querySelector('input[name="jbStrMode"]:checked') || {}).value; };
    var showMode = function(){
      var m = mode();
      document.getElementById('jbStrField').hidden = m !== 'field';
      document.getElementById('jbStrFormula').hidden = m !== 'formula';
      document.getElementById('jbStrAgg').hidden = m === 'copy';
      document.getElementById('jbStrCopy').hidden = m !== 'copy';
    };
    panel.querySelectorAll('input[name="jbStrMode"]').forEach(function(r){ r.addEventListener('change', showMode); });
    showMode();
    var syncDistinct = function(){ document.getElementById('jbStrDistinct').disabled = !document.getElementById('jbStrSum').checked; };
    document.getElementById('jbStrSum').addEventListener('change', syncDistinct);
    syncDistinct();
    /* копия спецификации из Structure */
    var picked = [];
    var parsePaste = function(){
      var sel = document.getElementById('jbStrPick');
      var text = document.getElementById('jbStrPaste').value.trim();
      picked = [];
      try { picked = text ? extractAttributeSpecs(text) : []; } catch(e){ picked = []; }
      sel.hidden = picked.length < 2;
      sel.innerHTML = picked.map(function(spec, i){ return '<option value="'+i+'">'+escapeHtml(describeAttribute(spec))+'</option>'; }).join('');
    };
    document.getElementById('jbStrPaste').addEventListener('input', parsePaste);
    parsePaste();
    /* поле и переменные формулы: все поля Jira, включая статус и даты */
    var varState = ed && ed.mode === 'formula' ? JSON.parse(JSON.stringify(ed.variables)) : {};
    var fillFieldSelect = function(){
      document.getElementById('jbStrFieldSel').innerHTML = formulaFieldOptions(fieldValue.field);
    };
    var renderVars = function(){
      var names = formulaVariables(document.getElementById('jbStrFormulaText').value);
      document.getElementById('jbStrVars').innerHTML = names.length ? '<div class="field-label">Переменные формулы</div>' + names.map(function(n){
        var v = varState[n] || guessVariable(n);
        varState[n] = v;
        return '<div class="jb-str-var"><span class="mono">'+escapeHtml(n)+'</span><select data-var="'+escapeAttr(n)+'">'+formulaFieldOptions(v.field, true)+'</select>'+
          '<select data-var-format="'+escapeAttr(n)+'">'+structureFormatOptions(v.format)+'</select></div>';
      }).join('') : '';
      document.querySelectorAll('#jbStrVars [data-var]').forEach(function(el){
        el.addEventListener('change', function(){
          varState[el.dataset.var].field = el.value;
          var f = formulaFieldById(el.value);
          if(f){ varState[el.dataset.var].format = f.format; renderVars(); }
        });
      });
      document.querySelectorAll('#jbStrVars [data-var-format]').forEach(function(el){ el.addEventListener('change', function(){ varState[el.dataset.varFormat].format = el.value; }); });
    };
    document.getElementById('jbStrFieldSel').addEventListener('change', function(e){
      fieldValue.field = e.target.value;
      var f = formulaFieldById(e.target.value);
      if(f){ fieldValue.format = f.format; document.getElementById('jbStrFieldFmt').value = f.format; }
    });
    document.getElementById('jbStrFieldFmt').addEventListener('change', function(e){ fieldValue.format = e.target.value; });
    document.getElementById('jbStrFormulaText').addEventListener('input', renderVars);
    fillFieldSelect();
    renderVars();
    loadFormulaFields().then(function(){ fillFieldSelect(); renderVars(); });
    var buildSpec = function(){
      var m = mode();
      if(m === 'copy'){
        if(!picked.length){ toast('Вставьте запрос value из Structure или спецификацию колонки (JSON)'); return null; }
        var spec = picked[picked.length < 2 ? 0 : parseInt(document.getElementById('jbStrPick').value, 10)];
        return {attribute: spec, editor: null, label: describeAttribute(spec), afterFiltering: document.getElementById('jbStrAfter').checked};
      }
      var editor = {mode: m, sum: document.getElementById('jbStrSum').checked, distinct: document.getElementById('jbStrDistinct').checked};
      if(m === 'field'){
        if(!fieldValue.field){ toast('Выберите поле Jira'); return null; }
        editor.field = fieldValue.field;
        editor.format = fieldValue.format;
        /* длительность Structure отдаёт в миллисекундах */
        var unitSel = document.getElementById('jbStrUnit_'+metric);
        if(fieldValue.format === 'duration' && unitSel.value === 'hours') unitSel.value = 'ms';
      } else {
        var formula = document.getElementById('jbStrFormulaText').value.trim();
        if(!formula){ toast('Введите формулу'); return null; }
        var vars = {}, bad = null;
        formulaVariables(formula).forEach(function(n){ var v = varState[n]; if(!v || !v.field) bad = bad || n; else vars[n] = {field: v.field, format: v.format}; });
        if(bad){ toast('Выберите поле Jira для переменной «' + bad + '»'); return null; }
        editor.formula = formula;
        editor.variables = vars;
      }
      var after = document.getElementById('jbStrAfter').checked;
      return {attribute: null, editor: editor, label: describeEditor(editor) + (after ? ', после фильтрации' : ''), afterFiltering: after};
    };
    document.getElementById('jbStrCheck').addEventListener('click', function(){
      var spec = buildSpec();
      if(!spec) return;
      var box = document.getElementById('jbStrPreview');
      var unit = document.getElementById('jbStrUnit_'+metric).value;
      box.innerHTML = '<p class="insight-empty">Запрашиваем значения у Structure…</p>';
      var body = {structureId: editorStructureId(), unit: unit, keys: editorKeys(), afterFiltering: spec.afterFiltering, boardFilter: readBoardFilter(),
                  structureScope: readScope()};
      if(spec.editor) body.editor = spec.editor; else body.attribute = spec.attribute;
      apiPost('/api/projects/'+currentProjectId+'/jira/structure-preview', body).then(function(r){
        var render = function(){
          var u = document.getElementById('jbStrUnit_'+metric).value, k = STRUCTURE_UNIT_HOURS[u];
          var hasVariants = r.values.some(function(v){ return v.variants; });
          var hasFiltered = r.values.some(function(v){ return v.variants && 'filtered' in v.variants; });
          var variantNames = ['own', 'distinct', 'all'].concat(hasFiltered ? ['filtered'] : []);
          var hours = function(x){ return x === null || x === undefined ? '—' : fmtNum(Math.round(x * k * 100) / 100) + ' ч'; };
          box.innerHTML = (r.values.length ? '<div class="table-scroll"><table class="data-table compact-table jb-str-check"><thead><tr><th>Задача</th><th class="num">Значение в Structure</th>'+
              '<th class="num">Часов</th>'+(hasVariants ? '<th class="num">Только задача</th><th class="num">Σ без дубликатов</th><th class="num">Σ с дубликатами</th>' +
                (hasFiltered ? '<th class="num">С фильтром доски</th>' : '') : '')+
              '</tr></thead><tbody>'+
            r.values.map(function(v){
              var h = v.asUnits[u];
              return '<tr><td class="mono nowrap">'+escapeHtml(v.key)+'</td><td class="num">'+escapeHtml(v.raw === null || v.raw === undefined ? '—' : String(v.raw))+'</td>'+
                '<td class="num jb-unit-current">'+(h === null ? '—' : fmtNum(h) + ' ч')+'</td>'+
                (hasVariants ? variantNames.map(function(n){ return '<td class="num">'+hours((v.variants || {})[n])+'</td>'; }).join('') : '')+'</tr>';
            }).join('')+'</tbody></table></div>'+
            '<div class="jb-unit-switch"><span class="bulk-note">Не совпадает с колонкой в структуре? Значение в:</span>'+STRUCTURE_UNITS.map(function(x){
              return '<button type="button" class="btn btn-ghost btn-sm'+(x[0] === u ? ' active' : '')+'" data-unit="'+x[0]+'">'+x[1]+'</button>';
            }).join('')+'</div>'+
            (hasVariants ? '<p class="setting-hint">Сверка: сравните с колонкой в Structure. Совпало с «Σ с дубликатами» — снимите «Исключать дубликаты» (или отметьте его в Structure); совпало с «С фильтром доски» — отметьте «После фильтрации»; на доске меньше всех вариантов — укажите у кнопки тот же фильтр, что включён на доске.</p>' : '') : '')+
            (r.warnings.length ? '<ul class="plan-list">'+r.warnings.map(function(w){ return '<li>'+escapeHtml(w)+'</li>'; }).join('')+'</ul>' : '')+
            placesHtml(r.values, hours)+varsHtml(r.values, spec.editor, k)+
            (r.attribute ? '<details class="jb-str-spec"><summary>Что отправляется в Structure</summary><pre class="mono">'+escapeHtml(JSON.stringify(r.attribute, null, 2))+'</pre></details>' : '')+
            '<div class="form-actions jb-diag"><button type="button" class="btn btn-ghost btn-sm" data-diag>Скачать диагностику</button>'+
            '<span class="setting-hint">файл со всеми значениями и запросом к Structure — пришлите его, если цифры не сходятся</span></div>';
          box.querySelector('[data-diag]').addEventListener('click', function(){
            downloadJson('burndown-structure-diagnostics.json', {generatedAt: new Date().toISOString(), version: APP_VERSION_TEXT(), request: body, response: r});
          });
          box.querySelectorAll('[data-unit]').forEach(function(b){
            b.addEventListener('click', function(){ document.getElementById('jbStrUnit_'+metric).value = b.dataset.unit; render(); });
          });
        };
        render();
      }).catch(function(){ box.innerHTML = ''; });
    });
    document.getElementById('jbStrCancel').addEventListener('click', function(){
      panel.hidden = true;
      if(!strSpecs[metric]){ document.getElementById('jbSrc_'+metric).value = 'jira'; document.getElementById('jbSrc_'+metric).dispatchEvent(new Event('change')); }
    });
    document.getElementById('jbStrApply').addEventListener('click', function(){
      var spec = buildSpec();
      if(!spec) return;
      strSpecs[metric] = spec;
      document.getElementById('jbStrLabel_'+metric).textContent = spec.label;
      panel.hidden = true;
      toast('Колонка выбрана — сохраните кнопку');
    });
  }
  document.getElementById('jbLoadFields').addEventListener('click', function(){
    var btn = this;
    btn.disabled = true;
    btn.textContent = 'Загрузка…';
    api('/api/projects/'+currentProjectId+'/jira/fields').then(function(list){
      jiraFieldList = list;
      JIRA_METRICS.forEach(function(m){
        var sel = document.getElementById('jbField_'+m[0]);
        sel.innerHTML = fieldOptions(list, sel.value, document.getElementById('jbSrc_'+m[0]).value === 'structure' ? FIELD_FROM_STRUCTURE_LABEL : null);
      });
      toast('Числовых полей загружено: ' + list.length);
    }).catch(function(){}).then(function(){ btn.disabled = false; btn.textContent = 'Загрузить поля из Jira'; });
  });
  var sel = document.getElementById('jbStructure');
  var manual = document.getElementById('jbStructureId');
  sel.addEventListener('change', function(){ manual.hidden = sel.value !== '__manual'; if(!manual.hidden) manual.focus(); });
  document.getElementById('jbLoadStructures').addEventListener('click', function(){
    var btn = this;
    btn.disabled = true;
    btn.textContent = 'Загрузка…';
    var current = sel.value && sel.value !== '__manual' ? parseInt(sel.value, 10) : null;
    api('/api/projects/'+currentProjectId+'/jira/structures').then(function(list){
      sel.innerHTML = structureOptions(list, current);
      toast('Структур загружено: ' + list.length);
    }).catch(function(){}).then(function(){ btn.disabled = false; btn.textContent = 'Загрузить из Jira'; });
  });
}


/* ════════════════════════════════════════════════════════════════
   Задачи Jira: состав эпиков, burn-up и качество данных
════════════════════════════════════════════════════════════════ */
var CATEGORY_LABELS = {done: 'Готово', indeterminate: 'В работе', 'new': 'Не начато'};
var CATEGORY_ORDER = ['done', 'indeterminate', 'new'];
var CATEGORY_BADGE = {done: 'badge-ok', indeterminate: 'badge-info', 'new': 'badge-muted'};

function loadJiraComposition(){
  STATE.jiraComp = null;
  if(!currentProjectId) return Promise.resolve();
  var pid = currentProjectId;
  return api('/api/projects/'+pid+'/jira/composition').then(function(data){
    if(pid !== currentProjectId) return;
    STATE.jiraComp = data;
    if(currentView === 'dashboard'){ renderJiraComposition(); renderBurnupChart(); }
    if(currentView === 'stages') renderStageJira();
  }).catch(function(){});
}

function compositionOf(sid){
  var c = STATE.jiraComp;
  return c && c.latest ? c.latest[sid] : null;
}

/* Задач с неполными данными — задачи, а не виды проблем: у одной задачи их может быть несколько */
function problemCount(latest){
  return latest.problemIssues || 0;
}

function compositionBar(latest){
  var total = latest.total || 0;
  var title = CATEGORY_ORDER.map(function(c){ return CATEGORY_LABELS[c] + ': ' + latest.counts[c]; }).join(' · ');
  if(!total) return '<div class="comp-bar empty" title="Задач нет — только контейнеры"></div>';
  return '<div class="comp-bar" title="'+escapeAttr(title)+'" role="img" aria-label="'+escapeAttr(title)+'">' +
    CATEGORY_ORDER.map(function(c){
      var n = latest.counts[c];
      return n ? '<span class="comp-seg '+c+'" style="flex-grow:'+n+'"></span>' : '';
    }).join('') + '</div>';
}

/* ── Прогноз по burn-up: где сойдутся линии «Выполнено» и «Объём» ──
   Темпы выполнения и роста объёма — наклоны линейной регрессии по срезам задач (с учётом метода burn rate:
   при скользящем среднем — только срезы окна). Если объём растёт не медленнее выполнения, линии не сходятся. */
function burnupForecast(hist){
  if(!hist || hist.length < 2) return null;
  var pts = hist;
  if(STATE.settings && STATE.settings.burnrateMethod === 'rolling'){
    var cutoff = addDays(hist[hist.length-1].date, -(STATE.settings.burnrateWindowDays || 14));
    var windowed = hist.filter(function(p){ return p.date >= cutoff; });
    pts = windowed.length >= 2 ? windowed : hist.slice(-2);
  }
  var base = pts[0].date, cal = workCalendar();
  if(cal.span(base, pts[pts.length-1].date) <= 0) return null;
  var slope = function(f){
    var reg = linearForecast(pts.map(function(p){ return {x: cal.span(base, p.date), y: p[f]}; }));
    return reg ? reg.slope : null;
  };
  var doneRate = slope('done'), scopeRate = slope('scope');
  if(doneRate === null || scopeRate === null) return null;
  var last = hist[hist.length-1];
  var res = {doneRate: doneRate, scopeRate: scopeRate, last: last, converges: false};
  var gap = last.scope - last.done;
  if(gap <= 0.005) return Object.assign(res, {converges: true, date: last.date, finish: last.scope, days: 0});
  var speed = doneRate - scopeRate;
  if(speed <= 0) return res;
  var days = gap / speed;
  if(days > cal.horizon) return Object.assign(res, {tooFar: true});
  return Object.assign(res, {converges: true, date: cal.shift(last.date, Math.ceil(days)), finish: last.scope + scopeRate * days, days: days});
}
function burnupRatesText(f){
  return 'выполнение ' + fmtWorkNum(f.doneRate) + ' ' + rateUnit() + ', рост объёма ' + fmtWorkNum(f.scopeRate) + ' ' + rateUnit();
}
function burnupForecastText(f){
  if(!f) return 'Для прогноза по задачам нужны срезы минимум в два разных дня.';
  if(f.converges) return 'Прогноз по задачам Jira: <b>' + fmtDate(f.date) + '</b> — ' + burnupRatesText(f) + '.';
  if(f.tooFar) return 'При текущем темпе (' + burnupRatesText(f) + ') работа займёт больше трёх лет.';
  return '<span class="prob-low">Объём растёт не медленнее, чем выполняется (' + burnupRatesText(f) + ') — при таком темпе работа не завершится.</span>';
}
function burnupCell(f, target){
  if(!f) return '<td class="nowrap"><span class="muted-cell" title="Нужны срезы минимум в два разных дня">—</span></td>';
  var title = escapeAttr(burnupRatesText(f).replace(/^./, function(c){ return c.toUpperCase(); }));
  if(!f.converges) return '<td class="nowrap"><span class="prob-low" title="'+title+'">'+(f.tooFar ? '> 3 лет' : 'не сходится')+'</span></td>';
  var cls = target ? (f.date <= target ? 'prob-high' : 'prob-low') : '';
  return '<td class="mono nowrap"><span class="'+cls+'" title="'+title+'">'+fmtDate(f.date)+'</span></td>';
}

/* Состав по группе эпиков (этап, проект): на каждую дату среза — сумма последних известных срезов эпиков */
function aggregateComposition(sids){
  var c = STATE.jiraComp;
  if(!c || !c.history) return null;
  var hists = sids.map(function(id){ return c.history[id] || []; }).filter(function(h){ return h.length; });
  if(!hists.length) return null;
  var dateSet = {};
  hists.forEach(function(h){ h.forEach(function(p){ dateSet[p.date] = true; }); });
  var pos = hists.map(function(){ return 0; }), cur = hists.map(function(){ return null; });
  var round = function(v){ return Math.round(v * 100) / 100; };
  var history = Object.keys(dateSet).sort().map(function(date){
    var agg = {date: date, scope: 0, done: 0, total: 0, doneCount: 0, measured: 0, counts: {}, volume: {}};
    CATEGORY_ORDER.forEach(function(k){ agg.counts[k] = 0; agg.volume[k] = 0; });
    hists.forEach(function(h, i){
      while(pos[i] < h.length && h[pos[i]].date <= date) cur[i] = h[pos[i]++];
      var p = cur[i] || h[0];  /* до первого среза эпика — его первый срез: без ложного «роста объёма» в итоге */
      agg.scope += p.scope; agg.done += p.done; agg.total += p.total; agg.doneCount += p.doneCount;
      agg.measured += p.measured === undefined ? 1 : p.measured;
      CATEGORY_ORDER.forEach(function(k){ agg.counts[k] += p.counts[k]; agg.volume[k] += p.volume[k]; });
    });
    agg.scope = round(agg.scope); agg.done = round(agg.done);
    CATEGORY_ORDER.forEach(function(k){ agg.volume[k] = round(agg.volume[k]); });
    return agg;
  });
  var problems = {}, problemIssues = 0;
  sids.forEach(function(id){
    var l = c.latest[id];
    if(!l) return;
    Object.keys(l.problems || {}).forEach(function(k){ problems[k] = (problems[k] || 0) + l.problems[k]; });
    problemIssues += l.problemIssues || 0;
  });
  return {history: history, latest: Object.assign({}, history[history.length-1], {problems: problems, problemIssues: problemIssues})};
}

/* Строка таблицы состава: эпик, этап или итог по проекту */
/* Срезы, где у задач были часы: срез без часов (до настройки показателей) — не «нулевой объём», а нет данных;
   по нему не считаются ни Δ объёма, ни burn-up */
function measuredHistory(hist){
  var measured = (hist || []).filter(function(p){ return p.measured !== 0; });
  return measured.length ? measured : (hist || []);
}
function compositionRowHtml(o){
  var l = o.latest, hist = measuredHistory(o.history);
  var labels = (STATE.jiraComp && STATE.jiraComp.problemLabels) || {};
  var pct = l.scope > 0 ? Math.round(l.done / l.scope * 100) : null;
  var probs = problemCount(l);
  var probTitle = Object.keys(l.problems || {}).map(function(k){ return (labels[k] || k) + ': ' + l.problems[k]; }).join('\n');
  var delta = '<span class="muted-cell">—</span>';
  var prev = hist.length >= 2 && hist[hist.length - 1].date === l.date ? hist[hist.length - 2] : null;
  if(prev && l.measured !== 0){
    var d = Math.round((l.scope - prev.scope) * 100) / 100;
    var cls = d > 0 ? 'delta-up' : (d < 0 ? 'delta-down' : 'muted-cell');
    var text = signedWork(d);
    delta = o.changesSid ? '<button type="button" class="link-btn '+cls+'" data-changes="'+o.changesSid+'" title="Что изменилось с '+fmtDate(prev.date)+'">'+text+'</button>' :
      '<span class="'+cls+'" title="С '+fmtDate(prev.date)+'">'+text+'</span>';
  }
  return '<tr class="'+(o.cls || '')+'"'+(o.attrs || '')+'>'+
    '<td>'+escapeHtml(o.label)+'</td><td class="comp-col">'+compositionBar(l)+'</td>'+
    '<td class="num">'+(pct === null ? '—' : pct + '%')+'</td>'+
    '<td class="num">'+l.doneCount+' / '+l.total+'</td>'+
    '<td class="num">'+hoursToStr(l.scope)+'</td>'+
    '<td class="num nowrap">'+delta+'</td>'+
    burnupCell(burnupForecast(hist), o.target)+
    '<td class="nowrap" data-v="'+(probs ? 'есть проблемы' : 'в порядке')+'">'+(probs ? '<span class="badge badge-risk" title="'+escapeAttr(probTitle)+'">⚠ '+probs+'</span>' : '<span class="badge badge-ok">✓ в порядке</span>')+'</td>'+
    '<td class="mono nowrap">'+fmtDate(l.date)+'</td></tr>';
}
var COMPOSITION_FILTER = ['text', null, 'num', 'num', 'num', 'num', 'text', 'list', 'text'];
var COMPOSITION_HEAD = '<th class="comp-col">Состав задач</th><th class="num">Выполнено объёма</th><th class="num">Задач готово</th>'+
  '<th class="num">Объём, '+unitSuffix()+'</th><th class="num" title="Изменение объёма с прошлого среза">Δ объёма</th>'+
  '<th title="Дата, когда «Выполнено» догонит «Объём» при текущих темпах (burn-up). Зелёным — не позже цели">Прогноз по задачам</th>'+
  '<th>Данные</th><th>Срез</th>';

function renderJiraComposition(){
  var panel = document.getElementById('jiraCompPanel');
  var rows = visibleDashboardServices().filter(function(s){ return compositionOf(s.id); });
  panel.hidden = !rows.length;
  if(!rows.length) return;
  var table = document.getElementById('jiraCompTable');
  var hist = STATE.jiraComp.history;
  var total = rows.length > 1 ? aggregateComposition(rows.map(function(s){ return s.id; })) : null;
  table.innerHTML = '<thead><tr><th>Эпик</th>'+COMPOSITION_HEAD+'</tr></thead><tbody>' +
    rows.map(function(s){
      return compositionRowHtml({label: s.name, latest: compositionOf(s.id), history: hist[s.id], target: s.targetDate,
        changesSid: s.id, cls: 'clickable' + (s.isDone ? ' done-row' : ''), attrs: ' data-sid="'+s.id+'" tabindex="0"'});
    }).join('') +
    (total ? compositionRowHtml({label: 'Итого по проекту', latest: total.latest, history: total.history, cls: 'total-row'}) : '') +
    '</tbody>';
  tableFilter(table, COMPOSITION_FILTER);
  table.querySelectorAll('[data-changes]').forEach(function(btn){
    btn.addEventListener('click', function(e){
      e.stopPropagation();
      openJiraChanges(parseInt(btn.dataset.changes, 10));
    });
  });
  table.querySelectorAll('tr[data-sid]').forEach(function(tr){
    var open = function(){ openJiraIssues(parseInt(tr.dataset.sid, 10)); };
    tr.addEventListener('click', open);
    tr.addEventListener('keydown', function(e){ if(e.key === 'Enter'){ e.preventDefault(); open(); } });
  });
}

/* Срезы задач по датам: найти и удалить ошибочные (неверные единицы, срез без часов) — сравнение, burn-up и
   «что изменилось» пересчитываются по оставшимся срезам */
function openJiraSnapshots(){
  var pid = currentProjectId;
  api('/api/projects/'+pid+'/jira/snapshots').then(function(list){
    var rows = list.map(function(x){
      var flags = (x.suspicious ? '<div class="text-danger">⚠ часы в задачах во много раз больше, чем в остальных срезах — похоже на ошибку единиц</div>' : '')+
        (x.noHours ? '<div class="muted-cell">у задач нет часов — показатели не были настроены</div>' : '')+
        (x.restored ? '<div class="muted-cell">восстановлен по журналу Jira</div>' : '');
      return '<tr><td class="center"><input type="checkbox" data-snap="'+x.date+'"'+(x.suspicious || x.noHours ? ' checked' : '')+' aria-label="Срез '+fmtDate(x.date)+'"></td>'+
        '<td class="mono nowrap">'+fmtDate(x.date)+'</td><td class="num">'+x.epics+'</td><td class="num">'+x.issues+'</td>'+
        '<td class="num">'+x.measured+'</td><td class="num">'+hoursToStr(x.volume)+'</td><td class="num">'+x.entries+'</td><td>'+flags+'</td></tr>';
    }).join('');
    openModal({
      title: 'Срезы задач Jira', size: 'xl', okLabel: 'Удалить выбранные', danger: true,
      html: '<p class="bulk-intro">Каждый срез сохраняет состав эпиков по задачам на дату. Если срез сделан с ошибкой — например, '+
          'с неверными единицами или до того, как у показателей появились часы, — удалите его: «Δ объёма», burn-up, прогноз по задачам '+
          'и «Что изменилось» пересчитаются по оставшимся срезам. Срез за сегодня можно просто повторить кнопкой среза.</p>'+
        (list.length ? '<div class="table-scroll jira-issues-scroll"><table class="data-table compact-table" id="snapTable"><thead><tr><th class="center">'+
          '<input type="checkbox" id="snapAll" aria-label="Выбрать все"></th><th>Дата</th><th class="num">Эпиков</th><th class="num">Задач</th>'+
          '<th class="num" title="Задач, у которых в срезе есть остаток, затрачено или оценка">С часами</th><th class="num">Объём</th>'+
          '<th class="num" title="Замеров эпиков из этого среза на эту дату">Замеров</th><th>Признаки</th></tr></thead><tbody>'+rows+'</tbody></table></div>'+
          '<label class="checkbox-row"><input type="checkbox" id="snapEntries"> Удалить и замеры эпиков за эти даты (остаток, затрачено, оценка), '+
          'записанные такими срезами — если в них попали те же ошибочные значения</label>' :
          '<p class="insight-empty">Срезов задач пока нет.</p>'),
      onOk: function(){
        var dates = Array.prototype.map.call(document.querySelectorAll('#snapTable [data-snap]:checked'), function(el){ return el.dataset.snap; });
        if(!dates.length){ toast('Отметьте срезы, которые нужно удалить'); return false; }
        var withEntries = document.getElementById('snapEntries').checked;
        apiPost('/api/projects/'+pid+'/jira/snapshots/delete', {dates: dates, measurements: withEntries}).then(function(r){
          closeModal();
          toast('Удалено срезов: ' + r.dates.length + (r.entries ? ', замеров: ' + r.entries : ''));
          invalidateInsights();
          if(pid === currentProjectId) loadProjectData(pid, currentView);
        }).catch(function(){});
        return false;
      }
    });
    var all = document.getElementById('snapAll');
    if(all) all.addEventListener('change', function(){
      document.querySelectorAll('#snapTable [data-snap]').forEach(function(el){ el.checked = all.checked; });
    });
  }).catch(function(){});
}
document.getElementById('jiraSnapshotsBtn').addEventListener('click', openJiraSnapshots);

/* Burn-up с прогнозом: факт по срезам и пунктир до точки, где «Выполнено» догонит «Объём» */
function drawBurnup(key, canvasId, legendId, noteId, hist){
  destroyChart(key);
  hist = measuredHistory(hist);
  var single = hist.length === 1;
  var f = burnupForecast(hist);
  var datasets = [
    {label: 'Объём', data: hist.map(function(p){ return {x: p.date, y: p.scope, total: p.total}; }),
     borderColor: getCss('--color-blue'), backgroundColor: 'transparent', borderWidth: 2, pointRadius: single ? 4 : 2, tension: 0},
    {label: 'Выполнено', data: hist.map(function(p){ return {x: p.date, y: p.done, count: p.doneCount, total: p.total}; }),
     borderColor: getCss('--color-success'), backgroundColor: getCss('--color-success') + '26', fill: 'origin', borderWidth: 2,
     pointRadius: single ? 4 : 2, tension: 0}
  ];
  if(f && f.converges && f.days > 0){
    var end = {x: f.date, y: f.finish};
    datasets.push({label: 'Прогноз: ' + fmtDate(f.date), data: [{x: f.last.date, y: f.last.scope}, end],
                   borderColor: getCss('--color-blue'), borderDash: [6, 4], borderWidth: 1.5, pointRadius: [0, 4], tension: 0, isForecast: true});
    datasets.push({label: '', data: [{x: f.last.date, y: f.last.done}, end], borderColor: getCss('--color-success'),
                   borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0, tension: 0, hideInLegend: true, isForecast: true});
  }
  renderLegend(legendId, datasets);
  document.getElementById(noteId).innerHTML = burnupForecastText(f);
  var opts = baseChartOptions(unitAxisTitle());
  opts.plugins.tooltip.callbacks.afterLabel = function(ctx){
    if(ctx.dataset.isForecast) return '';
    var raw = ctx.raw || {};
    return raw.count !== undefined ? 'Задач готово: ' + raw.count + ' из ' + raw.total : 'Задач: ' + raw.total;
  };
  charts[key] = makeChart(document.getElementById(canvasId), {type: 'line', data: {datasets: datasets}, options: opts});
}

function renderBurnupChart(){
  var panel = document.getElementById('jiraBurnupPanel');
  var sid = parseInt(document.getElementById('chartServiceSelect').value, 10);
  var hist = STATE.jiraComp && STATE.jiraComp.history ? STATE.jiraComp.history[sid] : null;
  panel.hidden = !(hist && hist.length);
  if(panel.hidden){ destroyChart('epicBurnup'); return; }
  drawBurnup('epicBurnup', 'chartBurnup', 'legendBurnup', 'burnupNote', hist);
}

/* ── Дрейф прогноза: какой была прогнозная дата завершения на каждую дату замера ── */
var DRIFT_MAX_POINTS = 90;
function forecastDrift(sid){
  var svc = serviceById(sid);
  var rows = entriesForService(sid).filter(function(r){ return isNum(r.remaining); });
  if(rows.length < 2) return null;
  var target = svc ? svc.targetDate || null : null;
  var points = [];
  /* при длинной истории — не больше DRIFT_MAX_POINTS дат (равномерно), последние две — всегда: прогноз
     пересчитывается на каждую дату, и при ежедневных замерах за год это сотни расчётов Монте-Карло */
  var idx = [];
  for(var i = 1; i < rows.length; i++) idx.push(i);
  if(idx.length > DRIFT_MAX_POINTS){
    var step = (rows.length - 3) / (DRIFT_MAX_POINTS - 2), picked = {};
    idx = [];
    for(var k = 0; k < DRIFT_MAX_POINTS - 2; k++) picked[1 + Math.round(k * step)] = true;
    picked[rows.length - 2] = true; picked[rows.length - 1] = true;
    idx = Object.keys(picked).map(Number).sort(function(a, b){ return a - b; });
  }
  idx.forEach(function(i){
    var sub = rows.slice(0, i + 1);
    var st = computeStatus(sub.map(function(r){ return r.date; }), sub.map(function(r){ return r.remaining; }), target);
    points.push({date: rows[i].date, forecast: st.forecastZeroDate, noBurn: st.noBurn,
                 lo: st.mc ? st.mc.lo : null, p50: st.mc ? st.mc.p50 : null, hi: st.mc ? st.mc.hi : null});
  });
  return {points: points, target: target};
}
function driftShiftText(from, to, since){
  var d = daysBetween(from, to);
  if(!d) return 'не изменился' + (since ? ' ' + since : '');
  return '<span class="'+(d > 0 ? 'delta-up' : 'delta-down')+'">'+(d > 0 ? 'позже на ' : 'раньше на ')+Math.abs(d)+' дн</span>'+(since ? ' ' + since : '');
}
function driftSummary(drift){
  var key = function(p){ return p.p50 || p.forecast; };
  var known = drift.points.filter(key);
  if(!known.length) return 'Остаток не сгорает — прогнозной даты нет ни на одну дату замера.';
  var last = drift.points[drift.points.length-1];
  if(!key(last)) return 'На последний замер прогноза нет: остаток не сгорает.';
  var parts = ['Прогноз завершения на последний замер: <b>' + fmtDate(key(last)) + '</b>' + (last.p50 ? ' (медиана)' : '') + '.'];
  var prev = drift.points.length > 1 ? drift.points[drift.points.length-2] : null;
  if(prev && key(prev)) parts.push('С прошлого замера (' + fmtDate(prev.date) + ') — ' + driftShiftText(key(prev), key(last), '') + '.');
  if(known.length > 2 && known[0] !== prev) parts.push('С первого прогноза (' + fmtDate(known[0].date) + ') — ' + driftShiftText(key(known[0]), key(last), '') + '.');
  return parts.join(' ');
}

function renderDriftChart(){
  destroyChart('epicDrift');
  var panel = document.getElementById('driftPanel');
  var sid = parseInt(document.getElementById('chartServiceSelect').value, 10);
  var drift = serviceById(sid) ? forecastDrift(sid) : null;
  panel.hidden = !drift;
  if(!drift) return;
  var pts = drift.points;
  /* очень далёкие прогнозы (не сгорает почти никогда) не сплющивают график — обрезаются через год после цели */
  var lastDate = pts[pts.length-1].date;
  var cap = addDays(drift.target && drift.target > lastDate ? drift.target : lastDate, 365);
  var v = function(d){ return d && d <= cap ? d : null; };
  var color = getCss('--color-orange');
  var datasets = [
    {label: 'Прогноз «0» (' + (((STATE.settings || {}).forecastMethod === 'average') ? 'средняя скорость' : 'линейн.') + ')',
     data: pts.map(function(p){ return {x: p.date, y: v(p.forecast)}; }), borderColor: color, borderDash: [3, 3], borderWidth: 2, pointRadius: 3, tension: 0},
    {label: 'Медиана (P50)', data: pts.map(function(p){ return {x: p.date, y: v(p.p50)}; }),
     borderColor: getCss('--color-primary'), borderWidth: 2.5, pointRadius: 4, tension: 0},
    {label: 'Диапазон ' + rangeConfidence() + '%', data: pts.map(function(p){ return {x: p.date, y: v(p.lo)}; }),
     borderColor: color + '55', backgroundColor: color + '1f', borderWidth: 1, pointRadius: 0, tension: 0, isRange: true},
    {label: '', data: pts.map(function(p){ return {x: p.date, y: v(p.hi)}; }), borderColor: color + '55', backgroundColor: color + '1f',
     borderWidth: 1, pointRadius: 0, tension: 0, fill: '-1', isRange: true, hideInLegend: true}
  ];
  if(drift.target){
    datasets.push({label: 'Цель: ' + fmtDate(drift.target), data: [{x: pts[0].date, y: drift.target}, {x: lastDate, y: drift.target}],
                   borderColor: getCss('--color-blue'), borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0, tension: 0});
  }
  renderLegend('legendDrift', datasets);
  document.getElementById('driftNote').innerHTML = driftSummary(drift);
  var grid = getCss('--color-divider'), muted = getCss('--color-text-muted');
  /* ось прогнозов — с запасом, чтобы линия цели и крайние прогнозы не прижимались к краю */
  var ys = [];
  datasets.forEach(function(d){ d.data.forEach(function(p){ if(p.y) ys.push(p.y); }); });
  ys.sort();
  var yMin = ys.length ? addDays(ys[0], -3) : undefined, yMax = ys.length ? addDays(ys[ys.length-1], 3) : undefined;
  charts.epicDrift = makeChart(document.getElementById('chartDrift'), {type: 'line', data: {datasets: datasets}, options: {
    responsive: true, maintainAspectRatio: false, spanGaps: false,
    interaction: {mode: 'nearest', axis: 'x', intersect: false},
    plugins: {legend: {display: false}, tooltip: {
      filter: function(item){ return item.raw && item.raw.y; },
      callbacks: {
        title: function(items){ return items.length ? 'На замер ' + fmtDate(items[0].raw.x) : ''; },
        label: function(ctx){
          if(ctx.dataset.isRange) return (ctx.dataset.hideInLegend ? 'Пессимистично' : 'Оптимистично') + ': ' + fmtDate(ctx.raw.y);
          return (ctx.dataset.label || '').replace(/: .*/, '') + ': ' + fmtDate(ctx.raw.y);
        }
      }}},
    scales: {
      x: {type: 'time', time: {unit: 'day', tooltipFormat: 'dd.MM.yyyy', displayFormats: {day: 'dd.MM'}}, grid: {color: grid}, ticks: {color: muted},
          title: {display: true, text: 'Дата замера', color: muted}},
      y: {type: 'time', min: yMin, max: yMax, time: {unit: 'week', displayFormats: {week: 'dd.MM.yy', day: 'dd.MM.yy'}}, grid: {color: grid}, ticks: {color: muted},
          title: {display: true, text: 'Прогноз завершения', color: muted}}
    }
  }});
}

/* ── Задачи Jira по этапам и по проекту (свод по этапам) ── */
function renderStageJira(){
  var panel = document.getElementById('stageJiraPanel');
  var burnPanel = document.getElementById('stageBurnupPanel');
  var groups = computeStageGroups();
  var names = Object.keys(groups).filter(function(n){ return aggregateComposition(groups[n]); });
  panel.hidden = burnPanel.hidden = !names.length;
  if(!names.length){ destroyChart('stageBurnup'); return; }
  var allIds = [];
  names.forEach(function(n){ allIds = allIds.concat(groups[n]); });
  var aggs = {};
  names.forEach(function(n){ aggs[n] = aggregateComposition(groups[n]); });
  var total = aggregateComposition(allIds);
  var table = document.getElementById('stageJiraTable');
  table.innerHTML = '<thead><tr><th>Этап</th>'+COMPOSITION_HEAD+'</tr></thead><tbody>' +
    names.map(function(n){
      return compositionRowHtml({label: n, latest: aggs[n].latest, history: aggs[n].history, target: STATE.stageTargets[n],
        cls: 'clickable', attrs: ' data-stage="'+escapeAttr(n)+'" tabindex="0"'});
    }).join('') +
    compositionRowHtml({label: 'Итого по проекту', latest: total.latest, history: total.history, cls: 'total-row'}) + '</tbody>';
  tableFilter(table, COMPOSITION_FILTER);
  var sel = document.getElementById('stageBurnupSelect');
  var current = sel.value;
  sel.innerHTML = names.map(function(n){ return '<option value="'+escapeAttr(n)+'">'+escapeHtml(n)+'</option>'; }).join('') +
    '<option value="__project">Весь проект</option>';
  sel.value = current && (current === '__project' || names.indexOf(current) !== -1) ? current : names[0];
  var draw = function(){
    var agg = sel.value === '__project' ? total : aggs[sel.value];
    drawBurnup('stageBurnup', 'chartStageBurnup', 'legendStageBurnup', 'stageBurnupNote', agg.history);
  };
  sel.onchange = draw;
  table.querySelectorAll('tr[data-stage]').forEach(function(tr){
    var pick = function(){ sel.value = tr.dataset.stage; draw(); burnPanel.scrollIntoView({behavior: 'smooth', block: 'nearest'}); };
    tr.addEventListener('click', pick);
    tr.addEventListener('keydown', function(e){ if(e.key === 'Enter'){ e.preventDefault(); pick(); } });
  });
  draw();
}

document.getElementById('jiraBurnupIssues').addEventListener('click', function(){
  openJiraIssues(parseInt(document.getElementById('chartServiceSelect').value, 10));
});
document.getElementById('jiraBurnupChanges').addEventListener('click', function(){
  openJiraChanges(parseInt(document.getElementById('chartServiceSelect').value, 10));
});

function jiraIssueLink(baseUrl, key){
  if(!baseUrl) return '<span class="mono">'+escapeHtml(key)+'</span>';
  return '<a class="mono" href="'+escapeAttr(baseUrl + '/browse/' + encodeURIComponent(key))+'" target="_blank" rel="noopener">'+escapeHtml(key)+'</a>';
}
function signedHours(v){ return signedWork(v); }

/* Что изменилось в задачах эпика между двумя срезами: каскад объёма и списки задач */
var CHANGE_GROUPS = [
  ['added', 'Новые задачи', 'Добавлены в эпик за период'],
  ['removed', 'Убраны из эпика', 'Были в эпике, в последнем срезе их нет'],
  ['reestimated', 'Переоценка', 'Изменился объём задач, которые были и остались'],
  ['closed', 'Закрыты за период', 'Перешли в «Готово»'],
  ['reopened', 'Переоткрыты', 'Были в «Готово», вернулись в работу']
];

function openJiraChanges(sid, from, to){
  var svc = serviceById(sid);
  if(!svc) return;
  var q = [];
  if(from) q.push('from=' + from);
  if(to) q.push('to=' + to);
  api('/api/services/'+sid+'/jira/changes' + (q.length ? '?' + q.join('&') : '')).then(function(c){
    destroyChart('scopeChanges');
    var title = 'Что изменилось: ' + svc.name;
    if(!c.from){
      openModal({title: title, okLabel: 'Закрыть', noCancel: true,
        html: '<p class="bulk-intro">Для сравнения нужны хотя бы два среза задач в разные дни. Срезов сейчас: '+c.dates.length+'.</p>'});
      return;
    }
    var options = function(selected, limit){
      return c.dates.filter(limit).map(function(d){
        return '<option value="'+d+'"'+(d === selected ? ' selected' : '')+'>'+fmtDate(d)+'</option>';
      }).join('');
    };
    var groupList = function(g){
      var grp = c[g[0]];
      if(!grp.count) return '';
      return '<details class="change-group"'+(g[0] === 'added' || g[0] === 'reestimated' ? ' open' : '')+'><summary><b>'+g[1]+'</b> · '+grp.count+
        (g[0] === 'closed' || g[0] === 'reopened' ? ' · ' + hoursToStr(grp.volume) : ' · ' + signedHours(grp.volume))+
        ' <span class="muted-cell">'+g[2]+'</span></summary><table class="data-table compact-table"><tbody>'+
        grp.issues.map(function(i){
          var value = i.delta !== undefined ? fmtNum(i.before) + ' → ' + fmtNum(i.volume) + ' ч (' + signedHours(i.delta) + ')' :
            hoursToStr(i.volume);
          return '<tr><td class="nowrap">'+jiraIssueLink(c.baseUrl, i.key)+'</td><td>'+escapeHtml(i.summary)+'</td>'+
            '<td class="nowrap"><span class="badge '+CATEGORY_BADGE[i.category]+'">'+escapeHtml(i.status || CATEGORY_LABELS[i.category])+'</span></td>'+
            '<td class="num nowrap">'+value+'</td></tr>';
        }).join('')+'</tbody></table></details>';
    };
    openModal({
      title: title, size: 'xl', okLabel: 'Закрыть', noCancel: true,
      html: '<div class="changes-period"><label class="small" for="chFrom">С</label><select id="chFrom">'+options(c.from, function(d){ return d < c.to; })+'</select>'+
          '<label class="small" for="chTo">по</label><select id="chTo">'+options(c.to, function(d){ return d > c.from; })+'</select>'+
          '<span class="muted-cell">даты срезов задач</span></div>'+
        '<p class="bulk-intro">Объём эпика: <b>'+hoursToStr(c.scopeFrom)+'</b> → <b>'+hoursToStr(c.scopeTo)+'</b> ('+signedHours(Math.round((c.scopeTo - c.scopeFrom) * 100) / 100)+'). '+
          'Выполнено: '+fmtNum(c.doneFrom)+' → '+fmtNum(c.doneTo)+' ч.</p>'+
        '<div class="chart-wrap chart-wrap-sm"><canvas id="chartScopeChanges" aria-label="Каскад изменения объёма"></canvas></div>'+
        '<div class="changes-lists">'+CHANGE_GROUPS.map(groupList).join('')+
        (CHANGE_GROUPS.every(function(g){ return !c[g[0]].count; }) ? '<p class="muted-cell">Изменений в задачах нет.</p>' : '')+'</div>'
    });
    renderScopeWaterfall(c);
    ['chFrom', 'chTo'].forEach(function(id){
      document.getElementById(id).addEventListener('change', function(){
        openJiraChanges(sid, document.getElementById('chFrom').value, document.getElementById('chTo').value);
      });
    });
  }).catch(function(){});
}

/* Каскад: объём «до» → новые → убранные → переоценка → объём «после» */
function renderScopeWaterfall(c){
  var steps = [['Объём ' + fmtShortDate(c.from), 0, c.scopeFrom, 'base']];
  var level = c.scopeFrom;
  [['added', 'Новые'], ['removed', 'Убраны'], ['reestimated', 'Переоценка']].forEach(function(g){
    var v = c[g[0]].volume;
    steps.push([g[1], level, level + v, v > 0 ? 'up' : (v < 0 ? 'down' : 'base')]);
    level += v;
  });
  steps.push(['Объём ' + fmtShortDate(c.to), 0, c.scopeTo, 'base']);
  var colors = {base: getCss('--color-blue'), up: getCss('--color-orange'), down: getCss('--color-success')};
  charts.scopeChanges = makeChart(document.getElementById('chartScopeChanges'), {
    type: 'bar',
    data: {labels: steps.map(function(st){ return st[0]; }),
           datasets: [{data: steps.map(function(st){ return [Math.min(st[1], st[2]), Math.max(st[1], st[2])]; }),
                       backgroundColor: steps.map(function(st){ return colors[st[3]]; }), borderRadius: 4, maxBarThickness: 64}]},
    options: {responsive: true, maintainAspectRatio: false, plugins: {legend: {display: false},
      tooltip: {callbacks: {label: function(ctx){
        var st = steps[ctx.dataIndex];
        return st[3] === 'base' && st[1] === 0 ? hoursToStr(st[2]) : signedHours(Math.round((st[2] - st[1]) * 100) / 100);
      }}}},
      scales: {x: {grid: {display: false}, ticks: {color: getCss('--color-text-muted')}},
               y: {beginAtZero: true, title: {display: true, text: unitAxisTitle(), color: getCss('--color-text-muted')},
                   grid: {color: getCss('--color-divider')}, ticks: {color: getCss('--color-text-muted'), callback: function(v){ return fmtWorkNum(v); }}}}}
  });
}

function openJiraIssues(sid){
  var svc = serviceById(sid);
  if(!svc) return;
  api('/api/services/'+sid+'/jira/issues').then(function(data){
    var labels = data.problemLabels || {};
    var issues = data.issues;
    var problemTotal = issues.filter(function(i){ return i.problems.length; }).length;
    var counts = {};
    issues.forEach(function(i){ if(!i.container) counts[i.category] = (counts[i.category] || 0) + 1; });
    var filters = [['all', 'Все', issues.length], ['problems', '⚠ С проблемами', problemTotal]].concat(
      CATEGORY_ORDER.map(function(c){ return [c, CATEGORY_LABELS[c], counts[c] || 0]; }));
    var link = function(key){ return jiraIssueLink(data.baseUrl, key); };
    var badgeClass = CATEGORY_BADGE;
    var deltaHtml = function(i, m){
      /* в прошлом срезе значения не было (показатель ещё не настроен) — это не изменение */
      if(!data.prevDate || !i.prev || !isNum(i[m]) || !isNum(i.prev[m])) return '';
      var d = Math.round(((i[m] || 0) - (i.prev[m] || 0)) * 100) / 100;
      return d ? '<div class="cell-delta '+(d > 0 ? 'delta-up' : 'delta-down')+'">'+(d > 0 ? '+' : '')+fmtNum(d)+'</div>' : '';
    };
    openModal({
      title: 'Задачи Jira: ' + svc.name, size: 'xl', okLabel: 'Закрыть', noCancel: true,
      html: '<p class="bulk-intro">Срез от '+fmtDate(data.date)+'. Задач: '+issues.filter(function(i){ return !i.container; }).length+
          (problemTotal ? ', с неполными данными: <b>'+problemTotal+'</b>' : '')+'. Ключ задачи открывает её в Jira.'+
          (data.prevDate ? ' Под значениями — изменение с прошлого среза ('+fmtDate(data.prevDate)+'), «новая» — задачи, которых тогда не было.' : '')+'</p>'+
        '<div class="seg-filter" id="jiFilter" role="group" aria-label="Фильтр задач">'+filters.map(function(f, i){
          return '<button type="button" class="btn btn-ghost btn-sm'+(i === 0 ? ' active' : '')+'" data-filter="'+f[0]+'"'+(f[2] ? '' : ' disabled')+'>'+escapeHtml(f[1])+' <span class="muted-cell">'+f[2]+'</span></button>';
        }).join('')+'</div>'+
        '<div class="table-scroll jira-issues-scroll"><table class="data-table compact-table" id="jiTable"><thead><tr>'+
          '<th>Задача</th><th>Тема</th><th>Статус</th><th class="num">Остаток, ч</th><th class="num">Затрачено, ч</th><th class="num">Оценка, ч</th><th>Данные</th></tr></thead><tbody>'+
        issues.map(function(i){
          return '<tr data-cat="'+i.category+'"'+(i.problems.length ? ' data-problem="1"' : '')+(i.container ? ' class="container-row"' : '')+'>'+
            '<td class="nowrap">'+link(i.key)+'</td>'+
            '<td>'+escapeHtml(i.summary)+(i.container ? ' <span class="muted-cell">· содержит задачи</span>' : '')+
              (data.prevDate && !i.prev ? ' <span class="badge badge-new">новая</span>' : '')+'</td>'+
            '<td class="nowrap"><span class="badge '+badgeClass[i.category]+'">'+escapeHtml(i.status || CATEGORY_LABELS[i.category])+'</span></td>'+
            ['remaining', 'spent', 'estimate'].map(function(m){
              return '<td class="num" data-num="'+(isNum(i[m]) ? i[m] : '')+'">'+(isNum(i[m]) ? fmtNum(i[m]) : '—')+deltaHtml(i, m)+'</td>';
            }).join('')+
            '<td class="issue-problems" data-values="'+escapeAttr(i.problems.length ? i.problems.map(function(p){ return labels[p] || p; }).join('|') : 'в порядке')+'">'+
              i.problems.map(function(p){ return '<div>⚠ '+escapeHtml(labels[p] || p)+'</div>'; }).join('')+'</td></tr>';
        }).join('')+'</tbody></table></div>',
      onOk: function(){ closeModal(); return false; }
    });
    var jiTable = document.getElementById('jiTable');
    delete tableFilterState.jiTable;  // у каждого эпика — свой список задач
    tableFilter(jiTable, ['text', 'text', 'list', 'num', 'num', 'num', 'list']);
    document.querySelectorAll('#jiFilter [data-filter]').forEach(function(btn){
      btn.addEventListener('click', function(){
        var f = btn.dataset.filter;
        document.querySelectorAll('#jiFilter [data-filter]').forEach(function(b){ b.classList.toggle('active', b === btn); });
        document.querySelectorAll('#jiTable tbody tr').forEach(function(tr){
          tr.hidden = !(f === 'all' || (f === 'problems' ? tr.dataset.problem : tr.dataset.cat === f));
        });
        applyTableFilter(jiTable);
      });
    });
  }).catch(function(){});
}


/* ════════════════════════════════════════════════════════════════
   Восстановление истории по журналу изменений Jira (фоновая задача на сервере)
════════════════════════════════════════════════════════════════ */
var HISTORY_STATUS = {running: 'выполняется', done: 'готово', error: 'ошибка', stale: 'прервано'};

function historyJobSummary(job){
  var p = job.params || {};
  return fmtDate(p.from) + ' — ' + fmtDate(p.to) + ', ' + (HISTORY_STATUS[job.status] || job.status) +
    (job.finishedAt ? ' ' + fmtDateTime(job.finishedAt) : '');
}

function openHistoryRestore(button){
  var mapped = {};
  button.mappings.forEach(function(m){ mapped[m.serviceId] = true; });
  var firstEntry = STATE.entries.filter(function(e){ return mapped[e.service_id]; })
    .map(function(e){ return e.date; }).sort()[0];
  var yesterday = addDays(todayIso(), -1);
  var from = firstEntry && firstEntry < yesterday ? firstEntry : addDays(todayIso(), -56);
  var browserTz = '';
  try { browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch(e){}
  var zones = TIMEZONES.slice();
  if(browserTz && zones.indexOf(browserTz) === -1) zones.push(browserTz);
  var tz = browserTz || 'Europe/Moscow';
  openModal({
    title: 'Восстановить историю: ' + button.name, okLabel: 'Восстановить', wide: true,
    html: '<p class="bulk-intro">Burndown прочитает журнал изменений задач кнопки в Jira и посчитает остаток, затраченное время, оценку и состав задач на прошлые даты — так, как они выглядели в конце каждого дня.</p>'+
      '<div class="form-grid history-form">'+
        field('С', '<input type="date" id="hrFrom" value="'+from+'" max="'+yesterday+'">')+
        field('По', '<input type="date" id="hrTo" value="'+yesterday+'" max="'+todayIso()+'">')+
        field('Шаг', '<select id="hrStep"><option value="week">раз в неделю</option><option value="day">каждый день</option></select>')+
        field('Часовой пояс', '<select id="hrTz">'+zones.map(function(z){ return '<option value="'+escapeAttr(z)+'"'+(z === tz ? ' selected' : '')+'>'+escapeHtml(z)+'</option>'; }).join('')+'</select>')+
      '</div>'+
      '<label class="quick-filter-item check-wrap"><input type="checkbox" id="hrKeep" checked> Не изменять даты, где замер уже внесён</label>'+
      '<ul class="bulk-note history-notes">'+
        '<li>Берётся текущий состав задач кнопки: задачи учитываются с даты их создания; задачи, которые раньше были в структуре и потом убраны, не учитываются.</li>'+
        '<li>Стандартные поля учёта времени и статусы восстанавливаются точно. Для пользовательских полей — если Jira пишет их изменения в журнал.</li>'+
        '<li>Срезы состава, сделанные раньше на эти даты, не перезаписываются. Все изменения попадут в реестр.</li>'+
      '</ul>',
    onOk: function(){
      var body = {from: document.getElementById('hrFrom').value, to: document.getElementById('hrTo').value,
                  step: document.getElementById('hrStep').value, timezone: document.getElementById('hrTz').value,
                  keepExisting: document.getElementById('hrKeep').checked};
      if(!body.from || !body.to){ toast('Укажите период'); return false; }
      confirmOk.disabled = true;
      apiPost('/api/jira/buttons/'+button.id+'/history', body).then(function(job){
        showHistoryProgress(button, job);
      }).catch(function(){ confirmOk.disabled = false; });
      return false;
    }
  });
}

function showHistoryProgress(button, job){
  var render = function(j){
    var pct = j.total ? Math.round(j.progress / j.total * 100) : 0;
    var finished = j.status !== 'running';
    confirmBody.innerHTML = '<div class="history-progress" id="hrProgress" data-status="'+j.status+'">'+
      '<div class="progress-bar'+(finished ? '' : ' active')+'"><span style="width:'+(finished && j.status === 'done' ? 100 : pct)+'%"></span></div>'+
      '<p class="history-message '+(j.status === 'done' ? 'ok' : (finished ? 'error' : ''))+'">'+escapeHtml(j.message || 'Подготовка…')+'</p>'+
      (finished ? '' : '<p class="bulk-note">Можно закрыть окно — восстановление продолжится на сервере, результат появится в таблице кнопок.</p>')+'</div>';
    confirmOk.textContent = 'Закрыть';
    confirmOk.disabled = false;
    confirmCancel.hidden = true;
    modalOkHandler = null;
    if(finished){
      replaceJiraButton(Object.assign({}, jiraButtonById(button.id) || button, {history: j}));
      if(j.status === 'done'){ loadProjectData(currentProjectId); toast('История восстановлена'); }
    }
    return finished;
  };
  if(render(job)) return;
  var timer = setInterval(function(){
    if(!document.getElementById('hrProgress')){ clearInterval(timer); loadJiraConfig(); return; }
    api('/api/jira/history-jobs/'+job.id).then(function(j){
      if(render(j)) clearInterval(timer);
    }).catch(function(){ clearInterval(timer); });
  }, 1000);
}


/* ════════════════════════════════════════════════════════════════
   Мастер подключения проекта к Jira и синхронизация эпиков
════════════════════════════════════════════════════════════════ */
var EPIC_TARGET_OPTIONS = [['duedate', 'Срок задачи (Due date)'], ['fixVersion', 'Дата выпуска версии (Fix version)'], ['none', 'Не заполнять']];
var EPIC_CHANGE_LABELS = {targetDate: 'цель', isDone: 'сдано', name: 'название', etap: 'этап'};
function epicSourceFields(prefix, cfg, structures, structureId){
  var opt = function(v, l, cur){ return '<option value="'+escapeAttr(v)+'"'+(String(cur) === String(v) ? ' selected' : '')+'>'+escapeHtml(l)+'</option>'; };
  var html = '';
  if(structures){
    html += field('Структура Jira', '<select id="'+prefix+'Structure">'+opt('', 'Без структуры — найти эпики запросом JQL', structureId)+
      structures.map(function(s){ return opt(s.id, s.name + ' (#' + s.id + ')', structureId); }).join('')+'</select>');
  }
  html += '<div class="setting-item" id="'+prefix+'LevelBox"><span class="field-label">Где в структуре эпики</span>'+
    '<label class="checkbox-row"><input type="radio" name="'+prefix+'Level" value="0"'+(cfg.level === 0 ? ' checked' : '')+'> Задачи верхнего уровня</label>'+
    '<label class="checkbox-row"><input type="radio" name="'+prefix+'Level" value="1"'+(cfg.level === 1 ? ' checked' : '')+'> Задачи второго уровня — задачи верхнего уровня станут этапами</label></div>';
  html += '<div class="setting-item" id="'+prefix+'JqlBox"><label class="small" for="'+prefix+'Jql">Запрос JQL</label><textarea id="'+prefix+'Jql" rows="2" placeholder="project = ABC AND issuetype = Epic AND resolution = Unresolved">'+escapeHtml(cfg.jql || '')+'</textarea></div>';
  html += field('Этап для эпиков', '<input type="text" id="'+prefix+'Stage" value="'+escapeAttr(cfg.stage || '')+'" placeholder="например, Этап 1" maxlength="300">');
  html += field('Целевая дата эпика', '<select id="'+prefix+'Target">'+EPIC_TARGET_OPTIONS.map(function(o){ return opt(o[0], o[1], cfg.target); }).join('')+'</select>');
  html += '<label class="checkbox-row"><input type="checkbox" id="'+prefix+'Done"'+(cfg.done ? ' checked' : '')+'> Отмечать эпик «Сдано», когда задача в Jira закрыта (и снимать отметку, если её переоткрыли)</label>';
  return html;
}
function bindEpicSourceFields(prefix, hasStructure){
  var sel = document.getElementById(prefix + 'Structure');
  var update = function(){
    var withStructure = sel ? !!sel.value : hasStructure;
    var level = (confirmBody.querySelector('input[name="'+prefix+'Level"]:checked') || {}).value;
    document.getElementById(prefix + 'LevelBox').hidden = !withStructure;
    document.getElementById(prefix + 'JqlBox').hidden = withStructure;
    document.getElementById(prefix + 'Stage').closest('.setting-item').hidden = withStructure && level === '1';
  };
  if(sel) sel.addEventListener('change', update);
  confirmBody.querySelectorAll('input[name="'+prefix+'Level"]').forEach(function(r){ r.addEventListener('change', update); });
  update();
}
function readEpicSource(prefix, hasStructure){
  var sel = document.getElementById(prefix + 'Structure');
  var structureId = sel ? sel.value : (hasStructure ? 'keep' : '');
  var withStructure = !!structureId;
  var levelEl = confirmBody.querySelector('input[name="'+prefix+'Level"]:checked');
  return {structureId: structureId, structureName: sel && sel.value ? sel.options[sel.selectedIndex].text.replace(/ \(#\d+\)$/, '') : '',
    config: {level: withStructure && levelEl ? parseInt(levelEl.value, 10) : 0, jql: withStructure ? '' : document.getElementById(prefix + 'Jql').value.trim(),
      stage: document.getElementById(prefix + 'Stage').value.trim(), target: document.getElementById(prefix + 'Target').value,
      done: document.getElementById(prefix + 'Done').checked}};
}
function epicPlanHtml(plan){
  var parts = [];
  if(plan.create.length) parts.push('<div class="insight-col-title">Новые эпики — ' + plan.create.length + '</div><ul class="plan-list">' + plan.create.map(function(c){
    return '<li><span class="mono">'+escapeHtml(c.key)+'</span> '+escapeHtml(c.name)+(c.etap ? ' <span class="muted-cell">· '+escapeHtml(c.etap)+'</span>' : '')+
      (c.target ? ' <span class="muted-cell">· цель '+fmtDate(c.target)+'</span>' : '')+(c.done ? ' <span class="badge badge-ok">сдано</span>' : '')+'</li>';
  }).join('') + '</ul>');
  if(plan.update.length) parts.push('<div class="insight-col-title">Изменения — ' + plan.update.length + '</div><ul class="plan-list">' + plan.update.map(function(u){
    return '<li><b>'+escapeHtml(u.name)+'</b>: '+u.changes.map(function(ch){
      var v = function(x){ return ch.field === 'isDone' ? (x ? 'да' : 'нет') : (ch.field === 'targetDate' ? fmtDate(x) : escapeHtml(x || '—')); };
      return EPIC_CHANGE_LABELS[ch.field] + ' ' + v(ch.old) + ' → ' + v(ch.new);
    }).join('; ')+'</li>';
  }).join('') + '</ul>');
  if(plan.link && plan.link.length) parts.push('<div class="insight-col-title">Связать с кнопкой — ' + plan.link.length + '</div><ul class="plan-list">' +
    plan.link.map(function(l){ return '<li>'+escapeHtml(l.name)+' ← <span class="mono">'+escapeHtml(l.key)+'</span></li>'; }).join('') + '</ul>');
  if(plan.missing.length) parts.push('<div class="insight-col-title">Нет в Jira — ' + plan.missing.length + '</div><p class="bulk-note">Эти эпики проекта связаны с задачами, которых больше нет в структуре или запросе. Burndown их не удаляет — проверьте и при необходимости отметьте «Сдано».</p><ul class="plan-list">' +
    plan.missing.map(function(m){ return '<li>'+escapeHtml(m.name)+' <span class="mono muted-cell">'+escapeHtml(m.keys)+'</span></li>'; }).join('') + '</ul>');
  if(!parts.length) return '<p class="insight-empty">Изменений нет: эпики проекта совпадают с Jira (' + plan.unchanged + ').</p>';
  return parts.join('') + (plan.unchanged ? '<p class="bulk-note">Без изменений: ' + plan.unchanged + '.</p>' : '');
}

function openJiraWizard(){
  var pid = currentProjectId;
  var browserTz = '';
  try { browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch(e){}
  var wz = {structures: null, src: {structureId: '', structureName: '', config: {level: 0, jql: '', stage: '', target: 'duedate', done: true}},
    name: 'Срез из Jira', epics: [], picked: {}, weeks: 8, auto: true,
    schedule: {enabled: true, days: [1, 2, 3, 4, 5], time: '09:00', timezone: TIMEZONES.indexOf(browserTz) !== -1 ? browserTz : 'Europe/Moscow'}};
  var steps = ['Откуда эпики', 'Эпики', 'Замеры и расписание', 'Готово'];
  var head = function(n){
    confirmTitle.textContent = 'Мастер подключения к Jira';
    return '<ol class="wizard-steps">' + steps.map(function(t, i){ return '<li class="'+(i + 1 === n ? 'active' : (i + 1 < n ? 'done' : ''))+'">'+t+'</li>'; }).join('') + '</ol>';
  };
  var back = function(fn){ var b = document.getElementById('wzBack'); if(b) b.addEventListener('click', fn); };
  function step1(){
    confirmBody.innerHTML = head(1) + '<div class="form-grid wizard-form">' + field('Название кнопки среза', '<input type="text" id="wzName" maxlength="200" value="'+escapeAttr(wz.name)+'">') +
      epicSourceFields('wz', wz.src.config, wz.structures || [], wz.src.structureId) + '</div>';
    bindEpicSourceFields('wz', false);
    confirmOk.textContent = 'Найти эпики';
    confirmOk.disabled = false;
    confirmCancel.hidden = false;
    modalOkHandler = function(){
      wz.name = document.getElementById('wzName').value.trim() || 'Срез из Jira';
      wz.src = readEpicSource('wz', false);
      if(!wz.src.structureId && !wz.src.config.jql){ toast('Выберите структуру или укажите запрос JQL'); return false; }
      confirmOk.disabled = true;
      confirmOk.textContent = 'Ищем эпики…';
      apiPost('/api/projects/'+pid+'/jira/epics/preview', Object.assign({structureId: wz.src.structureId}, wz.src.config)).then(function(r){
        wz.epics = r.epics;
        wz.picked = {};
        r.epics.forEach(function(e){ wz.picked[e.key] = true; });
        if(!r.epics.length){ toast('Эпиков не найдено — выберите другой уровень структуры или запрос'); step1(); return; }
        step2();
      }).catch(function(){ step1(); });
      return false;
    };
  }
  function step2(){
    var rows = wz.epics.map(function(e){
      return '<tr><td><input type="checkbox" data-key="'+escapeAttr(e.key)+'"'+(wz.picked[e.key] ? ' checked' : '')+' aria-label="Подключить '+escapeAttr(e.key)+'"></td>'+
        '<td class="mono nowrap">'+escapeHtml(e.key)+'</td><td>'+escapeHtml(e.summary)+(e.serviceId ? ' <span class="badge badge-muted" title="Эпик уже есть в проекте — он будет связан с кнопкой">в проекте</span>' : '')+'</td>'+
        '<td>'+escapeHtml(e.stage || wz.src.config.stage || '—')+'</td><td class="nowrap">'+(e.target ? fmtDate(e.target) : '—')+'</td>'+
        '<td>'+escapeHtml(e.status)+(e.done ? ' <span class="badge badge-ok">закрыта</span>' : '')+'</td></tr>';
    }).join('');
    confirmBody.innerHTML = head(2) + '<p class="bulk-intro">Найдено задач-эпиков: <b>'+wz.epics.length+'</b>. Отметьте, какие подключить к проекту. Новые эпики будут созданы, уже существующие — связаны с кнопкой.</p>'+
      '<div class="table-scroll wizard-table"><table class="data-table compact-table"><thead><tr><th><input type="checkbox" id="wzAll" checked aria-label="Все"></th><th>Задача</th><th>Название</th><th>Этап</th><th>Цель</th><th>Статус в Jira</th></tr></thead><tbody>'+rows+'</tbody></table></div>'+
      '<div class="form-actions wizard-back"><button type="button" class="btn btn-ghost btn-sm" id="wzBack">← Назад</button></div>';
    var boxes = confirmBody.querySelectorAll('[data-key]');
    document.getElementById('wzAll').checked = Array.prototype.every.call(boxes, function(b){ return b.checked; });
    document.getElementById('wzAll').addEventListener('change', function(e){ boxes.forEach(function(b){ b.checked = e.target.checked; wz.picked[b.dataset.key] = b.checked; }); });
    boxes.forEach(function(b){ b.addEventListener('change', function(){ wz.picked[b.dataset.key] = b.checked; }); });
    back(step1);
    confirmOk.textContent = 'Далее';
    confirmOk.disabled = false;
    modalOkHandler = function(){
      if(!wz.epics.some(function(e){ return wz.picked[e.key]; })){ toast('Отметьте хотя бы один эпик'); return false; }
      step3();
      return false;
    };
  }
  function step3(){
    var s = wz.schedule;
    var picked = wz.epics.filter(function(e){ return wz.picked[e.key]; });
    var fresh = picked.filter(function(e){ return !e.serviceId; }).length;
    var zones = TIMEZONES.slice();
    if(zones.indexOf(s.timezone) === -1) zones.push(s.timezone);
    confirmBody.innerHTML = head(3) + '<p class="bulk-intro">Будет создано эпиков: <b>'+fresh+'</b>, связано существующих: <b>'+(picked.length - fresh)+'</b>. Остаток, затраченное время и оценка — из стандартного учёта времени Jira; поля можно изменить позже в редакторе кнопки.</p>'+
      '<div class="form-grid wizard-form">'+
      field('Восстановить историю', '<select id="wzWeeks">'+[[0, 'Не восстанавливать'], [4, 'За 4 недели'], [8, 'За 8 недель'], [12, 'За 12 недель'], [26, 'За полгода']].map(function(o){
        return '<option value="'+o[0]+'"'+(o[0] === wz.weeks ? ' selected' : '')+'>'+o[1]+'</option>'; }).join('')+'</select>')+
      '<label class="checkbox-row"><input type="checkbox" id="wzAuto"'+(wz.auto ? ' checked' : '')+'> Перед каждым автозаполнением сверять эпики с Jira: новые — добавлять, цели и «Сдано» — обновлять</label>'+
      '<label class="checkbox-row"><input type="checkbox" id="wzSched"'+(s.enabled ? ' checked' : '')+'> Автозаполнение замеров по расписанию</label>'+
      '<div class="setting-item"><span class="field-label">Дни недели</span><div class="weekday-picker" id="wzDays">'+WEEKDAYS.map(function(d){
        return '<label><input type="checkbox" data-day="'+d[0]+'"'+(s.days.indexOf(d[0]) !== -1 ? ' checked' : '')+'><span>'+d[1]+'</span></label>'; }).join('')+'</div></div>'+
      field('Время', '<input type="time" id="wzTime" value="'+escapeAttr(s.time)+'">')+
      field('Часовой пояс', '<select id="wzTz">'+zones.map(function(z){ return '<option'+(z === s.timezone ? ' selected' : '')+'>'+escapeHtml(z)+'</option>'; }).join('')+'</select>')+
      '</div><div class="form-actions wizard-back"><button type="button" class="btn btn-ghost btn-sm" id="wzBack">← Назад</button></div>';
    var read = function(){
      wz.weeks = parseInt(document.getElementById('wzWeeks').value, 10);
      wz.auto = document.getElementById('wzAuto').checked;
      wz.schedule = {enabled: document.getElementById('wzSched').checked, time: document.getElementById('wzTime').value || '09:00',
        timezone: document.getElementById('wzTz').value,
        days: Array.prototype.filter.call(confirmBody.querySelectorAll('#wzDays input'), function(c){ return c.checked; }).map(function(c){ return parseInt(c.dataset.day, 10); })};
    };
    back(function(){ read(); step2(); });
    confirmOk.textContent = 'Подключить';
    confirmOk.disabled = false;
    modalOkHandler = function(){
      read();
      if(wz.schedule.enabled && !wz.schedule.days.length){ toast('Выберите дни недели для автозаполнения'); return false; }
      confirmOk.disabled = true;
      confirmOk.textContent = 'Подключаем…';
      apiPost('/api/projects/'+pid+'/jira/onboard', {name: wz.name, structureId: wz.src.structureId, structureName: wz.src.structureName,
        epics: Object.assign({}, wz.src.config, {enabled: wz.auto}), keys: wz.epics.filter(function(e){ return wz.picked[e.key]; }).map(function(e){ return e.key; }),
        historyWeeks: wz.weeks, schedule: wz.schedule}).then(step4).catch(function(){ step3(); });
      return false;
    };
  }
  function step4(res){
    var ok = res.snapshot.status === 'ok';
    var render = function(job){
      var hist = '';
      if(job){
        var finished = job.status !== 'running';
        var pct = job.total ? Math.round(job.progress / job.total * 100) : 0;
        hist = '<div class="history-progress" id="wzHistory"><div class="field-label">История за прошлые недели</div><div class="progress-bar'+(finished ? '' : ' active')+'"><span style="width:'+(finished && job.status === 'done' ? 100 : pct)+'%"></span></div>'+
          '<p class="history-message '+(job.status === 'done' ? 'ok' : (finished ? 'error' : ''))+'">'+escapeHtml(job.message || 'Подготовка…')+'</p></div>';
      }
      confirmBody.innerHTML = head(4) +
        '<p class="wizard-result '+(ok ? 'ok' : 'error')+'">'+(ok ? '✓ Проект подключён к Jira.' : '⚠ Кнопка и эпики созданы, но срез не удался.')+'</p>'+
        '<ul class="plan-list"><li>Создано эпиков: <b>'+res.created+'</b>, связано существующих: <b>'+res.linked+'</b>'+(res.updated ? ', обновлено: <b>'+res.updated+'</b>' : '')+'</li>'+
        '<li>Замер на сегодня: '+escapeHtml(res.snapshot.message)+'</li>'+
        (ok && res.button.schedule.enabled ? '<li>Автозаполнение: '+escapeHtml(scheduleSummary(res.button.schedule))+'</li>' : '')+
        (res.button.epicSync.enabled ? '<li>Эпики сверяются с Jira перед каждым автозаполнением</li>' : '')+'</ul>'+
        (ok ? '' : '<p class="bulk-note">Проверьте поля Jira в редакторе кнопки и выполните срез в разделе «Данные по датам».</p>') + hist;
      confirmOk.textContent = 'Открыть дашборд';
      confirmOk.disabled = false;
      confirmCancel.hidden = true;
      modalOkHandler = function(){ closeModal(); loadProjectData(pid, 'dashboard'); return false; };
    };
    render(res.history);
    loadProjectData(pid, currentView);
    if(res.history && res.history.status === 'running'){
      var timer = setInterval(function(){
        if(!document.getElementById('wzHistory')){ clearInterval(timer); return; }
        api('/api/jira/history-jobs/'+res.history.id).then(function(j){ render(j); if(j.status !== 'running'){ clearInterval(timer); loadProjectData(pid, currentView); } })
          .catch(function(){ clearInterval(timer); });
      }, 1000);
    }
  }
  openModal({title: 'Мастер подключения к Jira', html: '<p class="insight-empty">Загружаем структуры Jira…</p>', okLabel: 'Найти эпики', wide: true});
  confirmOk.disabled = true;
  api('/api/projects/'+pid+'/jira/structures').then(function(list){ wz.structures = list; if(list.length) wz.src.structureId = String(list[0].id); step1(); })
    .catch(function(){ wz.structures = []; step1(); });
}
document.getElementById('jiraWizardBtn').addEventListener('click', openJiraWizard);

/* Эпики из Jira для существующей кнопки: настройки, предпросмотр изменений, синхронизация */
function openEpicSync(button){
  var cfg = Object.assign({}, button.epicSync);
  var hasStructure = !!button.structureId;
  openModal({title: 'Эпики из Jira — «' + button.name + '»', okLabel: 'Сохранить и синхронизировать', wide: true,
    html: '<p class="bulk-intro">Список эпиков проекта, их целевые даты и признак «Сдано» берутся из Jira: новые задачи-эпики становятся эпиками проекта и связываются с этой кнопкой, у существующих обновляются цели и «Сдано».' +
      (button.epicSync.lastAt ? ' Последняя синхронизация — ' + fmtDateTime(button.epicSync.lastAt) + ': ' + escapeHtml(button.epicSync.lastMessage || '') + '.' : '') + '</p>' +
      '<div class="form-grid wizard-form">' + epicSourceFields('es', cfg, null, null) +
      '<label class="checkbox-row"><input type="checkbox" id="esNames"'+(cfg.names ? ' checked' : '')+'> Обновлять названия эпиков по темам задач</label>'+
      '<label class="checkbox-row"><input type="checkbox" id="esEnabled"'+(cfg.enabled ? ' checked' : '')+'> Синхронизировать автоматически перед каждым автозаполнением</label></div>'+
      '<div class="form-actions"><button type="button" class="btn btn-ghost btn-sm" id="esCheck">Показать изменения</button></div><div id="esPlan"></div>',
    onOk: function(){
      var body = Object.assign(readEpicSource('es', hasStructure).config, {names: document.getElementById('esNames').checked, enabled: document.getElementById('esEnabled').checked});
      confirmOk.disabled = true;
      apiPut('/api/jira/buttons/'+button.id+'/epic-sync', body).then(function(){
        return apiPost('/api/jira/buttons/'+button.id+'/epic-sync/run', {});
      }).then(function(r){
        replaceJiraButton(r.button);
        closeModal();
        toast(r.message);
        loadProjectData(currentProjectId, currentView);
      }).catch(function(){ confirmOk.disabled = false; });
      return false;
    }});
  bindEpicSourceFields('es', hasStructure);
  document.getElementById('esCheck').addEventListener('click', function(){
    var box = document.getElementById('esPlan');
    var body = Object.assign(readEpicSource('es', hasStructure).config, {names: document.getElementById('esNames').checked, enabled: document.getElementById('esEnabled').checked});
    box.innerHTML = '<p class="insight-empty">Сверяем с Jira…</p>';
    /* предпросмотр по введённым настройкам: сохраняем их, затем считаем изменения без записи */
    apiPut('/api/jira/buttons/'+button.id+'/epic-sync', body).then(function(b){
      replaceJiraButton(b);
      return apiPost('/api/jira/buttons/'+button.id+'/epic-sync/run', {dryRun: true});
    }).then(function(r){ box.innerHTML = epicPlanHtml(r.plan); }).catch(function(){ box.innerHTML = ''; });
  });
}

/* ════════════════════════════════════════════════════════════════
   Администратор: производственный календарь и состояние системы
════════════════════════════════════════════════════════════════ */
var orgCalendar = null;
function renderCalendarView(){
  api('/api/admin/calendar').then(function(c){
    orgCalendar = c;
    document.getElementById('calHolidays').value = formatDateList(c.holidays);
    document.getElementById('calExtra').value = formatDateList(c.extraWorkdays);
    renderCalendarPresets();
  }).catch(function(){});
}
function renderCalendarPresets(){
  var box = document.getElementById('calPresets');
  box.innerHTML = '<span class="field-label">Праздники РФ</span>' + orgCalendar.presets.map(function(p){
    return '<button type="button" class="btn btn-ghost btn-sm" data-year="'+p.year+'" title="'+escapeAttr(p.source)+'"></button>';
  }).join('') + '<span class="setting-hint">По постановлениям Правительства РФ о переносе выходных дней. Новый год добавляется кнопкой, когда выйдет постановление и обновление Burndown.</span>';
  box.querySelectorAll('[data-year]').forEach(function(btn){
    btn.addEventListener('click', function(){
      var p = orgCalendar.presets.find(function(x){ return String(x.year) === btn.dataset.year; });
      var merge = function(id, add){ var cur = parseDateList(document.getElementById(id).value).dates; document.getElementById(id).value = formatDateList(cur.concat(add.filter(function(d){ return cur.indexOf(d) === -1; })).sort()); };
      merge('calHolidays', p.holidays);
      merge('calExtra', p.extraWorkdays);
      refreshCalendarPresets();
      toast('Добавлены праздники ' + p.year + ' — сохраните календарь');
    });
  });
  refreshCalendarPresets();
}
/* состояние кнопок — на месте, без пересоздания: щелчок по кнопке сразу после правки поля не теряется */
function refreshCalendarPresets(){
  if(!orgCalendar) return;
  var current = parseDateList(document.getElementById('calHolidays').value).dates;
  document.querySelectorAll('#calPresets [data-year]').forEach(function(btn){
    var p = orgCalendar.presets.find(function(x){ return String(x.year) === btn.dataset.year; });
    var has = p.holidays.every(function(d){ return current.indexOf(d) !== -1; });
    btn.disabled = has;
    btn.textContent = (has ? '✓ ' : '+ ') + p.year;
  });
}
document.getElementById('calHolidays').addEventListener('input', refreshCalendarPresets);
document.getElementById('calSave').addEventListener('click', function(){
  var h = parseDateList(document.getElementById('calHolidays').value), x = parseDateList(document.getElementById('calExtra').value);
  var bad = h.bad.concat(x.bad);
  if(bad.length){ toast('Не распознаны даты: ' + bad.slice(0, 3).join(', ') + ' — формат ДД.ММ.ГГГГ или диапазон через «–»'); return; }
  apiPut('/api/admin/calendar', {holidays: h.dates, extraWorkdays: x.dates}).then(function(c){
    orgCalendar = c;
    document.getElementById('calHolidays').value = formatDateList(c.holidays);
    document.getElementById('calExtra').value = formatDateList(c.extraWorkdays);
    refreshCalendarPresets();
    invalidateInsights();
    if(currentProjectId) loadProjectData(currentProjectId, currentView, true);
    toast('Календарь сохранён');
  }).catch(function(){});
});

var HEALTH_BADGES = {ok: '<span class="badge badge-ok">✓ В порядке</span>', warn: '<span class="badge badge-risk">⚠ Требует внимания</span>',
  error: '<span class="badge badge-danger">✗ Ошибка</span>', off: '<span class="badge badge-muted">Не настроено</span>'};
var HEALTH_HEADLINES = {ok: 'Всё работает', warn: 'Есть замечания', error: 'Есть неполадки'};
function setHealthDot(status){
  var dot = document.getElementById('navHealthDot');
  dot.hidden = status === 'ok';
  dot.className = 'health-dot health-' + status;
  dot.title = HEALTH_HEADLINES[status] || '';
}
function renderHealth(h){
  setHealthDot(h.status);
  document.getElementById('healthBody').innerHTML =
    '<div class="insight-head insight-head-'+(h.status === 'warn' ? 'risk' : (h.status === 'error' ? 'danger' : 'ok'))+'"><div class="insight-headline">'+HEALTH_HEADLINES[h.status]+'</div>'+
    '<div class="insight-fact-hint">Проверено '+fmtDateTime(h.checkedAt)+'</div></div>'+
    '<div class="health-list">' + h.items.map(function(i){
      return '<div class="health-item health-'+i.status+'" data-key="'+i.key+'"><div class="health-item-head"><b>'+escapeHtml(i.title)+'</b>'+HEALTH_BADGES[i.status]+'</div>'+
        '<div class="health-summary">'+escapeHtml(i.summary)+'</div>'+
        (i.details.length ? '<ul class="health-details">'+i.details.map(function(d){ return '<li>'+escapeHtml(d)+'</li>'; }).join('')+'</ul>' : '')+'</div>';
    }).join('') + '</div>';
  document.getElementById('haEnabled').checked = h.alerts.enabled;
  document.getElementById('haEmail').checked = h.alerts.email;
  document.getElementById('haTelegram').checked = h.alerts.telegram;
  document.getElementById('haRecipients').textContent = h.recipients.length ?
    'Получатели: ' + h.recipients.map(function(r){ return r.name + ' (' + r.channels.map(function(c){ return c === 'email' ? 'почта' : 'Telegram'; }).join(', ') + ')'; }).join('; ') :
    'Получателей нет: укажите email администратора в «Пользователях» или привяжите Telegram в профиле, а почту и бота подключите в «Интеграциях».';
}
function renderHealthView(){
  api('/api/admin/health').then(renderHealth).catch(function(){ document.getElementById('healthBody').innerHTML = '<p class="insight-empty">Не удалось проверить состояние.</p>'; });
}
document.getElementById('healthRefresh').addEventListener('click', renderHealthView);
document.getElementById('healthCheckJira').addEventListener('click', function(e){
  e.target.disabled = true;
  apiPost('/api/admin/health/check-jira', {}).then(function(h){ renderHealth(h); toast('Jira проверена'); })
    .catch(function(){}).then(function(){ e.target.disabled = false; });
});
document.getElementById('healthAlertsForm').addEventListener('submit', function(e){
  e.preventDefault();
  apiPut('/api/admin/health/alerts', {enabled: document.getElementById('haEnabled').checked, email: document.getElementById('haEmail').checked,
    telegram: document.getElementById('haTelegram').checked}).then(function(h){ renderHealth(h); toast('Настройки оповещений сохранены'); }).catch(function(){});
});
document.getElementById('haTest').addEventListener('click', function(){
  apiPost('/api/admin/health/test-alert', {}).then(function(r){ toast(r.sent ? 'Отправлено сообщений: ' + r.sent : 'Не удалось отправить — см. журнал отправок в «Интеграциях»'); }).catch(function(){});
});

/* ════════════════════════════════════════════════════════════════
   Уведомления: подписка пользователя на проект (дайджест и оповещения)
════════════════════════════════════════════════════════════════ */
var notifyState = null;
var nfForm = document.getElementById('notifyForm');

function renderNotifyView(){
  var pid = currentProjectId;
  api('/api/projects/'+pid+'/notifications').then(function(data){
    if(pid !== currentProjectId) return;
    notifyState = data;
    fillNotifyForm(data);
  }).catch(function(){});
}

function fillNotifyForm(d){
  var ch = d.channels;
  document.getElementById('nfViaEmail').checked = d.viaEmail;
  document.getElementById('nfViaTelegram').checked = d.viaTelegram;
  document.getElementById('nfEmailState').innerHTML = !ch.emailReady ?
    '<span class="muted-cell">Администратор ещё не настроил отправку почты</span>' :
    (ch.email ? escapeHtml(ch.email) : '<span class="prob-low">В профиле не указан email — обратитесь к администратору</span>');
  document.getElementById('nfTelegramState').innerHTML = !ch.telegramReady ?
    '<span class="muted-cell">Администратор ещё не подключил Telegram-бота</span>' :
    (ch.telegramLinked ? '<span class="prob-high">✓ Привязан</span>' +
      (ch.telegramCommands ? ' <span class="muted-cell">· спросите бота: /status, /risks, /week</span>' : '') :
      '<span class="muted-cell">Не привязан</span>');
  document.getElementById('nfTelegramLink').hidden = !ch.telegramReady || ch.telegramLinked;
  document.getElementById('nfTelegramUnlink').hidden = !ch.telegramLinked;
  var dg = d.digest;
  document.getElementById('nfDigestOn').checked = dg.enabled;
  document.getElementById('nfDays').innerHTML = WEEKDAYS.map(function(w){
    return '<label><input type="checkbox" data-day="'+w[0]+'"'+(dg.days.indexOf(w[0]) !== -1 ? ' checked' : '')+'><span>'+w[1]+'</span></label>';
  }).join('');
  document.getElementById('nfTime').value = dg.time;
  var browserTz = '';
  try { browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch(e){}
  var zones = TIMEZONES.slice();
  [dg.timezone, browserTz].forEach(function(z){ if(z && zones.indexOf(z) === -1) zones.push(z); });
  document.getElementById('nfTz').innerHTML = zones.map(function(z){
    return '<option value="'+escapeAttr(z)+'"'+(z === dg.timezone ? ' selected' : '')+'>'+escapeHtml(z)+(z === browserTz ? ' — ваш' : '')+'</option>';
  }).join('');
  document.getElementById('nfPdf').checked = dg.pdf;
  document.getElementById('nfDigestStatus').innerHTML =
    '<dt>Последний дайджест</dt><dd>'+(dg.lastAt ? fmtDateTime(dg.lastAt) + ' — <span class="autofill-result '+(dg.lastStatus === 'ok' ? 'ok' : 'error')+'">'+escapeHtml(dg.lastMessage || '')+'</span>' : '—')+'</dd>'+
    '<dt>Следующий</dt><dd>'+(dg.enabled && dg.nextAt ? fmtLocalIso(dg.nextAt) + ' <span class="muted-cell">('+escapeHtml(dg.timezone)+')</span>' : '<span class="muted-cell">дайджест выключен</span>')+'</dd>';
  var al = d.alerts;
  document.getElementById('nfAlertsOn').checked = al.enabled;
  document.querySelectorAll('#nfAlertTypes [data-type]').forEach(function(cb){ cb.checked = al.types.indexOf(cb.dataset.type) !== -1; });
  document.getElementById('nfChance').value = al.chanceThreshold;
  document.getElementById('nfScope').value = al.scopeThreshold;
}

function notifyFormData(){
  return {
    viaEmail: document.getElementById('nfViaEmail').checked,
    viaTelegram: document.getElementById('nfViaTelegram').checked,
    digest: {enabled: document.getElementById('nfDigestOn').checked,
             days: Array.prototype.map.call(document.querySelectorAll('#nfDays [data-day]:checked'), function(c){ return parseInt(c.dataset.day, 10); }),
             time: document.getElementById('nfTime').value, timezone: document.getElementById('nfTz').value,
             pdf: document.getElementById('nfPdf').checked},
    alerts: {enabled: document.getElementById('nfAlertsOn').checked,
             types: Array.prototype.map.call(document.querySelectorAll('#nfAlertTypes [data-type]:checked'), function(c){ return c.dataset.type; }),
             chanceThreshold: parseInt(document.getElementById('nfChance').value, 10) || 50,
             scopeThreshold: parseInt(document.getElementById('nfScope').value, 10) || 10}
  };
}

nfForm.addEventListener('submit', function(e){
  e.preventDefault();
  apiPut('/api/projects/'+currentProjectId+'/notifications', notifyFormData()).then(function(data){
    notifyState = data;
    fillNotifyForm(data);
    toast('Настройки уведомлений сохранены');
  }).catch(function(){});
});

document.getElementById('nfPreview').addEventListener('click', function(){
  api('/api/projects/'+currentProjectId+'/notifications/preview').then(function(data){
    openModal({title: data.subject, size: 'xl', okLabel: 'Закрыть', noCancel: true,
      html: '<iframe class="digest-preview" title="Предпросмотр дайджеста" sandbox></iframe>'});
    confirmBody.querySelector('iframe').srcdoc = '<!doctype html><meta charset="utf-8"><body style="margin:16px;background:#fff">' + data.html;
  }).catch(function(){});
});

document.getElementById('nfSendNow').addEventListener('click', function(){
  var btn = this;
  btn.disabled = true;
  apiPost('/api/projects/'+currentProjectId+'/notifications/test').then(function(data){
    toast('Дайджест отправлен: ' + data.results.map(function(r){ return r.channel === 'email' ? 'почта' : 'Telegram'; }).join(', '));
  }).catch(function(){}).then(function(){ btn.disabled = false; renderNotifyView(); });
});

document.getElementById('nfTelegramLink').addEventListener('click', function(){
  apiPost('/api/profile/telegram/link').then(function(link){
    openModal({
      title: 'Привязать Telegram', okLabel: 'Проверить',
      html: '<ol class="telegram-steps">'+
        '<li>Откройте бота <b>@'+escapeHtml(link.botName)+'</b> по ссылке: <a href="'+escapeAttr(link.url)+'" target="_blank" rel="noopener">'+escapeHtml(link.url)+'</a></li>'+
        '<li>Нажмите в Telegram <b>Запустить</b> (Start).</li>'+
        '<li>Вернитесь сюда и нажмите <b>Проверить</b>.</li></ol>'+
        '<p class="bulk-note">Ссылка действует, пока вы не запросите новую. Бот пришлёт подтверждение.</p>',
      onOk: function(){
        apiPost('/api/profile/telegram/check').then(function(r){
          if(!r.linked){ toast('Бот ещё не получил команду Start — откройте ссылку и нажмите «Запустить»'); return; }
          closeModal();
          toast('Telegram привязан');
          renderNotifyView();
        }).catch(function(){});
        return false;
      }
    });
  }).catch(function(){});
});

document.getElementById('nfTelegramUnlink').addEventListener('click', function(){
  askConfirm('Отвязать Telegram?', 'Сообщения в Telegram перестанут приходить по всем проектам.', function(){
    apiDelete('/api/profile/telegram').then(function(){ toast('Telegram отвязан'); renderNotifyView(); });
  }, 'Отвязать');
});

/* ════════════════════════════════════════════════════════════════
   Администрирование: рассылки (SMTP, Telegram-бот, журнал отправок)
════════════════════════════════════════════════════════════════ */
var NOTIFY_KINDS = {digest: 'Дайджест', alert: 'Оповещение', test: 'Проверка'};

function renderNotifyAdminView(){
  api('/api/admin/notify-settings').then(fillNotifyAdmin).catch(function(){});
  loadNotifyLog();
}

function fillNotifyAdmin(d){
  document.getElementById('naHost').value = d.smtp.host;
  document.getElementById('naPort').value = d.smtp.port;
  document.getElementById('naSecurity').value = d.smtp.security;
  document.getElementById('naSender').value = d.smtp.sender;
  document.getElementById('naLogin').value = d.smtp.login;
  var pw = document.getElementById('naPassword');
  pw.value = '';
  pw.placeholder = d.smtp.hasPassword ? 'Сохранён — оставьте пустым, чтобы не менять' : 'если сервер требует входа';
  var token = document.getElementById('naToken');
  token.value = '';
  token.placeholder = d.telegram.hasToken ? 'Сохранён — оставьте пустым, чтобы не менять' : '123456:ABC-DEF…';
  var st = document.getElementById('naBotStatus');
  st.textContent = !d.telegram.hasToken ? 'Бот не подключён' : (d.telegram.botName ? '✓ Бот @' + d.telegram.botName : 'Токен сохранён, бот не проверен');
  st.className = 'jira-status' + (d.telegram.botName ? ' ok' : '');
  document.getElementById('naBotRemove').hidden = !d.telegram.hasToken;
  document.getElementById('naCommands').checked = d.telegram.commands;
  document.getElementById('naCommandsNote').textContent = 'Привязанный пользователь спрашивает бота о своих проектах — статус и главное, ' +
    'риски, изменения за неделю.' + (d.telegram.hasToken && d.telegram.commands ? (d.telegram.pollTick ?
    ' Бот на связи: последний опрос — ' + fmtDateTime(d.telegram.pollTick) + '.' : ' Бот начнёт отвечать в течение минуты.') : '');
  document.getElementById('naUrl').value = d.publicUrl;
}

function notifyAdminData(extra){
  return Object.assign({
    smtp: {host: document.getElementById('naHost').value.trim(), port: parseInt(document.getElementById('naPort').value, 10) || null,
           security: document.getElementById('naSecurity').value, sender: document.getElementById('naSender').value.trim(),
           login: document.getElementById('naLogin').value.trim(), password: document.getElementById('naPassword').value},
    telegram: {token: document.getElementById('naToken').value.trim(), commands: document.getElementById('naCommands').checked},
    publicUrl: document.getElementById('naUrl').value.trim()
  }, extra || {});
}

function saveNotifyAdmin(extra, message){
  return apiPut('/api/admin/notify-settings', notifyAdminData(extra)).then(function(d){
    fillNotifyAdmin(d);
    if(message) toast(message);
    return d;
  });
}

document.getElementById('notifyAdminForm').addEventListener('submit', function(e){
  e.preventDefault();
  saveNotifyAdmin(null, 'Настройки рассылок сохранены').catch(function(){});
});
document.getElementById('naTestEmail').addEventListener('click', function(){
  var to = document.getElementById('naTestTo').value.trim();
  if(!to){ toast('Укажите, куда отправить тестовое письмо'); return; }
  var btn = this;
  btn.disabled = true;
  saveNotifyAdmin().then(function(){
    return apiPost('/api/admin/notify-settings/test-email', {to: to});
  }).then(function(){ toast('Тестовое письмо отправлено на ' + to); }).catch(function(){}).then(function(){
    btn.disabled = false;
    loadNotifyLog();
  });
});
document.getElementById('naCommands').addEventListener('change', function(e){
  saveNotifyAdmin(null, e.target.checked ? 'Бот отвечает на команды' : 'Команды бота выключены').catch(function(){});
});
document.getElementById('naBotTest').addEventListener('click', function(){
  saveNotifyAdmin().then(function(){
    return apiPost('/api/admin/notify-settings/test-telegram');
  }).then(function(r){ toast('Бот @' + r.botName + ' отвечает'); renderNotifyAdminView(); }).catch(function(){});
});
document.getElementById('naBotRemove').addEventListener('click', function(){
  askConfirm('Отключить Telegram-бота?', 'Сообщения в Telegram перестанут приходить всем пользователям; привязки сохранятся.', function(){
    saveNotifyAdmin({telegram: {clearToken: true}}, 'Telegram-бот отключён').catch(function(){});
  }, 'Отключить');
});
document.getElementById('naLogReload').addEventListener('click', loadNotifyLog);

function loadNotifyLog(){
  var table = document.getElementById('notifyLogTable');
  api('/api/admin/notify-log').then(function(rows){
    if(!rows.length){ emptyTable(table, 'Отправок пока не было', 'Здесь появятся дайджесты, оповещения и тестовые отправки.'); return; }
    table.innerHTML = '<thead><tr><th>Время</th><th>Кому</th><th>Проект</th><th>Что</th><th>Канал</th><th>Результат</th></tr></thead><tbody>'+
      rows.map(function(r){
        return '<tr><td class="mono nowrap">'+fmtDateTime(r.createdAt)+'</td><td>'+escapeHtml(r.username || '—')+'</td><td>'+escapeHtml(r.projectName || '—')+'</td>'+
          '<td>'+escapeHtml(NOTIFY_KINDS[r.kind] || r.kind)+'</td><td>'+(r.channel === 'email' ? 'Почта' : 'Telegram')+'</td>'+
          '<td><span class="autofill-result '+(r.status === 'ok' ? 'ok' : 'error')+'">'+(r.status === 'ok' ? '✓ ' : '✗ ')+escapeHtml(r.message || (r.status === 'ok' ? 'отправлено' : ''))+'</span></td></tr>';
      }).join('')+'</tbody>';
  }).catch(function(){});
}


/* ════════════════════════════════════════════════════════════════
   Автозаполнение: срез по кнопке по расписанию (выполняет сервер)
════════════════════════════════════════════════════════════════ */
var WEEKDAYS = [[1, 'Пн'], [2, 'Вт'], [3, 'Ср'], [4, 'Чт'], [5, 'Пт'], [6, 'Сб'], [7, 'Вс']];
var TIMEZONES = ['Europe/Kaliningrad', 'Europe/Moscow', 'Europe/Samara', 'Asia/Yekaterinburg', 'Asia/Omsk',
  'Asia/Novosibirsk', 'Asia/Krasnoyarsk', 'Asia/Irkutsk', 'Asia/Yakutsk', 'Asia/Vladivostok', 'Asia/Magadan',
  'Asia/Kamchatka', 'Europe/Minsk', 'Asia/Almaty', 'Asia/Tashkent', 'Europe/London', 'Europe/Berlin', 'UTC'];

function scheduleSummary(s){
  var days = s.days.length === 7 ? 'ежедневно' : (s.days.join(',') === '1,2,3,4,5' ? 'по будням' :
    WEEKDAYS.filter(function(d){ return s.days.indexOf(d[0]) !== -1; }).map(function(d){ return d[1]; }).join(', '));
  return days + ' в ' + s.time;
}
function fmtLocalIso(v){
  if(!v) return '—';
  return v.slice(0, 10).split('-').reverse().join('.') + ' ' + v.slice(11, 16);
}

function renderAutofillView(){
  var box = document.getElementById('autofillList');
  var cfg = STATE.jira;
  if(!cfg){ box.innerHTML = ''; return; }
  if(!cfg.connected || !cfg.buttons.length){
    box.innerHTML = '<div class="empty-cell autofill-empty">'+(!cfg.connected ?
      (isGlobalAdmin() ? 'Сначала настройте подключение к Jira в разделе «Интеграции».' : 'Подключение к Jira не настроено — обратитесь к администратору.') :
      'Нет кнопок среза. Создайте кнопку в разделе «Jira и Confluence», выполните по ней срез — после этого здесь можно включить расписание.')+'</div>';
    return;
  }
  var browserTz = '';
  try { browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch(e){}
  box.innerHTML = cfg.buttons.map(function(b){
    var s = b.schedule;
    var locked = !b.lastSuccessAt;
    var zones = TIMEZONES.slice();
    [s.timezone, browserTz].forEach(function(z){ if(z && zones.indexOf(z) === -1) zones.push(z); });
    var status = locked ?
      '<div class="autofill-lock">🔒 Расписание станет доступно после первого успешного среза по этой кнопке. Выполните срез в разделе «Данные по датам» или «Jira и Confluence».</div>' :
      '<dl class="autofill-status">'+
        '<dt>Последний успешный срез</dt><dd>'+fmtDateTime(b.lastSuccessAt)+'</dd>'+
        '<dt>Последний автозапуск</dt><dd>'+(s.lastRunAt ? fmtDateTime(s.lastRunAt) + ' — <span class="autofill-result '+(s.lastStatus === 'ok' ? 'ok' : 'error')+'">'+escapeHtml(s.lastMessage || '')+'</span>' : '—')+'</dd>'+
        '<dt>Следующий запуск</dt><dd>'+(s.enabled && s.nextRunAt ? fmtLocalIso(s.nextRunAt) + ' <span class="muted-cell">('+escapeHtml(s.timezone)+')</span>' : '<span class="muted-cell">расписание выключено</span>')+'</dd>'+
        (s.onEvents ? '<dt>По событиям Jira</dt><dd>'+(s.eventsRunAt ? fmtDateTime(s.eventsRunAt) + ' — <span class="autofill-result '+(s.eventsStatus === 'ok' ? 'ok' : 'error')+'">'+escapeHtml(s.eventsMessage || '')+'</span>' : '<span class="muted-cell">событий ещё не было</span>')+'</dd>' : '')+
      '</dl>';
    return '<form class="autofill-card'+(locked ? ' locked' : '')+'" data-bid="'+b.id+'" novalidate>'+
      '<div class="autofill-head"><div><div class="autofill-title">'+escapeHtml(b.name)+'</div>'+
        '<div class="section-note">'+(b.structureId ? 'Структура «'+escapeHtml(b.structureName || ('#'+b.structureId))+'»' : 'Без структуры')+' · эпиков: '+b.mappings.length+'</div></div>'+
        '<label class="switch"><input type="checkbox" data-f="enabled"'+(s.enabled ? ' checked' : '')+(locked ? ' disabled' : '')+'> <span>Автозаполнение включено</span></label></div>'+
      status+
      '<fieldset class="autofill-fields"'+(locked ? ' disabled' : '')+'>'+
        '<div class="setting-item"><span class="field-label">Дни недели</span><div class="weekday-picker">'+WEEKDAYS.map(function(d){
          return '<label><input type="checkbox" data-day="'+d[0]+'"'+(s.days.indexOf(d[0]) !== -1 ? ' checked' : '')+'><span>'+d[1]+'</span></label>';
        }).join('')+'</div></div>'+
        '<div class="setting-item"><label class="small" for="afTime'+b.id+'">Время запуска</label><input type="time" id="afTime'+b.id+'" data-f="time" value="'+escapeAttr(s.time)+'" required></div>'+
        '<div class="setting-item"><label class="small" for="afTz'+b.id+'">Часовой пояс</label><select id="afTz'+b.id+'" data-f="timezone">'+zones.map(function(z){
          return '<option value="'+escapeAttr(z)+'"'+(z === s.timezone ? ' selected' : '')+'>'+escapeHtml(z)+(z === browserTz ? ' — ваш' : '')+'</option>';
        }).join('')+'</select></div>'+
        '<label class="quick-filter-item check-wrap autofill-overwrite"><input type="checkbox" data-f="overwrite"'+(s.overwrite ? ' checked' : '')+'> Перезаписывать замер, если на эту дату он уже внесён</label>'+
        '<label class="quick-filter-item check-wrap autofill-overwrite" title="Работает, если администратор включил вебхук Jira в разделе «Интеграции»"><input type="checkbox" data-f="onEvents"'+(s.onEvents ? ' checked' : '')+'> Также обновлять замер на сегодня при изменениях задач в Jira (вебхук)</label>'+
      '</fieldset>'+
      '<div class="form-actions">'+
        '<button type="button" class="btn btn-ghost btn-sm" data-action="run"'+(locked ? ' disabled' : '')+'>Запустить сейчас</button>'+
        '<button type="submit" class="btn btn-primary btn-sm"'+(locked ? ' disabled' : '')+'>Сохранить расписание</button>'+
      '</div></form>';
  }).join('');
  box.querySelectorAll('.autofill-card').forEach(function(form){
    var bid = parseInt(form.dataset.bid, 10);
    form.addEventListener('submit', function(e){
      e.preventDefault();
      var body = {
        enabled: form.querySelector('[data-f="enabled"]').checked,
        days: Array.prototype.map.call(form.querySelectorAll('[data-day]:checked'), function(c){ return parseInt(c.dataset.day, 10); }),
        time: form.querySelector('[data-f="time"]').value,
        timezone: form.querySelector('[data-f="timezone"]').value,
        overwrite: form.querySelector('[data-f="overwrite"]').checked,
        onEvents: form.querySelector('[data-f="onEvents"]').checked
      };
      if(body.enabled && !body.days.length){ toast('Выберите хотя бы один день недели'); return; }
      if(!body.time){ toast('Укажите время запуска'); return; }
      apiPut('/api/jira/buttons/'+bid+'/schedule', body).then(function(saved){
        replaceJiraButton(saved);
        renderAutofillView();
        toast(saved.schedule.enabled ? 'Расписание сохранено: ' + scheduleSummary(saved.schedule) + (saved.schedule.onEvents ? ' и по событиям Jira' : '') :
          (saved.schedule.onEvents ? 'Автозаполнение — по событиям Jira' : 'Автозаполнение выключено'));
      }).catch(function(){});
    });
    form.querySelector('[data-action="run"]').addEventListener('click', function(){
      var btn = this;
      btn.disabled = true;
      btn.textContent = 'Срез из Jira…';
      apiPost('/api/jira/buttons/'+bid+'/schedule/run').then(function(data){
        replaceJiraButton(data.button);
        renderAutofillView();
        toast(data.message);
        loadProjectData(currentProjectId);
      }).catch(function(){
        /* ошибку показал api(); статус запуска обновим */
        loadJiraConfig();
      }).then(function(){ btn.disabled = false; btn.textContent = 'Запустить сейчас'; });
    });
  });
}


document.getElementById('btnAddService').addEventListener('click', function(){
  if(!canEdit()){ toast('Нет прав на редактирование'); return; }
  var last = STATE.services[STATE.services.length-1];
  var svc = {etap: last ? last.etap : 'Этап 1', name: 'Новый эпик', targetDate: todayIso()};
  apiPost('/api/projects/'+currentProjectId+'/services', svc).then(function(data){
    var maxOrder = STATE.services.reduce(function(m, s){ return Math.max(m, s.sortOrder||0); }, 0);
    STATE.services.push(Object.assign(svc, {id:data.id, project_id:currentProjectId, isDone:false, stageGroup:'', sortOrder:maxOrder+1}));
    if(!currentDataServiceId) currentDataServiceId = data.id;
    renderServicesView();
    populateServiceSelects();
    renderDashboardFilterBar();
    toast('Эпик добавлен');
  });
});

/* Диапазон дат таблиц burn rate (дашборд и свод по этапам) */
function bindBurnRange(fromId, toId, resetId, setRange, render){
  var fromEl = document.getElementById(fromId), toEl = document.getElementById(toId);
  var onChange = function(){ setRange({from: fromEl.value || null, to: toEl.value || null}); saveViewState(); render(); };
  fromEl.addEventListener('change', onChange);
  toEl.addEventListener('change', onChange);
  document.getElementById(resetId).addEventListener('click', function(){
    fromEl.value = ''; toEl.value = '';
    setRange(null); saveViewState(); render();
  });
}
bindBurnRange('burnFrom', 'burnTo', 'burnResetRange', function(r){ burnRangeOverride = r; }, renderBurnRateTable);
bindBurnRange('stageBurnFrom', 'stageBurnTo', 'stageBurnResetRange', function(r){ stageBurnRangeOverride = r; }, renderStageBurnRateTable);

document.getElementById('btnExport').addEventListener('click', function(){
  if(!currentProjectId){ toast('Нет выбранного проекта'); return; }
  var a = document.createElement('a');
  a.href = '/api/projects/'+currentProjectId+'/export';
  a.download = '';
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast('Экспорт JSON скачивается');
});

/* Чтение фото с проверкой размера; cb(dataUrl) */
var MAX_PHOTO_BYTES = 1572864;
function readPhoto(file, cb){
  if(!file) return;
  if(file.size > MAX_PHOTO_BYTES){ toast('Файл слишком большой (макс. 1,5 МБ)'); return; }
  var reader = new FileReader();
  reader.onload = function(ev){ cb(ev.target.result); };
  reader.readAsDataURL(file);
}

/* Профиль — открывается по клику на блок пользователя */
/* ════════════════════════════════════════════════════════════════
   О программе, история версий и руководства (как в todo-planner: версия внизу меню открывает историю)
════════════════════════════════════════════════════════════════ */
var aboutInfo = null;
function loadAbout(){
  return aboutInfo ? Promise.resolve(aboutInfo) : api('/api/about').then(function(d){ aboutInfo = d; return d; });
}
function manualRowHtml(m){
  var has = function(ext){ return m.formats.indexOf(ext) !== -1; };
  return '<div class="about-manual"><span class="about-manual-title">'+escapeHtml(m.title)+'</span><span class="about-manual-links">'+
    (m.online ? '<a href="#/guide'+(m.id !== 'user-guide' ? '?doc=' + m.id : '')+'" data-guide-open="'+m.id+'">Читать онлайн</a>' : '')+
    (has('pdf') ? '<a href="/manuals/'+m.id+'.pdf" target="_blank" rel="noopener">Открыть</a>'+
                  '<a href="/manuals/'+m.id+'.pdf?download=1" download>Скачать PDF</a>' : '')+
    (has('docx') ? '<a href="/manuals/'+m.id+'.docx" download>Скачать Word</a>' : '')+'</span></div>';
}
function openAbout(){
  loadAbout().then(function(d){
    openModal({
      title: 'О программе', okLabel: 'Закрыть', noCancel: true, wide: true,
      html: '<div class="about">'+
        '<div class="about-brand">'+document.querySelector('.brand svg').outerHTML+
          '<div><div class="about-name">'+escapeHtml(d.name)+'</div><div class="about-tagline">Трекер сгорания работ по эпикам</div></div></div>'+
        '<dl class="about-facts"><dt>Версия</dt><dd>'+escapeHtml(d.version)+(d.date ? ' · ' + escapeHtml(d.date) : '')+'</dd>'+
          '<dt>Автор</dt><dd>© Александр Юрасов, 2026</dd></dl>'+
        '<p class="about-text">Burndown показывает, успевают ли эпики и этапы к целевым датам: остаток работ по замерам, прогноз завершения и шанс успеть, '+
          'состав задач из Jira, дайджест и оповещения на почту и в Telegram.</p>'+
        (d.manuals.length ? '<div class="about-section-title">Руководства</div>' + d.manuals.map(manualRowHtml).join('') : '')+
        '<button type="button" class="btn btn-ghost btn-sm about-versions" id="aboutVersions">История версий</button>'+
      '</div>'
    });
    document.getElementById('aboutVersions').addEventListener('click', openVersions);
    document.querySelectorAll('[data-guide-open]').forEach(function(a){
      a.addEventListener('click', function(e){
        e.preventDefault();
        closeModal();
        guideState.doc = a.dataset.guideOpen;
        guideState.section = null;
        navigate('guide');
        window.scrollTo({top: 0, behavior: 'instant'});
      });
    });
  }).catch(function(){});
}
function openVersions(){
  loadAbout().then(function(d){
    openModal({
      title: 'История версий', okLabel: 'Закрыть', noCancel: true, wide: true,
      html: '<p class="versions-intro">Что нового в Burndown — изменения по версиям, новые сверху.</p><div class="versions-list">'+
        d.releases.map(function(r, i){
          return '<details class="release'+(i === 0 ? ' latest' : '')+'"'+(i === 0 ? ' open' : '')+'>'+
            '<summary><span class="release-mark">'+(i === 0 ? 'NEW' : 'v')+'</span><span class="release-head">'+
              '<strong>Версия '+escapeHtml(r.version)+'</strong>'+(r.date ? ' <span class="release-date">'+escapeHtml(r.date)+'</span>' : '')+
              (r.title ? '<small>'+escapeHtml(r.title)+'</small>' : '')+'</span></summary>'+
            '<div class="release-body">'+r.sections.map(function(sec){
              return '<h4>'+escapeHtml(sec.title)+'</h4>'+
                sec.paragraphs.map(function(p){ return '<p>'+escapeHtml(p)+'</p>'; }).join('')+
                (sec.items.length ? '<ul>'+sec.items.map(function(it){ return '<li>'+escapeHtml(it)+'</li>'; }).join('')+'</ul>' : '');
            }).join('')+'</div></details>';
        }).join('')+'</div>'
    });
  }).catch(function(){});
}
document.getElementById('btnVersions').addEventListener('click', openVersions);
document.getElementById('btnAbout').addEventListener('click', openAbout);

document.getElementById('defaultPwWarning').addEventListener('click', function(){
  openProfile();
  var pw = document.getElementById('profilePassword');
  if(pw) pw.focus();
});

function openProfile(){
  var admin = isGlobalAdmin();
  var photo = currentUser.photo || '';  /* итоговое фото после правок в окне */
  var dis = admin ? '' : ' disabled';
  openModal({
    title: 'Профиль', okLabel: 'Сохранить', wide: true,
    html:
      '<div class="profile-grid">'+
        '<div class="profile-photo-section">'+
          '<div id="profileAvatarBox">'+userAvatarHtml(currentUser, 'profile-avatar')+'</div>'+
          '<div class="profile-photo-btns">'+
            '<label class="btn btn-ghost btn-sm file-btn">Загрузить фото<input type="file" accept="image/*" id="profilePhotoFile" class="visually-hidden"></label>'+
            '<button type="button" class="btn btn-ghost btn-sm text-danger" id="profileDeletePhoto"'+(photo?'':' hidden')+'>Удалить фото</button>'+
          '</div>'+
          '<div class="profile-photo-hint">JPG или PNG, до 1,5 МБ</div>'+
        '</div>'+
        '<div class="profile-fields">'+
          '<div>'+
            '<div class="profile-section-title">Учётная запись</div>'+
            '<div class="profile-info-row"><span class="profile-info-label">Логин</span><span class="profile-info-value">'+escapeHtml(currentUser.username)+'</span></div>'+
            '<div class="profile-info-row"><span class="profile-info-label">Роль</span><span class="profile-info-value">'+roleBadge(currentUser.role)+'</span></div>'+
          '</div>'+
          '<div class="form-stack">'+
            '<div class="profile-section-title">Личные данные</div>'+
            field('ФИО', '<input type="text" id="profileDisplayName" value="'+escapeAttr(currentUser.displayName||'')+'"'+dis+'>')+
            field('Email', '<input type="email" id="profileEmail" value="'+escapeAttr(currentUser.email||'')+'"'+dis+'>')+
            field('Должность', '<input type="text" id="profilePosition" value="'+escapeAttr(currentUser.position||'')+'"'+dis+'>')+
            field('Новый пароль', '<input type="password" id="profilePassword" placeholder="Не менять" autocomplete="new-password">')+
          '</div>'+
        '</div>'+
      '</div>',
    onOk: function(){
      var data = {};
      if(photo !== (currentUser.photo || '')) data.photo = photo;
      if(admin){
        data.displayName = document.getElementById('profileDisplayName').value;
        data.email = document.getElementById('profileEmail').value;
        data.position = document.getElementById('profilePosition').value;
      }
      var pw = document.getElementById('profilePassword').value;
      if(pw) data.password = pw;
      apiPut('/api/profile', data).then(function(){
        Object.assign(currentUser, data);
        if(data.password) currentUser.defaultPassword = false;
        delete currentUser.password;
        updateRoleUI();
        toast('Профиль обновлён');
      });
    }
  });

  var avatarBox = document.getElementById('profileAvatarBox');
  var delBtn = document.getElementById('profileDeletePhoto');
  var showPhoto = function(){
    avatarBox.innerHTML = userAvatarHtml({photo:photo, displayName:currentUser.displayName, username:currentUser.username}, 'profile-avatar');
    delBtn.hidden = !photo;
  };
  document.getElementById('profilePhotoFile').addEventListener('change', function(e){
    readPhoto(e.target.files[0], function(dataUrl){ photo = dataUrl; showPhoto(); });
  });
  delBtn.addEventListener('click', function(){ photo = ''; showPhoto(); });
}
var userInfoEl = document.getElementById('userInfo');
userInfoEl.addEventListener('click', function(){ if(currentUser) openProfile(); });
userInfoEl.addEventListener('keydown', function(e){ if((e.key === 'Enter' || e.key === ' ') && currentUser){ e.preventDefault(); openProfile(); } });

/* Add user form */
var newUserPhoto = '';
function resetNewUserPhoto(){
  newUserPhoto = '';
  document.getElementById('newUserPhotoPreview').innerHTML = '?';
  document.getElementById('newUserPhotoFile').value = '';
}
document.getElementById('addUserForm').addEventListener('submit', function(e){
  e.preventDefault();
  var username = document.getElementById('newUsername').value.trim();
  var password = document.getElementById('newPassword').value;
  if(!username || !password){ toast('Укажите логин и пароль'); return; }
  apiPost('/api/users', {
    username:username, password:password,
    displayName:document.getElementById('newDisplayName').value,
    email:document.getElementById('newEmail').value,
    position:document.getElementById('newPosition').value,
    role:document.getElementById('newRole').value,
    photo:newUserPhoto
  }).then(function(data){
    document.getElementById('addUserForm').reset();
    resetNewUserPhoto();
    selectedAdminUser = data.id;
    toast('Пользователь добавлен');
    loadUsers().then(renderUserProjectsMgmt);
  });
});

/* New user photo upload */
document.getElementById('newUserPhotoFile').addEventListener('change', function(e){
  readPhoto(e.target.files[0], function(dataUrl){
    newUserPhoto = dataUrl;
    document.getElementById('newUserPhotoPreview').innerHTML = '<img src="'+escapeAttr(dataUrl)+'" alt="">';
  });
});

/* User search */
document.getElementById('userSearch').addEventListener('input', renderUsersTable);

/* Add project */
document.getElementById('btnAddProject').addEventListener('click', function(){ openProjectModal(null); });

/* ════════════════════════════════════════════════════════════════
   Project loading & switching
════════════════════════════════════════════════════════════════ */
function renderProjectsNav(){
  var nav = document.getElementById('projectsNav');
  document.getElementById('overviewNav').hidden = projects.length < 2;  /* портфель — когда проектов несколько */
  var html = '';
  if(!projects.length){
    nav.innerHTML = html + '<div class="nav-empty">Нет доступных проектов</div>';
    return;
  }
  var palette = colorPalette();
  projects.forEach(function(p, i){
    html += '<a href="#" class="nav-project'+(p.id === currentProjectId ? ' active' : '')+'" data-pid="'+p.id+'" title="'+escapeAttr(p.description||p.name)+'">'+
      '<span class="dot" data-initial="'+escapeAttr((p.name || '?').trim().charAt(0).toUpperCase())+'" style="background:'+palette[i % palette.length]+'"></span>'+
      '<span class="nav-project-name">'+escapeHtml(p.name)+'</span></a>';
  });
  nav.innerHTML = html;
  nav.querySelectorAll('.nav-project').forEach(function(el){
    el.addEventListener('click', function(e){
      e.preventDefault();
      switchProject(parseInt(el.dataset.pid, 10));
    });
  });
}

function switchProject(pid){
  if(pid === currentProjectId){ if(GLOBAL_VIEWS.indexOf(currentView) !== -1) navigate('dashboard'); return; }
  var proj = projects.find(function(p){ return p.id === pid; });
  currentProjectId = pid;
  currentProjectRole = proj ? proj.role : null;
  currentDataServiceId = null;
  /* данные прежнего проекта не должны быть видны (и редактируемы) под новым, пока грузятся новые */
  STATE = { services:[], entries:[], stageTargets:{}, settings:Object.assign({}, DEFAULT_SETTINGS) };
  viewStateProject = pid;
  restoreViewState(pid);
  renderProjectsNav();
  updateRoleUI();
  /* из админ-разделов переходим к данным выбранного проекта */
  loadProjectData(pid, GLOBAL_VIEWS.indexOf(currentView) !== -1 ? 'dashboard' : currentView);
}

/* quiet — только обновить данные проекта, не перерисовывая текущий раздел (иначе затрётся то, что пользователь
   уже начал вводить) */
function loadProjectData(pid, view, quiet){
  return api('/api/projects/'+pid+'/data').then(function(data){
    if(pid !== currentProjectId) return; /* пользователь успел переключить проект */
    STATE.services = data.services || [];
    STATE.entries = data.entries || [];
    STATE.stageTargets = data.stageTargets || {};
    STATE.settings = Object.assign({}, DEFAULT_SETTINGS, data.settings || {});
    loadJiraConfig();
    loadJiraComposition();
    if(!serviceById(currentDataServiceId)) currentDataServiceId = STATE.services.length ? STATE.services[0].id : null;
    document.getElementById('navServiceCount').textContent = STATE.services.length;
    if(!quiet) navigate(view || currentView);
  });
}

function loadProjects(){
  return api('/api/projects').then(function(projs){
    projects = projs;
    var proj = projects.find(function(p){ return p.id === currentProjectId; });
    if(!proj && projects.length) proj = projects[0];
    if(proj && proj.id !== viewStateProject){ viewStateProject = proj.id; restoreViewState(proj.id); }
    currentProjectId = proj ? proj.id : null;
    currentProjectRole = proj ? proj.role : null;
    renderProjectsNav();
    updateRoleUI();
    if(currentProjectId) return loadProjectData(currentProjectId);
    STATE = { services:[], entries:[], stageTargets:{}, settings:Object.assign({}, DEFAULT_SETTINGS) };
    navigate(currentView);
  });
}

/* ════════════════════════════════════════════════════════════════
   Init
════════════════════════════════════════════════════════════════ */
function init(){
  api('/api/auth/me').then(function(data){
    currentUser = data.user;
    currentUser.defaultPassword = !!data.defaultPassword;
    updateRoleUI();
    /* раздел из адреса (#/раздел?project=…): после F5 — та же страница */
    var route = parseRoute();
    if(route){
      pendingRoute = route;
      currentView = route.view;
      if(route.tab) tabState[route.view] = route.tab;
      if(route.project) currentProjectId = route.project;
      if(route.view === 'data' && route.epic) currentDataServiceId = route.epic;
    }
    /* ссылка на проект из письма или ответа бота: /?project=3 */
    try {
      var linked = parseInt(new URLSearchParams(window.location.search).get('project'), 10);
      if(linked > 0){
        currentProjectId = linked;
        if(window.history && window.history.replaceState) window.history.replaceState(null, '', window.location.pathname + window.location.hash);
      }
    } catch(e){}
    loadProjects();
    if(isGlobalAdmin()) api('/api/admin/health').then(function(h){ setHealthDot(h.status); }).catch(function(){});
  }).catch(function(){
    window.location.href = '/login' + window.location.hash;  /* после входа — тот же раздел */
  });
}

/* ════════════════════════════════════════════════════════════════
   Audit log
════════════════════════════════════════════════════════════════ */
var auditPageLabels = {
  dashboard: 'Дашборд', stages: 'Свод по этапам', data: 'Данные по датам',
  services: 'Эпики', settings: 'Настройки', users: 'Пользователи',
  projects: 'Проекты', profile: 'Профиль', jira: 'Jira и Confluence', notify: 'Рассылки', integrations: 'Интеграции',
  calendar: 'Производственный календарь', health: 'Состояние системы', confluence: 'Отчёт в Confluence'
};
var auditActionLabels = { create: 'Создание', update: 'Изменение', delete: 'Удаление', autofill: 'Автозаполнение', restore: 'Восстановление истории',
  sync: 'Синхронизация с Jira', writeback: 'Запись в Jira', publish: 'Публикация' };
var auditEntityLabels = { project: 'Проект', service: 'Эпик', entry: 'Замер', user: 'Пользователь', stage_target: 'Цель этапа', settings: 'Настройки',
  jira_connection: 'Подключение к Jira', jira_button: 'Кнопка среза', jira_schedule: 'Расписание автозаполнения',
  jira_snapshot: 'Срезы задач Jira',
  notify_settings: 'Почта и Telegram', org_calendar: 'Производственный календарь', admin_alerts: 'Оповещения администратору' };
var auditFieldLabels = {
  name: 'Название', description: 'Описание', etap: 'Этап', targetDate: 'Целевая дата', isDone: 'Сдано', stageGroup: 'Группа в своде',
  date: 'Дата', remaining: 'Остаток', spent: 'Затрачено', estimate: 'Оценка',
  username: 'Логин', displayName: 'Имя', email: 'Email', position: 'Должность', role: 'Роль',
  jiraUrl: 'Адрес Jira', jiraAuth: 'Тип подключения', jiraLogin: 'Логин Jira', jiraToken: 'Токен Jira',
  autofill: 'Расписание', result: 'Результат',
  smtpHost: 'SMTP-сервер', smtpPort: 'Порт SMTP', smtpSecurity: 'Защита SMTP', smtpLogin: 'Логин SMTP', smtpFrom: 'Отправитель',
  smtpPassword: 'Пароль SMTP', telegramToken: 'Токен Telegram-бота', publicUrl: 'Адрес Burndown',
  forecastMethod: 'Метод прогноза', riskBufferDays: 'Буфер риска', burnrateMethod: 'Метод burn rate', burnrateWindowDays: 'Окно burn rate',
  rangeConfidence: 'Уровень доверия диапазона', workdays: 'Рабочие дни', holidays: 'Праздники', extraWorkdays: 'Рабочие выходные',
  units: 'Единицы', hoursPerDay: 'Часов в рабочем дне', staleDays: 'Замер устарел, дней', scopeThreshold: 'Рост объёма, %',
  slipThreshold: 'Сдвиг прогноза, дней', chanceThreshold: 'Порог шанса, %', insightWindowDays: 'Период сравнения, дней',
  epicSync: 'Эпики из Jira', adminAlerts: 'Оповещения администратору'
};
/* В БД время хранится в UTC (CURRENT_TIMESTAMP) — показываем в локальном часовом поясе */
function fmtDateTime(ts){
  if(!ts) return '—';
  var d = new Date(ts.replace(' ', 'T') + 'Z');
  if(isNaN(d)) return ts;
  return localIsoDate(d).split('-').reverse().join('.') + ' ' + String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
}
function fillSelect(sel, allLabel, items){
  var cur = sel.value;
  sel.innerHTML = '<option value="">'+allLabel+'</option>' + items.map(function(it){
    return '<option value="'+escapeAttr(it.value)+'">'+escapeHtml(it.label)+'</option>';
  }).join('');
  sel.value = items.some(function(it){ return String(it.value) === cur; }) ? cur : '';
}
/* Реестр подгружается страницами (сначала новые); сортировка — по уже загруженным записям */
var auditState = {entries:[], hasMore:false};
function renderAuditTable(append){
  var selU = document.getElementById('auditFilterUser');
  var selP = document.getElementById('auditFilterPage');
  var selPr = document.getElementById('auditFilterProject');
  var params = new URLSearchParams();
  if(selU.value) params.set('username', selU.value);
  if(selP.value) params.set('page', selP.value);
  if(selPr.value) params.set('projectId', selPr.value);
  if(append === true && auditState.entries.length){
    params.set('beforeId', auditState.entries.reduce(function(m, e){ return Math.min(m, e.id); }, Infinity));
  }
  return api('/api/audit?' + params.toString()).then(function(data){
    fillSelect(selU, 'Все авторы', data.filters.usernames.map(function(u){ return {value:u, label:u}; }));
    fillSelect(selP, 'Все страницы', data.filters.pages.map(function(p){ return {value:p, label:auditPageLabels[p] || p}; }));
    fillSelect(selPr, 'Все проекты', data.filters.projects.map(function(p){ return {value:p.id, label:p.name}; }));
    auditState.entries = append === true ? auditState.entries.concat(data.entries) : data.entries;
    auditState.hasMore = data.hasMore;
    renderAuditRows();
  });
}
function renderAuditRows(){
  var table = document.getElementById('auditLogTable');
  var more = document.getElementById('auditMore');
  more.hidden = !auditState.entries.length;
  document.getElementById('auditCount').textContent = 'Показано записей: ' + auditState.entries.length + (auditState.hasMore ? '' : ' (все)');
  document.getElementById('auditLoadMore').hidden = !auditState.hasMore;
  if(!auditState.entries.length){ emptyTable(table, 'Нет данных', 'Изменений пока не зафиксировано.'); return; }
  var html = '<thead><tr><th class="sortable" data-sort="createdAt">Дата/время</th><th class="sortable" data-sort="username">Автор</th><th class="sortable" data-sort="projectName">Проект</th><th class="sortable" data-sort="page">Страница</th><th class="sortable" data-sort="action">Действие</th><th>Сущность</th><th class="sortable" data-sort="fieldName">Поле</th><th class="sortable" data-sort="oldValue">Было</th><th class="sortable" data-sort="newValue">Стало</th></tr></thead><tbody>';
  applyTableSort('auditLogTable', auditState.entries).forEach(function(e){
    var entityStr = e.entityType ? escapeHtml(auditEntityLabels[e.entityType] || e.entityType) + (e.entityName ? ' «' + escapeHtml(e.entityName) + '»' : '') + (e.entityId ? ' #' + e.entityId : '') : '—';
    html += '<tr>'+
      '<td class="mono nowrap">'+fmtDateTime(e.createdAt)+'</td>'+
      '<td>'+escapeHtml(e.username||'')+'</td>'+
      '<td>'+escapeHtml(e.projectName||'—')+'</td>'+
      '<td>'+escapeHtml(auditPageLabels[e.page]||e.page||'')+'</td>'+
      '<td>'+escapeHtml(auditActionLabels[e.action]||e.action||'')+'</td>'+
      '<td>'+entityStr+'</td>'+
      '<td>'+escapeHtml(auditFieldLabels[e.fieldName]||e.fieldName||'—')+'</td>'+
      '<td class="muted-cell">'+escapeHtml(e.oldValue||'—')+'</td>'+
      '<td class="strong-cell">'+escapeHtml(e.newValue||'—')+'</td>'+
    '</tr>';
  });
  html += '</tbody>';
  table.innerHTML = html;
  initTableSort(table, renderAuditRows);
}
document.getElementById('auditLoadMore').addEventListener('click', function(){ renderAuditTable(true); });
['auditFilterUser','auditFilterPage','auditFilterProject'].forEach(function(id){
  document.getElementById(id).addEventListener('change', function(){ renderAuditTable(); });
});
document.getElementById('auditResetFilters').addEventListener('click', function(){
  ['auditFilterUser','auditFilterPage','auditFilterProject'].forEach(function(id){ document.getElementById(id).value = ''; });
  renderAuditTable();
});

/* ════════════════════════════════════════════════════════════════
   PDF export — учитывает текущие фильтры дашборда и свода по этапам
════════════════════════════════════════════════════════════════ */
/* Печать раздела с графиками: перед печатью раскрываем свёрнутый блок, графики подгоняются под ширину листа */
document.getElementById('btnPrint').addEventListener('click', function(){
  if(!allEpicsBlock.open && currentView === 'dashboard'){ allEpicsBlock.open = true; renderAllEpicsCharts(); }
  setTimeout(function(){ window.print(); }, 150);
});
window.addEventListener('beforeprint', function(){ Object.keys(charts).forEach(function(k){ charts[k].resize(); }); });
window.addEventListener('afterprint', function(){ Object.keys(charts).forEach(function(k){ charts[k].resize(); }); });

document.getElementById('btnPdf').addEventListener('click', function(){
  if(!currentProjectId) return;
  var params = new URLSearchParams({page: currentView});
  if(currentView === 'dashboard'){
    params.set('epics', visibleDashboardServices().map(function(s){ return s.id; }).join(','));
  } else if(currentView === 'stages' && stageShowDone){
    params.set('showDone', '1');
  }
  window.open('/api/projects/'+currentProjectId+'/pdf?'+params.toString(), '_blank');
});

init();

})();
