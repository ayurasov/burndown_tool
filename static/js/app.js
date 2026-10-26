(function(){
"use strict";

/* ════════════════════════════════════════════════════════════════
   STATE
════════════════════════════════════════════════════════════════ */
var currentUser = null;
var projects = [];
var currentProjectId = null;
var currentProjectRole = null; /* 'admin', 'manager', 'user' */
var STATE = { services:[], entries:[], stageTargets:{}, settings:{} };
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

var DEFAULT_SETTINGS = { forecastMethod:'linear', riskBufferDays:3, burnrateMethod:'all', burnrateWindowDays:14 };
var charts = {};
var currentView = 'dashboard';

/* ════════════════════════════════════════════════════════════════
   API helpers
════════════════════════════════════════════════════════════════ */
function api(url, opts){
  opts = opts || {};
  opts.headers = opts.headers || {};
  opts.headers['Content-Type'] = 'application/json';
  return fetch(url, Object.assign({credentials:'same-origin'}, opts)).catch(function(){
    toast('Нет связи с сервером — изменения не сохранены');
    var err = new Error('network');
    err.handled = true;
    throw err;
  }).then(function(r){
    if(r.status === 401){ window.location.href = '/login'; throw new Error('Unauthorized'); }
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
  modalOkHandler = opts.onOk || null;
  confirmModal.classList.add('show');
  var first = confirmBody.querySelector('input:not([type=file]):not([disabled]),select,textarea');
  (first || confirmOk).focus();
}
function closeModal(){
  confirmOk.disabled = false;
  confirmModal.classList.remove('show');
  modalOkHandler = null;
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
  if(e.key === 'Escape' && confirmModal.classList.contains('show')) closeModal();
});

function fmtNum(v){
  if(v === null || v === undefined || isNaN(v)) return '—';
  var r = Math.round(v*100)/100;
  return (r === 0 ? 0 : r).toLocaleString('ru-RU', {maximumFractionDigits:2}); /* без «-0» */
}
function hoursToStr(v){
  if(v === null || v === undefined || isNaN(v)) return '—';
  return fmtNum(v) + ' ч';
}
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
      renderFn();
    });
  });
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
function daysBetween(d1, d2){
  return Math.round((Date.parse(d2 + 'T00:00:00Z') - Date.parse(d1 + 'T00:00:00Z')) / 86400000);
}
function addDays(dateStr, days){
  var d = new Date(dateStr + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0,10);
}
function fmtDate(d){
  if(!d) return '—';
  return d.split('-').reverse().join('.');
}
function fmtShortDate(d){ return d.slice(5).split('-').reverse().join('.'); }
function isNum(v){ return v !== null && v !== undefined && !isNaN(v); }
/* Ячейка burn rate: отрицательное значение = остаток вырос */
function rateCell(v, suffix){
  if(!isNum(v)) return '<td class="num">—</td>';
  var neg = Math.round(v*100) < 0;
  return '<td class="num'+(neg?' neg':'')+'"'+(neg?' title="Остаток вырос"':'')+'>'+fmtNum(v)+(suffix||'')+'</td>';
}
/* Ячейка изменения оценки: рост объёма подсвечивается */
function scopeCell(v){
  if(!isNum(v)) return '<td class="num">—</td>';
  var r = Math.round(v*100)/100;
  return '<td class="num'+(r>0?' scope-up':'')+'"'+(r>0?' title="Оценка выросла с первого замера"':'')+'>'+(r>0?'+':'')+fmtNum(r)+' ч</td>';
}
function forecastText(row){ return row.forecastZero ? fmtDate(row.forecastZero) : (row.noBurn ? 'не сгорает' : '—'); }
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
   Sidebar
════════════════════════════════════════════════════════════════ */
var appRoot = document.getElementById('appRoot');
var sidebarToggle = document.getElementById('sidebarToggle');
var sidebarScrim = document.getElementById('sidebarScrim');
function setSidebarCollapsed(collapsed){ appRoot.classList.toggle('collapsed', collapsed); }
sidebarToggle.addEventListener('click', function(){ setSidebarCollapsed(!appRoot.classList.contains('collapsed')); });
sidebarScrim.addEventListener('click', function(){ setSidebarCollapsed(true); });
if(window.matchMedia('(max-width: 900px)').matches) setSidebarCollapsed(true);

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
  var days = daysBetween(first.d, last.d);
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

/* Сумма по этапу на каждую дату замеров. Для эпика без замера в эту дату берётся его последнее
   известное значение (если эпик ещё не замерялся — он не учитывается), чтобы замеры в разные дни
   не давали ложных провалов на графике. */
function aggregateStageSeries(stageName, stageGroups){
  var services = servicesInStage(stageName, stageGroups);
  var rowsBySvc = services.map(function(s){ return entriesForService(s.id); });
  var dateSet = {};
  rowsBySvc.forEach(function(rows){ rows.forEach(function(e){ dateSet[e.date] = true; }); });
  var dates = Object.keys(dateSet).sort();
  var series = {remaining:[], spent:[], estimate:[]};
  var last = rowsBySvc.map(function(){ return {remaining:null, spent:null, estimate:null}; });
  var pos = rowsBySvc.map(function(){ return 0; });
  dates.forEach(function(date){
    rowsBySvc.forEach(function(rows, i){
      while(pos[i] < rows.length && rows[pos[i]].date <= date){
        var row = rows[pos[i]++];
        Object.keys(series).forEach(function(f){ if(isNum(row[f])) last[i][f] = row[f]; });
      }
    });
    Object.keys(series).forEach(function(f){
      var sum = 0, has = false;
      last.forEach(function(l){ if(l[f] !== null){ sum += l[f]; has = true; } });
      series[f].push(has ? sum : null);
    });
  });
  return {dates:dates, remaining:series.remaining, spent:series.spent, estimate:series.estimate};
}

/* Изменение оценки (объёма работ) от первого до последнего заполненного значения */
function scopeChange(values){
  var filled = values.filter(isNum);
  return filled.length >= 2 ? filled[filled.length-1] - filled[0] : null;
}

function computeStatus(dates, remainingVals, targetDate){
  if(!dates.length) return {status:'unknown', forecastZeroDate:null, noBurn:false, burnRate:null, lastRemaining:null, reg:null, baseDate:null, lastDate:null};
  var baseDate = dates[0];
  var lastDate = dates[dates.length-1];
  var pts = dates.map(function(d,i){ return {x:daysBetween(baseDate, d), y:remainingVals[i]}; }).filter(function(p){ return p.y !== null && p.y !== undefined; });
  var burnRate = avgBurnRate(dates, remainingVals);
  /* Последний заполненный замер: пустая строка «+ Новая дата» не обнуляет остаток */
  var lastRemaining = pts.length ? pts[pts.length-1].y : null;
  var lastX = pts.length ? pts[pts.length-1].x : null;
  if(lastX !== null) lastDate = addDays(baseDate, lastX);
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
  var targetX = targetDate ? daysBetween(baseDate, targetDate) : null;
  /* есть замеры, но нет цели — отдельный статус; «нет данных» — только когда нечего прогнозировать */
  var status = (pts.length && targetX === null) ? 'notarget' : 'unknown', forecastZeroDate = null;
  var noBurn = !!(reg && reg.slope >= 0);
  if(reg && reg.slope < 0){
    var zeroX = -reg.intercept / reg.slope;
    forecastZeroDate = addDays(baseDate, Math.round(zeroX));
    if(targetX !== null){
      var buffer = (STATE.settings && STATE.settings.riskBufferDays) || 0;
      if(zeroX <= targetX) status = 'ok';
      else if(zeroX <= targetX + buffer) status = 'risk';
      else status = 'danger';
    }
  } else if(noBurn && targetX !== null){
    status = 'danger';
  }
  return {status:status, forecastZeroDate:forecastZeroDate, noBurn:noBurn, burnRate:burnRate, lastRemaining:lastRemaining, reg:reg, baseDate:baseDate, lastDate:lastDate};
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

function baseChartOptions(yTitle, extraTooltipCtx){
  var opts = {
    responsive:true, maintainAspectRatio:false,
    interaction:{mode:'nearest', axis:'x', intersect:false},
    plugins:{
      legend:{display:false},
      tooltip:{
        filter: function(item){
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
            var lines = [(ds.label || '') + ': ' + fmtNum(val) + ' ч'];
            /* For actual datasets, add deviation info using interpolation at this point's date */
            if(ds.isActual && extraTooltipCtx){
              var date = ctx.raw ? ctx.raw.x : null;
              if(date){
                if(extraTooltipCtx.avgPts){
                  var avgVal = interpolateValue(extraTooltipCtx.avgPts, date);
                  if(avgVal !== null && !isNaN(avgVal)){
                    var dev = val - avgVal;
                    lines.push('Средняя динамика: ' + fmtNum(avgVal) + ' ч (откл.: ' + (dev >= 0 ? '+' : '') + fmtNum(dev) + ' ч)');
                  }
                }
                if(extraTooltipCtx.forecastPts){
                  var fcVal = interpolateValue(extraTooltipCtx.forecastPts, date);
                  if(fcVal !== null && !isNaN(fcVal)){
                    var dev2 = val - fcVal;
                    lines.push('Прогноз: ' + fmtNum(fcVal) + ' ч (откл.: ' + (dev2 >= 0 ? '+' : '') + fmtNum(dev2) + ' ч)');
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
      y:{ title:{display:true, text:yTitle, color:getCss('--color-text-muted')}, grid:{color:getCss('--color-divider')}, ticks:{color:getCss('--color-text-muted')}, beginAtZero:true }
    }
  };
  return opts;
}

function renderLegend(containerId, datasets){
  var el = document.getElementById(containerId);
  if(!el) return;
  el.innerHTML = datasets.map(function(d){
    return '<span><span class="legend-dot" style="background:'+d.borderColor+'"></span>'+escapeHtml(d.label)+'</span>';
  }).join('');
}

function avgLineDataset(dates, burnRate, lastRemaining, lastDate){
  if(!dates.length || burnRate === null || burnRate === undefined || lastRemaining === null) return null;
  var baseDate = dates[0];
  lastDate = lastDate || dates[dates.length-1];
  var daysSpan = Math.max(1, daysBetween(baseDate, lastDate));
  var startVal = lastRemaining + burnRate * daysSpan;
  return {
    label: 'Средняя динамика (' + fmtNum(burnRate) + ' ч/день)',
    data: [{x: baseDate, y: Math.max(0,startVal)}, {x: lastDate, y: Math.max(0,lastRemaining)}],
    borderColor: getCss('--color-success'), borderDash: [6,4], pointRadius: 0, borderWidth: 1.5, tension: 0
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

function forecastDataset(reg, baseDate, firstDate, lastRow, targetDate){
  if(!reg) return null;
  var endDate = targetDate || addDays(lastRow.date, 21);
  var startX = daysBetween(baseDate, firstDate);
  var endX = Math.max(daysBetween(baseDate, endDate), startX + 1);
  var forecastPts = [];
  var steps = 16;
  for(var i=0; i<=steps; i++){
    var x = startX + (endX-startX)*i/steps;
    var y = Math.max(0, reg.slope*x + reg.intercept);
    forecastPts.push({x: addDays(baseDate, Math.round(x)), y:y});
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
function renderEpicCharts(){
  renderEpicRemainingChart();
  renderEpicSpentChart();
  renderEpicEstimateChart();
}

function renderEpicRemainingChart(){
  destroyChart('epicRemaining');
  var sid = parseInt(document.getElementById('chartServiceSelect').value, 10);
  var svc = serviceById(sid);
  if(!svc){ document.getElementById('legendRemaining').innerHTML = ''; return; }
  var rows = entriesForService(sid);
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
    var fc = forecastDataset(stat.reg, stat.baseDate, firstRow.date, lastRow, svc.targetDate);
    if(fc){ datasets.push(fc); fcData = fc.data; }
    var tl = targetLineDataset(stat.lastDate, stat.lastRemaining, svc.targetDate, 'Целевая линия (эпик)');
    if(tl) datasets.push(tl);
    if(avgLine) avgPts = avgLine.data;
  }

  renderLegend('legendRemaining', datasets);
  var ctx = document.getElementById('chartRemaining');
  var tooltipCtx = { avgPts: avgPts, forecastPts: fcData };
  charts.epicRemaining = new Chart(ctx, {type:'line', data:{datasets:datasets}, options: baseChartOptions('Часы (Ост.время)', tooltipCtx)});
}

function renderEpicSpentChart(){
  destroyChart('epicSpent');
  var sid = parseInt(document.getElementById('chartServiceSelect').value, 10);
  var svc = serviceById(sid);
  var rows = svc ? entriesForService(sid) : [];
  var palette = colorPalette();
  var data = rows.map(function(r){ return {x:r.date, y:r.spent}; });
  var datasets = [{ label: svc ? svc.name : '', data: data, borderColor: palette[1], backgroundColor: palette[1]+'33', spanGaps:true, tension:0.25, cubicInterpolationMode:'monotone', pointRadius:3, borderWidth:2 }];
  renderLegend('legendSpentEpic', datasets);
  var ctx = document.getElementById('chartSpentEpic');
  charts.epicSpent = new Chart(ctx, {type:'line', data:{datasets:datasets}, options: baseChartOptions('Часы')});
}

function renderEpicEstimateChart(){
  destroyChart('epicEstimate');
  var sid = parseInt(document.getElementById('chartServiceSelect').value, 10);
  var svc = serviceById(sid);
  var rows = svc ? entriesForService(sid) : [];
  var palette = colorPalette();
  var data = rows.map(function(r){ return {x:r.date, y:r.estimate}; });
  var datasets = [{ label: svc ? svc.name : '', data: data, borderColor: palette[2], backgroundColor: palette[2]+'33', spanGaps:true, tension:0.25, cubicInterpolationMode:'monotone', pointRadius:3, borderWidth:2 }];
  renderLegend('legendEstimateEpic', datasets);
  var ctx = document.getElementById('chartEstimateEpic');
  charts.epicEstimate = new Chart(ctx, {type:'line', data:{datasets:datasets}, options: baseChartOptions('Часы')});
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
  destroyChart('allRemaining');
  var ids = visibleDashboardServices().map(function(s){ return s.id; });
  var datasets = buildDatasetsForMetric('remaining', ids);
  renderLegend('legendRemainingAll', datasets);
  charts.allRemaining = new Chart(document.getElementById('chartRemainingAll'), {type:'line', data:{datasets:datasets}, options: baseChartOptions('Часы (Ост.время)')});

  destroyChart('allSpent');
  var spentDs = buildDatasetsForMetric('spent', ids);
  renderLegend('legendSpent', spentDs);
  charts.allSpent = new Chart(document.getElementById('chartSpent'), {type:'line', data:{datasets:spentDs}, options: baseChartOptions('Часы')});

  destroyChart('allEstimate');
  var estDs = buildDatasetsForMetric('estimate', ids);
  renderLegend('legendEstimate', estDs);
  charts.allEstimate = new Chart(document.getElementById('chartEstimate'), {type:'line', data:{datasets:estDs}, options: baseChartOptions('Часы')});
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
    '<th class="sortable num" data-sort="burnRate">Burn rate, ч/день</th>'+
    '<th class="sortable num" data-sort="scope" title="Изменение первоначальной оценки с первого замера">Δ оценки</th>'+
    '<th class="sortable" data-sort="targetDate">Цель</th>'+
    '<th class="sortable" data-sort="forecastZero">Прогноз «0»</th>'+
    '<th class="sortable" data-sort="status">Статус</th>'+
    '</tr></thead><tbody>';
  var totalRemaining = 0, totalBurnRate = 0, totalScope = 0, hasRemaining=false, hasBurn=false, hasScope=false;
  var rows = visibleDashboardServices().map(function(s){
    var stat = computeServiceStatus(s.id);
    if(isNum(stat.lastRemaining)){ totalRemaining += stat.lastRemaining; hasRemaining = true; }
    if(isNum(stat.burnRate)){ totalBurnRate += stat.burnRate; hasBurn = true; }
    if(isNum(stat.scope)){ totalScope += stat.scope; hasScope = true; }
    return {etap:s.etap, name:s.name, isDone:s.isDone, remaining:stat.lastRemaining, burnRate:stat.burnRate, scope:stat.scope, targetDate:s.targetDate||'', forecastZero:stat.forecastZeroDate||'', noBurn:stat.noBurn, status:stat.status};
  });
  if(!rows.length){
    html += '<tr><td colspan="8" class="empty-cell">Нет эпиков для отображения — проверьте фильтр выше.</td></tr>';
  }
  applyTableSort('summaryTable', rows).forEach(function(r){
    html += '<tr'+(r.isDone?' class="done-row"':'')+'><td class="nowrap">'+escapeHtml(r.etap)+'</td><td>'+escapeHtml(r.name)+'</td><td class="num">'+hoursToStr(r.remaining)+'</td>'+rateCell(r.burnRate,' ч')+scopeCell(r.scope)+'<td class="mono nowrap">'+fmtDate(r.targetDate)+'</td><td class="mono nowrap">'+forecastText(r)+'</td><td class="nowrap">'+statusBadge(r.status)+'</td></tr>';
  });
  if(rows.length){
    html += '<tr class="total-row"><td colspan="2">Итого</td><td class="num">'+(hasRemaining?hoursToStr(totalRemaining):'—')+'</td>'+rateCell(hasBurn?totalBurnRate:null,' ч')+scopeCell(hasScope?totalScope:null)+'<td colspan="3"></td></tr>';
  }
  html += '</tbody>';
  table.innerHTML = html;
  initTableSort(table, renderSummaryTable);
}

/* ════════════════════════════════════════════════════════════════
   Burn rate daily tables
════════════════════════════════════════════════════════════════ */
function unionOfAllDates(){
  var set = {};
  STATE.entries.forEach(function(e){ if(isNum(e.remaining)) set[e.date] = true; });
  return Object.keys(set).sort();
}

function burnRateDates(range){
  var allDates = unionOfAllDates();
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
    var days = daysBetween(rows[idx-1].date, d);
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
  var dates = burnRateDates(burnRangeOverride);
  if(!dates.length){ emptyTable(table, 'Нет данных', 'Добавьте замеры в разделе «Данные по датам».'); return; }
  var rows = visibleDashboardServices().map(function(s){
    return {label: s.etap+' — '+s.name, rates: serviceDailyRates(s.id, dates)};
  });
  table.innerHTML = burnRateTableHtml('Эпик', dates, rows);
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
    '<th class="sortable num" data-sort="burnRate">Burn rate, ч/день</th>'+
    '<th class="sortable num" data-sort="scope" title="Изменение суммарной оценки с первого замера">Δ оценки</th>'+
    '<th class="sortable" data-sort="targetDate">Цель</th>'+
    '<th class="sortable" data-sort="forecastZero">Прогноз «0»</th>'+
    '<th class="sortable" data-sort="status">Статус</th>'+
    '</tr></thead><tbody>';
  var rows = stageNames.map(function(name){
    var stat = computeStageStatus(name, stageGroups);
    return {name:name, count:stageGroups[name].length, remaining:stat.lastRemaining, burnRate:stat.burnRate, scope:stat.scope, targetDate:STATE.stageTargets[name]||'', forecastZero:stat.forecastZeroDate||'', noBurn:stat.noBurn, status:stat.status};
  });
  applyTableSort('stageSummaryTable', rows).forEach(function(r){
    html += '<tr><td>'+escapeHtml(r.name)+'</td><td class="num">'+r.count+'</td><td class="num">'+hoursToStr(r.remaining)+'</td>'+rateCell(r.burnRate,' ч')+scopeCell(r.scope)+'<td class="mono nowrap">'+fmtDate(r.targetDate)+'</td><td class="mono nowrap">'+forecastText(r)+'</td><td class="nowrap">'+statusBadge(r.status)+'</td></tr>';
  });
  html += '</tbody>';
  table.innerHTML = html;
  initTableSort(table, renderStageSummaryTable);
}

function renderStageBurnRateTable(){
  var table = document.getElementById('stageBurnRateTable');
  var dates = burnRateDates(stageBurnRangeOverride);
  if(!dates.length){ emptyTable(table, 'Нет данных', 'Добавьте замеры для отображения burn rate.'); return; }
  var stageGroups = computeStageGroups();
  var stageNames = Object.keys(stageGroups);
  if(!stageNames.length){ emptyTable(table, 'Нет данных', 'Нет этапов для отображения.'); return; }
  /* Burn rate этапа — сумма дневных burn rate его эпиков (согласовано с таблицей на дашборде) */
  var rows = stageNames.map(function(name){
    return {label: name, rates: sumRates(stageGroups[name].map(function(id){ return serviceDailyRates(id, dates); }), dates.length)};
  });
  table.innerHTML = burnRateTableHtml('Этап', dates, rows);
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
    ctx.font = '600 10px "General Sans", Arial, sans-serif';
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
    ctx.font = '600 10px "General Sans", Arial, sans-serif';
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
      var fc = forecastDataset(stat.reg, stat.baseDate, firstDate, lastRow, targetDate);
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
    charts['stage_'+safeId] = new Chart(canvas, {type:'line', data:{datasets:datasets}, options: baseChartOptions('Часы (суммарный остаток)', tooltipCtx), plugins:[epicTargetLabelPlugin]});
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

  var numInput = function(r, field){ return '<input type="number" step="0.01" min="0" value="'+(r[field] ?? '')+'" data-field="'+field+'">'; };
  var html = '<colgroup><col class="col-date"><col class="col-num"><col class="col-num"><col class="col-num"><col class="col-actions"></colgroup>'+
    '<thead><tr><th class="sortable" data-sort="date">Дата</th><th class="sortable" data-sort="remaining">Остаток, ч</th><th class="sortable" data-sort="spent">Затрачено, ч</th><th class="sortable" data-sort="estimate">Оценка, ч</th><th></th></tr></thead><tbody>';
  rows.forEach(function(r){
    html += '<tr data-id="'+r.id+'"><td><input type="date" value="'+r.date+'" data-field="date" required></td><td>'+numInput(r,'remaining')+'</td><td>'+numInput(r,'spent')+'</td><td>'+numInput(r,'estimate')+'</td><td class="row-actions">'+actionBtn('delete','Удалить',ICON_DELETE)+'</td></tr>';
  });
  html += '</tbody>';
  table.innerHTML = html;
  initTableSort(table, renderDataView);

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
    '<thead><tr><th></th><th class="sortable" data-sort="etap">Этап</th><th class="sortable" data-sort="name">Название эпика</th><th class="sortable" data-sort="targetDate">Целевая дата</th><th class="center" title="Сданный функционал не учитывается в рамках этапа">Сдано</th><th class="sortable" data-sort="stageGroup" title="'+groupHint+'">Группа в своде</th><th class="sortable num" data-sort="count">Замеров</th><th></th></tr></thead><tbody>';
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
function renderSettingsView(){
  var st = STATE.settings;
  var burnMethod = st.burnrateMethod || 'all';
  var opt = function(value, label, current){ return '<option value="'+value+'"'+(current===value?' selected':'')+'>'+label+'</option>'; };
  document.getElementById('settingsGrid').innerHTML =
    '<div class="setting-item"><label class="small" for="setForecastMethod">Метод прогноза</label><select id="setForecastMethod">'+opt('linear','Линейная регрессия',st.forecastMethod)+opt('average','Средняя скорость сгорания (burn rate)',st.forecastMethod)+'</select></div>'+
    '<div class="setting-item"><label class="small" for="setRiskBuffer">Буфер риска, дней</label><input type="number" id="setRiskBuffer" min="0" step="1" value="'+(st.riskBufferDays ?? 3)+'"></div>'+
    '<div class="settings-divider">Расчёт среднего burn rate</div>'+
    '<div class="setting-item"><label class="small" for="setBurnrateMethod">Метод расчёта</label><select id="setBurnrateMethod">'+opt('all','По всей истории',burnMethod)+opt('rolling','Скользящее среднее',burnMethod)+'</select></div>'+
    '<div class="setting-item"><label class="small" for="setBurnrateWindow">Окно скользящего среднего, дней</label><input type="number" id="setBurnrateWindow" min="1" step="1" value="'+(st.burnrateWindowDays || 14)+'"'+(burnMethod!=='rolling'?' disabled':'')+'></div>';

  var bind = function(id, apply){
    document.getElementById(id).addEventListener('change', function(e){
      var prev = Object.assign({}, st);
      apply(e.target);
      saveSettings(prev);
      renderSettingsView();
    });
  };
  bind('setForecastMethod', function(el){ st.forecastMethod = el.value; });
  bind('setRiskBuffer', function(el){ st.riskBufferDays = Math.max(0, parseInt(el.value, 10) || 0); });
  bind('setBurnrateMethod', function(el){ st.burnrateMethod = el.value; });
  bind('setBurnrateWindow', function(el){ st.burnrateWindowDays = Math.max(1, parseInt(el.value, 10) || 14); });
  applyReadonlyToView('view-settings');
}

function saveSettings(prevSettings){
  if(!canEdit()) return;
  apiPut('/api/projects/'+currentProjectId+'/settings', {
    forecastMethod: STATE.settings.forecastMethod,
    riskBufferDays: STATE.settings.riskBufferDays,
    burnrateMethod: STATE.settings.burnrateMethod,
    burnrateWindowDays: STATE.settings.burnrateWindowDays
  }).then(function(){ toast('Настройки сохранены'); }).catch(function(){
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
function field(label, inputHtml){ return '<div class="setting-item"><label class="small">'+label+'</label>'+inputHtml+'</div>'; }
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
  ['navUsers','navProjects','navAudit','btnExport'].forEach(function(id){ document.getElementById(id).style.display = admin ? '' : 'none'; });
  document.getElementById('navSettings').style.display = canEdit() ? '' : 'none';
  document.getElementById('sectionsNav').style.display = currentProjectId ? '' : 'none';
  if(currentUser){
    document.getElementById('userInfo').innerHTML = userAvatarHtml(currentUser, 'avatar')+
      '<div class="user-info-text"><span class="user-info-name">'+escapeHtml(currentUser.displayName||currentUser.username)+'</span>'+roleBadge(currentUser.role)+'</div>';
  }
}

/* ════════════════════════════════════════════════════════════════
   Navigation
════════════════════════════════════════════════════════════════ */
var navItems = document.querySelectorAll('.nav-item');
var pageTitle = document.getElementById('pageTitle');
var pageSub = document.getElementById('pageSub');
var ADMIN_VIEWS = ['users', 'projects', 'audit'];
var PDF_VIEWS = ['dashboard', 'stages', 'data'];
var titles = {
  dashboard: ['Дашборд сгорания', 'Свод по эпикам и этапам'],
  stages: ['Свод по этапам', 'Агрегированное сгорание по укрупнённым этапам'],
  data: ['Данные по датам', 'Внесение и редактирование замеров'],
  services: ['Эпики (блоки задач)', 'Список эпиков проекта и целевых дат'],
  settings: ['Настройки проекта', 'Параметры прогноза и расчёта burn rate'],
  users: ['Управление пользователями', 'Пользователи, роли и доступ к проектам'],
  projects: ['Управление проектами', 'Создание, редактирование и удаление проектов'],
  audit: ['Реестр изменений', 'Журнал всех изменений с фильтрами']
};

function projectTitleSuffix(){
  var p = projects.find(function(x){ return x.id === currentProjectId; });
  return p ? ' · ' + p.name : '';
}

function navigate(view){
  var isAdminView = ADMIN_VIEWS.indexOf(view) !== -1;
  if(isAdminView && !isGlobalAdmin()) view = 'dashboard';
  if(view === 'settings' && !canEdit()) view = 'dashboard';
  if(!isAdminView && !currentProjectId && isGlobalAdmin()) view = 'projects';
  currentView = view;
  navItems.forEach(function(n){ n.classList.toggle('active', n.dataset.view === view); });
  var viewId = (!currentProjectId && ADMIN_VIEWS.indexOf(view) === -1) ? 'view-empty' : 'view-'+view;
  document.querySelectorAll('.view').forEach(function(v){ v.classList.toggle('active', v.id === viewId); });
  if(titles[view]){
    pageTitle.textContent = titles[view][0];
    pageSub.textContent = titles[view][1] + (ADMIN_VIEWS.indexOf(view) === -1 ? projectTitleSuffix() : '');
  }
  var renderers = {dashboard:renderDashboard, stages:renderStagesView, data:renderDataView, services:renderServicesView,
    settings:renderSettingsView, users:renderUsersView, projects:renderProjectsMgmtTable, audit:function(){ renderAuditTable(); }};
  if(renderers[view] && (currentProjectId || ADMIN_VIEWS.indexOf(view) !== -1)) renderers[view]();
  var reportView = PDF_VIEWS.indexOf(view) !== -1 && currentProjectId;
  document.getElementById('btnPdf').style.display = reportView ? '' : 'none';
  document.getElementById('btnPrint').style.display = (reportView && view !== 'data') ? '' : 'none';
  if(window.matchMedia('(max-width: 900px)').matches) setSidebarCollapsed(true);
}

navItems.forEach(function(n){
  n.addEventListener('click', function(e){
    e.preventDefault();
    navigate(n.dataset.view);
  });
});

/* ════════════════════════════════════════════════════════════════
   Dashboard render
════════════════════════════════════════════════════════════════ */
function renderStagesView(){
  document.getElementById('stageShowDone').checked = stageShowDone;
  buildStagePanelsSkeleton();
  renderStageKpis();
  renderStageSummaryTable();
  renderStageCharts();
  renderStageBurnRateTable();
}

function renderDashboard(){
  renderDashboardFilterBar();
  populateServiceSelects();
  updateEpicTargetInput();
  renderKpis();
  renderSummaryTable();
  renderEpicCharts();
  renderAllEpicsCharts();
  renderBurnRateTable();
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
      renderDashboard();
    });
  });
}

document.getElementById('dashHideDone').addEventListener('change', function(e){
  dashboardFilter.hideDone = e.target.checked;
  renderDashboard();
});
document.getElementById('dashEpicAll').addEventListener('click', function(){
  dashboardFilter.epics = null;
  renderDashboard();
});
document.getElementById('dashEpicNone').addEventListener('click', function(){
  dashboardFilter.epics = {};
  STATE.services.forEach(function(s){ dashboardFilter.epics[s.id] = false; });
  renderDashboard();
});
document.getElementById('stageShowDone').addEventListener('change', function(e){
  stageShowDone = e.target.checked;
  renderStagesView();
});

/* ════════════════════════════════════════════════════════════════
   Event handlers (epic select, target date, add row/service, etc)
════════════════════════════════════════════════════════════════ */
document.getElementById('chartServiceSelect').addEventListener('change', function(){
  updateEpicTargetInput();
  renderEpicCharts();
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

function openBulkMeasurement(){
  if(!canEdit()){ toast('Нет прав на редактирование'); return; }
  var services = STATE.services.filter(function(s){ return !s.isDone; });
  if(!services.length) services = STATE.services.slice();
  if(!services.length){ toast('Сначала добавьте эпик'); return; }
  var fields = ['remaining', 'spent', 'estimate'];
  var FIELD_LABELS = {remaining:'Остаток, ч', spent:'Затрачено, ч', estimate:'Оценка, ч'};
  openModal({
    title: 'Замер по всем эпикам', okLabel: 'Сохранить', wide: true,
    html:
      '<div class="bulk-head">'+
        '<label class="small" for="bulkDate">Дата замера</label><input type="date" id="bulkDate" value="'+todayIso()+'">'+
        '<button type="button" class="btn btn-ghost btn-sm" id="bulkFillPrev" title="Заполнить пустые поля значениями предыдущего замера">Подставить прошлые значения</button>'+
      '</div>'+
      '<p class="bulk-note">Пустые строки не сохраняются. Если замер на эту дату уже есть, он будет обновлён. Серым показаны значения предыдущего замера; сданные эпики не выводятся.</p>'+
      '<div class="table-scroll"><table class="data-table compact-table bulk-table" id="bulkTable"><thead><tr><th>Эпик</th><th class="num">Остаток, ч</th><th class="num">Затрачено, ч</th><th class="num">Оценка, ч</th></tr></thead><tbody>'+
      services.map(function(s){
        return '<tr data-sid="'+s.id+'"><td><span class="bulk-etap">'+escapeHtml(s.etap)+'</span> '+escapeHtml(s.name)+'</td>'+
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
  /* при смене даты подставляем уже существующие значения на эту дату и подсказки с прошлого замера */
  var fill = function(){
    var date = dateEl.value;
    document.querySelectorAll('#bulkTable tbody tr').forEach(function(tr){
      var sid = parseInt(tr.dataset.sid, 10);
      var existing = STATE.entries.find(function(e){ return e.service_id === sid && e.date === date; });
      var prev = lastValuesBefore(sid, date || '9999-12-31');
      tr.querySelectorAll('input').forEach(function(inp){
        var f = inp.dataset.field;
        inp.value = existing && isNum(existing[f]) ? existing[f] : '';
        inp.placeholder = isNum(prev[f]) ? (Math.round(prev[f]*100)/100).toLocaleString('ru-RU', {maximumFractionDigits:2, useGrouping:false}) : '';
        inp.dataset.prev = isNum(prev[f]) ? prev[f] : '';
      });
    });
  };
  dateEl.addEventListener('change', fill);
  fill();
  document.getElementById('bulkFillPrev').addEventListener('click', function(){
    document.querySelectorAll('#bulkTable input[data-field]').forEach(function(inp){
      if(inp.value === '' && inp.dataset.prev !== '') inp.value = inp.dataset.prev;
    });
  });
  enableEnterNavigation(document.getElementById('bulkTable'));
}
document.getElementById('btnBulkMeasure').addEventListener('click', openBulkMeasurement);

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
  var onChange = function(){ setRange({from: fromEl.value || null, to: toEl.value || null}); render(); };
  fromEl.addEventListener('change', onChange);
  toEl.addEventListener('change', onChange);
  document.getElementById(resetId).addEventListener('click', function(){
    fromEl.value = ''; toEl.value = '';
    setRange(null); render();
  });
}
bindBurnRange('burnFrom', 'burnTo', 'burnResetRange', function(r){ burnRangeOverride = r; }, renderBurnRateTable);
bindBurnRange('stageBurnFrom', 'stageBurnTo', 'stageBurnResetRange', function(r){ stageBurnRangeOverride = r; }, renderStageBurnRateTable);
function resetBurnRanges(){
  burnRangeOverride = null;
  stageBurnRangeOverride = null;
  ['burnFrom','burnTo','stageBurnFrom','stageBurnTo'].forEach(function(id){ document.getElementById(id).value = ''; });
}

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
            '<label class="btn btn-ghost btn-sm">Загрузить фото<input type="file" accept="image/*" id="profilePhotoFile" hidden></label>'+
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
  var html = '<div class="nav-label">Проекты</div>';
  if(!projects.length){
    nav.innerHTML = html + '<div class="nav-empty">Нет доступных проектов</div>';
    return;
  }
  var palette = colorPalette();
  projects.forEach(function(p, i){
    html += '<a href="#" class="nav-project'+(p.id === currentProjectId ? ' active' : '')+'" data-pid="'+p.id+'" title="'+escapeAttr(p.description||p.name)+'"><span class="dot" style="background:'+palette[i % palette.length]+'"></span><span class="nav-project-name">'+escapeHtml(p.name)+'</span></a>';
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
  if(pid === currentProjectId){ if(ADMIN_VIEWS.indexOf(currentView) !== -1) navigate('dashboard'); return; }
  var proj = projects.find(function(p){ return p.id === pid; });
  currentProjectId = pid;
  currentProjectRole = proj ? proj.role : null;
  currentDataServiceId = null;
  /* данные прежнего проекта не должны быть видны (и редактируемы) под новым, пока грузятся новые */
  STATE = { services:[], entries:[], stageTargets:{}, settings:Object.assign({}, DEFAULT_SETTINGS) };
  dashboardFilter = { hideDone: dashboardFilter.hideDone, epics: null };
  stageShowDone = false;
  resetBurnRanges();
  renderProjectsNav();
  updateRoleUI();
  /* из админ-разделов переходим к данным выбранного проекта */
  loadProjectData(pid, ADMIN_VIEWS.indexOf(currentView) !== -1 ? 'dashboard' : currentView);
}

function loadProjectData(pid, view){
  return api('/api/projects/'+pid+'/data').then(function(data){
    if(pid !== currentProjectId) return; /* пользователь успел переключить проект */
    STATE.services = data.services || [];
    STATE.entries = data.entries || [];
    STATE.stageTargets = data.stageTargets || {};
    STATE.settings = Object.assign({}, DEFAULT_SETTINGS, data.settings || {});
    if(!serviceById(currentDataServiceId)) currentDataServiceId = STATE.services.length ? STATE.services[0].id : null;
    navigate(view || currentView);
  });
}

function loadProjects(){
  return api('/api/projects').then(function(projs){
    projects = projs;
    var proj = projects.find(function(p){ return p.id === currentProjectId; });
    if(!proj && projects.length){
      proj = projects[0];
      resetBurnRanges();
      dashboardFilter.epics = null;
    }
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
    updateRoleUI();
    loadProjects();
  }).catch(function(){
    window.location.href = '/login';
  });
}

/* ════════════════════════════════════════════════════════════════
   Audit log
════════════════════════════════════════════════════════════════ */
var auditPageLabels = {
  dashboard: 'Дашборд', stages: 'Свод по этапам', data: 'Данные по датам',
  services: 'Эпики', settings: 'Настройки', users: 'Пользователи',
  projects: 'Проекты', profile: 'Профиль'
};
var auditActionLabels = { create: 'Создание', update: 'Изменение', delete: 'Удаление' };
var auditEntityLabels = { project: 'Проект', service: 'Эпик', entry: 'Замер', user: 'Пользователь', stage_target: 'Цель этапа', settings: 'Настройки' };
var auditFieldLabels = {
  name: 'Название', description: 'Описание', etap: 'Этап', targetDate: 'Целевая дата', isDone: 'Сдано', stageGroup: 'Группа в своде',
  date: 'Дата', remaining: 'Остаток', spent: 'Затрачено', estimate: 'Оценка',
  username: 'Логин', displayName: 'Имя', email: 'Email', position: 'Должность', role: 'Роль',
  forecastMethod: 'Метод прогноза', riskBufferDays: 'Буфер риска', burnrateMethod: 'Метод burn rate', burnrateWindowDays: 'Окно burn rate'
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
