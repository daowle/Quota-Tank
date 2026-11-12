// 공통 로직: widget.py 의 계산(forecast_line, weekly_line, plan_cycles, burn_rate)을 옮긴 것과 모션 도구.
const H5 = 5 * 3600, WEEK = 7 * 86400;
const REDUCE = matchMedia('(prefers-reduced-motion: reduce)').matches;
const $ = (s, r = document) => r.querySelector(s);
const escapeHtml = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const pad = n => String(n).padStart(2, '0');
const hm = ts => { const d = new Date(ts * 1000); return pad(d.getHours()) + ':' + pad(d.getMinutes()); };
const hhmm = m => `${pad(Math.floor(m / 60) % 24)}:${pad(Math.round(m) % 60)}`;
const dur = s => { s = Math.max(0, Math.floor(s)); return `${Math.floor(s / 3600)}:${pad(Math.floor(s % 3600 / 60))}:${pad(s % 60)}`; };
const durShort = s => { s = Math.max(0, s); const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return d ? `${d}일 ${h}시간` : h ? `${h}시간 ${m}분` : `${m}분`; };
const WD = '일월화수목금토';
const dayLabel = ts => { const d = new Date(ts * 1000); return `${d.getMonth() + 1}/${d.getDate()}(${WD[d.getDay()]}) ${hm(ts)}`; };

function applyTheme(t) {
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}

function waitApi() {
  return new Promise(res => {
    if (window.pywebview && window.pywebview.api) return res(window.pywebview.api);
    window.addEventListener('pywebviewready', () => res(window.pywebview.api), { once: true });
  });
}

function dayEnd(t, hour) { const d = new Date(t * 1000), e = new Date(d); e.setHours(hour, 0, 0, 0); if (d >= e) e.setDate(e.getDate() + 1); return e.getTime() / 1000; }
function planCycles(t, r5, active, hour) { const end = dayEnd(t, hour), out = []; let s = active ? r5 : t; while (s < end && out.length < 6) { out.push(s); s += H5; } return out; }
function burnRate(s, t) {
  const pts = s.hist.filter(p => t - p[0] <= 5400);
  if (pts.length < 2) return null;
  const a = pts[0], b = pts[pts.length - 1], dt = (b[0] - a[0]) / 3600;
  if (dt < 5 / 60 || b[1] <= a[1]) return dt >= 5 / 60 ? 0 : null;
  return (b[1] - a[1]) / dt;
}
function view(s, t) {
  const active = !!s.r5 && s.r5 > t, u5 = active ? s.u5 : 0, el5 = active ? 1 - (s.r5 - t) / H5 : null;
  const wok = !!s.rw && s.rw > t, uw = wok ? s.uw : 0, elw = wok ? 1 - (s.rw - t) / WEEK : null;
  const pace = el5 == null ? null : el5 * 100, diff = pace == null ? 0 : u5 - pace;
  const state = !active ? ['대기', 'var(--muted)'] : u5 >= 100 ? ['한도', 'var(--crit)'] : diff > 5 ? ['과속', 'var(--crit)'] : diff < -8 ? ['여유', 'var(--ok)'] : ['적정', 'var(--warn)'];
  return { active, u5, el5, wok, uw, elw, pace, state, over: active && diff > 5 && u5 < 100 };
}
function forecast(s, v, t) {
  if (!v.active) return { txt: '첫 메시지부터 5시간 시작' };
  if (v.u5 >= 100) return { txt: `한도 도달 · ${hm(s.r5)} 풀림`, warn: true };
  const rate = burnRate(s, t);
  if (rate == null) return { txt: '속도 측정 중' };
  if (rate === 0) return { txt: '90분간 사용 없음', rate };
  const eta = t + (100 - v.u5) / rate * 3600;
  if (eta < s.r5) return { txt: `${hm(eta)} 소진 예상 · ${durShort(s.r5 - eta)} 공백`, warn: true, rate, eta };
  return { txt: `리셋 때 약 ${Math.min(100, v.u5 + rate * (s.r5 - t) / 3600).toFixed(0)}%`, rate };
}
// 주간 페이스: 지난 시간 비율만큼 썼으면 0, 더 썼으면 +(앞섬). 이 속도면 언제 바닥나는지도.
function weekly(s, v, t) {
  if (!v.wok) return { txt: '주간 창 정보 없음' };
  const left = s.rw - t, el = WEEK - left, days = Math.max(left / 86400, 0.01), budget = (100 - v.uw) / days;
  const diff = v.uw - v.elw * 100, d = Math.round(diff);
  const pace = Math.abs(diff) < 3 ? '주간 페이스 적정' : diff > 0 ? `주간 +${d}%p 앞섬` : `주간 ${d}%p 여유`;
  // 첫 12시간은 표본이 적어 소진 예측이 과장된다
  const eta = el > 43200 && v.uw > 0 ? t + (100 - v.uw) / (v.uw / el) : null;
  const td = today(s, v, t);
  const detail = td ? `${pace} · ${td.txt}` : `${pace} · 하루 ${budget.toFixed(1)}%p 이내 권장`;
  const tip = `이번 주 평균 속도 기준 · ${dayLabel(s.rw)} 리셋 (${durShort(left)} 남음) · 남은 ${days.toFixed(1)}일 동안 하루 ${budget.toFixed(1)}%p 이내 권장`;
  if (v.uw >= 100) return { txt: `주간 한도 도달 · ${dayLabel(s.rw)} 풀림`, detail: `${durShort(left)} 후 리셋`, warn: true, tip };
  if (eta && eta < s.rw) return { txt: `주간 ${dayLabel(eta)} 소진 예상`, detail: `${durShort(eta - t)} 후 · ${detail}`, warn: true, tip, eta };
  if (el <= 43200) return { txt: '주간 소진 예측 준비 중 · 시작 12시간 후 표시', detail, tip };
  const projected = v.uw / el * WEEK;
  return { txt: `주간 리셋까지 여유 · 리셋 때 약 ${projected.toFixed(0)}% 예상`, detail, tip };
}
// 오늘 쓸 몫: 하루 기준 시각(day0 = 그때의 주간 사용률)에 남은 주간 한도를 리셋까지 남은 날수로 나눈다.
// 하루 동안 고정이라 쓰는 만큼만 줄어든다. 리셋이 내일 기준 시각 전이면 남은 한도 전부가 오늘 몫
let DAY_HOUR = 6;
function today(s, v, t) {
  if (!v.wok || typeof s.day0 !== 'number') return null;
  const end = dayEnd(t, DAY_HOUR), start = end - 86400, base = clamp(s.day0, 0, 100);
  const allow = (100 - base) / Math.max(1, (s.rw - start) / 86400), used = Math.max(0, v.uw - base), left = allow - used;
  const txt = left >= 0.05 ? `오늘 ${left.toFixed(1)}%p 남음` : left > -0.05 ? '오늘 몫 다 씀' : `오늘 몫 ${(-left).toFixed(1)}%p 넘김`;
  const tip = `오늘 쓸 몫 ${allow.toFixed(1)}%p 중 ${used.toFixed(1)}%p 사용 · ${hm(end)}에 새로 계산`;
  return { allow, used, left, end, txt, tip, over: left <= -0.05 };
}
function lvl(p) {
  if (p <= 55) return 'var(--ok)';
  if (p < 75) return `color-mix(in oklch, var(--warn) ${((p - 55) / 20 * 100).toFixed(1)}%, var(--ok))`;
  if (p < 90) return `color-mix(in oklch, var(--crit) ${((p - 75) / 15 * 100).toFixed(1)}%, var(--warn))`;
  return 'var(--crit)';
}

// 숫자 롤링
class Ticker {
  constructor(el) { this.el = el; this.cur = null; el.classList.add('tk'); }
  set(str) {
    if (str === this.cur) return;
    const isD = c => c >= '0' && c <= '9';
    if (this.cur == null || str.length !== this.cur.length || [...str].some((c, i) => isD(c) !== isD(this.cur[i]))) {
      this.el.innerHTML = [...str].map(c => isD(c) ? `<span class="col">${[...'0123456789'].map(d => `<span>${d}</span>`).join('')}</span>` : `<span class="ch">${c === ' ' ? '&nbsp;' : escapeHtml(c)}</span>`).join('');
      this.cols = [...this.el.children];
      void this.el.offsetWidth;
    }
    [...str].forEach((c, i) => { if (isD(c)) { const col = this.cols[i]; col.style.transitionDelay = (str.length - i) * 25 + 'ms'; col.style.transform = `translateY(${-c}em)`; } });
    this.el.setAttribute('aria-label', str);
    this.cur = str;
  }
}

// 스프링 — 움직이는 동안만 requestAnimationFrame 이 돈다
const moving = new Set(); let raf = 0, lastTs = 0;
class Spring {
  constructor(x, cb) { this.x = x; this.t = x; this.v = 0; this.cb = cb; cb(x); }
  to(t) {
    if (t === this.t && !moving.has(this)) return;
    this.t = t;
    if (REDUCE) { this.x = t; this.v = 0; this.cb(t); return; }
    moving.add(this); if (!raf) raf = requestAnimationFrame(loop);
  }
  step(dt) {
    const f = -170 * (this.x - this.t) - 21 * this.v; this.v += f * dt; this.x += this.v * dt;
    if (Math.abs(this.x - this.t) < 0.02 && Math.abs(this.v) < 0.02) { this.x = this.t; this.v = 0; moving.delete(this); }
    this.cb(this.x);
  }
}
function loop(ts) { const dt = lastTs ? Math.min(0.032, (ts - lastTs) / 1000) : 0.016; lastTs = ts; moving.forEach(s => s.step(dt)); if (moving.size) raf = requestAnimationFrame(loop); else { raf = 0; lastTs = 0; } }
