// Расчёт статуса и Монте-Карло кодом из static/js/app.js (в node) — для сверки с forecast.py (tests/test_forecast.py)
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function grab(name){ const i = src.indexOf('function ' + name + '('); let depth = 0, j = src.indexOf('{', i);
  for(let k = j; k < src.length; k++){ if(src[k] === '{') depth++; else if(src[k] === '}'){ depth--; if(!depth) return src.slice(i, k + 1); } } }
var STATE = {settings: JSON.parse(process.argv[3])};
const names = ['daysBetween','addDays','isNum','avgBurnRate','linearForecast','seededRandom','hashString','burnIntervals','monteCarloForecast','computeStatus'];
eval(names.map(grab).join('\n') + '\nvar MC_RUNS = 2000, MC_HORIZON_DAYS = 3 * 365; var mcCache = {};' +
  '\nglobalThis.run = function(d, r, t){ return computeStatus(d, r, t); };');
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
console.log(JSON.stringify(cases.map(c => { const s = run(c[0], c[1], c[2]);
  return [s.status, s.forecastZeroDate, s.noBurn, s.mc ? [s.mc.p15, s.mc.p50, s.mc.p85, s.mc.probability] : null]; })));
