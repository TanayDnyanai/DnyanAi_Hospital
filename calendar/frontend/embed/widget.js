/*!
 * DnyanAI Clinic Calendar — embeddable widget
 * -------------------------------------------
 * One script tag mounts a sandboxed iframe, so the host page's CSS can
 * never collide with the calendar.
 *
 *   <div id="clinic-calendar"></div>
 *   <script src="http://localhost:8100/embed/widget.js"
 *           data-role="patient"
 *           data-view="week"
 *           data-doctor="dr-rajesh-sharma"
 *           data-height="720"></script>
 *
 * Events (bubbling CustomEvents on the mount element):
 *   clinic-calendar:ready
 *   clinic-calendar:rendered
 *   clinic-calendar:appointmentBooked
 *   clinic-calendar:appointmentChanged
 *   clinic-calendar:error
 *
 * Imperative API: window.ClinicCalendar.navigate("2026-11-20", "day")
 *                 window.ClinicCalendar.refresh()
 */
(function () {
  "use strict";

  var SOURCE = "dnyanai-calendar";
  var HOST_SOURCE = "dnyanai-calendar-host";
  var instances = [];

  // Capture the script tag now: `document.currentScript` is null by the time
  // DOMContentLoaded fires (which is when auto-mount runs).
  var SCRIPT = document.currentScript;
  if (!SCRIPT) {
    var allScripts = document.getElementsByTagName("script");
    for (var i = 0; i < allScripts.length; i++) {
      if (allScripts[i].src && allScripts[i].src.indexOf("/embed/widget.js") !== -1) {
        SCRIPT = allScripts[i];
        break;
      }
    }
  }

  function scriptOrigin() {
    if (SCRIPT && SCRIPT.src) {
      try {
        return new URL(SCRIPT.src).origin;
      } catch (error) {
        /* fall through */
      }
    }
    return window.location.origin;
  }

  function buildUrl(origin, options) {
    var params = new URLSearchParams();
    params.set("embed", "1");
    if (options.role) params.set("role", options.role);
    if (options.view) params.set("view", options.view);
    if (options.doctor) params.set("doctor", options.doctor);
    if (options.date) params.set("date", options.date);
    return origin + "/embed?" + params.toString();
  }

  function mount(target, options) {
    var element =
      typeof target === "string" ? document.getElementById(target) : target;
    if (!element) {
      console.error("[ClinicCalendar] mount element not found:", target);
      return null;
    }
    if (element.dataset.clinicCalendarMounted === "1") return null;

    var origin = (options.origin || scriptOrigin()).replace(/\/$/, "");
    var height = parseInt(options.height, 10) || 720;

    var frame = document.createElement("iframe");
    frame.src = buildUrl(origin, options);
    frame.title = options.title || "Clinic calendar";
    frame.loading = "lazy";
    frame.setAttribute("allow", "clipboard-write");
    frame.setAttribute("referrerpolicy", "strict-origin-when-cross-origin");
    frame.style.width = "100%";
    frame.style.height = height + "px";
    frame.style.border = "0";
    frame.style.display = "block";
    frame.style.borderRadius = options.radius || "12px";
    frame.style.background = "#ffffff";
    frame.style.boxShadow = options.shadow || "0 1px 3px rgba(60,64,67,.25)";

    element.appendChild(frame);
    element.dataset.clinicCalendarMounted = "1";

    var instance = { element: element, frame: frame, origin: origin, options: options };
    instances.push(instance);

    function post(message) {
      if (frame.contentWindow) {
        frame.contentWindow.postMessage(
          Object.assign({ source: HOST_SOURCE }, message),
          origin
        );
      }
    }

    instance.navigate = function (date, view) {
      post({ type: "navigate", date: date, view: view });
    };
    instance.refresh = function () {
      post({ type: "refresh" });
    };
    instance.setRole = function (role, doctor) {
      post({ type: "setRole", role: role, doctor: doctor });
    };

    window.addEventListener("message", function (event) {
      if (event.source !== frame.contentWindow) return;
      var data = event.data || {};
      if (data.source !== SOURCE) return;

      var detail = Object.assign({ instance: instance }, data);
      var custom = new CustomEvent("clinic-calendar:" + data.type, {
        detail: detail,
        bubbles: true,
      });
      element.dispatchEvent(custom);

      if (typeof options.onEvent === "function") options.onEvent(data);
    });

    return instance;
  }

  function autoMount() {
    var script = SCRIPT;
    var options = {};
    if (script) {
      options.role = script.getAttribute("data-role") || "patient";
      options.view = script.getAttribute("data-view") || "week";
      options.doctor = script.getAttribute("data-doctor") || undefined;
      options.date = script.getAttribute("data-date") || undefined;
      options.height = script.getAttribute("data-height") || undefined;
      options.title = script.getAttribute("data-title") || undefined;
      options.radius = script.getAttribute("data-radius") || undefined;
      options.shadow = script.getAttribute("data-shadow") || undefined;
      if (script.getAttribute("data-origin")) {
        options.origin = script.getAttribute("data-origin");
      }
    }

    var targetId = (script && script.getAttribute("data-mount")) || "clinic-calendar";
    var element = document.getElementById(targetId);
    if (!element) {
      // Auto-create the mount point at the script's position.
      element = document.createElement("div");
      element.id = targetId;
      if (script && script.parentNode) {
        script.parentNode.insertBefore(element, script.nextSibling);
      } else {
        document.body.appendChild(element);
      }
    }
    mount(element, options);
  }

  window.ClinicCalendar = {
    mount: mount,
    navigate: function (date, view) {
      instances.forEach(function (instance) {
        instance.navigate(date, view);
      });
    },
    refresh: function () {
      instances.forEach(function (instance) {
        instance.refresh();
      });
    },
    setRole: function (role, doctor) {
      instances.forEach(function (instance) {
        instance.setRole(role, doctor);
      });
    },
    instances: instances,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", autoMount);
  } else {
    autoMount();
  }
})();
