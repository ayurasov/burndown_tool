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
var TODAY = new Date().toISOString().slice(0,10);

var DEFAULT_SETTINGS = { forecastMethod:'linear', riskBufferDays:3, burnrateMethod:'all', burnrateWindowDays:14 };
var charts = {};

/* ════════════════════════════════════════════════════════════════
   API helpers
════════════════════════════════════════════════════════════════ */
function api(url, opts){
  opts = opts || {};
  opts.headers = opts.headers || {};
  opts.headers['Content-Type'] = 'application/json';
  return fetch(url, Object.assign({credentials:'same-origin'}, opts)).then(function(r){
    if(r.status === 401){ window.location.href = '/login'; throw new Error('Unauthorized'); }
    return r.json();
  });
}

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
var currentConfirmHandler = null;
function askConfirm(title, body, cb, okLabel){
  confirmTitle.textContent = title;
  confirmBody.textContent = body;
  confirmOk.textContent = okLabel || 'Удалить';
  if(currentConfirmHandler){
    confirmOk.removeEventListener('click', currentConfirmHandler);
  }
  confirmModal.classList.add('show');
  var handler = function(){
    confirmModal.classList.remove('show');
    confirmOk.removeEventListener('click', handler);
    currentConfirmHandler = null;
    cb();
  };
  currentConfirmHandler = handler;
  confirmOk.addEventListener('click', handler);
}
confirmCancel.addEventListener('click', function(){
  confirmModal.classList.remove('show');
  if(currentConfirmHandler){
    confirmOk.removeEventListener('click', currentConfirmHandler);
    currentConfirmHandler = null;
  }
});

function fmtNum(v){
  if(v === null || v === undefined || isNaN(v)) return '—';
  return (Math.round(v*100)/100).toLocaleString('ru-RU', {maximumFractionDigits:2});
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
function getSortedServices(){
  var list = STATE.services.slice();
  var sort = tableSortState['servicesTable'];
  if(!sort) { list.sort(function(a,b){ return (a.sortOrder||0)-(b.sortOrder||0); }); return list; }
  list.sort(function(a,b){
    var av = a[sort.field], bv = b[sort.field];
    if(sort.field === 'count'){ av = entriesForService(a.id).length; bv = entriesForService(b.id).length; }
    if(av == null) av = ''; if(bv == null) bv = '';
    av = String(av).toLowerCase(); bv = String(bv).toLowerCase();
    if(av < bv) return sort.dir === 'desc' ? 1 : -1;
    if(av > bv) return sort.dir === 'desc' ? -1 : 1;
    return 0;
  });
  return list;
}
function initTableSort(table, data, renderFn){
  var key = table.id;
  table.querySelectorAll('th.sortable').forEach(function(th){
    th.style.cursor = 'pointer';
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
    var av = a[sort.field], bv = b[sort.field];
    if(av == null) av = ''; if(bv == null) bv = '';
    av = String(av).toLowerCase(); bv = String(bv).toLowerCase();
    if(av < bv) return sort.dir === 'desc' ? 1 : -1;
    if(av > bv) return sort.dir === 'desc' ? -1 : 1;
    return 0;
  });
}
function getCss(varName){
  return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
}
function colorPalette(){
  return [getCss('--color-primary'), getCss('--color-blue'), getCss('--color-orange'), getCss('--color-error'), getCss('--color-success'), getCss('--color-warning')];
}
function daysBetween(d1, d2){
  return Math.round((new Date(d2) - new Date(d1)) / 86400000);
}
function addDays(dateStr, days){
  var d = new Date(dateStr);
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0,10);
}
function fmtDate(d){
  if(!d) return '—';
  return d.split('-').reverse().join('.');
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
function entriesForService(id){
  return STATE.entries.filter(function(e){ return e.service_id === id; }).slice().sort(function(a,b){
    return a.date < b.date ? -1 : a.date > b.date ? 1 : 0;
  });
}

function computeStageGroups(){
  var groups = {};
  STATE.services.forEach(function(s){
    var etap = s.etap || '';
    var match = etap.match(/^(Этап\s+\d+)(\.\d+)?$/i);
    if(match){
      var g = match[1];
      if(!groups[g]) groups[g] = [];
      if(groups[g].indexOf(etap) === -1) groups[g].push(etap);
    } else {
      if(!groups[etap]) groups[etap] = [];
      if(groups[etap].indexOf(etap) === -1) groups[etap].push(etap);
    }
  });
  return groups;
}

function servicesInStage(stageName, stageGroups){
  var sg = stageGroups || computeStageGroups();
  var etaps = sg[stageName] || [stageName];
  return STATE.services.filter(function(s){ return etaps.includes(s.etap); });
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

function aggregateStageSeries(stageName, stageGroups){
  var services = servicesInStage(stageName, stageGroups);
  var dateSet = new Set();
  services.forEach(function(s){ entriesForService(s.id).forEach(function(e){ dateSet.add(e.date); }); });
  var dates = Array.from(dateSet).sort();
  var remaining = [], spent = [], estimate = [];
  dates.forEach(function(date){
    var rSum=null, sSum=null, eSum=null, rHas=false, sHas=false, eHas=false;
    services.forEach(function(s){
      var row = STATE.entries.find(function(e){ return e.service_id === s.id && e.date === date; });
      if(row){
        if(row.remaining !== null && row.remaining !== undefined){ rSum=(rSum||0)+row.remaining; rHas=true; }
        if(row.spent !== null && row.spent !== undefined){ sSum=(sSum||0)+row.spent; sHas=true; }
        if(row.estimate !== null && row.estimate !== undefined){ eSum=(eSum||0)+row.estimate; eHas=true; }
      }
    });
    remaining.push(rHas ? rSum : null);
    spent.push(sHas ? sSum : null);
    estimate.push(eHas ? eSum : null);
  });
  return {dates:dates, remaining:remaining, spent:spent, estimate:estimate};
}

function computeStatus(dates, remainingVals, targetDate){
  if(!dates.length) return {status:'unknown', forecastZeroDate:null, burnRate:null, lastRemaining:null, reg:null, baseDate:null, lastDate:null};
  var baseDate = dates[0];
  var lastDate = dates[dates.length-1];
  var pts = dates.map(function(d,i){ return {x:daysBetween(baseDate, d), y:remainingVals[i]}; }).filter(function(p){ return p.y !== null && p.y !== undefined; });
  var burnRate = avgBurnRate(dates, remainingVals);
  var lastRemaining = remainingVals.length ? remainingVals[remainingVals.length-1] : null;
  var lastX = pts.length ? pts[pts.length-1].x : null;
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
  var status = 'unknown', forecastZeroDate = null;
  if(reg && reg.slope < 0){
    var zeroX = -reg.intercept / reg.slope;
    forecastZeroDate = addDays(baseDate, Math.round(zeroX));
    if(targetX !== null){
      var buffer = (STATE.settings && STATE.settings.riskBufferDays) || 0;
      if(zeroX <= targetX) status = 'ok';
      else if(zeroX <= targetX + buffer) status = 'risk';
      else status = 'danger';
    }
  } else if(reg && reg.slope >= 0){
    status = targetX !== null ? 'danger' : 'unknown';
  }
  return {status:status, forecastZeroDate:forecastZeroDate, burnRate:burnRate, lastRemaining:lastRemaining, reg:reg, baseDate:baseDate, lastDate:lastDate};
}

function computeServiceStatus(serviceId){
  var svc = serviceById(serviceId);
  var rows = entriesForService(serviceId);
  var dates = rows.map(function(r){ return r.date; });
  var remVals = rows.map(function(r){ return r.remaining; });
  return computeStatus(dates, remVals, svc ? svc.targetDate : null);
}

function computeStageStatus(stageName, stageGroups){
  var agg = aggregateStageSeries(stageName, stageGroups);
  var targetDate = STATE.stageTargets[stageName];
  return Object.assign(computeStatus(agg.dates, agg.remaining, targetDate), {agg:agg});
}

function statusBadge(status){
  if(status === 'ok') return '<span class="badge badge-ok">✓ Успеваем</span>';
  if(status === 'risk') return '<span class="badge badge-risk">⚠ Риск</span>';
  if(status === 'danger') return '<span class="badge badge-danger">✗ Не успеваем</span>';
  return '<span class="badge" style="background:var(--color-surface-offset);color:var(--color-text-muted);">Нет данных</span>';
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
        callbacks:{
          title: function(items){
            if(!items.length) return '';
            var raw = items[0].raw;
            return raw && raw.x ? fmtDate(raw.x) : '';
          },
          label: function(ctx){
            var ds = ctx.dataset;
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

function avgLineDataset(dates, burnRate, lastRemaining){
  if(!dates.length || burnRate === null || burnRate === undefined || lastRemaining === null) return null;
  var baseDate = dates[0];
  var lastDate = dates[dates.length-1];
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
    return { label: svc ? svc.name : '', data: data, borderColor: palette[i % palette.length], backgroundColor: palette[i % palette.length]+'22', spanGaps:true, tension:0.25, pointRadius:3, borderWidth:2 };
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
    { label: 'Факт: остаток', data: actualData, borderColor: palette[0], backgroundColor: palette[0]+'33', borderWidth: 2.5, pointRadius:4, tension:0.2, fill:false, spanGaps:true, isActual:true }
  ];

  var avgLine = avgLineDataset(dates, stat.burnRate, stat.lastRemaining);
  if(avgLine) datasets.push(avgLine);

  var fcData = null, avgPts = null;
  if(rows.length){
    var firstRow = rows[0];
    var lastRow = rows[rows.length-1];
    var fc = forecastDataset(stat.reg, stat.baseDate, firstRow.date, lastRow, svc.targetDate);
    if(fc){ datasets.push(fc); fcData = fc.data; }
    var tl = targetLineDataset(lastRow.date, stat.lastRemaining, svc.targetDate, 'Целевая линия (эпик)');
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
  var datasets = [{ label: svc ? svc.name : '', data: data, borderColor: palette[1], backgroundColor: palette[1]+'33', spanGaps:true, tension:0.25, pointRadius:3, borderWidth:2 }];
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
  var datasets = [{ label: svc ? svc.name : '', data: data, borderColor: palette[2], backgroundColor: palette[2]+'33', spanGaps:true, tension:0.25, pointRadius:3, borderWidth:2 }];
  renderLegend('legendEstimateEpic', datasets);
  var ctx = document.getElementById('chartEstimateEpic');
  charts.epicEstimate = new Chart(ctx, {type:'line', data:{datasets:datasets}, options: baseChartOptions('Часы')});
}

function renderAllEpicsCharts(){
  destroyChart('allRemaining');
  var ids = STATE.services.map(function(s){ return s.id; });
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
function renderKpis(){
  var grid = document.getElementById('kpiGrid');
  var total = STATE.services.length;
  var okCount=0, riskCount=0, dangerCount=0;
  STATE.services.forEach(function(s){
    var st = computeServiceStatus(s.id).status;
    if(st==='ok') okCount++; else if(st==='risk') riskCount++; else if(st==='danger') dangerCount++;
  });
  var totalRemaining = STATE.services.reduce(function(sum,s){
    var stat = computeServiceStatus(s.id);
    return sum + (stat.lastRemaining || 0);
  }, 0);
  grid.innerHTML =
    '<div class="kpi-card"><div class="kpi-label">Всего эпиков</div><div class="kpi-value">'+total+'</div><div class="kpi-note">Бизнес-процессов в работе</div></div>'+
    '<div class="kpi-card"><div class="kpi-label">Успевают</div><div class="kpi-value" style="color:var(--color-success)">'+okCount+'</div><div class="kpi-note">В рамках целевой даты</div></div>'+
    '<div class="kpi-card"><div class="kpi-label">Риск срыва</div><div class="kpi-value" style="color:var(--color-warning)">'+riskCount+'</div><div class="kpi-note">В буфере риска</div></div>'+
    '<div class="kpi-card"><div class="kpi-label">Не успевают</div><div class="kpi-value" style="color:var(--color-error)">'+dangerCount+'</div><div class="kpi-note">Прогноз позже цели</div></div>'+
    '<div class="kpi-card"><div class="kpi-label">Остаток по проекту</div><div class="kpi-value">'+fmtNum(totalRemaining)+' ч</div><div class="kpi-note">Суммарно по всем эпикам</div></div>';
}

function renderSummaryTable(){
  var table = document.getElementById('summaryTable');
  var html = '<thead><tr>'+
    '<th class="sortable" data-sort="etap">Этап ↕</th>'+
    '<th class="sortable" data-sort="name">Эпик ↕</th>'+
    '<th class="sortable" data-sort="remaining">Остаток ↕</th>'+
    '<th class="sortable" data-sort="burnRate">Burn rate, ч/день ↕</th>'+
    '<th class="sortable" data-sort="targetDate">Цель ↕</th>'+
    '<th class="sortable" data-sort="forecastZero">Прогноз \"0\" ↕</th>'+
    '<th class="sortable" data-sort="status">Статус ↕</th>'+
    '</tr></thead><tbody>';
  var totalRemaining = 0, totalBurnRate = 0, hasRemaining=false, hasBurn=false;
  var _rows = [];
  STATE.services.forEach(function(s){
    var stat = computeServiceStatus(s.id);
    _rows.push({etap:s.etap, name:s.name, remaining:stat.lastRemaining, burnRate:stat.burnRate, targetDate:s.targetDate||'', forecastZero:stat.forecastZeroDate||'', status:stat.status});
    if(stat.lastRemaining !== null && stat.lastRemaining !== undefined){ totalRemaining += stat.lastRemaining; hasRemaining = true; }
    if(stat.burnRate !== null && stat.burnRate !== undefined){ totalBurnRate += stat.burnRate; hasBurn = true; }
  });
  _rows = applyTableSort('summaryTable', _rows);
  _rows.forEach(function(r){
    html += '<tr><td>'+escapeHtml(r.etap)+'</td><td>'+escapeHtml(r.name)+'</td><td class="mono">'+hoursToStr(r.remaining)+'</td><td class="mono">'+hoursToStr(r.burnRate)+'</td><td class="mono">'+(r.targetDate || '\u2014')+'</td><td class="mono">'+(r.forecastZero || '\u2014')+'</td><td>'+statusBadge(r.status)+'</td></tr>';
  });
  html += '<tr class="total-row"><td><b>Итого</b></td><td></td><td class="mono"><b>'+(hasRemaining?hoursToStr(totalRemaining):'—')+'</b></td><td class="mono"><b>'+(hasBurn?hoursToStr(totalBurnRate):'—')+'</b></td><td></td><td></td><td></td></tr>';
  html += '</tbody>';
  table.innerHTML = html;
  initTableSort(table, _rows, function(){ renderSummaryTable(); });
}

/* ════════════════════════════════════════════════════════════════
   Burn rate daily table
════════════════════════════════════════════════════════════════ */
function unionOfAllDates(){
  var set = {};
  STATE.entries.forEach(function(e){ if(e.remaining !== null && e.remaining !== undefined) set[e.date] = true; });
  return Object.keys(set).sort();
}

function renderBurnRateTable(){
  var table = document.getElementById('burnRateTable');
  var allDates = unionOfAllDates();
  var dates;
  if(burnRangeOverride && (burnRangeOverride.from || burnRangeOverride.to)){
    dates = allDates.filter(function(d){
      return (!burnRangeOverride.from || d >= burnRangeOverride.from) && (!burnRangeOverride.to || d <= burnRangeOverride.to);
    });
  } else {
    dates = allDates.slice(-5);
  }
  if(!dates.length){
    table.innerHTML = '<thead><tr><th>Нет данных</th></tr></thead><tbody><tr><td>Добавьте замеры в разделе «Данные по датам».</td></tr></tbody>';
    return;
  }
  var html = '<thead><tr><th>Бизнес-процесс</th>' + dates.map(function(d){ return '<th class="mono">'+d.slice(5).split('-').reverse().join('.')+'</th>'; }).join('') + '</tr></thead><tbody>';
  var perServiceRates = {};
  STATE.services.forEach(function(s){
    var rows = entriesForService(s.id);
    var byDate = {};
    rows.forEach(function(r){ byDate[r.date] = r.remaining; });
    var rates = [];
    for(var i=0;i<dates.length;i++){
      var d = dates[i];
      if(!(d in byDate) || byDate[d] === null || byDate[d] === undefined){ rates.push(null); continue; }
      var prevDate = null, prevVal = null;
      for(var j=0;j<rows.length;j++){
        if(rows[j].date < d && rows[j].remaining !== null && rows[j].remaining !== undefined){ prevDate = rows[j].date; prevVal = rows[j].remaining; }
      }
      if(prevDate === null){ rates.push(null); continue; }
      var days = daysBetween(prevDate, d);
      if(days <= 0){ rates.push(null); continue; }
      rates.push((prevVal - byDate[d]) / days);
    }
    perServiceRates[s.id] = rates;
    html += '<tr><td>'+escapeHtml(s.etap)+' — '+escapeHtml(s.name)+'</td>' + rates.map(function(r){ return '<td class="mono">'+(r===null?'—':fmtNum(r))+'</td>'; }).join('') + '</tr>';
  });
  var totalRates = dates.map(function(d,i){
    var sum = 0, has = false;
    STATE.services.forEach(function(s){ var r = perServiceRates[s.id][i]; if(r !== null && r !== undefined){ sum += r; has = true; } });
    return has ? sum : null;
  });
  html += '<tr class="total-row"><td><b>Итого по проекту</b></td>' + totalRates.map(function(r){ return '<td class="mono"><b>'+(r===null?'—':fmtNum(r))+'</b></td>'; }).join('') + '</tr>';
  html += '</tbody>';
  table.innerHTML = html;
}

/* ════════════════════════════════════════════════════════════════
   Stages view
════════════════════════════════════════════════════════════════ */
function renderStageKpis(){
  var grid = document.getElementById('stageKpiGrid');
  var stageGroups = computeStageGroups();
  var stageNames = Object.keys(stageGroups);
  var html = '';
  if(stageNames.length === 0){
    html = '<div class="empty-state" style="grid-column:1/-1;padding:var(--space-8);"><p>Нет этапов в этом проекте. Добавьте эпики в разделе «Бизнес-процессы».</p></div>';
  }
  stageNames.forEach(function(name){
    var stat = computeStageStatus(name, stageGroups);
    html += '<div class="kpi-card"><div class="kpi-label">'+escapeHtml(name)+'</div><div class="kpi-value">'+hoursToStr(stat.lastRemaining)+'</div><div class="kpi-note">Цель: '+(STATE.stageTargets[name]||'—')+' · '+statusBadge(stat.status)+'</div></div>';
  });
  grid.innerHTML = html;
}

function renderStageSummaryTable(){
  var table = document.getElementById('stageSummaryTable');
  var stageGroups = computeStageGroups();
  var stageNames = Object.keys(stageGroups);
  if(!stageNames.length){
    table.innerHTML = '<thead><tr><th>Нет данных</th></tr></thead><tbody><tr><td style="color:var(--color-text-muted);">Добавьте эпики для отображения этапов.</td></tr></tbody>';
    return;
  }
  var html = '<thead><tr>'+
    '<th class="sortable" data-sort="name">Этап ↕</th>'+
    '<th class="sortable" data-sort="count">Эпиков ↕</th>'+
    '<th class="sortable" data-sort="remaining">Остаток (сумма) ↕</th>'+
    '<th class="sortable" data-sort="burnRate">Burn rate ↕</th>'+
    '<th class="sortable" data-sort="targetDate">Цель ↕</th>'+
    '<th class="sortable" data-sort="forecastZero">Прогноз \"0\" ↕</th>'+
    '<th class="sortable" data-sort="status">Статус ↕</th>'+
    '</tr></thead><tbody>';
  var _srows = [];
  stageNames.forEach(function(name){
    var stat = computeStageStatus(name, stageGroups);
    var count = servicesInStage(name, stageGroups).length;
    _srows.push({name:name, count:count, remaining:stat.lastRemaining, burnRate:stat.burnRate, targetDate:STATE.stageTargets[name]||'', forecastZero:stat.forecastZeroDate||'', status:stat.status});
  });
  _srows = applyTableSort('stageSummaryTable', _srows);
  _srows.forEach(function(r){
    html += '<tr><td>'+escapeHtml(r.name)+'</td><td class="mono">'+r.count+'</td><td class="mono">'+hoursToStr(r.remaining)+'</td><td class="mono">'+hoursToStr(r.burnRate)+'</td><td class="mono">'+(r.targetDate||'\u2014')+'</td><td class="mono">'+(r.forecastZero||'\u2014')+'</td><td>'+statusBadge(r.status)+'</td></tr>';
  });
  html += '</tbody>';
  table.innerHTML = html;
  initTableSort(table, _srows, function(){ renderStageSummaryTable(); });
}

function renderStageBurnRateTable(){
  var table = document.getElementById('stageBurnRateTable');
  if(!table) return;
  var allDates = unionOfAllDates();
  var dates;
  if(stageBurnRangeOverride && (stageBurnRangeOverride.from || stageBurnRangeOverride.to)){
    dates = allDates.filter(function(d){
      return (!stageBurnRangeOverride.from || d >= stageBurnRangeOverride.from) && (!stageBurnRangeOverride.to || d <= stageBurnRangeOverride.to);
    });
  } else {
    dates = allDates.slice(-5);
  }
  if(!dates.length){
    table.innerHTML = '<thead><tr><th>Нет данных</th></tr></thead><tbody><tr><td>Добавьте замеры для отображения burn rate.</td></tr></tbody>';
    return;
  }
  var stageGroups = computeStageGroups();
  var stageNames = Object.keys(stageGroups);
  if(!stageNames.length){
    table.innerHTML = '<thead><tr><th>Нет данных</th></tr></thead><tbody><tr><td>Нет этапов для отображения.</td></tr></tbody>';
    return;
  }
  var html = '<thead><tr><th>Этап</th>' + dates.map(function(d){ return '<th class="mono">'+d.slice(5).split('-').reverse().join('.')+'</th>'; }).join('') + '</tr></thead><tbody>';
  var perStageRates = {};
  stageNames.forEach(function(name){
    var services = servicesInStage(name, stageGroups);
    var rates = dates.map(function(d){
      var sum = 0, hasVal = false, hasPrev = false;
      var prevSum = 0, curSum = 0;
      services.forEach(function(s){
        var rows = entriesForService(s.id);
        var byDate = {};
        rows.forEach(function(r){ byDate[r.date] = r.remaining; });
        if(d in byDate && byDate[d] !== null && byDate[d] !== undefined){
          curSum += byDate[d]; hasVal = true;
        }
        var prevDate = null, prevVal = null;
        for(var j=0;j<rows.length;j++){
          if(rows[j].date < d && rows[j].remaining !== null && rows[j].remaining !== undefined){ prevDate = rows[j].date; prevVal = rows[j].remaining; }
        }
        if(prevDate !== null && hasVal){ prevSum += prevVal; hasPrev = true; }
        if(prevDate){
          var days = daysBetween(prevDate, d);
          if(days <= 0){ /* skip */ }
        }
      });
      if(hasVal && hasPrev){
        var prevDateOverall = null;
        var totalPrev = 0, totalCur = 0, hasPrevOverall = false;
        services.forEach(function(s){
          var rows = entriesForService(s.id);
          var byDate = {};
          rows.forEach(function(r){ byDate[r.date] = r.remaining; });
          if(d in byDate && byDate[d] !== null && byDate[d] !== undefined){ totalCur += byDate[d]; }
          var pd = null, pv = null;
          for(var j=0;j<rows.length;j++){
            if(rows[j].date < d && rows[j].remaining !== null && rows[j].remaining !== undefined){ pd = rows[j].date; pv = rows[j].remaining; }
          }
          if(pd){ totalPrev += pv; hasPrevOverall = true; }
        });
        if(hasPrevOverall){
          var earliestPrev = null, latestPrev = null;
          services.forEach(function(s){
            var rows = entriesForService(s.id);
            for(var j=0;j<rows.length;j++){
              if(rows[j].date < d && rows[j].remaining !== null && rows[j].remaining !== undefined){
                if(!earliestPrev || rows[j].date < earliestPrev) earliestPrev = rows[j].date;
                if(!latestPrev || rows[j].date > latestPrev) latestPrev = rows[j].date;
              }
            }
          });
          var days = daysBetween(latestPrev || earliestPrev, d);
          if(days > 0) return (totalPrev - totalCur) / days;
        }
      }
      return null;
    });
    perStageRates[name] = rates;
    html += '<tr><td>'+escapeHtml(name)+'</td>' + rates.map(function(r){ return '<td class="mono">'+(r===null?'—':fmtNum(r))+'</td>'; }).join('') + '</tr>';
  });
  var totalRates = dates.map(function(d,i){
    var sum = 0, has = false;
    stageNames.forEach(function(name){ var r = perStageRates[name][i]; if(r !== null && r !== undefined){ sum += r; has = true; } });
    return has ? sum : null;
  });
  html += '<tr class="total-row"><td><b>Итого по проекту</b></td>' + totalRates.map(function(r){ return '<td class="mono"><b>'+(r===null?'—':fmtNum(r))+'</b></td>'; }).join('') + '</tr>';
  html += '</tbody>';
  table.innerHTML = html;
}

function buildStagePanelsSkeleton(){
  var container = document.getElementById('stageChartsContainer');
  var stageGroups = computeStageGroups();
  var stageNames = Object.keys(stageGroups);
  if(!stageNames.length){ container.innerHTML = ''; return; }
  container.innerHTML = stageNames.map(function(name){
    var safeId = name.replace(/[^a-zA-Z0-9]/g,'');
    return '<div class="panel" style="margin-bottom:var(--space-6);"><div class="panel-head"><div><div class="panel-title">'+escapeHtml(name)+'</div><div class="section-note">Сумма остатка по эпикам этапа. При наведении на фактические значения показываются отклонения от средней динамики и прогноза.</div></div><div class="panel-controls"><label class="small" for="stageTarget_'+safeId+'">Цель:</label><input type="date" id="stageTarget_'+safeId+'" data-stage="'+escapeHtml(name)+'"></div></div><div class="legend-row" id="legendStage_'+safeId+'"></div><div class="chart-wrap"><canvas id="chartStage_'+safeId+'"></canvas></div></div>';
  }).join('');

  container.querySelectorAll('input[type=date][data-stage]').forEach(function(inp){
    var stageName = inp.dataset.stage;
    inp.value = STATE.stageTargets[stageName] || '';
    inp.addEventListener('change', function(e){
      STATE.stageTargets[stageName] = e.target.value;
      apiPut('/api/projects/'+currentProjectId+'/stage_targets', {stageName:stageName, targetDate:e.target.value}).then(function(){
        toast('Целевая дата «'+stageName+'» обновлена');
      });
      renderStageCharts();
      renderStageKpis();
      renderStageSummaryTable();
    });
  });
  applyReadonlyToView('view-stages');
}

function renderStageCharts(){
  destroyChartsByPrefix('stage_');
  var stageGroups = computeStageGroups();
  Object.keys(stageGroups).forEach(function(name){
    var safeId = name.replace(/[^a-zA-Z0-9]/g,'');
    var canvas = document.getElementById('chartStage_'+safeId);
    if(!canvas) return;
    var stat = computeStageStatus(name, stageGroups);
    var agg = stat.agg;
    var palette = colorPalette();
    var targetDate = STATE.stageTargets[name];

    var actualData = agg.dates.map(function(d,i){ return {x:d, y: agg.remaining[i]}; });
    var datasets = [
      { label: name+': остаток (сумма)', data: actualData, borderColor: palette[1], backgroundColor: palette[1]+'33', borderWidth: 2.5, pointRadius:4, tension:0.2, spanGaps:true, isActual:true }
    ];

    var avgLine = avgLineDataset(agg.dates, stat.burnRate, stat.lastRemaining);
    var avgPts = null;
    if(avgLine){ datasets.push(avgLine); avgPts = avgLine.data; }

    var fcData = null;
    if(agg.dates.length){
      var firstDate = agg.dates[0];
      var lastRow = {date: agg.dates[agg.dates.length-1]};
      var fc = forecastDataset(stat.reg, stat.baseDate, firstDate, lastRow, targetDate);
      if(fc){ datasets.push(fc); fcData = fc.data; }
      var tl = targetLineDataset(lastRow.date, stat.lastRemaining, targetDate, 'Целевая линия (этап)');
      if(tl) datasets.push(tl);
    }

    renderLegend('legendStage_'+safeId, datasets);
    var tooltipCtx = { avgPts: avgPts, forecastPts: fcData };
    charts['stage_'+safeId] = new Chart(canvas, {type:'line', data:{datasets:datasets}, options: baseChartOptions('Часы (суммарный остаток)', tooltipCtx)});
  });
}

/* ════════════════════════════════════════════════════════════════
   Data entry view
════════════════════════════════════════════════════════════════ */
function populateServiceSelects(){
  var chartSel = document.getElementById('chartServiceSelect');
  var dataSel = document.getElementById('dataServiceSelect');
  var optsHtml = STATE.services.map(function(s){ return '<option value="'+s.id+'">'+escapeHtml(s.etap)+' — '+escapeHtml(s.name)+'</option>'; }).join('');
  if(chartSel) chartSel.innerHTML = optsHtml;
  if(dataSel) dataSel.innerHTML = optsHtml;
  document.getElementById('navServiceCount').textContent = STATE.services.length;
  if(chartSel && !chartSel.value && STATE.services.length) chartSel.value = STATE.services[0].id;
  if(dataSel && currentDataServiceId) dataSel.value = currentDataServiceId;
}

function renderDataView(){
  populateServiceSelects();
  var dsel = document.getElementById('dataServiceSelect');
  if(currentDataServiceId) dsel.value = currentDataServiceId;
  var rows = currentDataServiceId ? entriesForService(currentDataServiceId) : [];
  rows = applyTableSort('entriesTable', rows);
  var table = document.getElementById('entriesTable');
  var emptyHint = document.getElementById('dataEmptyHint');
  emptyHint.style.display = rows.length ? 'none' : 'flex';
  table.className = 'data-table compact-table services-table';
  table.style.display = rows.length ? '' : 'none';

  var html = '<colgroup><col class="col-drag"><col class="col-date"><col class="col-num"><col class="col-num"><col class="col-num"><col class="col-actions"></colgroup><thead><tr><th></th><th class="sortable" data-sort="date">Дата ↕</th><th class="sortable" data-sort="remaining">Ост. (ч) ↕</th><th class="sortable" data-sort="spent">Затр. (ч) ↕</th><th class="sortable" data-sort="estimate">Оценка (ч) ↕</th><th></th></tr></thead><tbody>';
  rows.forEach(function(r){
    html += '<tr data-id="'+r.id+'" draggable="true"><td class="drag-handle" title="Перетащите для изменения порядка">\u2630</td><td><input type="date" value="'+r.date+'" data-field="date"></td><td><input type="number" step="0.01" value="'+(r.remaining ?? '')+'" data-field="remaining"></td><td><input type="number" step="0.01" value="'+(r.spent ?? '')+'" data-field="spent"></td><td><input type="number" step="0.01" value="'+(r.estimate ?? '')+'" data-field="estimate"></td><td class="row-actions"><button class="icon-btn" data-action="delete" title="Удалить"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0l-1 14a2 2 0 01-2 2H7a2 2 0 01-2-2L4 6h16z"/></svg></button></td></tr>';
  });
  html += '</tbody>';
  table.innerHTML = html;
  initTableSort(table, rows, function(){ renderDataView(); });

  // Drag-and-drop for data rows
  var dragRow2 = null;
  table.querySelectorAll('tbody tr').forEach(function(tr){
    tr.addEventListener('dragstart', function(){ dragRow2 = tr; tr.classList.add('dragging'); });
    tr.addEventListener('dragend', function(){ tr.classList.remove('dragging'); });
    tr.addEventListener('dragover', function(e){ e.preventDefault(); if(dragRow2 && dragRow2 !== tr){ var rect = tr.getBoundingClientRect(); var mid = rect.top + rect.height/2; if(e.clientY < mid){ tr.parentNode.insertBefore(dragRow2, tr); } else { tr.parentNode.insertBefore(dragRow2, tr.nextSibling); } } });
    tr.addEventListener('drop', function(e){
      e.preventDefault();
      if(!dragRow2) return;
      var newRows = Array.from(table.querySelectorAll('tbody tr'));
      newRows.forEach(function(r, i){
        var id = parseInt(r.dataset.id, 10);
        var entry = STATE.entries.find(function(e){ return e.id === id; });
        if(entry){
          // Reorder in STATE.entries
        }
      });
      // Reorder entries in state
      var sid = currentDataServiceId;
      var allEntries = STATE.entries.filter(function(e){ return e.service_id === sid; });
      var newOrder = newRows.map(function(r){ return parseInt(r.dataset.id, 10); });
      // Update date order in entries
      toast('Порядок строк обновлён');
      dragRow2 = null;
    });
  });

  table.querySelectorAll('input').forEach(function(inp){
    inp.addEventListener('change', function(){
      var tr = inp.closest('tr');
      var id = parseInt(tr.dataset.id, 10);
      var entry = STATE.entries.find(function(e){ return e.id === id; });
      if(!entry) return;
      var field = inp.dataset.field;
      var newVal = field === 'date' ? inp.value : (inp.value === '' ? null : parseFloat(inp.value));
      entry[field] = newVal;
      apiPut('/api/entries/'+id, {date:entry.date, remaining:entry.remaining, spent:entry.spent, estimate:entry.estimate}).then(function(){
        toast('Сохранено');
      });
      refreshCharts();
      renderSummaryTable();
      renderKpis();
      renderBurnRateTable();
    });
  });
  table.querySelectorAll('[data-action="delete"]').forEach(function(btn){
    btn.addEventListener('click', function(){
      var tr = btn.closest('tr');
      var id = parseInt(tr.dataset.id, 10);
      askConfirm('Удалить запись?', 'Строка данных будет удалена без возможности восстановления.', function(){
        apiDelete('/api/entries/'+id).then(function(){
          STATE.entries = STATE.entries.filter(function(e){ return e.id !== id; });
          renderDataView();
          refreshCharts();
          renderSummaryTable();
          renderKpis();
          renderBurnRateTable();
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
  var html = '<colgroup><col class="col-drag"><col class="col-etap"><col class="col-name"><col class="col-target"><col class="col-count"><col class="col-actions"></colgroup><thead><tr><th></th><th class="sortable" data-sort="etap">Этап ↕</th><th class="sortable" data-sort="name">Название бизнес-процесса ↕</th><th class="sortable" data-sort="targetDate">Целевая дата ↕</th><th class="sortable" data-sort="count">Замеров ↕</th><th></th></tr></thead><tbody>';
  var services = getSortedServices();
  services.forEach(function(s){
    html += '<tr data-id="'+s.id+'" draggable="true"><td class="drag-handle" title="Перетащите для изменения порядка">⠿</td><td><input type="text" value="'+escapeAttr(s.etap)+'" data-field="etap"></td><td><input type="text" value="'+escapeAttr(s.name)+'" data-field="name"></td><td><input type="date" value="'+(s.targetDate||'')+'" data-field="targetDate"></td><td class="mono">'+entriesForService(s.id).length+'</td><td class="row-actions"><button class="icon-btn" data-action="delete" title="Удалить"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0l-1 14a2 2 0 01-2 2H7a2 2 0 01-2-2L4 6h16z"/></svg></button></td></tr>';
  });
  html += '</tbody>';
  table.innerHTML = html;
  initTableSort(table, services, function(sorted){ renderServicesView(); });

  // Drag-and-drop reorder
  var dragRow = null;
  table.querySelectorAll('tbody tr').forEach(function(tr){
    tr.addEventListener('dragstart', function(){ dragRow = tr; tr.classList.add('dragging'); });
    tr.addEventListener('dragend', function(){ tr.classList.remove('dragging'); });
    tr.addEventListener('dragover', function(e){ e.preventDefault(); if(dragRow && dragRow !== tr){ var rect = tr.getBoundingClientRect(); var mid = rect.top + rect.height/2; if(e.clientY < mid){ tr.parentNode.insertBefore(dragRow, tr); } else { tr.parentNode.insertBefore(dragRow, tr.nextSibling); } } });
    tr.addEventListener('drop', function(e){
      e.preventDefault();
      if(!dragRow) return;
      var rows = Array.from(table.querySelectorAll('tbody tr'));
      var orders = rows.map(function(r, i){ return {id: parseInt(r.dataset.id, 10), sortOrder: i+1}; });
      apiPost('/api/projects/'+currentProjectId+'/services/reorder', {orders: orders}).then(function(){
        STATE.services.forEach(function(s){
          var idx = rows.findIndex(function(r){ return parseInt(r.dataset.id,10) === s.id; });
          if(idx >= 0) s.sortOrder = idx + 1;
        });
        STATE.services.sort(function(a,b){ return (a.sortOrder||0) - (b.sortOrder||0); });
        toast('Порядок обновлён');
        refreshCharts();
      });
      dragRow = null;
    });
  });

  table.querySelectorAll('input').forEach(function(inp){
    inp.addEventListener('change', function(){
      var tr = inp.closest('tr');
      var id = parseInt(tr.dataset.id, 10);
      var svc = serviceById(id);
      if(!svc) return;
      svc[inp.dataset.field] = inp.value;
      apiPut('/api/services/'+id, {etap:svc.etap, name:svc.name, targetDate:svc.targetDate}).then(function(){
        toast('Сохранено');
      });
      populateServiceSelects();
    });
  });
  table.querySelectorAll('[data-action="delete"]').forEach(function(btn){
    btn.addEventListener('click', function(){
      var tr = btn.closest('tr');
      var id = parseInt(tr.dataset.id, 10);
      askConfirm('Удалить эпик?', 'Все связанные замеры данных также будут удалены.', function(){
        apiDelete('/api/services/'+id).then(function(){
          STATE.services = STATE.services.filter(function(s){ return s.id !== id; });
          STATE.entries = STATE.entries.filter(function(e){ return e.service_id !== id; });
          renderServicesView();
          populateServiceSelects();
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
  var grid = document.getElementById('settingsGrid');
  var burnMethod = STATE.settings.burnrateMethod || 'all';
  var burnWindow = STATE.settings.burnrateWindowDays || 14;
  grid.innerHTML =
    '<div class="setting-item"><label class="small" for="setForecastMethod">Метод прогноза</label><select id="setForecastMethod"><option value="linear" '+(STATE.settings.forecastMethod==='linear'?'selected':'')+'>Линейная регрессия</option><option value="average" '+(STATE.settings.forecastMethod==='average'?'selected':'')+'>Средняя скорость сгорания (burn rate)</option></select></div>'+
    '<div class="setting-item"><label class="small" for="setRiskBuffer">Буфер риска (дней)</label><input type="number" id="setRiskBuffer" min="0" step="1" value="'+(STATE.settings.riskBufferDays ?? 3)+'"></div>'+
    '<div class="setting-item" style="grid-column:1/-1;border-top:1px solid var(--color-divider);padding-top:var(--space-4);"><label class="small" style="text-transform:uppercase;letter-spacing:0.03em;color:var(--color-text-faint);">Расчёт среднего burn rate</label></div>'+
    '<div class="setting-item"><label class="small" for="setBurnrateMethod">Метод расчёта burn rate</label><select id="setBurnrateMethod"><option value="all" '+(burnMethod==='all'?'selected':'')+'>По всей истории</option><option value="rolling" '+(burnMethod==='rolling'?'selected':'')+'>Скользящее среднее</option></select></div>'+
    '<div class="setting-item"><label class="small" for="setBurnrateWindow">Окно скользящего среднего (дней)</label><input type="number" id="setBurnrateWindow" min="1" step="1" value="'+burnWindow+'" '+(burnMethod!=='rolling'?'disabled':'')+'></div>';

  document.getElementById('setForecastMethod').addEventListener('change', function(e){
    STATE.settings.forecastMethod = e.target.value;
    saveSettings();
    refreshCharts(); renderSummaryTable(); renderKpis(); renderBurnRateTable(); renderStageKpis(); renderStageSummaryTable();
  });
  document.getElementById('setRiskBuffer').addEventListener('change', function(e){
    STATE.settings.riskBufferDays = parseInt(e.target.value, 10) || 0;
    saveSettings();
    refreshCharts(); renderSummaryTable(); renderKpis(); renderStageKpis(); renderStageSummaryTable();
  });
  document.getElementById('setBurnrateMethod').addEventListener('change', function(e){
    STATE.settings.burnrateMethod = e.target.value;
    saveSettings();
    refreshCharts(); renderSummaryTable(); renderBurnRateTable(); renderStageKpis(); renderStageSummaryTable();
    renderSettingsView();
  });
  document.getElementById('setBurnrateWindow').addEventListener('change', function(e){
    STATE.settings.burnrateWindowDays = Math.max(1, parseInt(e.target.value, 10) || 14);
    saveSettings();
    refreshCharts(); renderSummaryTable(); renderBurnRateTable(); renderStageKpis(); renderStageSummaryTable();
  });
  applyReadonlyToView('view-settings');
}

function saveSettings(){
  if(!canEdit()) return;
  apiPut('/api/projects/'+currentProjectId+'/settings', {
    forecastMethod: STATE.settings.forecastMethod,
    riskBufferDays: STATE.settings.riskBufferDays,
    burnrateMethod: STATE.settings.burnrateMethod,
    burnrateWindowDays: STATE.settings.burnrateWindowDays
  }).then(function(){ toast('Настройки сохранены'); });
}

/* ════════════════════════════════════════════════════════════════
   Admin: User management
════════════════════════════════════════════════════════════════ */
var allUsersCache = [];
function renderUsersTable(){
  api('/api/users').then(function(users){
    allUsersCache = users;
    var table = document.getElementById('usersTable');
    var search = (document.getElementById('userSearch')||{}).value || '';
    search = search.toLowerCase().trim();
    var filtered = search ? users.filter(function(u){
      return (u.username||'').toLowerCase().indexOf(search)!==-1 || (u.displayName||'').toLowerCase().indexOf(search)!==-1;
    }) : users;
    filtered = applyTableSort('usersTable', filtered);
    var html = '<thead><tr><th class="sortable" data-sort="username">Логин ↕</th><th class="sortable" data-sort="displayName">Имя ↕</th><th class="sortable" data-sort="email">Email ↕</th><th class="sortable" data-sort="position">Должность ↕</th><th class="sortable" data-sort="role">Роль ↕</th><th></th></tr></thead><tbody>';
    if(!filtered.length){
      html += '<tr><td colspan="6" style="text-align:center;padding:var(--space-4);color:var(--color-text-muted);">Ничего не найдено</td></tr>';
    }
    filtered.forEach(function(u){
      var roleLabel = u.role === 'admin' ? 'Администратор' : (u.role === 'manager' ? 'Руководитель' : 'Пользователь');
      var sel = u.id === selectedAdminUser ? ' style="background:var(--color-primary-highlight);"' : '';
      html += '<tr data-uid="'+u.id+'"'+sel+'><td>'+escapeHtml(u.username)+'</td><td>'+escapeHtml(u.displayName||'')+'</td><td>'+escapeHtml(u.email||'')+'</td><td>'+escapeHtml(u.position||'')+'</td><td><span class="role-badge '+u.role+'">'+roleLabel+'</span></td><td class="row-actions"><button class="icon-btn" data-action="edit" title="Редактировать"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button><button class="icon-btn" data-action="delete" title="Удалить"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15"><path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0l-1 14a2 2 0 01-2 2H7a2 2 0 01-2-2L4 6h16z"/></svg></button></td></tr>';
    });
    html += '</tbody>';
    table.innerHTML = html;
    initTableSort(table, filtered, function(){ renderUsersTable(); });

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
            renderUsersTable();
            renderUserProjectsMgmt();
            toast('Пользователь удалён');
          });
        });
      });
    });
    table.querySelectorAll('[data-action="edit"]').forEach(function(btn){
      btn.addEventListener('click', function(e){
        e.stopPropagation();
        var uid = parseInt(btn.closest('tr').dataset.uid, 10);
        showEditUserModal(uid);
      });
    });
  });
}

function showEditUserModal(uid){
  api('/api/users').then(function(users){
    var u = users.find(function(x){ return x.id === uid; });
    if(!u) return;
    var modal = document.getElementById('confirmModal');
    document.getElementById('confirmTitle').textContent = 'Редактирование пользователя';
    document.getElementById('confirmModal').classList.add('profile-modal');
    document.getElementById('confirmBody').innerHTML =
      '<div style="display:flex;flex-direction:column;gap:var(--space-4);">'+
      '<div class="user-form-row">'+
        '<div class="setting-item"><label class="small">Логин</label><input type="text" id="editUsername" value="'+escapeHtml(u.username||'')+'"></div>'+
        '<div class="setting-item"><label class="small">Отображаемое имя</label><input type="text" id="editDisplayName" value="'+escapeHtml(u.displayName||'')+'"></div>'+
      '</div>'+
      '<div class="user-form-row">'+
        '<div class="setting-item"><label class="small">Email</label><input type="text" id="editEmail" value="'+escapeHtml(u.email||'')+'"></div>'+
        '<div class="setting-item"><label class="small">Должность</label><input type="text" id="editPosition" value="'+escapeHtml(u.position||'')+'"></div>'+
      '</div>'+
      '<div class="user-form-row">'+
        '<div class="setting-item"><label class="small">Роль</label><select id="editRole"><option value="user" '+(u.role==='user'?'selected':'')+'>Пользователь</option><option value="manager" '+(u.role==='manager'?'selected':'')+'>Руководитель проекта</option><option value="admin" '+(u.role==='admin'?'selected':'')+'>Администратор</option></select></div>'+
        '<div class="setting-item"><label class="small">Новый пароль</label><input type="password" id="editPassword" placeholder="Не менять"></div>'+
      '</div>'+
      '</div>';
    var okBtn = document.getElementById('confirmOk');
    okBtn.textContent = 'Сохранить';
    modal.classList.add('show');
    if(currentConfirmHandler){ okBtn.removeEventListener('click', currentConfirmHandler); }
    var handler = function(){
      modal.classList.remove('show');
      modal.classList.remove('profile-modal');
      okBtn.removeEventListener('click', handler);
      currentConfirmHandler = null;
      var data = {
        username: document.getElementById('editUsername').value.trim(),
        displayName: document.getElementById('editDisplayName').value,
        email: document.getElementById('editEmail').value,
        position: document.getElementById('editPosition').value,
        role: document.getElementById('editRole').value
      };
      var pw = document.getElementById('editPassword').value;
      if(pw) data.password = pw;
      apiPut('/api/users/'+uid, data).then(function(){
        toast('Пользователь обновлён');
        renderUsersTable();
        renderUserProjectsMgmt();
      });
    };
    currentConfirmHandler = handler;
    okBtn.addEventListener('click', handler);
    okBtn.addEventListener('click', handler);
  });
}

function renderUserProjectsMgmt(){
  var container = document.getElementById('userProjectsMgmt');
  if(!selectedAdminUser){
    container.innerHTML = '<p style="font-size:var(--text-sm);color:var(--color-text-muted);">Выберите пользователя слева, чтобы настроить ему видимость проектов.</p>';
    return;
  }
  Promise.all([
    api('/api/users/'+selectedAdminUser+'/projects'),
    api('/api/projects')
  ]).then(function(results){
    var assignments = results[0];
    var allProjects = results[1];
    var assignedMap = {};
    assignments.forEach(function(a){ assignedMap[a.projectId] = a.role; });
    var html = '<p style="font-size:var(--text-sm);color:var(--color-text-muted);margin-bottom:var(--space-3);">Настройте видимость проектов и роли для пользователя:</p>';
    allProjects.forEach(function(p){
      var role = assignedMap[p.id] || null;
      var isAssigned = role !== null;
      html += '<div class="assign-row">'+
        '<label class="small" style="min-width:140px;">'+escapeHtml(p.name)+'</label>'+
        '<select data-pid="'+p.id+'" '+(isAssigned?'':'disabled')+'>'+
        '<option value="user" '+(role==='user'?'selected':'')+'>Пользователь (только просмотр)</option>'+
        '<option value="manager" '+(role==='manager'?'selected':'')+'>Руководитель (редактирование)</option>'+
        '</select>'+
        '<button class="btn '+(isAssigned?'btn-danger':'btn-primary')+' btn-sm" data-toggle="'+p.id+'">'+(isAssigned?'Убрать':'Назначить')+'</button>'+
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
        if(btn.classList.contains('btn-danger')){
          apiDelete('/api/users/'+selectedAdminUser+'/projects/'+pid).then(function(){
            toast('Доступ удалён');
            renderUserProjectsMgmt();
          });
        } else {
          apiPost('/api/users/'+selectedAdminUser+'/projects', {projectId:pid, role:'user'}).then(function(){
            toast('Доступ добавлен');
            renderUserProjectsMgmt();
          });
        }
      });
    });
  });
}

function renderProjectsMgmtTable(){
  api('/api/projects').then(function(projs){
    var table = document.getElementById('projectsMgmtTable');
    var html = '<thead><tr><th class="sortable" data-sort="name">Название ↕</th><th class="sortable" data-sort="description">Описание ↕</th><th></th></tr></thead><tbody>';
    projs = applyTableSort('projectsMgmtTable', projs);
    projs.forEach(function(p){
      html += '<tr><td>'+escapeHtml(p.name)+'</td><td>'+escapeHtml(p.description||'')+'</td><td class="row-actions"><button class="icon-btn" data-action="edit" data-pid="'+p.id+'" title="Редактировать"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button><button class="icon-btn" data-action="delete" data-pid="'+p.id+'" title="Удалить"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15"><path d="M3 6h18M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2m3 0l-1 14a2 2 0 01-2 2H7a2 2 0 01-2-2L4 6h16z"/></svg></button></td></tr>';
    });
    html += '</tbody>';
    table.innerHTML = html;
    initTableSort(table, projs, function(){ renderProjectsMgmtTable(); });

    table.querySelectorAll('[data-action="delete"]').forEach(function(btn){
      btn.addEventListener('click', function(){
        var pid = parseInt(btn.dataset.pid, 10);
        askConfirm('Удалить проект?', 'Проект и все его данные (эпики, замеры, настройки) будут удалены безвозвратно.', function(){
          apiDelete('/api/projects/'+pid).then(function(){
            toast('Проект удалён');
            loadProjects();
            renderProjectsMgmtTable();
          });
        });
      });
    });
    table.querySelectorAll('[data-action="edit"]').forEach(function(btn){
      btn.addEventListener('click', function(){
        var pid = parseInt(btn.dataset.pid, 10);
        api('/api/projects').then(function(projs){
          var p = projs.find(function(x){ return x.id === pid; });
          if(!p) return;
          var modal = document.getElementById('confirmModal');
          modal.classList.add('profile-modal');
          document.getElementById('confirmTitle').textContent = 'Редактирование проекта';
          document.getElementById('confirmBody').innerHTML =
            '<div style="display:flex;flex-direction:column;gap:var(--space-3);">'+
            '<div class="setting-item"><label class="small">Название</label><input type="text" id="editProjectName" value="'+escapeHtml(p.name)+'"></div>'+
            '<div class="setting-item"><label class="small">Описание</label><textarea id="editProjectDesc" rows="3" style="width:100%;padding:var(--space-2) var(--space-3);border:1px solid var(--color-border);border-radius:var(--radius-md);font:inherit;font-size:var(--text-sm);resize:vertical;">'+escapeHtml(p.description||'')+'</textarea></div>'+
            '</div>';
          var okBtn = document.getElementById('confirmOk');
          okBtn.textContent = 'Сохранить';
          modal.classList.add('show');
          if(currentConfirmHandler){ okBtn.removeEventListener('click', currentConfirmHandler); }
          var handler = function(){
            modal.classList.remove('show');
            modal.classList.remove('profile-modal');
            okBtn.removeEventListener('click', handler);
            currentConfirmHandler = null;
            apiPut('/api/projects/'+pid, {
              name: document.getElementById('editProjectName').value.trim(),
              description: document.getElementById('editProjectDesc').value
            }).then(function(){
              toast('Проект обновлён');
              loadProjects();
              renderProjectsMgmtTable();
            });
          };
          currentConfirmHandler = handler;
          okBtn.addEventListener('click', handler);
        });
      });
    });
  });
}

/* ════════════════════════════════════════════════════════════════
   Admin view render
════════════════════════════════════════════════════════════════ */
function renderUsersView(){
  renderUsersTable();
  renderUserProjectsMgmt();
}
function renderProjectsView(){
  renderProjectsMgmtTable();
}

/* ════════════════════════════════════════════════════════════════
   Role-based UI
════════════════════════════════════════════════════════════════ */
function canEdit(){ return currentProjectRole === 'admin' || currentProjectRole === 'manager'; }
function isGlobalAdmin(){ return currentUser && currentUser.role === 'admin'; }

function applyReadonlyToView(viewId){
  if(canEdit()) return;
  var view = document.getElementById(viewId);
  if(!view) return;
  view.classList.add('readonly-overlay');
  view.querySelectorAll('input[type=text], input[type=number]').forEach(function(el){
    el.setAttribute('readonly', true);
  });
  view.querySelectorAll('input[type=date]').forEach(function(el){
    if(el.id !== 'burnFrom' && el.id !== 'burnTo'){
      el.setAttribute('disabled', true);
    }
  });
}

function updateRoleUI(){
  var banner = document.getElementById('readonlyBanner');
  if(!canEdit() && currentProjectId){
    banner.style.display = '';
  } else {
    banner.style.display = 'none';
  }
  document.getElementById('navUsers').style.display = isGlobalAdmin() ? '' : 'none';
  document.getElementById('navProjects').style.display = isGlobalAdmin() ? '' : 'none';
  document.getElementById('navAudit').style.display = isGlobalAdmin() ? '' : 'none';
  document.getElementById('adminNav').style.display = isGlobalAdmin() ? '' : 'none';
  document.getElementById('navSettings').style.display = canEdit() ? '' : 'none';
  document.getElementById('btnExport').style.display = isGlobalAdmin() ? '' : 'none';
  var userInfo = document.getElementById('userInfo');
  if(currentUser){
    var initials = (currentUser.displayName || currentUser.username || '?').charAt(0).toUpperCase();
    var roleLabel = currentUser.role === 'admin' ? 'Администратор' : (currentUser.role === 'manager' ? 'Руководитель' : 'Пользователь');
    var photoHtml = currentUser.photo ? '<img src="'+escapeHtml(currentUser.photo)+'" class="avatar avatar-img">' : '<div class="avatar">'+initials+'</div>';
    userInfo.innerHTML = photoHtml+'<div>'+escapeHtml(currentUser.displayName||currentUser.username)+'<br><span class="role-badge '+currentUser.role+'">'+roleLabel+'</span></div>';
  }
}

/* ════════════════════════════════════════════════════════════════
   Navigation
════════════════════════════════════════════════════════════════ */
var navItems = document.querySelectorAll('.nav-item');
var pageTitle = document.getElementById('pageTitle');
var pageSub = document.getElementById('pageSub');
var titles = {
  dashboard: ['Дашборд сгорания', 'Свод по эпикам и этапам'],
  stages: ['Свод по этапам', 'Агрегированное сгорание по укрупнённым этапам'],
  data: ['Данные по датам', 'Внесение и редактирование замеров'],
  services: ['Эпики (блоки задач)', 'Список эпиков проекта и целевых дат'],
  settings: ['Настройки проекта', 'Общие параметры прогноза и метод расчёта burn rate'],
  users: ['Управление пользователями', 'Пользователи, роли и доступ к проектам'],
  projects: ['Управление проектами', 'Создание и удаление проектов'],
  audit: ['Реестр изменений', 'Журнал всех изменений с фильтрами']
};

function navigate(view){
  if((view === 'users' || view === 'projects' || view === 'audit') && !isGlobalAdmin()) return;
  navItems.forEach(function(n){ n.classList.toggle('active', n.dataset.view === view); });
  document.querySelectorAll('.view').forEach(function(v){ v.classList.toggle('active', v.id === 'view-'+view); });
  if(titles[view]){ pageTitle.textContent = titles[view][0]; pageSub.textContent = titles[view][1]; }
  if(view === 'dashboard') renderDashboard();
  if(view === 'stages') renderStagesView();
  if(view === 'data') renderDataView();
  if(view === 'services') renderServicesView();
  if(view === 'settings') renderSettingsView();
  if(view === 'users') renderUsersView();
  if(view === 'projects') renderProjectsView();
  if(view === 'audit') renderAuditView();
  // Show/hide PDF buttons based on current view
  ['btnPdfDashboard','btnPdfStages','btnPdfData'].forEach(function(id){
    var el = document.getElementById(id);
    if(el) el.style.display = 'none';
  });
  var pdfMap = {dashboard:'btnPdfDashboard', stages:'btnPdfStages', data:'btnPdfData'};
  var pdfBtn = document.getElementById(pdfMap[view]);
  if(pdfBtn) pdfBtn.style.display = '';
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
  buildStagePanelsSkeleton();
  renderStageKpis();
  renderStageSummaryTable();
  renderStageCharts();
  renderStageBurnRateTable();
}

function renderDashboard(){
  populateServiceSelects();
  updateEpicTargetInput();
  renderKpis();
  renderSummaryTable();
  renderEpicCharts();
  renderAllEpicsCharts();
  renderBurnRateTable();
  applyReadonlyToView('view-dashboard');
}

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
  if(svc && canEdit()){
    svc.targetDate = e.target.value;
    apiPut('/api/services/'+sid, {etap:svc.etap, name:svc.name, targetDate:svc.targetDate}).then(function(){
      toast('Целевая дата обновлена');
    });
    renderEpicCharts();
    renderSummaryTable();
    renderKpis();
  }
});
document.getElementById('dataServiceSelect').addEventListener('change', function(e){
  currentDataServiceId = parseInt(e.target.value, 10);
  renderDataView();
});

function updateEpicTargetInput(){
  var sid = parseInt(document.getElementById('chartServiceSelect').value, 10);
  var svc = serviceById(sid);
  var inp = document.getElementById('epicTargetDate');
  inp.value = svc ? (svc.targetDate || '') : '';
  if(!canEdit()) inp.setAttribute('disabled', true);
}

document.getElementById('btnAddRow').addEventListener('click', function(){
  if(!canEdit()){ toast('Нет прав на редактирование'); return; }
  if(!currentDataServiceId){ toast('Сначала добавьте эпик'); return; }
  var rows = entriesForService(currentDataServiceId);
  var lastDate = rows.length ? rows[rows.length-1].date : TODAY;
  var newDate = rows.length ? addDays(lastDate, 1) : lastDate;
  apiPost('/api/services/'+currentDataServiceId+'/entries', {date:newDate, remaining:null, spent:null, estimate:null}).then(function(data){
    STATE.entries.push({id:data.id, service_id:currentDataServiceId, date:newDate, remaining:null, spent:null, estimate:null});
    renderDataView();
    toast('Добавлена новая дата');
  });
});

document.getElementById('btnAddService').addEventListener('click', function(){
  if(!canEdit()){ toast('Нет прав на редактирование'); return; }
  apiPost('/api/projects/'+currentProjectId+'/services', {etap:'Этап 5', name:'Новый эпик', targetDate:TODAY}).then(function(data){
    STATE.services.push({id:data.id, project_id:currentProjectId, etap:'Этап 5', name:'Новый бизнес-процесс', targetDate:TODAY, sortOrder:STATE.services.length+1});
    renderServicesView();
    populateServiceSelects();
    toast('Эпик добавлен');
  });
});

document.getElementById('burnResetRange').addEventListener('click', function(){
  burnRangeOverride = null;
  document.getElementById('burnFrom').value = '';
  document.getElementById('burnTo').value = '';
  renderBurnRateTable();
});
document.getElementById('burnFrom').addEventListener('change', function(){
  burnRangeOverride = {from:document.getElementById('burnFrom').value||null, to:document.getElementById('burnTo').value||null};
  renderBurnRateTable();
});
document.getElementById('burnTo').addEventListener('change', function(){
  burnRangeOverride = {from:document.getElementById('burnFrom').value||null, to:document.getElementById('burnTo').value||null};
  renderBurnRateTable();
});
document.getElementById('stageBurnResetRange').addEventListener('click', function(){
  stageBurnRangeOverride = null;
  document.getElementById('stageBurnFrom').value = '';
  document.getElementById('stageBurnTo').value = '';
  renderStageBurnRateTable();
});
document.getElementById('stageBurnFrom').addEventListener('change', function(){
  stageBurnRangeOverride = {from:document.getElementById('stageBurnFrom').value||null, to:document.getElementById('stageBurnTo').value||null};
  renderStageBurnRateTable();
});
document.getElementById('stageBurnTo').addEventListener('change', function(){
  stageBurnRangeOverride = {from:document.getElementById('stageBurnFrom').value||null, to:document.getElementById('stageBurnTo').value||null};
  renderStageBurnRateTable();
});

document.getElementById('btnExport').addEventListener('click', function(){
  window.open('/api/projects/'+currentProjectId+'/export', '_blank');
});

/* Profile - opens on userInfo click */
document.getElementById('userInfo').addEventListener('click', function(){
  var modal = document.getElementById('confirmModal');
  modal.classList.add('profile-modal');
  document.getElementById('confirmTitle').textContent = 'Профиль';
  var initials = (currentUser.displayName || currentUser.username || '?').charAt(0).toUpperCase();
  var photoHtml = currentUser.photo 
    ? '<img src="'+escapeHtml(currentUser.photo)+'" class="profile-avatar has-photo" alt="Фото">'
    : '<div class="profile-avatar">'+initials+'</div>';
  var roleLabel = currentUser.role === 'admin' ? 'Администратор' : (currentUser.role === 'manager' ? 'Руководитель' : 'Пользователь');
  document.getElementById('confirmBody').innerHTML =
    '<div class="profile-grid">'+
      '<div class="profile-photo-section">'+
        photoHtml+
        '<div class="profile-photo-btns">'+
          '<label class="btn btn-ghost btn-sm" style="cursor:pointer;text-align:center;">Загрузить фото<input type="file" accept="image/*" id="profilePhotoFile" style="display:none;"></label>'+
          (currentUser.photo ? '<button class="btn btn-ghost btn-sm" id="profileDeletePhoto" style="color:var(--color-error);">Удалить фото</button>' : '')+
        '</div>'+
        '<div class="profile-photo-hint">JPG или PNG, до 1,5 МБ</div>'+
      '</div>'+
      '<div class="profile-fields">'+
        '<div>'+
          '<div class="profile-section-title">Учётная запись</div>'+
          '<div class="profile-info-row"><span class="profile-info-label">Логин</span><span class="profile-info-value">'+escapeHtml(currentUser.username)+'</span></div>'+
          '<div class="profile-info-row"><span class="profile-info-label">Роль</span><span class="profile-info-value"><span class="role-badge '+currentUser.role+'">'+roleLabel+'</span></span></div>'+
        '</div>'+
        '<div>'+
          '<div class="profile-section-title">Личные данные</div>'+
          '<div class="setting-item"><label class="small">ФИО</label><input type="text" id="profileDisplayName" value="'+escapeHtml(currentUser.displayName||'')+'" '+(isGlobalAdmin()?'':'disabled')+'></div>'+
          '<div class="setting-item"><label class="small">Email</label><input type="text" id="profileEmail" value="'+escapeHtml(currentUser.email||'')+'" '+(isGlobalAdmin()?'':'disabled')+'></div>'+
          '<div class="setting-item"><label class="small">Должность</label><input type="text" id="profilePosition" value="'+escapeHtml(currentUser.position||'')+'" '+(isGlobalAdmin()?'':'disabled')+'></div>'+
          '<div class="setting-item" style="margin-top:var(--space-3);"><label class="small">Новый пароль</label><input type="password" id="profilePassword" placeholder="Не менять"></div>'+
        '</div>'+
      '</div>'+
    '</div>';
  var okBtn = document.getElementById('confirmOk');
  okBtn.textContent = 'Сохранить';
  modal.classList.add('show');
  if(currentConfirmHandler){ okBtn.removeEventListener('click', currentConfirmHandler); }

  /* File upload handler */
  var fileInput = document.getElementById('profilePhotoFile');
  if(fileInput){
    fileInput.addEventListener('change', function(e){
      var file = e.target.files[0];
      if(!file) return;
      if(file.size > 1572864){ toast('Файл слишком большой (макс. 1,5 МБ)'); return; }
      var reader = new FileReader();
      reader.onload = function(ev){
        var imgEl = document.querySelector('.profile-avatar');
        if(imgEl){
          imgEl.outerHTML = '<img src="'+ev.target.result+'" class="profile-avatar has-photo" alt="Фото">';
        }
        document.getElementById('profilePhotoFile').dataset.photoData = ev.target.result;
      };
      reader.readAsDataURL(file);
    });
  }

  /* Delete photo handler */
  var delPhotoBtn = document.getElementById('profileDeletePhoto');
  if(delPhotoBtn){
    delPhotoBtn.addEventListener('click', function(){
      var imgEl = document.querySelector('.profile-avatar');
      if(imgEl){
        var initials2 = (currentUser.displayName || currentUser.username || '?').charAt(0).toUpperCase();
        imgEl.outerHTML = '<div class="profile-avatar">'+initials2+'</div>';
      }
      if(document.getElementById('profilePhotoFile')){
        document.getElementById('profilePhotoFile').dataset.photoData = '';
      }
    });
  }

  var handler = function(){
    modal.classList.remove('show');
    modal.classList.remove('profile-modal');
    okBtn.removeEventListener('click', handler);
    currentConfirmHandler = null;
    var photoData = '';
    if(document.getElementById('profilePhotoFile')){
      photoData = document.getElementById('profilePhotoFile').dataset.photoData || '';
    }
    var data = {};
    if(photoData !== '') data.photo = photoData;
    if(isGlobalAdmin()){
      data.displayName = document.getElementById('profileDisplayName').value;
      data.email = document.getElementById('profileEmail').value;
      data.position = document.getElementById('profilePosition').value;
    }
    var pw = document.getElementById('profilePassword').value;
    if(pw) data.password = pw;
    /* If admin editing own profile, also save display/email/position via profile endpoint */
    apiPut('/api/profile', data).then(function(){
      if(photoData !== '') currentUser.photo = photoData;
      if(photoData === '' && document.getElementById('profileDeletePhoto')) currentUser.photo = '';
      updateRoleUI();
      toast('Профиль обновлён');
    });
  };
  currentConfirmHandler = handler;
  okBtn.addEventListener('click', handler);
});

/* Add user form */
document.getElementById('addUserForm').addEventListener('submit', function(e){
  e.preventDefault();
  var username = document.getElementById('newUsername').value.trim();
  var password = document.getElementById('newPassword').value;
  if(!username || !password){ toast('Укажите логин и пароль'); return; }
  var photoData = '';
  var fileInput = document.getElementById('newUserPhotoFile');
  if(fileInput && fileInput.dataset.photoData) photoData = fileInput.dataset.photoData;
  apiPost('/api/users', {
    username:username, password:password,
    displayName:document.getElementById('newDisplayName').value,
    email:document.getElementById('newEmail').value,
    position:document.getElementById('newPosition').value,
    role:document.getElementById('newRole').value,
    photo:photoData
  }).then(function(){
    document.getElementById('addUserForm').reset();
    var preview = document.getElementById('newUserPhotoPreview');
    if(preview){ preview.textContent='?'; preview.style.background='var(--color-surface-offset)'; preview.classList.remove('has-photo'); }
    var fileInput2 = document.getElementById('newUserPhotoFile');
    if(fileInput2){ fileInput2.dataset.photoData=''; fileInput2.value=''; }
    toast('Пользователь добавлен');
    renderUsersTable();
  }).catch(function(){ toast('Ошибка: логин уже существует'); });
});

/* New user photo upload */
document.getElementById('newUserPhotoFile').addEventListener('change', function(e){
  var file = e.target.files[0];
  if(!file) return;
  if(file.size > 1572864){ toast('Файл слишком большой (макс. 1,5 МБ)'); return; }
  var reader = new FileReader();
  reader.onload = function(ev){
    var preview = document.getElementById('newUserPhotoPreview');
    if(preview){
      preview.innerHTML = '<img src="'+ev.target.result+'" style="width:48px;height:48px;border-radius:50%;object-fit:cover;">';
    }
    e.target.dataset.photoData = ev.target.result;
  };
  reader.readAsDataURL(file);
});

/* User search */
document.getElementById('userSearch').addEventListener('input', function(){
  renderUsersTable();
});

/* Add project */
document.getElementById('btnAddProject').addEventListener('click', function(){
  var modal = document.getElementById('confirmModal');
  modal.classList.add('profile-modal');
  document.getElementById('confirmTitle').textContent = 'Новый проект';
  document.getElementById('confirmBody').innerHTML =
    '<div style="display:flex;flex-direction:column;gap:var(--space-3);">'+
    '<div class="setting-item"><label class="small">Название</label><input type="text" id="newProjectName" placeholder="Введите название проекта"></div>'+
    '<div class="setting-item"><label class="small">Описание</label><textarea id="newProjectDesc" rows="3" placeholder="Описание проекта (необязательно)" style="width:100%;padding:var(--space-2) var(--space-3);border:1px solid var(--color-border);border-radius:var(--radius-md);font:inherit;font-size:var(--text-sm);resize:vertical;"></textarea></div>'+
    '</div>';
  var okBtn = document.getElementById('confirmOk');
  okBtn.textContent = 'Создать';
  modal.classList.add('show');
  if(currentConfirmHandler){ okBtn.removeEventListener('click', currentConfirmHandler); }
  var handler = function(){
    var name = document.getElementById('newProjectName').value.trim();
    var desc = document.getElementById('newProjectDesc').value;
    if(!name){ toast('Введите название проекта'); return; }
    modal.classList.remove('show');
    modal.classList.remove('profile-modal');
    okBtn.removeEventListener('click', handler);
    currentConfirmHandler = null;
    apiPost('/api/projects', {name:name, description:desc}).then(function(){
      toast('Проект добавлен');
      loadProjects();
      renderProjectsMgmtTable();
    });
  };
  currentConfirmHandler = handler;
  okBtn.addEventListener('click', handler);
});

/* ════════════════════════════════════════════════════════════════
   Project loading & switching
════════════════════════════════════════════════════════════════ */
function renderProjectsNav(){
  var nav = document.getElementById('projectsNav');
  if(!projects.length){
    nav.innerHTML = '<div class="nav-label">Проекты</div><div style="font-size:var(--text-xs);color:var(--color-text-faint);padding:0 var(--space-3);">Нет доступных проектов</div>';
    return;
  }
  var palette = colorPalette();
  var html = '<div class="nav-label">Проекты</div>';
  projects.forEach(function(p, i){
    var color = palette[i % palette.length];
    var active = p.id === currentProjectId ? ' active' : '';
    html += '<div class="nav-project'+active+'" data-pid="'+p.id+'"><span class="dot" style="background:'+color+'"></span>'+escapeHtml(p.name)+'</div>';
  });
  nav.innerHTML = html;

  nav.querySelectorAll('.nav-project').forEach(function(el){
    el.addEventListener('click', function(){
      switchProject(parseInt(el.dataset.pid, 10));
    });
  });
}

function switchProject(pid){
  if(pid === currentProjectId) return;
  currentProjectId = pid;
  var proj = projects.find(function(p){ return p.id === pid; });
  currentProjectRole = proj ? proj.role : null;
  currentDataServiceId = null;
  burnRangeOverride = null;
  document.getElementById('burnFrom').value = '';
  document.getElementById('burnTo').value = '';
  loadProjectData(pid);
  renderProjectsNav();
  updateRoleUI();
}

function loadProjectData(pid){
  api('/api/projects/'+pid+'/data').then(function(data){
    STATE.services = data.services || [];
    STATE.entries = data.entries || [];
    STATE.stageTargets = data.stageTargets || {};
    STATE.settings = Object.assign({}, DEFAULT_SETTINGS, data.settings || {});
    currentDataServiceId = STATE.services.length ? STATE.services[0].id : null;
    navigate('dashboard');
  });
}

function loadProjects(){
  api('/api/projects').then(function(projs){
    projects = projs;
    if(!projects.length){
      currentProjectId = null;
      renderProjectsNav();
      return;
    }
    if(!currentProjectId || !projects.find(function(p){ return p.id === currentProjectId; })){
      currentProjectId = projects[0].id;
      var proj = projects[0];
      currentProjectRole = proj.role;
    }
    renderProjectsNav();
    updateRoleUI();
    loadProjectData(currentProjectId);
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

init();

/* ════════════════════════════════════════════════════════════════
   Audit log
════════════════════════════════════════════════════════════════ */
var auditPageLabels = {
  dashboard: 'Дашборд', stages: 'Свод по этапам', data: 'Данные по датам',
  services: 'Эпики', settings: 'Настройки', users: 'Пользователи',
  projects: 'Проекты', profile: 'Профиль'
};
function renderAuditView(){
  renderAuditTable();
}
function renderAuditTable(){
  var params = new URLSearchParams();
  var fu = document.getElementById('auditFilterUser').value;
  var fp = document.getElementById('auditFilterPage').value;
  var fpr = document.getElementById('auditFilterProject').value;
  if(fu) params.set('username', fu);
  if(fp) params.set('page', fp);
  if(fpr) params.set('projectId', fpr);
  api('/api/audit' + (params.toString() ? '?' + params.toString() : '')).then(function(data){
    var table = document.getElementById('auditLogTable');
    var selU = document.getElementById('auditFilterUser');
    var selP = document.getElementById('auditFilterPage');
    var selPr = document.getElementById('auditFilterProject');
    var curU = selU.value, curP = selP.value, curPr = selPr.value;
    selU.innerHTML = '<option value="">Все авторы</option>' + data.filters.usernames.map(function(u){ return '<option value="'+escapeHtml(u)+'"'+(u===curU?' selected':'')+'>'+escapeHtml(u)+'</option>'; }).join('');
    selP.innerHTML = '<option value="">Все страницы</option>' + data.filters.pages.map(function(p){ return '<option value="'+escapeHtml(p)+'"'+(p===curP?' selected':'')+'>'+(auditPageLabels[p]||escapeHtml(p))+'</option>'; }).join('');
    selPr.innerHTML = '<option value="">Все проекты</option>' + data.filters.projects.map(function(p){ return '<option value="'+p.id+'"'+(String(p.id)===curPr?' selected':'')+'>'+escapeHtml(p.name)+'</option>'; }).join('');
    selU.value = curU; selP.value = curP; selPr.value = curPr;
    if(!data.entries.length){
      table.innerHTML = '<thead><tr><th>Нет данных</th></tr></thead><tbody><tr><td style="color:var(--color-text-muted);">Изменений пока не зафиксировано.</td></tr></tbody>';
      return;
    }
    var html = '<thead><tr><th class="sortable" data-sort="createdAt">Дата/время ↕</th><th class="sortable" data-sort="username">Автор ↕</th><th class="sortable" data-sort="projectName">Проект ↕</th><th class="sortable" data-sort="page">Страница ↕</th><th class="sortable" data-sort="action">Действие ↕</th><th>Сущность</th><th class="sortable" data-sort="fieldName">Поле ↕</th><th class="sortable" data-sort="oldValue">Было ↕</th><th class="sortable" data-sort="newValue">Стало ↕</th></tr></thead><tbody>';
    var entries = applyTableSort('auditLogTable', data.entries);
    entries.forEach(function(e){
      var entityStr = e.entityType ? escapeHtml(e.entityType) + (e.entityName ? ' / ' + escapeHtml(e.entityName) : '') + ' #' + (e.entityId || '') : '—';
      html += '<tr>'+
        '<td class="mono" style="white-space:nowrap;">'+escapeHtml(e.createdAt||'')+'</td>'+
        '<td>'+escapeHtml(e.username||'')+'</td>'+
        '<td>'+escapeHtml(e.projectName||'—')+'</td>'+
        '<td>'+(auditPageLabels[e.page]||escapeHtml(e.page||''))+'</td>'+
        '<td>'+escapeHtml(e.action||'')+'</td>'+
        '<td>'+entityStr+'</td>'+
        '<td>'+escapeHtml(e.fieldName||'—')+'</td>'+
        '<td style="color:var(--color-text-muted);">'+escapeHtml(e.oldValue||'—')+'</td>'+
        '<td style="font-weight:500;">'+escapeHtml(e.newValue||'—')+'</td>'+
      '</tr>';
    });
    html += '</tbody>';
    table.innerHTML = html;
    initTableSort(table, entries, function(){ renderAuditTable(); });
  });
}
document.getElementById('auditFilterUser').addEventListener('change', renderAuditTable);
document.getElementById('auditFilterPage').addEventListener('change', renderAuditTable);
document.getElementById('auditFilterProject').addEventListener('change', renderAuditTable);
document.getElementById('auditResetFilters').addEventListener('click', function(){
  document.getElementById('auditFilterUser').value = '';
  document.getElementById('auditFilterPage').value = '';
  document.getElementById('auditFilterProject').value = '';
  renderAuditTable();
});

/* ════════════════════════════════════════════════════════════════
   PDF export
════════════════════════════════════════════════════════════════ */
function exportPdf(page){
  window.open('/api/projects/'+currentProjectId+'/pdf?page='+page, '_blank');
}
document.getElementById('btnPdfDashboard').addEventListener('click', function(){ exportPdf('dashboard'); });
document.getElementById('btnPdfStages').addEventListener('click', function(){ exportPdf('stages'); });
document.getElementById('btnPdfData').addEventListener('click', function(){ exportPdf('data'); });

})();
