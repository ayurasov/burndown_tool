// Расчёт статуса и Монте-Карло кодом из static/js/app.js (в node) — для сверки с forecast.py (tests/test_forecast.py)
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function grab(name){ const i = src.indexOf('function ' + name + '('); let depth = 0, j = src.indexOf('{', i);
  for(let k = j; k < src.length; k++){ if(src[k] === '{') depth++; else if(src[k] === '}'){ depth--; if(!depth) return src.slice(i, k + 1); } } }
var STATE = {settings: JSON.parse(process.argv[3])};
const names = ['daysBetween','addDays','isNum','avgBurnRate','linearForecast','seededRandom','hashString','burnIntervals',
  'zeroReachedDate','monteCarloForecast','computeStatus','aggregateSeries','isoOrdinal','ordinalIso','workCalendar',
  'rangeConfidence','burnupForecast'];
eval(names.map(grab).join('\n') + '\nvar MC_RUNS = 2000, MC_HORIZON_DAYS = 3 * 365, SLOPE_EPS = 1e-9; var mcCache = {};' +
  '\nvar MC_HORIZON_WORKDAYS = 3 * 261; var CALENDAR_DAYS = {workdays: false, signature: "", horizon: 3 * 365, span: daysBetween, shift: addDays};' +
  '\nvar calCache = {key: null, cal: null}; var MC_QUANTILES = {50: [0.25, 0.75], 70: [0.15, 0.85], 80: [0.1, 0.9], 90: [0.05, 0.95]};' +
  '\nglobalThis.burnup = burnupForecast; globalThis.cal = workCalendar;' +
  '\nglobalThis.run = function(d, r, t){ return computeStatus(d, r, t); }; globalThis.agg = aggregateSeries;');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
if(input.calendar){  // календарь: {calendar: [[метод, дата, n|дата], …]}
  const c = cal();
  console.log(JSON.stringify(input.calendar.map(q => q[0] === 'span' ? c.span(q[1], q[2]) : c.shift(q[1], q[2]))));
} else if(input.burnup){  // burn-up по срезам: {burnup: [[срезы], …]}
  console.log(JSON.stringify(input.burnup.map(h => { const f = burnup(h);
    return f ? [f.converges, f.date || null, !!f.tooFar, Math.round(f.doneRate * 1e6) / 1e6, Math.round(f.scopeRate * 1e6) / 1e6] : null; })));
} else if(input.aggregate){  // сумма по этапу: {aggregate: [[строки эпика], …]}
  const a = agg(input.aggregate);
  console.log(JSON.stringify([a.dates, a.remaining, a.spent, a.estimate]));
} else {
  console.log(JSON.stringify(input.map(c => { const s = run(c[0], c[1], c[2]);
    return [s.status, s.forecastZeroDate, s.noBurn, s.tooFar, s.finished,
            s.mc ? [s.mc.lo, s.mc.p50, s.mc.hi, s.mc.probability, s.mc.confidence] : null, s.lastDate]; })));
}
