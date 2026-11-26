// Application shell: state, navigation, sidebar, modals and popovers.

import { api, setAuth } from "./api.js";
import * as u from "./util.js";
import { renderMonth } from "./views/month.js";
import { renderWeek } from "./views/week.js";
import { renderDay } from "./views/day.js";

const $ = (id) => document.getElementById(id);

const STORAGE = {
  role: "dnyanai.calendar.role",
  phone: "dnyanai.patient.phone",
  doctorSearch: "dnyanai.calendar.doctorSearchHistory",
};

function loadRecentSearches() {
  try {
    const stored = localStorage.getItem(STORAGE.doctorSearch);
    const parsed = stored ? JSON.parse(stored) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveRecentSearch(term) {
  const value = String(term || "").trim();
  if (!value) return;
  let history = loadRecentSearches().filter((item) => item !== value);
  history.unshift(value);
  history = history.slice(0, 8);
  localStorage.setItem(STORAGE.doctorSearch, JSON.stringify(history));
  state.searchHistory = history;
}

const state = {
  view: "week",
  date: null,
  role: "receptionist",
  doctorId: null,
  meta: null,
  data: null,
  stats: null,
  grid: null,
  searchValue: "",
  specializationFilter: "All",
  searchHistory: [],
  myLeaves: [],
};

let toastTimer = null;
let leaveModalOpen = false;

const canManageAppointments = () =>
  state.role === "receptionist" || state.role === "admin" || state.role === "patient";

// Patients and receptionists share the same appointment-management tasks.
const canEdit = canManageAppointments;
const canBook = () =>
  state.role === "patient"
    ? Boolean(state.meta?.flags?.allow_patient_booking)
    : canEdit();

/* ============================================================
   Bootstrap
   ============================================================ */

async function init() {
  try {
    state.meta = await api.meta();
  } catch (error) {
    showFatal(`Could not reach the calendar API: ${error.message}`);
    return;
  }

  const params = new URLSearchParams(location.search);
  const view = params.get("view");
  state.view = ["month", "week", "day"].includes(view) ? view : "week";
  state.date = params.get("date") || state.meta.today;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(state.date)) state.date = state.meta.today;
  state.embedded = params.get("embed") === "1";
  if (state.embedded) document.body.classList.add("embed-mode");

  state.searchHistory = loadRecentSearches();
  state.searchValue = "";
  state.specializationFilter = "All";

  const role = params.get("role") || localStorage.getItem(STORAGE.role);
  state.role = state.meta.roles.includes(role) ? role : "receptionist";
  state.doctorId = params.get("doctor") || null;

  if (state.role === "doctor" && !state.doctorId) {
    state.doctorId = state.meta.doctors[0]?.id || null;
  }

  buildDoctorSelect();
  syncControls();
  bindGlobalEvents();
  await refresh();
}

function showFatal(message) {
  $("view").innerHTML = `<div class="empty-state">${u.escapeHtml(message)}</div>`;
}

/* ============================================================
   Data + rendering
   ============================================================ */

function rangeFor() {
  const anchor = u.parseISO(state.date);
  if (state.view === "month") {
    const start = u.startOfWeek(u.startOfMonth(anchor));
    return { from: u.iso(start), to: u.iso(u.addDays(start, 41)) };
  }
  if (state.view === "week") {
    const start = u.startOfWeek(anchor);
    return { from: u.iso(start), to: u.iso(u.addDays(start, 6)) };
  }
  return { from: state.date, to: state.date };
}

async function refresh() {
  const { from, to } = rangeFor();
  const doctorParam = state.doctorId || undefined;

  setAuth({
    role: state.role,
    doctorId: state.role === "doctor" ? state.doctorId : null,
  });

  $("view").innerHTML = `<div class="loading"><div class="spinner"></div></div>`;

  try {
    const leaveEnd = u.iso(u.addDays(u.parseISO(state.meta.today), 365));
    const leaveRequest =
      state.role === "doctor" && typeof api.holidays === "function"
        ? api.holidays(state.meta.today, leaveEnd)
        : Promise.resolve(null);
    // Fetch everything in parallel. Each Neon round trip costs ~70ms, so a
    // sequential day-grid request added a full extra wait on every refresh.
    const [data, stats, leaveData, grid] = await Promise.all([
      api.calendar(from, to, doctorParam ? { doctor_id: doctorParam } : {}),
      api.stats(from, to, doctorParam),
      leaveRequest,
      state.view === "day"
        ? api.dayGrid(state.date, doctorParam)
        : Promise.resolve(null),
    ]);
    state.data = data;
    state.stats = stats;
    state.grid = grid;
    state.myLeaves =
      state.role === "doctor"
        ? (leaveData?.holidays || [])
            .filter(
              (holiday) =>
                holiday.scope === "DOCTOR" &&
                holiday.type === "LEAVE" &&
                holiday.doctor_id === state.doctorId
            )
            .sort((a, b) => a.date.localeCompare(b.date))
        : [];
    writeUrl();
    render();
    if (!state.readySent) {
      state.readySent = true;
      notifyParent("ready", {
        role: state.role,
        view: state.view,
        date: state.date,
        today: state.meta.today,
      });
    }
    notifyParent("rendered", {
      role: state.role,
      view: state.view,
      date: state.date,
      appointments: state.stats?.total_appointments ?? 0,
    });
  } catch (error) {
    showFatal(error.message);
    toast(error.message, "error");
  }
}

function render() {
  $("rangeLabel").textContent = u.fmtRange(state.view, u.parseISO(state.date));
  renderStats();
  renderSidebar();
  renderView();
  syncControls();
}

function renderStats() {
  const stats = state.stats || {};
  const items = [];

  items.push(stat(stats.total_appointments ?? 0, "appointments", ""));
  items.push(stat(stats.doctors ?? 0, (stats.doctors ?? 0) === 1 ? "doctor" : "doctors", ""));
  items.push(stat(stats.free_slots ?? 0, "free slots", "green"));
  const occupancy = stats.occupancy ?? 0;
  const occupancyText =
    occupancy > 0 && occupancy < 0.005 ? "<1%" : `${Math.round(occupancy * 100)}%`;
  items.push(stat(occupancyText, "occupancy", "amber"));
  if (stats.emergency !== undefined) {
    items.push(stat(stats.emergency, "emergency", "red"));
  }

  const range = state.view === "day" ? "Selected day" : state.view === "week" ? "This week" : "This month";
  items.unshift(
    `<span class="stat" style="background:transparent;border:none;color:#5f6368;font-weight:600">${range}</span>`
  );

  $("statsStrip").innerHTML = items.join("");
}

function stat(value, label, dot) {
  return `<span class="stat"><span class="dot ${dot}"></span><strong>${u.escapeHtml(String(value))}</strong> ${u.escapeHtml(label)}</span>`;
}

function renderView() {
  const container = $("view");
  container.innerHTML = "";

  const ctx = {
    state,
    meta: state.meta,
    data: state.data,
    stats: state.stats,
    grid: state.grid,
    canEdit: canEdit(),
    canBook: canBook(),
    isPatient: state.role === "patient",
    isDoctor: state.role === "doctor",
    openEvent: openEventPopover,
    openBooking,
    goToDay,
    toast,
  };

  if (state.view === "month") renderMonth(container, ctx);
  else if (state.view === "week") renderWeek(container, ctx);
  else renderDay(container, ctx);
}

/* ============================================================
   Sidebar
   ============================================================ */

function renderSidebar() {
  const parts = [miniMonthHtml()];
  if (
    state.role === "receptionist" ||
    state.role === "admin" ||
    state.role === "patient"
  ) {
    parts.push(searchPanelHtml());
  }
  if (state.role === "doctor") parts.push(doctorLeavePanelHtml());
  if (state.role === "patient") parts.push(patientCardHtml());
  if (state.role === "admin") {
    parts.push(doctorAdminHtml());
    parts.push(holidayPanelHtml());
  }
  $("sidebar").innerHTML = parts.join("");
  bindSidebarEvents();
}

function miniMonthHtml() {
  const anchor = u.parseISO(state.date);
  const dates = u.monthGridDates(anchor);
  const counts = new Map(
    (state.data?.days || []).map((day) => [day.date, day.counts?.total ?? 0])
  );

  const cells = dates
    .map((date) => {
      const value = u.iso(date);
      const classes = ["mini-day"];
      if (date.getMonth() !== anchor.getMonth()) classes.push("out");
      if (value === state.meta.today) classes.push("today");
      if (value === state.date) classes.push("selected");
      if ((counts.get(value) || 0) > 0) classes.push("has-events");
      return `<button class="${classes.join(" ")}" data-date="${value}">${date.getDate()}</button>`;
    })
    .join("");

  return `<div>
      <div class="mini-month-head">
        <span>${u.MONTHS[anchor.getMonth()]} ${anchor.getFullYear()}</span>
        <div>
          <button class="icon-btn" data-mini-nav="-1" aria-label="Previous month">‹</button>
          <button class="icon-btn" data-mini-nav="1" aria-label="Next month">›</button>
        </div>
      </div>
      <div class="mini-grid">
        ${u.WEEKDAYS_SHORT.map((name) => `<div class="dow">${name[0]}</div>`).join("")}
        ${cells}
      </div>
    </div>`;
}

function searchContainerHtml() {
  const filterActive =
    state.specializationFilter && state.specializationFilter !== "All";
  return `<div class="sidebar-search-container">
      <div class="search-row">
        <input type="text" id="doctorSearch" class="doctor-search-input"
               placeholder="Search name or specialization"
               autocomplete="off"
               value="${u.escapeHtml(state.searchValue)}">
        <button id="specializationFilterBtn" type="button"
                class="filter-btn ${filterActive ? "active" : ""}"
                title="Filter by specialization"
                aria-label="Filter by specialization">
          <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" focusable="false">
            <path d="M3 5h18l-7 8v6l-4 2v-8L3 5z" fill="none"
                  stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
          </svg>
        </button>
      </div>
      <div class="doctor-search-dropdown" id="doctorSearchDropdown" hidden></div>
      <div class="specialization-menu" id="specializationMenu" hidden></div>
    </div>`;
}

function searchPanelHtml() {
  return `<div class="doctor-panel">
      <h3 class="section-title">Doctors</h3>
      ${searchContainerHtml()}
    </div>`;
}

function leaveRowHtml(leave) {
  const start = u.parseISO(leave.date);
  const end =
    leave.end_date && leave.end_date !== leave.date
      ? u.parseISO(leave.end_date)
      : null;
  const range = end
    ? ` – ${u.pad(end.getDate())} ${u.MONTHS_SHORT[end.getMonth()]}`
    : "";
  const weekday = u.WEEKDAYS_SHORT[(start.getDay() + 6) % 7];

  return `<div class="leave-card" title="${u.escapeHtml(
    `${leave.date}${leave.end_date ? ` to ${leave.end_date}` : ""} — ${leave.name}`
  )}">
      <div class="leave-date">
        <span class="leave-day">${u.pad(start.getDate())}</span>
        <span class="leave-month">${u.MONTHS_SHORT[start.getMonth()]}</span>
      </div>
      <div class="leave-info">
        <span class="leave-reason">${u.escapeHtml(leave.name)}</span>
        <span class="leave-meta"><span class="leave-dot"></span>${weekday}${range} · Confirmed</span>
      </div>
      <button class="leave-cancel" data-leave-delete="${leave.id}"
              title="Cancel leave" aria-label="Cancel leave">✕</button>
    </div>`;
}

function upcomingLeaves() {
  const today = state.meta.today;
  return (state.myLeaves || []).filter((leave) => leave.date >= today);
}

function leaveIds(prefix = "") {
  return prefix
    ? {
        date: `${prefix}LeaveDate`,
        reason: `${prefix}LeaveReason`,
        apply: `${prefix}ApplyLeaveBtn`,
      }
    : { date: "leaveDate", reason: "leaveReason", apply: "applyLeaveBtn" };
}

function leaveFormHtml(prefix = "") {
  const today = state.meta.today;
  const selected = state.date && state.date >= today ? state.date : today;
  const ids = leaveIds(prefix);
  return `<div class="leave-form">
      <label class="field-label" for="${ids.date}">Leave date</label>
      <input type="date" id="${ids.date}" min="${today}" value="${selected}">
      <label class="field-label" for="${ids.reason}">Reason</label>
      <input id="${ids.reason}" placeholder="e.g. Family function" maxlength="120">
      <button class="btn leave-apply-btn" id="${ids.apply}">Apply for leave</button>
      <p class="leave-note">This day will be blocked for new appointments.</p>
    </div>`;
}

function leaveListHtml(leaves) {
  const items = leaves.length
    ? leaves.map(leaveRowHtml).join("")
    : `<div class="side-empty">
        <span class="side-empty-icon">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none"
               stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
               stroke-linejoin="round" aria-hidden="true">
            <rect x="3" y="5" width="18" height="16" rx="2"/>
            <path d="M16 3v4M8 3v4M3 11h18"/>
          </svg>
        </span>
        <strong>No upcoming leave</strong>
        <span>Confirmed leave will appear here.</span>
      </div>`;

  return `<div class="leave-list">
      <div class="leave-list-head">
        <span>Upcoming</span>
        ${leaves.length ? `<span class="leave-count">${leaves.length}</span>` : ""}
      </div>
      ${items}
    </div>`;
}

function leaveHeaderHtml(count, subtitle = "Plan a day off") {
  return `<div class="side-panel-head">
      <span class="side-panel-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none"
             stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
             stroke-linejoin="round">
          <rect x="3" y="5" width="18" height="16" rx="2"/>
          <path d="M16 3v4M8 3v4M3 11h18"/>
          <path d="M9 16l2 2 4-4"/>
        </svg>
      </span>
      <div class="side-panel-heading">
        <h3 class="side-panel-title">My leave</h3>
        <p class="side-panel-sub">${subtitle}</p>
      </div>
      ${count ? `<span class="side-panel-count">${count}</span>` : ""}
    </div>`;
}

function doctorLeavePanelHtml() {
  const leaves = upcomingLeaves();
  return `<div class="panel side-panel leave-panel">
      ${leaveHeaderHtml(leaves.length)}
      <div class="side-panel-body">
        ${leaveFormHtml()}
        ${leaveListHtml(leaves)}
      </div>
    </div>`;
}

function renderLeaveModal() {
  const leaves = upcomingLeaves();
  const modal = $("modal");
  modal.innerHTML = `<div class="leave-panel">
      ${leaveHeaderHtml(leaves.length, "Block a day for appointments")}
      <div class="side-panel-body leave-modal-body">
        ${leaveFormHtml("modal")}
        ${leaveListHtml(leaves)}
      </div>
      <div class="modal-actions">
        <button type="button" class="btn" data-close>Close</button>
      </div>
    </div>`;
  bindLeaveForm("modal", modal);
  modal.querySelector("[data-close]").addEventListener("click", closeModal);
}

async function openLeaveModal() {
  if (state.role !== "doctor") {
    await setRole("doctor");
  }
  leaveModalOpen = true;
  renderLeaveModal();
  $("modalBackdrop").hidden = false;
}

function patientCardHtml() {
  const phone = localStorage.getItem(STORAGE.phone) || "";
  return `<div class="panel">
      <h3 class="section-title">My appointments</h3>
      <div class="inline-form">
        <input id="patientPhone" inputmode="numeric" placeholder="10-digit phone number"
               value="${u.escapeHtml(phone)}">
        <button class="btn" id="patientLookupBtn">Show my appointments</button>
      </div>
      <div id="patientResults"></div>
    </div>`;
}

function doctorAdminHtml() {
  const rows = state.meta.doctors
    .map(
      (doctor) => `<div class="holiday-item">
        <span class="swatch" style="background:${doctor.color};width:9px;height:9px;border-radius:3px;flex:0 0 auto"></span>
        <span class="h-name" title="${u.escapeHtml(doctor.full_name)}">${u.escapeHtml(doctor.full_name)}</span>
        <button data-doctor-delete="${doctor.id}" title="Remove doctor">✕</button>
      </div>`
    )
    .join("");

  return `<div class="panel">
      <h3 class="section-title">Doctors (${state.meta.doctors.length})</h3>
      <details>
        <summary class="empty-note" style="cursor:pointer">Manage list</summary>
        ${rows}
      </details>
      <div class="inline-form">
        <input id="doctorName" placeholder="Dr. Full Name" maxlength="120">
        <input id="doctorSpecialty" placeholder="Specialization" maxlength="120">
        <select id="doctorDays">
          <option>Monday to Saturday</option>
          <option>Monday to Friday</option>
          <option>Monday, Wednesday, Friday</option>
          <option>Tuesday, Thursday, Saturday</option>
        </select>
        <button class="btn" id="addDoctorBtn">Add doctor</button>
      </div>
    </div>`;
}

function holidayDisplayName(holiday) {
  if (holiday.scope !== "DOCTOR") return holiday.name;
  const doctor = state.meta.doctors.find((item) => item.id === holiday.doctor_id);
  const who = doctor?.full_name || holiday.doctor_id || "Doctor";
  return `${who} · ${holiday.name}`;
}

function holidayKind(holiday) {
  if (holiday.scope === "CLINIC") {
    const label =
      holiday.type === "LEAVE"
        ? "Common leave"
        : holiday.type === "CLOSED"
        ? "Closure"
        : "Public holiday";
    return { label, className: "common-leave" };
  }
  if (holiday.scope === "DOCTOR" && holiday.type === "LEAVE") {
    return { label: "Leave", className: "leave" };
  }
  if (holiday.type === "CLOSED") {
    return { label: "Closure", className: "closure" };
  }
  return { label: "Public holiday", className: "" };
}

function holidayCardHtml(holiday) {
  const start = u.parseISO(holiday.date);
  const end =
    holiday.end_date && holiday.end_date !== holiday.date
      ? u.parseISO(holiday.end_date)
      : null;
  const range = end
    ? ` – ${u.pad(end.getDate())} ${u.MONTHS_SHORT[end.getMonth()]}`
    : "";
  const label = holidayDisplayName(holiday);
  const kind = holidayKind(holiday);
  const isLeave = holiday.scope === "DOCTOR" && holiday.type === "LEAVE";
  const isCommonLeave = holiday.scope === "CLINIC";
  const cardClass = isLeave ? " is-leave" : isCommonLeave ? " is-common-leave" : "";

  return `<div class="holiday-card${cardClass}" title="${u.escapeHtml(label)}">
      <div class="holiday-date">
        <span class="holiday-day">${u.pad(start.getDate())}</span>
        <span class="holiday-month">${u.MONTHS_SHORT[start.getMonth()]}</span>
      </div>
      <div class="holiday-info">
        <span class="holiday-name">${u.escapeHtml(label)}</span>
        <span class="holiday-meta">
          <span class="holiday-kind ${kind.className}">${kind.label}</span>
          ${range ? `<span class="holiday-range">${range}</span>` : ""}
        </span>
      </div>
      <button class="holiday-remove" data-holiday-delete="${holiday.id}"
              title="Remove" aria-label="Remove">✕</button>
    </div>`;
}

function holidayPanelHtml() {
  const holidays = state.data?.holidays || [];
  const items = holidays.length
    ? holidays.map(holidayCardHtml).join("")
    : `<div class="side-empty">
        <span class="side-empty-icon">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none"
               stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
               stroke-linejoin="round" aria-hidden="true">
            <rect x="3" y="5" width="18" height="16" rx="2"/>
            <path d="M16 3v4M8 3v4M3 11h18"/>
          </svg>
        </span>
        <strong>No holidays in this range</strong>
        <span>Clinic closures will appear here.</span>
      </div>`;

  return `<div class="panel side-panel holiday-panel">
      <div class="side-panel-head">
        <span class="side-panel-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none"
               stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
               stroke-linejoin="round">
            <rect x="3" y="5" width="18" height="16" rx="2"/>
            <path d="M16 3v4M8 3v4M3 11h18"/>
            <path d="M9.5 15.5l5 5M14.5 15.5l-5 5"/>
          </svg>
        </span>
        <div class="side-panel-heading">
          <h3 class="side-panel-title">Holidays &amp; closures</h3>
          <p class="side-panel-sub">Clinic-wide days off</p>
        </div>
        ${holidays.length ? `<span class="side-panel-count">${holidays.length}</span>` : ""}
      </div>
      <div class="side-panel-body">
        <div class="holiday-list">${items}</div>
        <div class="inline-form holiday-form">
          <label class="field-label" for="holidayDate">Date</label>
          <input type="date" id="holidayDate" value="${state.date}">
          <label class="field-label" for="holidayName">Name</label>
          <input id="holidayName" placeholder="e.g. Diwali" maxlength="120">
          <label class="field-label" for="holidayType">Type</label>
          <select id="holidayType">
            <option value="PUBLIC_HOLIDAY">Public holiday</option>
            <option value="CLOSED">Clinic closure</option>
            <option value="LEAVE">Common leave (booking allowed)</option>
          </select>
          <button class="btn holiday-add-btn" id="addHolidayBtn">Add entry</button>
        </div>
      </div>
    </div>`;
}

function bindSidebarEvents() {
  document.querySelectorAll(".mini-day").forEach((node) => {
    node.addEventListener("click", () => {
      state.date = node.dataset.date;
      refresh();
    });
  });

  document.querySelectorAll("[data-mini-nav]").forEach((node) => {
    node.addEventListener("click", () => {
      const anchor = u.parseISO(state.date);
      const shifted = new Date(
        anchor.getFullYear(),
        anchor.getMonth() + Number(node.dataset.miniNav),
        1
      );
      state.date = u.iso(shifted);
      refresh();
    });
  });

  bindSearchEvents();

  bindLeaveForm();

  $("patientLookupBtn")?.addEventListener("click", () => {
    const phone = ($("patientPhone")?.value || "").trim();
    if (!phone) {
      toast("Enter a phone number first.", "error");
      return;
    }
    lookupPatient(phone);
  });

  $("addHolidayBtn")?.addEventListener("click", addHoliday);

  $("addDoctorBtn")?.addEventListener("click", addDoctor);

  document.querySelectorAll("[data-doctor-delete]").forEach((node) => {
    node.addEventListener("click", () => removeDoctor(node.dataset.doctorDelete));
  });

  document.querySelectorAll("[data-holiday-delete]").forEach((node) => {
    node.addEventListener("click", async () => {
      if (!confirm("Remove this holiday or leave?")) return;
      try {
        await api.deleteHoliday(node.dataset.holidayDelete);
        toast("Closure removed", "success");
        refresh();
      } catch (error) {
        toast(error.message, "error");
      }
    });
  });
}

/* ============================================================
   Doctor leave
   ============================================================ */

async function afterLeaveChange() {
  await refresh();
  if (leaveModalOpen) renderLeaveModal();
}

function bindLeaveForm(prefix = "", root = document) {
  const ids = leaveIds(prefix);
  root.querySelector(`#${ids.apply}`)?.addEventListener("click", () => applyLeave(prefix));

  root.querySelectorAll("[data-leave-delete]").forEach((node) => {
    node.addEventListener("click", () => removeLeave(node.dataset.leaveDelete));
  });
}

async function applyLeave(prefix = "") {
  const ids = leaveIds(prefix);
  const dateInput = $(ids.date);
  const reasonInput = $(ids.reason);
  const day = dateInput?.value || "";
  const reason = (reasonInput?.value || "").trim();

  if (!day) {
    toast("Choose a leave date.", "error");
    return;
  }
  if (day < state.meta.today) {
    toast("Leave cannot be applied for a past date.", "error");
    return;
  }
  if (!reason) {
    toast("Enter a reason for the leave.", "error");
    reasonInput?.focus();
    return;
  }

  let conflicts = 0;
  try {
    const preview = await api.leaveConflicts(day);
    conflicts = preview.count || 0;
  } catch (error) {
    toast(error.message, "error");
    return;
  }

  const dateLabel = u.fmtRange("day", u.parseISO(day));
  const conflictNote = conflicts
    ? `\n\n${conflicts} appointment(s) are already booked on this day. They will NOT be cancelled.`
    : "";
  const question = `Apply leave for ${dateLabel}?\n\nReason: ${reason}${conflictNote}\n\nThis day will be blocked for new appointments.`;
  if (!confirm(question)) return;

  try {
    await api.createLeave({
      date: day,
      reason,
      confirm_conflicts: conflicts > 0,
    });
    toast("Leave applied", "success");
    if (reasonInput) reasonInput.value = "";
    await afterLeaveChange();
  } catch (error) {
    if (error.code === "leave_conflicts") {
      const count = error.payload?.conflicts ?? 0;
      const retry = confirm(
        `${count} appointment(s) are already booked on ${dateLabel}. They will NOT be cancelled.\n\nApply the leave anyway?`
      );
      if (!retry) return;
      try {
        await api.createLeave({
          date: day,
          reason,
          confirm_conflicts: true,
        });
        toast("Leave applied", "success");
        await afterLeaveChange();
      } catch (retryError) {
        toast(retryError.message, "error");
      }
      return;
    }
    toast(error.message, "error");
  }
}

async function removeLeave(id) {
  if (
    !confirm(
      "Cancel this leave? The day will become available for appointments again."
    )
  ) {
    return;
  }
  try {
    await api.deleteLeave(id);
    toast("Leave cancelled", "success");
    await afterLeaveChange();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ============================================================
   Patient lookup
   ============================================================ */

function formatAddress(address) {
  if (!address) return "";
  const cityState = [address.city, address.state].filter(Boolean).join(", ");
  const pinCountry = [address.pincode, address.country].filter(Boolean).join(" ");
  return [
    address.address_line1,
    address.address_line2,
    cityState,
    pinCountry,
  ]
    .filter(Boolean)
    .join(", ");
}

async function lookupPatient(phone) {
  const box = $("patientResults");
  if (!box) return;
  box.innerHTML = `<div class="empty-note">Loading…</div>`;
  try {
    const data = await api.patientAppointments(phone);
    localStorage.setItem(STORAGE.phone, phone);
    const addressText = formatAddress(data.patient?.address);
    const addressCard = addressText
      ? `<div class="patient-address">
           <span class="pa-label">Saved address</span>
           <span>${u.escapeHtml(addressText)}</span>
         </div>`
      : "";
    if (!data.appointments.length) {
      box.innerHTML =
        addressCard +
        `<div class="empty-note">No appointments found for that number.</div>`;
      return;
    }
    box.innerHTML =
      addressCard +
      data.appointments
      .map(
        (appointment) => `<div class="appt-card">
          <div class="ac-top">
            <span class="ac-doctor">${u.escapeHtml(appointment.doctor_name)}</span>
            <span class="badge ${appointment.status.toLowerCase()}">${u.statusLabel(appointment.status)}</span>
          </div>
          <span class="ac-when">${appointment.start.slice(0, 10)} · ${u.labelFromHHMM(u.timeHHMM(appointment.start))}</span>
          ${appointment.reason ? `<span class="ac-when">${u.escapeHtml(appointment.reason)}</span>` : ""}
        </div>`
      )
      .join("");
  } catch (error) {
    box.innerHTML = `<div class="empty-note">${u.escapeHtml(error.message)}</div>`;
  }
}

/* ============================================================
   Event popover
   ============================================================ */

function openEventPopover(event, anchor) {
  const popover = $("popover");
  const doctor = state.meta.doctors.find((item) => item.id === event.doctor_id);
  const status = String(event.status || "").toLowerCase();

  const rows = [
    ["Date", event.start.slice(0, 10)],
    ["Time", `${u.labelFromHHMM(u.timeHHMM(event.start))} – ${u.labelFromHHMM(u.timeHHMM(event.end))}`],
    ["Doctor", doctor?.full_name || event.doctor_name],
    ["Patient", event.patient_name],
    ["Phone", event.patient_phone],
    ["Address", formatAddress(event.patient_address) || "—"],
    ["Reason", event.reason || "—"],
    ["Token", event.token_number ? `#${event.token_number}` : "—"],
    ["Source", event.source || "—"],
  ]
    .map(
      ([key, value]) =>
        `<div class="pv-row"><span class="k">${key}</span><span class="v">${u.escapeHtml(value)}</span></div>`
    )
    .join("");

  const actions = [];
  if (event.is_active) {
    if (state.role === "doctor" || canEdit()) {
      if (!event.arrived) actions.push(`<button class="btn" data-action="check-in">Check in</button>`);
      if (canEdit()) actions.push(`<button class="btn" data-action="reschedule">Reschedule</button>`);
      actions.push(`<button class="btn danger" data-action="no-show">No-show</button>`);
    }
    if (canEdit()) actions.push(`<button class="btn danger" data-action="cancel">Cancel</button>`);
  }

  popover.innerHTML = `
    <button class="popover-close" data-close>✕</button>
    <h3>${u.escapeHtml(event.patient_name)}</h3>
    <div class="pv-sub">
      <span class="badge ${status}">${u.statusLabel(event.status)}</span>
      ${event.emergency ? `<span class="badge emergency">Emergency</span>` : ""}
    </div>
    ${rows}
    ${actions.length ? `<div class="popover-actions">${actions.join("")}</div>` : ""}`;

  popover.hidden = false;
  positionPopover(popover, anchor);

  popover.querySelector("[data-close]")?.addEventListener("click", closePopover);

  popover.querySelectorAll("[data-action]").forEach((node) => {
    node.addEventListener("click", () => handleEventAction(node.dataset.action, event));
  });
}

function positionPopover(popover, anchor) {
  const rect = anchor.getBoundingClientRect();
  const width = 310;
  const height = popover.offsetHeight;
  let left = rect.left;
  let top = rect.bottom + 8;

  if (left + width > window.innerWidth - 12) left = window.innerWidth - width - 12;
  if (top + height > window.innerHeight - 12) top = rect.top - height - 8;

  popover.style.left = `${Math.max(12, left)}px`;
  popover.style.top = `${Math.max(12, top)}px`;
}

async function handleEventAction(action, event) {
  try {
    let changed = null;
    if (action === "check-in") {
      changed = await api.checkIn(event.id);
      toast("Patient checked in", "success");
    } else if (action === "no-show") {
      if (!confirm(`Mark ${event.patient_name} as a no-show? The slot will be freed.`)) return;
      changed = await api.noShow(event.id);
      toast("Marked as no-show", "success");
    } else if (action === "cancel") {
      if (!confirm(`Cancel appointment for ${event.patient_name}?`)) return;
      changed = await api.cancel(event.id, "Cancelled from calendar");
      toast("Appointment cancelled", "success");
    } else if (action === "reschedule") {
      closePopover();
      openReschedule(event);
      return;
    }
    closePopover();
    if (changed) {
      notifyParent("appointmentChanged", { action, appointment: changed });
    }
    refresh();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ============================================================
   Booking + reschedule modals
   ============================================================ */

const ADDRESS_INPUTS = [
  ["address_line1", "Address line 1"],
  ["address_line2", "Address line2"],
  ["city", "City"],
  ["state", "State"],
  ["pincode", "PIN code"],
  ["country", "Country"],
];

const REQUIRED_MARK = `<span class="req" title="Required" aria-hidden="true">*</span>`;

function addressFieldsHtml() {
  const field = (key, label, placeholder = "", value = "", extra = "") => `
    <div class="field"><label>${label}</label>
      <input type="text" name="${key}" maxlength="${key === "pincode" ? 6 : 120}"
             autocomplete="off" placeholder="${placeholder}" value="${value}" ${extra}>
      ${key === "pincode"
        ? '<span id="pincodeHint" class="phone-hint" aria-live="polite"></span>'
        : ""}</div>`;

  return `<fieldset class="address-fields">
      <legend>Address <span class="optional">optional</span></legend>
      ${field("address_line1", "Address line 1", "House no., street, area")}
      ${field("address_line2", "Address line 2", "Landmark, locality")}
      <div class="two">
        ${field("city", "City", "City")}
        ${field("state", "State", "State")}
      </div>
      <div class="two">
        ${field("pincode", "PIN code", "6-digit PIN", "", 'inputmode="numeric"')}
        ${field("country", "Country", "", "India")}
      </div>
    </fieldset>`;
}

async function prefillPatientFromPhone(form, phone) {
  const hint = form.querySelector("#addressHint");
  const digits = String(phone || "").replace(/\D/g, "");
  if (!hint) return;
  if (digits.length < 6) {
    hint.textContent = "";
    hint.className = "address-hint";
    return;
  }

  const token = String(Number(form.dataset.lookupToken || 0) + 1);
  form.dataset.lookupToken = token;
  hint.textContent = "Looking up saved details…";
  hint.className = "address-hint loading";

  try {
    const patient = await api.lookupPatient(phone.trim());
    if (form.dataset.lookupToken !== token) return;
    if (!patient) {
      hint.textContent = "New patient — add an address if needed.";
      hint.className = "address-hint muted";
      return;
    }
    if (!form.elements.patient_name.value.trim() && patient.name) {
      form.elements.patient_name.value = patient.name;
    }
    const address = patient.address || {};
    ADDRESS_INPUTS.forEach(([key]) => {
      const input = form.elements[key];
      if (input && address[key] && !input.value.trim()) input.value = address[key];
    });
    const hasAddress = Boolean(address.address_line1 || address.city);
    hint.textContent = hasAddress
      ? "Saved address loaded — edit if needed."
      : "Patient found — no saved address yet.";
    hint.className = "address-hint success";
  } catch {
    if (form.dataset.lookupToken !== token) return;
    hint.textContent = "";
    hint.className = "address-hint";
  }
}

const PHONE_LENGTH = 10;
const PINCODE_LENGTH = 6;

function cleanPhone(value) {
  let digits = String(value || "").replace(/\D/g, "");
  if (digits.length === PHONE_LENGTH + 2 && digits.startsWith("91")) {
    digits = digits.slice(2);
  }
  return digits.slice(0, PHONE_LENGTH);
}

function cleanPincode(value) {
  return String(value || "").replace(/\D/g, "").slice(0, PINCODE_LENGTH);
}

function updateBookingSubmit(form) {
  const submit = form.querySelector("#bookingSubmit");
  if (!submit) return;
  const phoneOk =
    cleanPhone(form.elements.patient_phone.value).length === PHONE_LENGTH;
  const pinRaw = form.elements.pincode.value.trim();
  const pinOk = !pinRaw || cleanPincode(pinRaw).length === PINCODE_LENGTH;
  const reasonOk = Boolean(form.elements.reason?.value.trim());
  submit.disabled = !phoneOk || !pinOk || !reasonOk;
}

function validatePhone(form, { touched = false } = {}) {
  const input = form.elements.patient_phone;
  if (!input) return false;
  const digits = cleanPhone(input.value);
  const valid = digits.length === PHONE_LENGTH;

  if (touched && input.value !== digits) input.value = digits;

  const hint = form.querySelector("#phoneHint");
  if (hint) {
    if (valid) {
      hint.textContent = "✓ Valid 10-digit number";
      hint.className = "phone-hint valid";
    } else if (touched || digits.length) {
      hint.textContent = `Enter ${PHONE_LENGTH} digits (${digits.length}/${PHONE_LENGTH})`;
      hint.className = "phone-hint invalid";
    } else {
      hint.textContent = "Numbers only — exactly 10 digits";
      hint.className = "phone-hint";
    }
  }

  input.classList.toggle("invalid", !valid && (touched || digits.length > 0));
  updateBookingSubmit(form);
  return valid;
}

function validatePincode(form, { touched = false } = {}) {
  const input = form.elements.pincode;
  if (!input) return true;
  const raw = input.value.trim();
  const digits = cleanPincode(raw);
  const empty = raw === "";
  const valid = empty || digits.length === PINCODE_LENGTH;

  if (touched && input.value !== digits) input.value = digits;

  const hint = form.querySelector("#pincodeHint");
  if (hint) {
    if (empty) {
      hint.textContent = "";
      hint.className = "phone-hint";
    } else if (digits.length === PINCODE_LENGTH) {
      hint.textContent = "✓ Valid 6-digit PIN";
      hint.className = "phone-hint valid";
    } else {
      hint.textContent = `Enter ${PINCODE_LENGTH} digits (${digits.length}/${PINCODE_LENGTH})`;
      hint.className = "phone-hint invalid";
    }
  }

  input.classList.toggle("invalid", !valid);
  updateBookingSubmit(form);
  return valid;
}

function validateReason(form, { touched = false } = {}) {
  const input = form.elements.reason;
  if (!input) return false;
  const valid = Boolean(input.value.trim());
  input.classList.toggle("invalid", touched && !valid);
  updateBookingSubmit(form);
  return valid;
}

function openBooking({ date, time, doctorId } = {}) {
  if (!canBook()) return;

  const chosenDoctor = doctorId || state.doctorId || state.meta.doctors[0]?.id;
  const chosenDate = date || state.date;
  const savedPhone = localStorage.getItem(STORAGE.phone) || "";

  const doctorOptions = state.meta.doctors
    .map(
      (doctor) =>
        `<option value="${doctor.id}">${u.escapeHtml(doctor.full_name)} — ${u.escapeHtml(doctor.specialization)}</option>`
    )
    .join("");

  $("modal").innerHTML = `
    <h2>New appointment</h2>
    <p class="modal-sub">${u.escapeHtml(state.meta.clinic.name)} · ${u.escapeHtml(chosenDate)}</p>
    <form id="bookingForm" class="form-grid">
      <div class="two">
        <div class="field"><label>Patient name ${REQUIRED_MARK}</label>
          <input name="patient_name" required maxlength="120" autocomplete="off" placeholder="Full name"></div>
        <div class="field"><label>Phone ${REQUIRED_MARK}</label>
          <input name="patient_phone" required maxlength="20" inputmode="tel" autocomplete="off"
                 placeholder="10-digit number" value="${u.escapeHtml(savedPhone)}">
          <span id="phoneHint" class="phone-hint" aria-live="polite"></span></div>
      </div>
      <div class="two">
        <div class="field"><label>Doctor ${REQUIRED_MARK}</label><select name="doctor_id">${doctorOptions}</select></div>
        <div class="field"><label>Date ${REQUIRED_MARK}</label><input type="date" name="date" value="${chosenDate}" required></div>
      </div>
      <div class="field"><label>Time slot ${REQUIRED_MARK}</label><select name="time" required><option value="">Loading…</option></select></div>
      <p id="addressHint" class="address-hint" aria-live="polite"></p>
      ${addressFieldsHtml()}
      <div class="field"><label>Reason / symptoms ${REQUIRED_MARK}</label>
        <textarea name="reason" required maxlength="500" placeholder="e.g. Fever, follow-up, chest pain"></textarea></div>
      <label class="checkbox"><input type="checkbox" name="emergency"> Emergency / urgent</label>
      <div id="bookingError" class="modal-error" hidden></div>
      <div class="modal-actions">
        <button type="button" class="btn" data-close>Cancel</button>
        <button type="submit" class="btn primary" id="bookingSubmit">Book appointment</button>
      </div>
    </form>`;

  $("modalBackdrop").hidden = false;
  const form = $("bookingForm");
  form.elements.doctor_id.value = chosenDoctor;

  const loadSlots = () => fillSlots(form, time);
  form.elements.doctor_id.addEventListener("change", loadSlots);
  form.elements.date.addEventListener("change", loadSlots);
  loadSlots();

  const phoneInput = form.elements.patient_phone;
  const lookup = () => prefillPatientFromPhone(form, phoneInput.value);
  phoneInput.addEventListener("input", () => validatePhone(form, { touched: true }));
  phoneInput.addEventListener("change", lookup);
  phoneInput.addEventListener("blur", () => {
    validatePhone(form, { touched: true });
    lookup();
  });
  const pincodeInput = form.elements.pincode;
  pincodeInput.addEventListener("input", () => validatePincode(form, { touched: true }));
  pincodeInput.addEventListener("blur", () => validatePincode(form, { touched: true }));
  const reasonInput = form.elements.reason;
  reasonInput.addEventListener("input", () => validateReason(form, { touched: true }));
  reasonInput.addEventListener("blur", () => validateReason(form, { touched: true }));

  validatePhone(form);
  validatePincode(form);
  validateReason(form);
  if (savedPhone) lookup();

  form.querySelector("[data-close]").addEventListener("click", closeModal);
  form.addEventListener("submit", async (submitEvent) => {
    submitEvent.preventDefault();
    const errorBox = $("bookingError");
    errorBox.hidden = true;

    const name = form.elements.patient_name.value.trim();
    const phone = cleanPhone(form.elements.patient_phone.value);
    const reason = form.elements.reason.value.trim();
    if (!validatePhone(form, { touched: true })) {
      errorBox.textContent = `Please enter a valid ${PHONE_LENGTH}-digit phone number.`;
      errorBox.hidden = false;
      return;
    }
    if (!validatePincode(form, { touched: true })) {
      errorBox.textContent = `PIN code must be exactly ${PINCODE_LENGTH} digits.`;
      errorBox.hidden = false;
      return;
    }
    if (!validateReason(form, { touched: true })) {
      errorBox.textContent = "Enter a reason / symptoms for the appointment.";
      errorBox.hidden = false;
      return;
    }
    if (!name || !form.elements.doctor_id.value || !form.elements.date.value || !form.elements.time.value) {
      errorBox.textContent = "Please complete all required fields (marked *).";
      errorBox.hidden = false;
      return;
    }

    const address = {};
    ADDRESS_INPUTS.forEach(([key]) => {
      address[key] =
        key === "pincode"
          ? cleanPincode(form.elements[key].value)
          : form.elements[key].value.trim();
    });

    const payload = {
      doctor_id: form.elements.doctor_id.value,
      date: form.elements.date.value,
      time: form.elements.time.value,
      patient_name: name,
      patient_phone: phone,
      address,
      reason,
      emergency: form.elements.emergency.checked,
    };

    try {
      const created = await api.createAppointment(payload);
      closeModal();
      toast("Appointment booked", "success");
      notifyParent("appointmentBooked", { appointment: created });
      refresh();
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
    }
  });
}

async function fillSlots(form, preferred) {
  const select = form.elements.time;
  const doctorId = form.elements.doctor_id.value;
  const day = form.elements.date.value;
  select.innerHTML = `<option value="">Loading…</option>`;

  try {
    const grid = await api.dayGrid(day, doctorId);
    const block = grid.doctors[0];
    let slots = (block?.slots || []).filter((slot) => slot.status === "AVAILABLE");

    // day-grid does not expire slots that have already ended today, but the
    // booking API rejects them. Hide them here so the modal never offers a
    // slot that cannot actually be booked.
    if (day === state.meta?.today) {
      const nowMinutes = u.istMinutesNow();
      const duration = state.meta.slot_minutes || 30;
      slots = slots.filter(
        (slot) => u.minutesOfDay(slot.time) + duration > nowMinutes
      );
    }

    if (!slots.length) {
      const holiday = block?.holiday;
      let message = "No free slots on this day";
      if (holiday?.type === "LEAVE" || holiday?.type === "CLOSED") {
        message = `${holiday.name} — no consultations this day`;
      } else if (block && !block.scheduled) {
        message = "No consultations scheduled this day";
      } else if (day === state.meta?.today) {
        message = "No slots left today — pick a future date";
      }
      select.innerHTML = `<option value="">${u.escapeHtml(message)}</option>`;
      return;
    }
    select.innerHTML = slots
      .map((slot) => `<option value="${slot.time}">${slot.label}</option>`)
      .join("");
    if (preferred && slots.some((slot) => slot.time === preferred)) {
      select.value = preferred;
    }
  } catch (error) {
    select.innerHTML = `<option value="">Could not load slots</option>`;
  }
}

function openReschedule(event) {
  const doctor = state.meta.doctors.find((item) => item.id === event.doctor_id);

  $("modal").innerHTML = `
    <h2>Reschedule appointment</h2>
    <p class="modal-sub">${u.escapeHtml(event.patient_name)} · ${u.escapeHtml(doctor?.full_name || "")}</p>
    <form id="rescheduleForm" class="form-grid">
      <div class="field"><label>New date</label>
        <input type="date" name="date" value="${event.start.slice(0, 10)}" required></div>
      <div class="field"><label>New time slot</label>
        <select name="time" required><option value="">Loading…</option></select></div>
      <div id="rescheduleError" class="modal-error" hidden></div>
      <div class="modal-actions">
        <button type="button" class="btn" data-close>Cancel</button>
        <button type="submit" class="btn primary">Move appointment</button>
      </div>
    </form>`;

  $("modalBackdrop").hidden = false;
  const form = $("rescheduleForm");

  const loadSlots = async () => {
    const select = form.elements.time;
    select.innerHTML = `<option value="">Loading…</option>`;
    try {
      const grid = await api.dayGrid(form.elements.date.value, event.doctor_id);
      const block = grid.doctors[0];
      const slots = (block?.slots || []).filter((slot) => slot.status === "AVAILABLE");
      select.innerHTML = slots.length
        ? slots.map((slot) => `<option value="${slot.time}">${slot.label}</option>`).join("")
        : `<option value="">No free slots on this day</option>`;
    } catch (error) {
      select.innerHTML = `<option value="">Could not load slots</option>`;
    }
  };

  form.elements.date.addEventListener("change", loadSlots);
  loadSlots();
  form.querySelector("[data-close]").addEventListener("click", closeModal);

  form.addEventListener("submit", async (submitEvent) => {
    submitEvent.preventDefault();
    const errorBox = $("rescheduleError");
    errorBox.hidden = true;
    try {
      const moved = await api.reschedule(event.id, {
        date: form.elements.date.value,
        time: form.elements.time.value,
      });
      closeModal();
      toast("Appointment moved", "success");
      notifyParent("appointmentChanged", {
        action: "rescheduled",
        appointment: moved,
      });
      refresh();
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
    }
  });
}

/* ============================================================
   Holidays + doctors (admin)
   ============================================================ */

async function addDoctor() {
  const fullName = ($("doctorName")?.value || "").trim();
  const specialization = ($("doctorSpecialty")?.value || "").trim();
  const availableDays = $("doctorDays")?.value || "Monday to Saturday";

  if (!fullName || !specialization) {
    toast("Enter the doctor's name and specialization.", "error");
    return;
  }

  try {
    await api.createDoctor({
      full_name: fullName,
      specialization,
      available_days: availableDays,
    });
    state.meta = await api.meta();
    buildDoctorSelect();
    toast("Doctor added", "success");
    refresh();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function removeDoctor(doctorId) {
  const doctor = state.meta.doctors.find((item) => item.id === doctorId);
  if (!confirm(`Remove ${doctor?.full_name || doctorId} from the calendar?`)) return;

  try {
    await api.deleteDoctor(doctorId);
    if (state.doctorId === doctorId) state.doctorId = null;
    state.meta = await api.meta();
    buildDoctorSelect();
    toast("Doctor removed", "success");
    refresh();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function addHoliday() {
  const day = $("holidayDate")?.value;
  const name = $("holidayName")?.value.trim();
  const type = $("holidayType")?.value || "PUBLIC_HOLIDAY";
  if (!day || !name) {
    toast("Enter a date and a name for this entry.", "error");
    return;
  }
  try {
    await api.createHoliday({ date: day, name, type });
    toast(type === "LEAVE" ? "Common leave added" : "Closure added", "success");
    refresh();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ============================================================
   Navigation + controls
   ============================================================ */

function navigate(delta) {
  const anchor = u.parseISO(state.date);
  if (state.view === "month") {
    state.date = u.iso(u.addMonths(anchor, delta));
  } else if (state.view === "week") {
    state.date = u.iso(u.addDays(anchor, delta * 7));
  } else {
    state.date = u.iso(u.addDays(anchor, delta));
  }
  refresh();
}

function goToDay(date) {
  state.view = "day";
  state.date = date;
  refresh();
}

function setView(view) {
  state.view = view;
  refresh();
}

function setRole(role, doctorId) {
  state.role = role;
  localStorage.setItem(STORAGE.role, role);

  if (role === "doctor") {
    state.doctorId = doctorId || state.doctorId || state.meta.doctors[0]?.id || null;
  } else if (role === "patient") {
    state.doctorId = null;
  } else if (doctorId !== undefined) {
    state.doctorId = doctorId || null;
  }
  syncControls();
  return refresh();
}

function buildDoctorSelect() {
  const select = $("doctorSelect");
  select.innerHTML = state.meta.doctors
    .map(
      (doctor) =>
        `<option value="${doctor.id}">${u.escapeHtml(doctor.full_name)}</option>`
    )
    .join("");
}

function syncControls() {
  document.querySelectorAll("#viewSwitch button").forEach((node) => {
    node.classList.toggle("active", node.dataset.view === state.view);
  });

  $("roleSelect").value = state.role;
  $("doctorSelect").hidden = state.role !== "doctor";
  if (state.doctorId) $("doctorSelect").value = state.doctorId;
  $("newApptBtn").hidden = !canBook();

  const leaveBtn = $("topLeaveBtn");
  leaveBtn.hidden = state.role !== "doctor";
  leaveBtn.classList.toggle("active", state.role === "doctor");
  leaveBtn.title = "Manage my leave";
}

function writeUrl() {
  const params = new URLSearchParams();
  params.set("view", state.view);
  params.set("date", state.date);
  params.set("role", state.role);
  if (state.doctorId) params.set("doctor", state.doctorId);
  history.replaceState(null, "", `${location.pathname}?${params.toString()}`);
}

/* ============================================================
   Popover / modal / toast plumbing
   ============================================================ */

function closePopover() {
  $("popover").hidden = true;
}

function closeModal() {
  $("modalBackdrop").hidden = true;
  $("modal").innerHTML = "";
  leaveModalOpen = false;
}

function toast(message, type = "") {
  const node = $("toast");
  node.textContent = message;
  node.className = `toast ${type} show`.trim();
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    node.classList.remove("show");
    setTimeout(() => {
      node.hidden = true;
    }, 250);
  }, 3200);
}

/* ============================================================
   Embed bridge (iframe -> host page)
   ============================================================ */

function notifyParent(type, payload = {}) {
  if (!state.embedded || window.parent === window) return;
  window.parent.postMessage(
    { source: "dnyanai-calendar", type, ...payload },
    "*"
  );
}

function bindEmbedBridge() {
  window.addEventListener("message", (event) => {
    const data = event.data || {};
    if (data.source !== "dnyanai-calendar-host") return;

    if (data.type === "navigate" && data.date) {
      state.date = data.date;
      if (["month", "week", "day"].includes(data.view)) state.view = data.view;
      refresh();
    } else if (data.type === "refresh") {
      refresh();
    } else if (
      data.type === "setRole" &&
      state.meta.roles.includes(data.role)
    ) {
      setRole(data.role, data.doctor);
    }
  });
}

function bindGlobalEvents() {
  const sidebar = $("sidebar");
  const sidebarToggle = $("sidebarToggleBtn");
  const sidebarBackdrop = $("sidebarBackdrop");
  const setSidebar = (open) => {
    if (!sidebar || !sidebarToggle || !sidebarBackdrop) return;
    sidebar.classList.toggle("mobile-open", open);
    sidebarToggle.setAttribute("aria-expanded", String(open));
    sidebarBackdrop.hidden = !open;
  };
  sidebarToggle?.addEventListener("click", () =>
    setSidebar(!sidebar.classList.contains("mobile-open"))
  );
  sidebarBackdrop?.addEventListener("click", () => setSidebar(false));
  let wasPhone = window.matchMedia("(max-width: 700px)").matches;
  window.addEventListener("resize", () => {
    if (window.innerWidth > 900) setSidebar(false);
    // The phone renderers (week agenda / compact day columns) are chosen at
    // render time, so re-render when a resize crosses the phone breakpoint.
    const isPhone = window.matchMedia("(max-width: 700px)").matches;
    if (isPhone !== wasPhone) {
      wasPhone = isPhone;
      closePopover();
      renderView();
    }
  });

  $("todayBtn").addEventListener("click", () => {
    state.date = state.meta.today;
    refresh();
  });
  $("prevBtn").addEventListener("click", () => navigate(-1));
  $("nextBtn").addEventListener("click", () => navigate(1));

  document.querySelectorAll("#viewSwitch button").forEach((node) => {
    node.addEventListener("click", () => setView(node.dataset.view));
  });

  $("roleSelect").addEventListener("change", (event) => setRole(event.target.value));

  $("doctorSelect").addEventListener("change", (event) => {
    state.doctorId = event.target.value;
    refresh();
  });

  $("newApptBtn").addEventListener("click", () => openBooking({ date: state.date }));
  $("topLeaveBtn").addEventListener("click", () => openLeaveModal());

  $("modalBackdrop").addEventListener("click", (event) => {
    if (event.target === $("modalBackdrop")) closeModal();
  });

  document.addEventListener("click", (event) => {
    const popover = $("popover");
    if (popover.hidden) return;
    if (popover.contains(event.target)) return;
    if (event.target.closest(".chip, .event, .res-event")) return;
    closePopover();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closePopover();
      closeModal();
      return;
    }
    if (event.target.matches("input, textarea, select")) return;

    if (event.key === "t") {
      state.date = state.meta.today;
      refresh();
    } else if (event.key === "m") setView("month");
    else if (event.key === "w") setView("week");
    else if (event.key === "d") setView("day");
    else if (event.key === "ArrowLeft") navigate(-1);
    else if (event.key === "ArrowRight") navigate(1);
  });

  

  window.addEventListener("resize", closePopover);
  bindEmbedBridge();

  document.addEventListener("click", (event) => {
    if (event.target.closest(".sidebar-search-container")) return;
    closeSearchDropdown();
    const menu = $("specializationMenu");
    if (menu) menu.hidden = true;
  });
}

/* ============================================================
   Doctor search + specialization filter
   ============================================================ */

// Everyday words (symptoms/body parts) mapped to specializations.
const SPECIALTY_KEYWORDS = {
  cold: ["general physician"],
  cough: ["general physician"],
  fever: ["general physician"],
  flu: ["general physician"],
  general: ["general physician"],
  checkup: ["general physician"],
  skin: ["dermatologist"],
  derma: ["dermatologist"],
  rash: ["dermatologist"],
  acne: ["dermatologist"],
  hair: ["dermatologist"],
  heart: ["cardiologist"],
  cardio: ["cardiologist"],
  chest: ["cardiologist"],
  child: ["pediatrician"],
  kid: ["pediatrician"],
  baby: ["pediatrician"],
  pediatric: ["pediatrician"],
  eye: ["ophthalmologist"],
  vision: ["ophthalmologist"],
  ophthal: ["ophthalmologist"],
  ear: ["ent specialist"],
  nose: ["ent specialist"],
  throat: ["ent specialist"],
  ent: ["ent specialist"],
  bone: ["orthopedic"],
  joint: ["orthopedic"],
  ortho: ["orthopedic"],
  fracture: ["orthopedic"],
  mental: ["psychiatrist"],
  psych: ["psychiatrist"],
  anxiety: ["psychiatrist"],
  depression: ["psychiatrist"],
  women: ["gynecologist"],
  gyne: ["gynecologist"],
  pregnancy: ["gynecologist"],
  brain: ["neurologist"],
  neuro: ["neurologist"],
  headache: ["neurologist"],
  migraine: ["neurologist"],
};

function specializationTerms(query) {
  const q = String(query || "").trim().toLowerCase();
  if (!q) return [];
  const terms = [q];
  const key = Object.keys(SPECIALTY_KEYWORDS).find(
    (word) => q === word || q.startsWith(word)
  );
  if (key) terms.push(...SPECIALTY_KEYWORDS[key]);
  return terms;
}

function wordsOf(text) {
  return String(text || "")
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean);
}

function doctorMatchesQuery(doctor, query) {
  const q = String(query || "").trim().toLowerCase();
  if (!q) return true;

  const name = String(doctor.full_name || "").toLowerCase();
  const bareName = name.replace(/^dr\.?\s*/, "");

  // 1) name: prefix of the full name, or of any name word
  //    ("ra" -> "Dr. Rajesh", "kulkarni" -> "Dr. Meera Kulkarni")
  if (name.startsWith(q) || bareName.startsWith(q)) return true;
  if (!q.includes(" ") && wordsOf(bareName).some((word) => word.startsWith(q))) {
    return true;
  }

  // 2) specialization: word-prefix match, plus symptom keywords.
  //    Word-prefix avoids "ra" matching "geneRAI physician".
  const spec = String(doctor.specialization || "").toLowerCase();
  return specializationTerms(q).some((term) => {
    if (term.includes(" ")) return spec.includes(term);
    return wordsOf(spec).some((word) => word.startsWith(term));
  });
}

function filteredDoctors() {
  const filter = state.specializationFilter || "All";
  return state.meta.doctors.filter((doctor) => {
    if (state.role === "doctor") return doctor.id === state.doctorId;
    if (!doctorMatchesQuery(doctor, state.searchValue)) return false;
    if (
      filter !== "All" &&
      !doctor.specialization.toLowerCase().includes(filter.toLowerCase())
    ) {
      return false;
    }
    return true;
  });
}

function specializations() {
  const set = new Set(state.meta.doctors.map((doctor) => doctor.specialization));
  return ["All", ...Array.from(set).sort()];
}

function handleDoctorSearch() {
  const input = $("doctorSearch");
  if (!input) return;
  state.searchValue = input.value;
  openSearchDropdown();
}

function allDoctorsDropdownItem() {
  return `<button type="button" class="dropdown-item all-doctors${
    state.doctorId ? "" : " active"
  }" data-result-all>
      <span class="swatch" style="background:#dadce0"></span>
      <span class="result-name">All doctors</span>
    </button>`;
}

function renderSearchDropdown() {
  const dropdown = $("doctorSearchDropdown");
  if (!dropdown) return;
  const query = state.searchValue.trim();

  if (!query) {
    const recent = loadRecentSearches();
    const recentHtml = recent.length
      ? `<div class="dropdown-header">
          <span class="recent-label">Recent</span>
          <button type="button" class="clear-btn" data-clear-recent>Clear</button>
        </div>
        <div class="dropdown-list">
          ${recent
            .map(
              (term) => `<button type="button" class="dropdown-item" data-recent="${u.escapeHtml(term)}">
                <span class="recent-icon">↺</span>
                <span class="result-name">${u.escapeHtml(term)}</span>
              </button>`
            )
            .join("")}
        </div>`
      : `<div class="dropdown-empty">No recent searches yet.</div>`;

    dropdown.innerHTML = `<div class="dropdown-header"><span class="recent-label">Doctors</span></div>
      <div class="dropdown-list">${allDoctorsDropdownItem()}</div>
      ${recentHtml}`;
    return;
  }

  const results = filteredDoctors().slice(0, 8);
  const resultsHtml = results.length
    ? `<div class="dropdown-list">
        ${allDoctorsDropdownItem()}
        ${results
          .map(
            (doctor) => `<button type="button" class="dropdown-item" data-result="${doctor.id}">
              <span class="swatch" style="background:${doctor.color}"></span>
              <span class="result-name">${u.escapeHtml(doctor.full_name)}</span>
              <span class="result-spec">${u.escapeHtml(doctor.specialization)}</span>
            </button>`
          )
          .join("")}
      </div>`
    : `<div class="dropdown-list">${allDoctorsDropdownItem()}</div>
       <div class="dropdown-empty">No doctors found.</div>`;

  dropdown.innerHTML = `<div class="dropdown-header"><span class="recent-label">Doctors</span></div>
    ${resultsHtml}`;
}

function openSearchDropdown() {
  const dropdown = $("doctorSearchDropdown");
  if (!dropdown) return;
  renderSearchDropdown();
  dropdown.hidden = false;
}

function closeSearchDropdown() {
  const dropdown = $("doctorSearchDropdown");
  if (dropdown) dropdown.hidden = true;
}

function selectRecentSearch(term) {
  state.searchValue = term;
  const input = $("doctorSearch");
  if (input) {
    input.value = term;
    input.focus();
  }
  openSearchDropdown();
}

function clearRecentSearches() {
  localStorage.removeItem(STORAGE.doctorSearch);
  state.searchHistory = [];
  renderSearchDropdown();
}

function chooseDoctor(doctor) {
  saveRecentSearch(doctor.full_name);
  state.doctorId = doctor.id;
  state.searchValue = "";
  const input = $("doctorSearch");
  if (input) input.value = "";
  closeSearchDropdown();
  refresh();
}

function chooseAllDoctors() {
  state.doctorId = null;
  state.searchValue = "";
  const input = $("doctorSearch");
  if (input) input.value = "";
  closeSearchDropdown();
  refresh();
}

function toggleSpecializationFilter() {
  const menu = $("specializationMenu");
  if (!menu) return;
  if (!menu.hidden) {
    menu.hidden = true;
    return;
  }
  const current = state.specializationFilter || "All";
  menu.innerHTML = specializations()
    .map(
      (spec) =>
        `<button type="button" class="specialization-option ${
          spec === current ? "active" : ""
        }" data-specialization="${u.escapeHtml(spec)}">${u.escapeHtml(spec)}</button>`
    )
    .join("");
  menu.hidden = false;
  closeSearchDropdown();
}

function selectSpecialization(value) {
  state.specializationFilter = value || "All";
  const menu = $("specializationMenu");
  if (menu) menu.hidden = true;
  const btn = $("specializationFilterBtn");
  if (btn) btn.classList.toggle("active", state.specializationFilter !== "All");
  openSearchDropdown();
}

function bindSearchEvents() {
  const input = $("doctorSearch");
  if (!input) return;

  input.addEventListener("input", handleDoctorSearch);
  input.addEventListener("focus", openSearchDropdown);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeSearchDropdown();
      return;
    }
    if (event.key !== "Enter") return;
    event.preventDefault();
    const first = filteredDoctors()[0];
    if (first) chooseDoctor(first);
  });

  const dropdown = $("doctorSearchDropdown");
  if (dropdown) {
    dropdown.addEventListener("click", (event) => {
      if (event.target.closest("[data-clear-recent]")) {
        clearRecentSearches();
        return;
      }
      const all = event.target.closest("[data-result-all]");
      if (all) {
        chooseAllDoctors();
        return;
      }
      const recent = event.target.closest("[data-recent]");
      if (recent) {
        selectRecentSearch(recent.dataset.recent);
        return;
      }
      const result = event.target.closest("[data-result]");
      if (result) {
        const doctor = state.meta.doctors.find(
          (item) => item.id === result.dataset.result
        );
        if (doctor) chooseDoctor(doctor);
      }
    });
  }

  $("specializationFilterBtn")?.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleSpecializationFilter();
  });

  const menu = $("specializationMenu");
  if (menu) {
    menu.addEventListener("click", (event) => {
      const option = event.target.closest("[data-specialization]");
      if (option) selectSpecialization(option.dataset.specialization);
    });
  }
}

init();
