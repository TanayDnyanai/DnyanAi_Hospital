// Day view: doctor columns x 30-minute slot rows (the "resource" view).

import {
  escapeHtml,
  labelFromHHMM,
  shade,
  shortName,
  tint,
} from "../util.js";

export function renderDay(root, ctx) {
  const { grid } = ctx;
  if (!grid || !grid.doctors || !grid.doctors.length) {
    root.innerHTML = `<div class="empty-state">No doctors scheduled for this day.</div>`;
    return;
  }

  const doctors = grid.doctors;
  const times = [
    ...new Set(doctors.flatMap((block) => block.slots.map((slot) => slot.time))),
  ].sort();

  const colWidth = doctors.length <= 3 ? "260px" : "minmax(154px, 1fr)";
  const columns = `56px repeat(${doctors.length}, ${colWidth})`;

  const headCells = doctors.map((block) => {
    const doctor = block.doctor;
    const holiday = block.holiday;
    const doctorLeave = holiday?.scope === "DOCTOR" && holiday.type === "LEAVE";
    const commonLeave = holiday?.scope === "CLINIC";
    const sub = doctorLeave
      ? `<span class="rh-leave">On leave</span>
         <span class="rh-leave-reason" title="${escapeHtml(holiday.name)}">${escapeHtml(holiday.name)}</span>`
      : commonLeave
      ? `<span class="rh-common-leave">Common leave</span>
         <span class="rh-leave-reason" title="${escapeHtml(holiday.name)}">${escapeHtml(holiday.name)}</span>`
      : `${escapeHtml(doctor.specialization)}${holiday ? ` · ${escapeHtml(holiday.name)}` : ""}`;
    const headClass = doctorLeave ? " on-leave" : commonLeave ? " on-common-leave" : "";
    return `<div class="res-head-cell${headClass}">
        <div class="rh-bar" style="background:${doctor.color}"></div>
        <span class="rh-name" title="${escapeHtml(doctor.full_name)}">${escapeHtml(doctor.full_name)}</span>
        <span class="rh-sub">${sub}</span>
      </div>`;
  }).join("");

  const cells = [];
  times.forEach((time, index) => {
    const showLabel = index > 0 && time.endsWith(":00");
    cells.push(
      `<div class="res-time"><span>${showLabel ? labelFromHHMM(time) : ""}</span></div>`
    );
    for (const block of doctors) {
      const slot = block.slots.find((item) => item.time === time);
      if (!slot) {
        cells.push(`<div class="res-cell" style="background:#fcfcfd"></div>`);
        continue;
      }
      if (slot.status === "HOLIDAY") {
        const isLeave =
          block.holiday?.scope === "DOCTOR" && block.holiday.type === "LEAVE";
        const isCommonLeave = block.holiday?.scope === "CLINIC";
        const holidayClass = isLeave
          ? " leave"
          : isCommonLeave
          ? " common-leave"
          : "";
        cells.push(`<div class="res-cell holiday${holidayClass}"></div>`);
        continue;
      }
      if (slot.status === "BOOKED" && slot.appointment) {
        const appointment = slot.appointment;
        const color = block.doctor.color;
        const classes = ["res-event"];
        if (appointment.emergency) classes.push("emergency");
        if (!appointment.is_active) classes.push("cancelled");
        const style = `background:${tint(color)};border-left-color:${color};color:${shade(color)}`;

        const inner = `<span class="re-name">${escapeHtml(shortName(appointment.patient_name))}</span>
             <span class="re-sub">${escapeHtml(appointment.reason || "")}${appointment.token_number ? ` · #${appointment.token_number}` : ""}</span>`;

        cells.push(`<div class="res-cell booked">
            <div class="${classes.join(" ")}" style="${style}" data-event-id="${escapeHtml(appointment.id || "")}">
              ${inner}
            </div>
          </div>`);
        continue;
      }
      cells.push(`<div class="res-cell free" data-date="${grid.date}" data-time="${slot.time}"
                       data-doctor="${escapeHtml(block.doctor.id)}"
                       title="Free — click to book">
          <span class="res-free">${ctx.isPatient ? "Free" : "Available"}</span>
        </div>`);
    }
  });

  root.innerHTML = `
    <div class="resource">
      <div class="resource-scroll">
        <div class="resource-head" style="grid-template-columns:${columns}">
          <div class="res-head-cell"></div>
          ${headCells}
        </div>
        <div class="resource-body" style="grid-template-columns:${columns}">
          ${cells.join("")}
        </div>
      </div>
    </div>`;

  root.querySelectorAll(".res-event[data-event-id]").forEach((node) => {
    node.addEventListener("click", (event) => {
      event.stopPropagation();
      const id = String(node.dataset.eventId);
      for (const block of doctors) {
        for (const slot of block.slots) {
          if (slot.appointment && String(slot.appointment.id) === id) {
            ctx.openEvent(slot.appointment, node);
            return;
          }
        }
      }
    });
  });

  root.querySelectorAll(".res-cell.free").forEach((node) => {
    node.addEventListener("click", () => {
      if (!ctx.canBook) return;
      ctx.openBooking({
        date: node.dataset.date,
        time: node.dataset.time,
        doctorId: node.dataset.doctor,
      });
    });
  });
}
