// Week view: 30-minute time axis, absolute event blocks, overlap layout.

import {
  WEEKDAYS_SHORT,
  escapeHtml,
  istMinutesNow,
  minutesOfDay,
  parseISO,
  shortName,
  shade,
  tint,
  timeHHMM,
} from "../util.js";

const ROW_PX = 44; // pixels per 30 minutes
const PX_PER_MIN = ROW_PX / 30;

function layoutEvents(events) {
  const enriched = events
    .map((event) => ({
      event,
      start: minutesOfDay(timeHHMM(event.start)),
      end: minutesOfDay(timeHHMM(event.end)),
    }))
    .sort((a, b) => a.start - b.start || a.end - b.end);

  const placed = [];
  let cluster = [];
  let clusterEnd = -1;

  const flush = () => {
    const columns = [];
    const batch = [];
    for (const item of cluster) {
      let index = columns.findIndex((end) => end <= item.start);
      if (index === -1) {
        index = columns.length;
        columns.push(item.end);
      } else {
        columns[index] = item.end;
      }
      batch.push({ ...item, column: index });
    }
    const total = Math.max(1, columns.length);
    batch.forEach((item) => placed.push({ ...item, total }));
    cluster = [];
    clusterEnd = -1;
  };

  for (const item of enriched) {
    if (cluster.length && item.start >= clusterEnd) flush();
    cluster.push(item);
    clusterEnd = Math.max(clusterEnd, item.end);
  }
  if (cluster.length) flush();

  return placed;
}

function eventHtml(item, ctx, doctorsById) {
  const { event, start, end, column, total } = item;
  const doctor = doctorsById.get(event.doctor_id);
  const color = doctor?.color || "#1a73e8";
  const classes = ["event"];
  if (event.emergency) classes.push("emergency");
  if (!event.is_active) classes.push("cancelled");

  const width = 100 / total;
  const style = [
    `top:${(start - ctx.axisStart) * PX_PER_MIN}px`,
    `height:${Math.max(18, (end - start) * PX_PER_MIN - 2)}px`,
    `left:calc(${column * width}% + 1px)`,
    `width:calc(${width}% - 3px)`,
    `background:${tint(color)}`,
    `border-left-color:${color}`,
    `color:${shade(color)}`,
  ].join(";");

  const subtitle = ctx.isDoctor
    ? event.reason || doctor?.specialization || ""
    : `${shortName(doctor?.full_name || event.doctor_name)}${
        event.token_number ? ` · #${event.token_number}` : ""
      }`;

  return `<div class="${classes.join(" ")}" style="${style}"
            data-event-id="${escapeHtml(event.id)}"
            title="${escapeHtml(event.patient_name || "Booked")}">
    <div class="ev-time">${timeHHMM(event.start)}</div>
    <div class="ev-title">${escapeHtml(shortName(event.patient_name))}</div>
    <div class="ev-sub">${escapeHtml(subtitle)}</div>
  </div>`;
}

export function renderWeek(root, ctx) {
  const { data, meta } = ctx;
  const days = data.days;
  const sessions = meta.sessions || [];
  const axisStart = minutesOfDay(sessions[0]?.start || "09:00");
  const axisEnd = minutesOfDay(sessions[sessions.length - 1]?.end || "17:00");
  const height = (axisEnd - axisStart) * PX_PER_MIN;
  const doctorsById = new Map(meta.doctors.map((d) => [d.id, d]));
  const nowMinutes = istMinutesNow();

  ctx.axisStart = axisStart;

  // ---- header ----------------------------------------------------------
  const dayHeads = days.map((day) => {
    const date = parseISO(day.date);
    const classes = ["week-day-head"];
    if (day.is_today) classes.push("today");
    const commonLeave = day.holiday?.scope === "CLINIC";
    if (day.is_holiday) classes.push(commonLeave ? "common-leave" : "holiday");
    const index = (date.getDay() + 6) % 7;
    return `<div class="${classes.join(" ")}">
        <div class="dow">${WEEKDAYS_SHORT[index]}</div>
        <div class="dnum">${date.getDate()}</div>
        ${day.holiday ? `<span class="hname" title="${escapeHtml(day.holiday.name)}">${escapeHtml(day.holiday.name)}</span>` : ""}
      </div>`;
  }).join("");

  // ---- time gutter -----------------------------------------------------
  const gutter = [];
  for (let minute = axisStart; minute <= axisEnd; minute += 60) {
    const top = (minute - axisStart) * PX_PER_MIN;
    const label = minute === axisStart ? "" : formatHour(minute);
    gutter.push(`<span class="hour-label" style="top:${top}px">${label}</span>`);
  }

  // ---- columns ---------------------------------------------------------
  const columns = days.map((day) => {
    const lines = [];
    for (let minute = axisStart; minute <= axisEnd; minute += 30) {
      const top = (minute - axisStart) * PX_PER_MIN;
      lines.push(
        `<div class="hour-line ${minute % 60 ? "half" : ""}" style="top:${top}px"></div>`
      );
    }

    const hits = [];
    for (const session of sessions) {
      const sessionStart = minutesOfDay(session.start);
      const sessionEnd = minutesOfDay(session.end);
      for (let minute = sessionStart; minute < sessionEnd; minute += 30) {
        hits.push(
          `<div class="slot-hit" style="top:${(minute - axisStart) * PX_PER_MIN}px;height:${ROW_PX}px"
                data-date="${day.date}" data-time="${hhmm(minute)}"></div>`
        );
      }
    }

    const events = layoutEvents(
      day.events.filter((event) => event.is_active)
    ).map((item) => eventHtml(item, ctx, doctorsById)).join("");

    const nowLine =
      day.is_today && nowMinutes >= axisStart && nowMinutes <= axisEnd
        ? `<div class="now-line" style="top:${(nowMinutes - axisStart) * PX_PER_MIN}px"></div>`
        : "";

    const classes = ["day-col"];
    if (day.is_today) classes.push("today");
    const commonLeave = day.holiday?.scope === "CLINIC";
    if (day.is_holiday) classes.push(commonLeave ? "common-leave" : "holiday");

    return `<div class="${classes.join(" ")}" style="height:${height}px">
        ${lines.join("")}
        ${hits.join("")}
        ${events}
        ${nowLine}
      </div>`;
  }).join("");

  root.innerHTML = `
    <div class="week">
      <div class="week-head">
        <div class="corner"></div>
        ${dayHeads}
      </div>
      <div class="week-scroll">
        <div class="week-body" style="height:${height}px">
          <div class="time-gutter" style="height:${height}px">${gutter.join("")}</div>
          ${columns}
        </div>
      </div>
    </div>`;

  root.querySelectorAll(".event[data-event-id]").forEach((node) => {
    node.addEventListener("click", (event) => {
      event.stopPropagation();
      const id = node.dataset.eventId;
      const found = days
        .flatMap((day) => day.events)
        .find((item) => String(item.id) === String(id));
      if (found) ctx.openEvent(found, node);
    });
  });

  root.querySelectorAll(".slot-hit").forEach((node) => {
    node.addEventListener("click", () => {
      if (!ctx.canBook) return;
      ctx.openBooking({ date: node.dataset.date, time: node.dataset.time });
    });
  });

  // Scroll to the first session so the morning is visible immediately.
  const scroller = root.querySelector(".week-scroll");
  if (scroller) scroller.scrollTop = 0;
}

function formatHour(minutes) {
  const hour = Math.floor(minutes / 60);
  const suffix = hour >= 12 ? "PM" : "AM";
  const display = hour % 12 === 0 ? 12 : hour % 12;
  return `${display} ${suffix}`;
}

function hhmm(minutes) {
  const h = String(Math.floor(minutes / 60)).padStart(2, "0");
  const m = String(minutes % 60).padStart(2, "0");
  return `${h}:${m}`;
}
