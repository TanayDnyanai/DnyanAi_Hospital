// Thin fetch wrapper that injects the demo role headers.

let auth = { role: "receptionist", doctorId: null };

export function setAuth(next) {
  auth = { ...auth, ...next };
}

export function currentAuth() {
  return { ...auth };
}

function qs(params) {
  const search = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, value);
    }
  });
  const text = search.toString();
  return text ? `?${text}` : "";
}

async function request(path, { method = "GET", body } = {}) {
  const headers = { "X-Role": auth.role };
  if (auth.doctorId) headers["X-Doctor-Id"] = auth.doctorId;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  const response = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail || `Request failed (${response.status})`);
    error.code = payload.code;
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export const api = {
  meta: () => request("/api/meta"),

  calendar: (from, to, extra = {}) =>
    request(`/api/calendar${qs({ from, to, ...extra })}`),

  dayGrid: (date, doctorId) =>
    request(`/api/day-grid${qs({ date, doctor_id: doctorId })}`),

  stats: (from, to, doctorId) =>
    request(`/api/stats${qs({ from, to, doctor_id: doctorId })}`),

  createAppointment: (body) =>
    request("/api/appointments", { method: "POST", body }),

  reschedule: (id, body) =>
    request(`/api/appointments/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body,
    }),

  cancel: (id, reason) =>
    request(`/api/appointments/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
      body: { reason: reason || "Cancelled from calendar" },
    }),

  noShow: (id) =>
    request(`/api/appointments/${encodeURIComponent(id)}/no-show`, {
      method: "POST",
    }),

  checkIn: (id) =>
    request(`/api/appointments/${encodeURIComponent(id)}/check-in`, {
      method: "POST",
    }),

  patientAppointments: (phone) =>
    request(`/api/patients/${encodeURIComponent(phone)}/appointments`),

  lookupPatient: async (phone) => {
    try {
      return await request(`/api/patients/lookup${qs({ phone })}`);
    } catch (error) {
      if (error.status === 404) return null;
      throw error;
    }
  },

  createHoliday: (body) => request("/api/holidays", { method: "POST", body }),

  holidays: (start, end) =>
    request(`/api/holidays${qs({ start, end })}`),

  leaveConflicts: (date) =>
    request(`/api/doctor/leave/conflicts${qs({ date })}`),

  createLeave: (body) =>
    request("/api/doctor/leave", { method: "POST", body }),

  deleteLeave: (id) =>
    request(`/api/doctor/leave/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  deleteHoliday: (id) =>
    request(`/api/holidays/${encodeURIComponent(id)}`, { method: "DELETE" }),

  createDoctor: (body) =>
    request("/api/admin/doctors", { method: "POST", body }),

  updateDoctor: (id, body) =>
    request(`/api/admin/doctors/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body,
    }),

  deleteDoctor: (id) =>
    request(`/api/admin/doctors/${encodeURIComponent(id)}`, { method: "DELETE" }),
};
