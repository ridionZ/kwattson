// en-AU formatting in the *site* time zone (from /api/status), not the browser's.

const LOCALE = 'en-AU';
let zone = 'UTC';
const cache = new Map<string, Intl.DateTimeFormat>();

export function setZone(tz?: string | null) {
  if (tz && tz !== zone) {
    zone = tz;
    cache.clear();
  }
}
export const getZone = () => zone;

function dtf(key: string, options: Intl.DateTimeFormatOptions) {
  let f = cache.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat(LOCALE, { timeZone: zone, ...options });
    cache.set(key, f);
  }
  return f;
}

type DateLike = string | number | Date | null | undefined;
const toDate = (v: DateLike) => (v instanceof Date ? v : new Date(typeof v === 'number' ? v : String(v)));
const ok = (v: DateLike) => v !== null && v !== undefined && v !== '' && !Number.isNaN(toDate(v).getTime());

/** 3:05 pm */
export const time = (v: DateLike) => (ok(v) ? dtf('t', { hour: 'numeric', minute: '2-digit' }).format(toDate(v)).replace(/\s?([ap])m/i, ' $1m') : '–');
/** Tue 17 Nov, 3:05 pm */
export const dateTime = (v: DateLike) =>
  ok(v) ? dtf('dt', { weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' }).format(toDate(v)) : '–';
export const day = (v: DateLike) => (ok(v) ? dtf('d', { weekday: 'short', day: 'numeric', month: 'short' }).format(toDate(v)) : '–');
export const date = (v: DateLike) => (ok(v) ? dtf('date', { day: 'numeric', month: 'short', year: 'numeric' }).format(toDate(v)) : '–');
export const hourTick = (v: DateLike) => dtf('h', { hour: 'numeric' }).format(toDate(v)).replace(/\s?([ap])m/i, ' $1m');

export function parts(v: DateLike) {
  const out: Record<string, string> = {};
  for (const p of dtf('parts', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', weekday: 'short' }).formatToParts(toDate(v)))
    out[p.type] = p.value;
  return out;
}
/** Site-local calendar day key "2026-11-17". */
export const dayKey = (v: DateLike) => {
  const p = parts(v);
  return `${p.year}-${p.month}-${p.day}`;
};
/** Instant → "YYYY-MM-DDTHH:MM" for datetime-local inputs (site wall clock; server treats naive as site-local). */
export function localInput(v: DateLike) {
  if (!ok(v)) return '';
  const p = parts(v);
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

/** "Now" / "6:00 am" / "6:00 am tomorrow" / "Thu 6:00 am" relative to now in the site zone. */
export function when(v: DateLike, now: DateLike = Date.now(), isNow = false) {
  if (!ok(v)) return '–';
  if (isNow) return 'Now';
  const d = toDate(v).getTime();
  const k = dayKey(v);
  const today = dayKey(now);
  const tomorrow = dayKey(toDate(now).getTime() + 86400e3);
  const yesterday = dayKey(toDate(now).getTime() - 86400e3);
  if (k === today) return time(d);
  if (k === tomorrow) return `${time(d)} tomorrow`;
  if (k === yesterday) return `${time(d)} yesterday`;
  return `${dtf('wd', { weekday: 'short' }).format(toDate(v))} ${time(d)}`;
}
/** Day label for chart boundaries. */
export function dayLabel(v: DateLike, now: DateLike = Date.now()) {
  const k = dayKey(v);
  if (k === dayKey(now)) return 'Today';
  if (k === dayKey(toDate(now).getTime() + 86400e3)) return 'Tomorrow';
  return dtf('wdl', { weekday: 'long' }).format(toDate(v));
}

const rtf = new Intl.RelativeTimeFormat(LOCALE, { numeric: 'auto' });
export function relative(v: DateLike, now = Date.now()) {
  if (!ok(v)) return '–';
  const sec = (toDate(v).getTime() - now) / 1000;
  const abs = Math.abs(sec);
  if (abs < 45) return sec < 0 ? 'just now' : 'in a moment';
  if (abs < 2700) return rtf.format(Math.round(sec / 60), 'minute');
  if (abs < 129600) return rtf.format(Math.round(sec / 3600), 'hour');
  return rtf.format(Math.round(sec / 86400), 'day');
}
export const ageMinutes = (v: DateLike, now = Date.now()) => (ok(v) ? (now - toDate(v).getTime()) / 60000 : Infinity);

const nf = new Map<string, Intl.NumberFormat>();
function num(digits: number, v: number, opts: Intl.NumberFormatOptions = {}) {
  const key = `${digits}|${JSON.stringify(opts)}`;
  let f = nf.get(key);
  if (!f) {
    f = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: digits, maximumFractionDigits: digits, ...opts });
    nf.set(key, f);
  }
  return f.format(v);
}
export const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
export const n = (v: unknown, digits = 1) => (isNum(v) ? num(digits, v) : '–');
export const kw = (v: unknown, digits = 1) => (isNum(v) ? `${num(digits, v)} kW` : '–');
export const kwh = (v: unknown, digits = 1) => (isNum(v) ? `${num(digits, v)} kWh` : '–');
export const pct = (v: unknown, digits = 0) => (isNum(v) ? `${num(digits, v)}%` : '–');
export const money = (v: unknown) => (isNum(v) ? num(2, Math.abs(v) < 0.005 ? 0 : v, { style: 'currency', currency: 'AUD' }) : '–');
/** AUD/kWh → "23.4c" */
export const cents = (rate: unknown, digits = 1) => (isNum(rate) ? `${num(digits, rate * 100)}c` : '–');
export function better(v: unknown) {
  if (!isNum(v)) return '–';
  if (Math.abs(v) < 0.005) return 'About the same';
  return `${money(Math.abs(v))} ${v > 0 ? 'better' : 'worse'}`;
}
export function duration(minutes: number) {
  if (minutes < 60) return `${minutes} min`;
  const h = minutes / 60;
  if (h < 48) return `${n(h, h % 1 ? 1 : 0)} h`;
  return `${n(h / 24, 0)} days`;
}
export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
