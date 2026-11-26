// Month view: 7 x 6 grid with chips, holidays and free-slot badges.

import {
  MONTHS,
  WEEKDAYS_SHORT,
  escapeHtml,
  parseISO,
  shortName,
  shade,
  tint,
  timeHHMM,
} from "../util.js";

const MAX_CHIPS = 3;

export function renderMonth(root, ctx) {
  const { data, meta, state } = ctx;
  const anchor = parseISO(state.date);
  const anchorMonth = anchor.getMonth();
  const days = data.days;
  const doctorsById = new Map(meta.doctors.map((d) => [d.id, d]));
  const eventIndex = new Map();
  days.forEach((day) => day.events.forEach((e) => eventIndex.set(e.id, e)));

  const head = WEEKDAYS_SHORT.map((name) => `<div>${name}</div>`).join("");

  const cells = days.map((day) => {
    const date = parseISO(day.date);
    const out = date.getMonth() !== anchorMonth;
    const weekend = date.getDay() === 0 || date.getDay() === 6;
    const classes = ["day-cell"];
    if (out) classes.push("out");
    if (weekend) classes.push("weekend");
    if (day.is_today) classes.push("today");
    const commonLeave = day.holiday?.scope === "CLINIC";
    if (day.is_holiday) classes.push(commonLeave ? "common-leave" : "holiday");

    const holidayTag = day.holiday
      ? `<span class="holiday-tag${commonLeave ? " common-leave" : ""}" title="${escapeHtml(day.holiday.name)}">${escapeHtml(day.holiday.name)}</span>`
      : "";

    const active = day.events
      .filter((event) => event.is_active)
      .sort((a, b) => a.start.localeCompare(b.start));
    if (active.length) classes.push("has-events");
    const shown = active.slice(0, MAX_CHIPS);
    let body = shown
      .map((event) => {
        const doctor = doctorsById.get(event.doctor_id);
        const color = doctor?.color || "#1a73e8";
        const classes = ["chip"];
        if (event.emergency) classes.push("emergency");
        const title = `${timeHHMM(event.start)} ${shortName(event.patient_name)}`;
        return `<span class="${classes.join(" ")}" data-event-id="${escapeHtml(event.id)}"
                  style="background:${tint(color)};border-left-color:${color};color:${shade(color)}"
                  title="${escapeHtml(title)}">
                  <span class="chip-time">${timeHHMM(event.start)}</span>
                  <span class="chip-text">${escapeHtml(shortName(event.patient_name))}</span>
                </span>`;
      })
      .join("");

    const remaining = active.length - shown.length;
    if (remaining > 0) {
      body += `<button class="more-btn" data-day="${day.date}">+${remaining} more</button>`;
    }

    return `<div class="${classes.join(" ")}" data-date="${day.date}" data-out="${out ? 1 : 0}"
         data-count="${active.length}">
        <div class="day-head">
          <span class="daynum">${date.getDate()}</span>
          ${holidayTag}
        </div>
        ${body}
      </div>`;
  }).join("");

  root.innerHTML = `
    <div class="month">
      <div class="month-head">${head}</div>
      <div class="month-grid">${cells}</div>
    </div>`;

  root.querySelectorAll(".chip[data-event-id]").forEach((chip) => {
    chip.addEventListener("click", (event) => {
      event.stopPropagation();
      const found = eventIndex.get(chip.dataset.eventId);
      if (found) ctx.openEvent(found, chip);
    });
  });

  root.querySelectorAll(".more-btn").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      ctx.goToDay(button.dataset.day);
    });
  });

  root.querySelectorAll(".day-cell").forEach((cell) => {
    cell.addEventListener("click", () => {
      const out = cell.dataset.out === "1";
      // Phones show a compact month: tap a day to open it, not a full-screen form.
      if (out || window.matchMedia("(max-width: 700px)").matches) {
        ctx.goToDay(cell.dataset.date);
      } else {
        ctx.openBooking({ date: cell.dataset.date });
      }
    });
  });

  const label = `${MONTHS[anchorMonth]} ${anchor.getFullYear()}`;
  root.closest(".view")?.setAttribute("data-label", label);
}
