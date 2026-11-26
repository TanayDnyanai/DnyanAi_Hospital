// Small, dependency-free helpers shared by every view.

export const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
export const MONTHS_SHORT = MONTHS.map((m) => m.slice(0, 3));
export const WEEKDAYS_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
export const WEEKDAYS_LONG = [
  "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
];

export const pad = (n) => String(n).padStart(2, "0");

export function iso(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function parseISO(value) {
  const [y, m, d] = String(value).split("-").map(Number);
  return new Date(y, m - 1, d);
}

export function addDays(date, days) {
  const copy = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  copy.setDate(copy.getDate() + days);
  return copy;
}

export function addMonths(date, months) {
  return new Date(date.getFullYear(), date.getMonth() + months, 1);
}

/** Monday-first week start. */
export function startOfWeek(date) {
  const offset = (date.getDay() + 6) % 7;
  return addDays(date, -offset);
}

export function startOfMonth(date) {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

export function monthGridDates(anchor) {
  const start = startOfWeek(startOfMonth(anchor));
  return Array.from({ length: 42 }, (_, i) => addDays(start, i));
}

export function weekDates(anchor) {
  const start = startOfWeek(anchor);
  return Array.from({ length: 7 }, (_, i) => addDays(start, i));
}

export function fmtRange(view, anchor) {
  if (view === "month") {
    return `${MONTHS[anchor.getMonth()]} ${anchor.getFullYear()}`;
  }
  if (view === "week") {
    const start = startOfWeek(anchor);
    const end = addDays(start, 6);
    const sameMonth = start.getMonth() === end.getMonth();
    if (sameMonth) {
      return `${MONTHS_SHORT[start.getMonth()]} ${start.getDate()} – ${end.getDate()}, ${end.getFullYear()}`;
    }
    const sameYear = start.getFullYear() === end.getFullYear();
    const left = `${MONTHS_SHORT[start.getMonth()]} ${start.getDate()}`;
    const right = sameYear
      ? `${MONTHS_SHORT[end.getMonth()]} ${end.getDate()}, ${end.getFullYear()}`
      : `${MONTHS_SHORT[end.getMonth()]} ${end.getDate()}, ${end.getFullYear()}`;
    return `${left} – ${right}`;
  }
  return `${WEEKDAYS_LONG[(anchor.getDay() + 6) % 7]}, ${anchor.getDate()} ${MONTHS[anchor.getMonth()]} ${anchor.getFullYear()}`;
}

export function timeHHMM(isoString) {
  return String(isoString || "").slice(11, 16);
}

export function labelFromHHMM(hhmm) {
  const [h, m] = String(hhmm).split(":").map(Number);
  const suffix = h >= 12 ? "PM" : "AM";
  const hour = h % 12 === 0 ? 12 : h % 12;
  return `${hour}:${pad(m)} ${suffix}`;
}

export function minutesOfDay(hhmm) {
  const [h, m] = String(hhmm).split(":").map(Number);
  return h * 60 + m;
}

/** Minutes since midnight in IST, regardless of the browser timezone. */
export function istMinutesNow() {
  const now = new Date();
  return (now.getUTCHours() * 60 + now.getUTCMinutes() + 330) % 1440;
}

export function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function hexToRgb(hex) {
  const clean = String(hex || "#1a73e8").replace("#", "");
  return {
    r: parseInt(clean.slice(0, 2), 16) || 26,
    g: parseInt(clean.slice(2, 4), 16) || 115,
    b: parseInt(clean.slice(4, 6), 16) || 232,
  };
}

/** Very light background version of a doctor colour. */
export function tint(hex, keep = 0.14) {
  const { r, g, b } = hexToRgb(hex);
  const mix = (c) => Math.round(255 - (255 - c) * keep);
  return `rgb(${mix(r)}, ${mix(g)}, ${mix(b)})`;
}

/** Darker text version of a doctor colour. */
export function shade(hex, amount = 0.28) {
  const { r, g, b } = hexToRgb(hex);
  const mix = (c) => Math.round(c * (1 - amount));
  return `rgb(${mix(r)}, ${mix(g)}, ${mix(b)})`;
}

export function shortName(name) {
  const clean = String(name || "").replace(/^Dr\.?\s+/i, "");
  const parts = clean.split(/\s+/).filter(Boolean);
  if (parts.length <= 1) return clean || "—";
  return `${parts[0]} ${parts[parts.length - 1][0]}.`;
}

export function statusLabel(status) {
  return String(status || "")
    .replace(/_/g, " ")
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function debounce(fn, wait = 200) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}
