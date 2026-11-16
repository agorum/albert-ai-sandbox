/* ALBERT | AI Sandbox - Admin UI
 *
 * Vanilla JS single-page app, no build step, no external resources.
 * Served at /admin/ (behind nginx: /manager/admin/), therefore every URL is
 * relative ("api/...", "static/..."). Strict CSP: no inline scripts, no inline
 * event handlers, no style attributes - all DOM is built with createElement
 * and textContent, styles only via classes (or element.style from JS).
 */
(() => {
  "use strict";

  const REFRESH_MS = 15000;
  const GIB = 1024 * 1024 * 1024;

  const state = {
    user: null,
    csrf: null,
    overview: null,
    overviewAt: 0,
    view: null,          // controller of the current view: {refresh, rerender}
    routeSeq: 0,
    lastRefresh: 0,
    refreshing: false,
    refreshFailed: false,
    pending: new Map(),  // sandbox name -> {action, since}
    sbxFilter: { text: "", status: "" },
    auditFilter: "",
    lastReconcile: null,
    profiles: null,      // cached profile list (client forms)
    profilesAt: 0,
    baseImage: null,
  };

  // ------------------------------------------------------------------------
  // DOM helpers
  // ------------------------------------------------------------------------

  const $ = (sel, root = document) => root.querySelector(sel);

  const PROP_KEYS = new Set(["value", "checked", "disabled", "hidden", "required", "readOnly",
    "selected", "open", "multiple", "indeterminate"]);

  /** createElement with props and children. Strings become text nodes (never HTML). */
  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    if (props) {
      for (const [key, value] of Object.entries(props)) {
        if (value === null || value === undefined || value === false) continue;
        if (key === "class") {
          el.className = Array.isArray(value) ? value.filter(Boolean).join(" ") : value;
        } else if (key === "text") {
          el.textContent = String(value);
        } else if (key === "dataset") {
          Object.assign(el.dataset, value);
        } else if (key.startsWith("on") && typeof value === "function") {
          el.addEventListener(key.slice(2).toLowerCase(), value);
        } else if (key === "style") {
          // Style attributes are blocked by the CSP - never set them.
          continue;
        } else if (PROP_KEYS.has(key)) {
          el[key] = value;
        } else {
          el.setAttribute(key, value === true ? "" : String(value));
        }
      }
    }
    append(el, children);
    return el;
  }

  function append(el, children) {
    for (const child of [children].flat(Infinity)) {
      if (child === null || child === undefined || child === false || child === "") continue;
      el.append(child instanceof Node ? child : String(child));
    }
    return el;
  }

  function loadingEl(text = "Lade …") {
    return h("div", { class: "loading" }, text);
  }

  function pageHead(title, sub, ...actions) {
    const acts = actions.flat().filter(Boolean);
    return h("div", { class: "page-head" },
      h("div", null, h("h1", null, title), sub ? h("p", { class: "sub" }, sub) : null),
      acts.length ? h("div", { class: "actions" }, acts) : null);
  }

  function sectionHead(title, ...right) {
    return h("div", { class: "section-head" }, h("h2", null, title), right.flat().filter(Boolean));
  }

  function notice(kind, title, ...body) {
    return h("div", { class: ["notice", kind] }, title ? h("strong", null, title) : null,
      body.flat().filter(Boolean).map((b) => (b instanceof Node ? b : h("p", null, b))));
  }

  function tag(text, kind) {
    return h("span", { class: ["tag", kind] }, text);
  }

  function safeUrl(url) {
    if (typeof url !== "string") return null;
    try {
      const parsed = new URL(url, location.href);
      return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
    } catch (_) {
      return null;
    }
  }

  const enc = encodeURIComponent;

  // ------------------------------------------------------------------------
  // formatting (de-DE)
  // ------------------------------------------------------------------------

  const nfCache = new Map();
  function nf(maxDigits, minDigits = 0) {
    const key = maxDigits + ":" + minDigits;
    if (!nfCache.has(key)) {
      nfCache.set(key, new Intl.NumberFormat("de-DE", {
        maximumFractionDigits: maxDigits, minimumFractionDigits: minDigits,
      }));
    }
    return nfCache.get(key);
  }

  function isNum(n) {
    return typeof n === "number" && Number.isFinite(n);
  }

  function fmtNum(n, digits = 2) {
    return isNum(n) ? nf(digits).format(n) : "–";
  }

  function fmtPct(ratio) {
    return isNum(ratio) ? nf(0).format(ratio * 100) + " %" : "–";
  }

  function fmtBytes(bytes) {
    if (!isNum(bytes)) return "–";
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let v = Math.abs(bytes);
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i += 1;
    }
    const digits = i === 0 ? 0 : v < 10 ? 2 : v < 100 ? 1 : 0;
    return (bytes < 0 ? "-" : "") + nf(digits).format(v) + " " + units[i];
  }

  function fmtMb(mb) {
    if (!isNum(mb)) return "–";
    if (Math.abs(mb) >= 1024) return nf(2).format(mb / 1024) + " GB";
    return nf(0).format(mb) + " MB";
  }

  function fmtGb(gb) {
    return isNum(gb) ? nf(2).format(gb) + " GB" : "–";
  }

  function fmtCores(c) {
    if (!isNum(c)) return "–";
    return nf(2).format(c) + (c === 1 ? " Kern" : " Kerne");
  }

  function parseDate(iso) {
    if (!iso) return null;
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function fmtDate(iso, withSeconds = false) {
    const d = parseDate(iso);
    if (!d) return iso ? String(iso) : "–";
    const opts = { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" };
    if (withSeconds) opts.second = "2-digit";
    return d.toLocaleString("de-DE", opts);
  }

  const rtf = new Intl.RelativeTimeFormat("de-DE", { numeric: "auto" });
  function fmtRelative(iso) {
    const d = parseDate(iso);
    if (!d) return "–";
    const diff = (d.getTime() - Date.now()) / 1000;
    const abs = Math.abs(diff);
    if (abs < 45) return "gerade eben";
    if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
    if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
    if (abs < 86400 * 30) return rtf.format(Math.round(diff / 86400), "day");
    return fmtDate(iso);
  }

  function timeEl(iso, relative = false) {
    if (!iso) return h("span", { class: "faint" }, "–");
    return h("time", { datetime: iso, title: fmtDate(iso, true), class: "nowrap" },
      relative ? fmtRelative(iso) : fmtDate(iso));
  }

  /** MB -> GB string for number inputs (dot decimal, max. 3 digits). */
  function mbToGbInput(mb) {
    if (!isNum(mb)) return "";
    return String(Math.round((mb / 1024) * 1000) / 1000);
  }

  // ------------------------------------------------------------------------
  // usage bars
  // ------------------------------------------------------------------------

  function ratioOf(used, limit) {
    if (limit === null || limit === undefined) return null;
    const u = Number(used) || 0;
    if (limit <= 0) return u > 0 ? Infinity : 0;
    return u / limit;
  }

  function bar(used, limit, { big = false } = {}) {
    const el = h("div", { class: ["bar", big && "big"], "aria-hidden": "true" });
    const ratio = ratioOf(used, limit);
    if (ratio === null) {
      el.classList.add("unlimited");
      return el;
    }
    if (ratio > 1 + 1e-9) el.classList.add("over");
    else if (ratio >= 0.85) el.classList.add("warn");
    const fill = h("span");
    fill.style.width = (Math.min(1, Math.max(0, ratio)) * 100).toFixed(1) + "%";
    el.append(fill);
    return el;
  }

  function usageCell(used, limit, fmt, sub) {
    return h("div", { class: "usage" },
      h("div", { class: "usage-text" }, fmt(used),
        h("span", { class: "of" }, " / ", limit === null || limit === undefined ? "unbegrenzt" : fmt(limit))),
      bar(used, limit),
      sub ? h("div", { class: "cell-sub" }, sub) : null);
  }

  function metric(label, valueText, used, limit, ...notes) {
    return h("div", { class: "metric" },
      h("div", { class: "metric-head" },
        h("span", { class: "metric-label" }, label),
        h("span", { class: "metric-value" }, valueText)),
      bar(used, limit, { big: true }),
      notes.flat().filter(Boolean).map((n) => h("div", { class: "metric-note" }, n)));
  }

  // ------------------------------------------------------------------------
  // API
  // ------------------------------------------------------------------------

  class ApiError extends Error {
    constructor(status, code, message, data) {
      super(message);
      this.status = status;
      this.code = code;
      this.data = data;
    }
  }

  function httpMessage(status) {
    if (status === 429) return "Zu viele Anfragen – bitte kurz warten und erneut versuchen.";
    if (status === 502 || status === 503 || status === 504) {
      return `Der Manager antwortet nicht (HTTP ${status}).`;
    }
    return `Anfrage fehlgeschlagen (HTTP ${status}).`;
  }

  async function api(path, { method = "GET", body, auth = true, retried = false } = {}) {
    const init = {
      method,
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    };
    if (method !== "GET" && method !== "HEAD") {
      init.headers["Content-Type"] = "application/json";
      init.headers["X-CSRF-Token"] = state.csrf || "";
      init.body = JSON.stringify(body === undefined ? {} : body);
    }
    let res;
    try {
      res = await fetch("api/" + path, init);
    } catch (_) {
      throw new ApiError(0, "network", "Server nicht erreichbar – bitte Verbindung prüfen.");
    }
    let data = null;
    const text = await res.text().catch(() => "");
    if (text) {
      try {
        data = JSON.parse(text);
      } catch (_) {
        data = null;
      }
    }
    if (res.ok) return data || {};

    const code = data && typeof data.error === "string" ? data.error : null;
    const message = (data && typeof data.message === "string" && data.message)
      || (code && code.includes(" ") ? code : null)
      || httpMessage(res.status);

    if (res.status === 401 && auth) {
      handleUnauthorized(data);
      throw new ApiError(401, code || "unauthorized", "Sitzung abgelaufen – bitte erneut anmelden.", data);
    }
    if (res.status === 403 && code === "csrf" && auth && !retried) {
      // Token out of sync (e.g. session renewed in another tab): fetch it again and retry once.
      if (await refreshSession()) return api(path, { method, body, auth, retried: true });
    }
    throw new ApiError(res.status, code, message, data);
  }

  async function refreshSession() {
    try {
      const data = await api("session", { auth: false });
      state.user = data.user;
      state.csrf = data.csrf;
      return true;
    } catch (_) {
      return false;
    }
  }

  async function getOverview(maxAgeMs = 60000) {
    if (state.overview && Date.now() - state.overviewAt < maxAgeMs) return state.overview;
    const ov = await api("overview");
    state.overview = ov;
    state.overviewAt = Date.now();
    return ov;
  }

  function storeProfiles(data) {
    state.profiles = data.profiles || [];
    state.profilesAt = Date.now();
    state.baseImage = data.baseImage || state.baseImage;
    return state.profiles;
  }

  async function getProfiles(maxAgeMs = 15000) {
    if (state.profiles && Date.now() - state.profilesAt < maxAgeMs) return state.profiles;
    return storeProfiles(await api("profiles"));
  }

  const ERROR_TITLES = {
    quota_exceeded: "Kontingent überschritten",
    sandbox_limit_exceeded: "Limit pro Sandbox überschritten",
    running_limit_exceeded: "Zu viele laufende Sandboxen",
    persistent_not_allowed: "Persistenz nicht freigegeben",
    forbidden: "Nicht erlaubt",
    conflict: "Konflikt",
    not_found: "Nicht gefunden",
    bad_request: "Ungültige Eingabe",
    docker: "Docker-Fehler",
    unavailable: "Nicht verfügbar",
    mcphub_timeout: "MCP Hub nicht bereit",
    remove_incomplete: "Löschen unvollständig",
    client_delete_incomplete: "Client nicht gelöscht",
    create_failed: "Erstellen fehlgeschlagen",
    image_missing: "Docker-Image fehlt",
    profile_not_ready: "Profil noch nicht gebaut",
    profile_in_use: "Profil wird verwendet",
    invalid_package: "Ungültiger Paketname",
    build_failed: "Bau fehlgeschlagen",
    csrf: "Sicherheits-Token ungültig",
    throttled: "Zu viele Fehlversuche",
    network: "Keine Verbindung",
    lockout: "Eigene Adresse fehlt",
    invalid_address: "Ungültige Adresse",
    nginx_failed: "nginx hat die Änderung abgelehnt",
    tls_key_mismatch: "Schlüssel passt nicht",
    tls_name_mismatch: "Zertifikat passt nicht zum Namen",
    tls_expired: "Zertifikat abgelaufen",
    tls_key_encrypted: "Schlüssel mit Passwort",
    tls_terms: "Nutzungsbedingungen",
    tls_certbot_missing: "certbot fehlt",
    tls_busy: "Anfrage läuft bereits",
    invalid_net: "Ungültiges internes Ziel",
    host_full: "Sandbox-Server ausgelastet",
    rate_limited: "Zu viele Anfragen",
    firewall_failed: "Firewall hat die Änderung abgelehnt",
  };

  function errorTitle(err) {
    if (err && ERROR_TITLES[err.code]) return ERROR_TITLES[err.code];
    if (err && typeof err.code === "string" && err.code.startsWith("tls_")) return "Zertifikat";
    return "Fehler";
  }

  function errorText(err) {
    if (!err) return "Unbekannter Fehler";
    let text = err.message || String(err);
    const data = err.data;
    if (data && data.failed && typeof data.failed === "object") {
      const lines = Object.entries(data.failed).map(([name, msg]) => `• ${name}: ${msg}`);
      if (lines.length) text += "\n" + lines.join("\n");
    }
    return text;
  }

  // ------------------------------------------------------------------------
  // toasts
  // ------------------------------------------------------------------------

  function toast(message, { type = "info", title = null, timeout = null } = {}) {
    const box = $("#toasts");
    const closeBtn = h("button", { type: "button", class: "icon-btn", "aria-label": "Meldung schließen" }, "×");
    const el = h("div", { class: ["toast", type], role: type === "error" ? "alert" : "status" },
      h("div", { class: "toast-body" },
        title ? h("div", { class: "toast-title" }, title) : null,
        message ? h("div", null, message) : null),
      closeBtn);
    const remove = () => el.remove();
    closeBtn.addEventListener("click", remove);
    box.append(el);
    while (box.children.length > 5) box.firstElementChild.remove();
    const ms = timeout === null ? (type === "error" ? 12000 : 5000) : timeout;
    if (ms > 0) setTimeout(remove, ms);
  }

  function showError(err, title) {
    if (err && err.status === 401) return; // login screen is already shown
    toast(errorText(err), { type: "error", title: title || errorTitle(err) });
  }

  // ------------------------------------------------------------------------
  // dialogs (native <dialog>)
  // ------------------------------------------------------------------------

  /**
   * Generic modal. onSubmit(form, ctl) may be async; returning false keeps the
   * dialog open, throwing shows the error inside the dialog. Resolves with the
   * value returned by onSubmit, or null when cancelled.
   */
  function modal({
    title, body, submitLabel = "Speichern", submitClass = "primary", cancelLabel = "Abbrechen",
    onSubmit = null, wide = false, noCancel = false, lockClose = false, extraButtons = [], onOpen = null,
  }) {
    return new Promise((resolve) => {
      const errorBox = h("div", { class: "form-error dlg-error", role: "alert", hidden: true });
      const submitBtn = submitLabel ? h("button", { type: "submit", class: ["btn", submitClass] }, submitLabel) : null;
      const cancelBtn = noCancel ? null : h("button", { type: "button", class: "btn" }, cancelLabel);
      const closeX = lockClose ? null
        : h("button", { type: "button", class: "icon-btn", "aria-label": "Dialog schließen" }, "×");
      const form = h("form", { class: "dlg-form" },
        h("div", { class: "dlg-head" }, h("h2", null, title), closeX),
        h("div", { class: "dlg-body" }, body),
        errorBox,
        h("div", { class: "dlg-foot" }, extraButtons, h("span", { class: "spacer" }), cancelBtn, submitBtn));
      const dlg = h("dialog", { class: ["dlg", wide && "wide"] }, form);
      let result = null;
      let busy = false;

      const ctl = {
        dialog: dlg,
        form,
        submitBtn,
        close(value = null) {
          result = value;
          if (dlg.open) dlg.close();
        },
        setError(message) {
          errorBox.textContent = message || "";
          errorBox.hidden = !message;
        },
        setBusy(on) {
          busy = on;
          if (submitBtn) {
            submitBtn.disabled = on;
            submitBtn.classList.toggle("busy", on);
          }
          if (cancelBtn) cancelBtn.disabled = on;
          if (closeX) closeX.disabled = on;
        },
      };

      dlg.addEventListener("cancel", (ev) => {
        if (busy || lockClose) ev.preventDefault();
      });
      dlg.addEventListener("close", () => {
        dlg.remove();
        resolve(result);
      });
      if (closeX) closeX.addEventListener("click", () => { if (!busy) ctl.close(null); });
      if (cancelBtn) cancelBtn.addEventListener("click", () => { if (!busy) ctl.close(null); });

      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        if (busy) return;
        ctl.setError(null);
        if (!form.checkValidity()) {
          form.reportValidity();
          return;
        }
        if (!onSubmit) {
          ctl.close(true);
          return;
        }
        ctl.setBusy(true);
        let value;
        try {
          value = await onSubmit(form, ctl);
        } catch (err) {
          ctl.setBusy(false);
          if (err && err.status === 401) {
            ctl.close(null);
            return;
          }
          ctl.setError(errorText(err));
          return;
        }
        ctl.setBusy(false);
        if (value === false) return;
        ctl.close(value === undefined ? true : value);
      });

      document.body.append(dlg);
      dlg.showModal();
      const first = form.querySelector(".dlg-body input:not([disabled]):not([readonly]), .dlg-body select");
      if (first) first.focus();
      else if (submitBtn) submitBtn.focus();
      else if (cancelBtn) cancelBtn.focus();
      if (onOpen) onOpen(ctl);
    });
  }

  function confirmDialog({ title, message, confirmLabel = "OK", danger = false, requireText = null, onConfirm = null }) {
    const body = [message].flat().filter(Boolean).map((m) => (m instanceof Node ? m : h("p", null, m)));
    let input = null;
    if (requireText !== null) {
      input = h("input", { type: "text", autocomplete: "off", spellcheck: "false", "aria-label": "Bestätigung" });
      body.push(h("label", { class: "field confirm-input" },
        h("span", { class: "field-label" }, "Zur Bestätigung ", h("strong", null, requireText), " eingeben:"),
        input));
    }
    const matches = () => input === null || input.value.trim() === requireText.trim();
    return modal({
      title,
      body,
      submitLabel: confirmLabel,
      submitClass: danger ? "danger" : "primary",
      onOpen: (ctl) => {
        if (!input) return;
        ctl.submitBtn.disabled = true;
        input.addEventListener("input", () => { ctl.submitBtn.disabled = !matches(); });
      },
      onSubmit: async () => {
        if (!matches()) return false;
        if (onConfirm) {
          const value = await onConfirm();
          return value === undefined ? true : value;
        }
        return true;
      },
    });
  }

  async function copyText(text, fallbackInput) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) { /* fall through */ }
    try {
      fallbackInput.focus();
      fallbackInput.select();
      return document.execCommand("copy");
    } catch (_) {
      return false;
    }
  }

  /** Shows a freshly generated API key exactly once. */
  function showKeyDialog(apiKey, { title, intro }) {
    const keyInput = h("input", {
      type: "text", readOnly: true, value: apiKey, class: "mono", spellcheck: "false",
      "aria-label": "API-Key", autocomplete: "off",
    });
    const copyBtn = h("button", { type: "button", class: "btn" }, "Kopieren");
    copyBtn.addEventListener("click", async () => {
      const ok = await copyText(apiKey, keyInput);
      copyBtn.textContent = ok ? "Kopiert ✓" : "Bitte manuell kopieren";
      if (!ok) {
        keyInput.focus();
        keyInput.select();
      }
    });
    keyInput.addEventListener("focus", () => keyInput.select());
    return modal({
      title,
      wide: true,
      noCancel: true,
      lockClose: true,
      submitLabel: "Ich habe den Key gesichert",
      body: [
        intro ? h("p", null, intro) : null,
        notice("warn", "Der Key wird nur jetzt angezeigt.",
          "Gespeichert wird nur ein Hash – der Key kann später nicht erneut angezeigt werden. "
          + "Bei Verlust muss ein neuer Key erzeugt werden; der alte wird dabei ungültig."),
        h("div", { class: "field mt-12" },
          h("span", { class: "field-label" }, "API-Key"),
          h("div", { class: "split" }, h("div", { class: "grow-input" }, keyInput), copyBtn)),
        h("p", { class: "muted small mt-8" },
          "Der Client verwendet ihn als HTTP-Header: Authorization: Bearer <API-Key>"),
      ],
    });
  }

  function closeAllDialogs() {
    document.querySelectorAll("dialog[open]").forEach((d) => d.close());
  }

  // ------------------------------------------------------------------------
  // form helpers
  // ------------------------------------------------------------------------

  function field(label, control, hint) {
    return h("label", { class: "field" },
      h("span", { class: "field-label" }, label),
      control,
      hint ? h("span", { class: "field-hint" }, hint) : null);
  }

  function numInput({ name, value = "", min, max, step = "any", placeholder, disabled = false, unit }) {
    const input = h("input", {
      type: "number", name, step, min, max, placeholder, disabled, inputmode: "decimal",
    });
    const init = value === null || value === undefined ? "" : String(value);
    input.value = init;
    input.dataset.initial = init;
    const wrap = unit ? h("div", { class: "input-unit" }, input, h("span", { class: "unit" }, unit)) : input;
    return { input, node: wrap };
  }

  function readNumber(input) {
    const raw = (input.value || "").trim();
    if (raw === "") return null;
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  }

  function changed(input) {
    return (input.value || "").trim() !== (input.dataset.initial || "");
  }

  // Client fields: API key, group, label, unit, kind (cpus|mem|int), minimum.
  const CLIENT_FIELDS = [
    { key: "defaultCpus", group: "default", label: "CPU", unit: "Kerne", kind: "cpus", min: 0.1 },
    { key: "defaultMemoryMb", group: "default", label: "RAM", unit: "GB", kind: "mem", min: 0.5 },
    { key: "defaultDiskGb", group: "default", label: "Disk", unit: "GB", kind: "int", min: 1 },
    { key: "quotaCpus", group: "quota", label: "CPU", unit: "Kerne", kind: "cpus", min: 0 },
    { key: "quotaMemoryMb", group: "quota", label: "RAM", unit: "GB", kind: "mem", min: 0 },
    { key: "quotaDiskGb", group: "quota", label: "Disk", unit: "GB", kind: "int", min: 0 },
    { key: "quotaSandboxes", group: "quota", label: "Sandboxen", unit: "", kind: "int", min: 0,
      title: "Anzahl Sandboxen insgesamt (laufend und gestoppt)" },
    { key: "quotaRunning", group: "quota", label: "davon laufend", unit: "", kind: "int", min: 0,
      title: "Wie viele Sandboxen gleichzeitig laufen dürfen. Ein weiterer Start oder eine weitere "
        + "Sandbox wird abgelehnt (running_limit_exceeded). Leer = unbegrenzt." },
  ];

  function clientValue(client, key) {
    if (!client) return null;
    const d = client.defaults || {};
    const q = client.quota || {};
    switch (key) {
      case "defaultCpus": return d.cpus;
      case "defaultMemoryMb": return d.memoryMb;
      case "defaultDiskGb": return d.diskGb;
      case "quotaCpus": return q.cpus;
      case "quotaMemoryMb": return q.memoryMb;
      case "quotaDiskGb": return q.diskGb;
      case "quotaSandboxes": return q.sandboxes;
      case "quotaRunning": return q.running;
      default: return null;
    }
  }

  function toInputValue(kind, value) {
    if (value === null || value === undefined) return "";
    return kind === "mem" ? mbToGbInput(value) : String(value);
  }

  function toApiValue(kind, n) {
    if (n === null) return null;
    if (kind === "mem") return Math.round(n * 1024);
    if (kind === "int") return Math.round(n);
    return Math.round(n * 100) / 100;
  }

  function hoursText(seconds) {
    if (!isNum(seconds)) return null;
    const hours = seconds / 3600;
    return hours >= 1 ? `${fmtNum(hours, 1)} Stunde${hours === 1 ? "" : "n"}` : `${fmtNum(seconds / 60, 0)} Minuten`;
  }

  /**
   * "Persistente Sandboxen erlauben" switch.
   * Returns {node, input, value(), changed()}.
   */
  function persistentPolicyField(client, overview) {
    const initial = !!(client && client.allowPersistent);
    const input = h("input", { type: "checkbox", class: "switch", name: "allowPersistent", checked: initial });
    const maxAge = hoursText(((overview && overview.lifecycle) || {}).maxAgeSeconds);
    const node = h("label", { class: "switch-field" },
      input,
      h("span", null,
        h("span", { class: "field-label" }, "Persistente Sandboxen erlauben"),
        h("span", { class: "field-hint" },
          "Ohne Freigabe lehnt die API persistente Sandboxen ab (persistent_not_allowed)"
          + (maxAge ? `; alle anderen werden nach ${maxAge} automatisch gelöscht.` : "."))));
    return {
      node,
      input,
      value: () => input.checked,
      changed: () => input.checked !== initial,
    };
  }

  /** "Internetzugriff" switch. Returns {node, input, value(), changed()}. */
  function internetPolicyField(client) {
    const initial = !client || client.allowInternet !== false;
    const input = h("input", { type: "checkbox", class: "switch", name: "allowInternet", checked: initial });
    const node = h("label", { class: "switch-field" },
      input,
      h("span", null,
        h("span", { class: "field-label" }, "Internetzugriff"),
        h("span", { class: "field-hint" },
          "Aus: keine Verbindungen nach außen und keine Namensauflösung, nur freigegebene interne Ziele "
          + "(Einstellungen und Client). Gilt sofort auch für bestehende Sandboxen.")));
    return {
      node,
      input,
      value: () => input.checked,
      changed: () => input.checked !== initial,
    };
  }

  /** Lines of a textarea with addresses/networks (one per line or comma separated). */
  function netLines(text) {
    return String(text || "").split(/[\s,;]+/).map((v) => v.trim()).filter(Boolean);
  }

  /** "Interne Ziele" of a client (in addition to the global ones). Returns {node, value(), changed()}. */
  function allowNetsField(client) {
    const initial = ((client && client.allowNets) || []).join("\n");
    const area = h("textarea", {
      class: "mono", rows: "3", name: "allowNets", spellcheck: "false", autocomplete: "off",
      placeholder: "z. B.\n10.0.5.20\n10.0.5.0/24",
    });
    area.value = initial;
    const node = field("Interne Ziele dieses Clients", area,
      "Zusätzlich zu den Zielen für alle Sandboxen (Einstellungen): eine IPv4-Adresse oder ein Netz pro Zeile, "
      + "z. B. das agorum core System des Kunden. Der Client bekommt dafür ein eigenes Netz.");
    return {
      node,
      value: () => netLines(area.value),
      changed: () => netLines(area.value).join("\n") !== netLines(initial).join("\n"),
    };
  }

  /** "Alte Anbindung erlauben" switch. Returns {node, input, value(), changed()}. */
  function legacyAccessField(client) {
    const initial = !!(client && client.legacyAccess);
    const input = h("input", { type: "checkbox", class: "switch", name: "legacyAccess", checked: initial });
    const node = h("label", { class: "switch-field" },
      input,
      h("span", null,
        h("span", { class: "field-label" }, "Alte Anbindung erlauben"),
        h("span", { class: "field-hint" },
          "Nur für ALBERT | AI ohne Sicherheitsupdate: MCP mit festem Schlüssel, Dateien ohne Schlüssel, "
          + "Desktop ohne Einmal-Link, keine Bindung an Benutzer. Nach dem Update von ALBERT ausschalten.")));
    return {
      node,
      input,
      value: () => input.checked,
      changed: () => input.checked !== initial,
    };
  }

  /** "Software-Profil" select. Returns {node, value(), changed()}. */
  function profileSelectField(client, profiles) {
    const initial = client && client.profileId ? String(client.profileId) : "";
    const list = [...(profiles || [])];
    if (client && client.profile && !list.some((p) => p.id === client.profile.id)) list.push(client.profile);
    const select = h("select", { name: "profileId", class: "wide" },
      h("option", { value: "" }, "Standard (mitgeliefertes Image)"),
      list.map((p) => h("option", { value: String(p.id) },
        p.name + (p.status === "new" || (p.status === "failed" && !p.image) ? " – noch nicht gebaut" : ""))));
    select.value = initial;
    const node = field("Software-Profil", select,
      "Zusatzsoftware für neue Sandboxen. Bestehende werden als veraltet markiert und per „Neu aufbauen“ umgestellt.");
    return {
      node,
      value: () => (select.value ? Number(select.value) : null),
      changed: () => select.value !== initial,
    };
  }

  /**
   * "Limit pro Sandbox" + "Gesamtkontingent" fieldsets, side by side.
   * Returns {nodes, values(), changedValues()}.
   */
  function clientLimitFields(client, overview) {
    const g = (overview && overview.defaults) || {};
    const host = (overview && overview.host) || {};
    const placeholders = {
      defaultCpus: isNum(g.cpus) ? "Standard: " + String(g.cpus) : "Standard",
      defaultMemoryMb: isNum(g.memoryMb) ? "Standard: " + mbToGbInput(g.memoryMb) : "Standard",
      defaultDiskGb: isNum(g.diskGb) ? "Standard: " + String(g.diskGb) : "Standard",
    };
    const inputs = {};
    const build = (spec) => {
      let max;
      if (spec.key === "defaultCpus" && isNum(host.cpus)) max = host.cpus;
      if (spec.key === "defaultMemoryMb" && isNum(host.memoryMb) && host.memoryMb > 0) {
        max = Math.ceil((host.memoryMb / 1024) * 1000) / 1000;
      }
      const ni = numInput({
        name: spec.key,
        value: toInputValue(spec.kind, clientValue(client, spec.key)),
        min: spec.min,
        max,
        step: spec.kind === "int" ? "1" : "any",
        placeholder: spec.group === "default" ? placeholders[spec.key] : "unbegrenzt",
        unit: spec.unit,
      });
      inputs[spec.key] = { input: ni.input, spec };
      const f = field(spec.label, ni.node);
      if (spec.title) f.title = spec.title;
      return f;
    };
    const defaults = CLIENT_FIELDS.filter((f) => f.group === "default").map(build);
    const quotas = CLIENT_FIELDS.filter((f) => f.group === "quota").map(build);
    const nodes = [
      h("div", { class: "limit-groups" },
        h("fieldset", null,
          h("legend", null, "Limit pro Sandbox"),
          h("p", { class: "legend-hint" }, "Obergrenze je Sandbox, per API darf weniger angefordert werden. Leer = Standard."),
          h("div", { class: "field-grid" }, defaults)),
        h("fieldset", null,
          h("legend", null, "Gesamtkontingent"),
          h("p", { class: "legend-hint" },
            "Leer = unbegrenzt. CPU, RAM und „davon laufend“ zählen laufende Sandboxen, Disk alle."),
          h("div", { class: "field-grid cols-5" }, quotas))),
    ];
    return {
      nodes,
      values() {
        const out = {};
        for (const [key, { input, spec }] of Object.entries(inputs)) {
          out[key] = toApiValue(spec.kind, readNumber(input));
        }
        return out;
      },
      changedValues() {
        const out = {};
        for (const [key, { input, spec }] of Object.entries(inputs)) {
          if (changed(input)) out[key] = toApiValue(spec.kind, readNumber(input));
        }
        return out;
      },
    };
  }

  // ------------------------------------------------------------------------
  // auth / session
  // ------------------------------------------------------------------------

  function showLogin(data) {
    state.user = null;
    state.csrf = null;
    state.view = null;
    state.routeSeq += 1;
    closeAllDialogs();
    showVersion(data);
    $("#mainnav").hidden = true;
    $("#userbox").hidden = true;
    $("#view").hidden = true;
    $("#view").replaceChildren();
    $("#login").hidden = false;
    $("#login-noadmins").hidden = !(data && data.hasAdmins === false);
    $("#login-error").hidden = true;
    const submit = $("#login-submit");
    submit.disabled = false;
    submit.classList.remove("busy");
    document.title = "Anmelden – ALBERT | AI Sandbox Admin";
    setTimeout(() => {
      const user = $("#login-username");
      (user.value ? $("#login-password") : user).focus();
    }, 0);
  }

  function showVersion(data) {
    if (data && data.version) $("#app-version").textContent = data.version;
  }

  // Colour scheme: auto (follow the OS) -> light -> dark -> auto. static/theme.js
  // applies the stored choice before the first paint.
  const THEME_KEY = "albert-admin-theme";
  const THEME_NEXT = { auto: "light", light: "dark", dark: "auto" };
  const THEME_LABEL = { auto: "automatisch wie das Betriebssystem", light: "hell", dark: "dunkel" };

  function currentTheme() {
    const t = document.documentElement.getAttribute("data-theme");
    return THEME_NEXT[t] ? t : "auto";
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem(THEME_KEY, theme); } catch (_) { /* storage blocked */ }
    const btn = $("#theme-toggle");
    const label = `Darstellung: ${THEME_LABEL[theme]} – klicken für ${THEME_LABEL[THEME_NEXT[theme]]}`;
    btn.title = label;
    btn.setAttribute("aria-label", label);
  }

  function handleUnauthorized(data) {
    if (!state.user) return;
    showLogin(data || { hasAdmins: true });
    toast("Die Sitzung ist abgelaufen. Bitte erneut anmelden.", { type: "warn" });
  }

  function setSession(data) {
    state.user = data.user;
    state.csrf = data.csrf;
    state.refreshFailed = false;
    showVersion(data);
    $("#login").hidden = true;
    $("#mainnav").hidden = false;
    $("#userbox").hidden = false;
    $("#view").hidden = false;
    $("#current-user").replaceChildren("Angemeldet als ", h("strong", null, data.user.username));
    if (!location.hash || location.hash === "#" || location.hash === "#/") {
      history.replaceState(null, "", "#/overview");
    }
    render();
  }

  function loginErrorText(err) {
    if (err.code === "invalid_credentials") return "Benutzername oder Passwort ist falsch.";
    if (err.code === "throttled") {
      const m = /(\d+)\s*s/.exec(err.message || "");
      return "Zu viele Fehlversuche. " + (m ? `Bitte in ${m[1]} Sekunden erneut versuchen.` : "Bitte später erneut versuchen.");
    }
    if (err.code === "no_admins") return "Es existiert noch kein Admin-Benutzer.";
    return err.message;
  }

  async function onLoginSubmit(ev) {
    ev.preventDefault();
    const form = ev.currentTarget;
    if (!form.checkValidity()) {
      form.reportValidity();
      return;
    }
    const submit = $("#login-submit");
    const errBox = $("#login-error");
    errBox.hidden = true;
    submit.disabled = true;
    submit.classList.add("busy");
    try {
      const data = await api("login", {
        method: "POST",
        auth: false,
        body: { username: $("#login-username").value.trim(), password: $("#login-password").value },
      });
      $("#login-password").value = "";
      setSession(data);
    } catch (err) {
      if (err.code === "no_admins") $("#login-noadmins").hidden = false;
      errBox.textContent = loginErrorText(err);
      errBox.hidden = false;
      $("#login-password").select();
    } finally {
      submit.disabled = false;
      submit.classList.remove("busy");
    }
  }

  async function logout() {
    try {
      await api("logout", { method: "POST", auth: false });
    } catch (_) { /* ignore: we show the login screen anyway */ }
    showLogin({ hasAdmins: true });
  }

  // ------------------------------------------------------------------------
  // routing + refresh
  // ------------------------------------------------------------------------

  const VIEWS = {
    overview: { title: "Übersicht", nav: "overview", fn: viewOverview },
    clients: { title: "Clients", nav: "clients", fn: viewClients },
    client: { title: "Client", nav: "clients", fn: viewClientDetail },
    sandboxes: { title: "Sandboxen", nav: "sandboxes", fn: viewSandboxes },
    profiles: { title: "Profile", nav: "profiles", fn: viewProfiles },
    profile: { title: "Profil", nav: "profiles", fn: viewProfileDetail },
    admins: { title: "Admins", nav: "admins", fn: viewAdmins },
    maintenance: { title: "Wartung", nav: "maintenance", fn: viewMaintenance },
    settings: { title: "Einstellungen", nav: "settings", fn: viewSettings },
  };

  function parseRoute() {
    let parts;
    try {
      parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
    } catch (_) {
      parts = [];
    }
    let name = parts[0] || "overview";
    const params = {};
    if (name === "clients" && parts[1]) {
      name = "client";
      params.id = parts[1];
    } else if (name === "profiles" && parts[1]) {
      name = "profile";
      params.id = parts[1];
    }
    if (!VIEWS[name]) name = "overview";
    return { name, params };
  }

  function render() {
    if (!state.user) return;
    const { name, params } = parseRoute();
    const def = VIEWS[name];
    state.routeSeq += 1;
    const seq = state.routeSeq;
    const ctx = { alive: () => seq === state.routeSeq && state.user !== null };
    document.querySelectorAll("[data-nav]").forEach((a) => {
      const on = a.dataset.nav === def.nav;
      a.classList.toggle("active", on);
      if (on) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    setTitle(def.title);
    const root = $("#view");
    root.replaceChildren();
    state.view = def.fn(root, params, ctx) || null;
    state.lastRefresh = Date.now();
    window.scrollTo(0, 0);
  }

  function setTitle(title) {
    document.title = (title ? title + " – " : "") + "ALBERT | AI Sandbox Admin";
  }

  /**
   * Re-rendering replaces the tables; keep the horizontal scroll position of
   * every .table-wrap (matched by order) so the user does not lose their place.
   */
  async function keepTableScroll(fn) {
    const before = [...document.querySelectorAll("#view .table-wrap")].map((el) => el.scrollLeft);
    try {
      return await fn();
    } finally {
      document.querySelectorAll("#view .table-wrap").forEach((el, i) => {
        if (before[i]) el.scrollLeft = before[i];
      });
    }
  }

  /** True while the admin edits a form of the view: an automatic refresh would re-render it and lose the input. */
  function editingForm() {
    const active = document.activeElement;
    if (active && active.closest && active.closest("#view form") && active.matches("input, textarea, select")) {
      return true;
    }
    return !!document.querySelector("#view form[data-dirty]");
  }

  async function autoRefresh() {
    if (!state.user || document.hidden || state.refreshing) return;
    if (document.querySelector("dialog[open]") || editingForm()) return;
    const view = state.view;
    if (!view || !view.refresh) return;
    state.refreshing = true;
    try {
      await keepTableScroll(() => view.refresh({ silent: true }));
    } finally {
      state.refreshing = false;
      state.lastRefresh = Date.now();
    }
  }

  /** Re-render the current view from cached data (e.g. to show busy states). */
  function rerenderView() {
    if (state.view && state.view.rerender) keepTableScroll(() => state.view.rerender());
  }

  function refreshView() {
    if (state.view && state.view.refresh) return keepTableScroll(() => state.view.refresh({ silent: true }));
    return Promise.resolve();
  }

  /**
   * Runs load(); on first load errors replace the box content, on silent
   * (auto) refresh errors a single toast is shown until the next success.
   */
  async function guarded(ctx, box, load, silent) {
    try {
      await load();
      state.refreshFailed = false;
    } catch (err) {
      if (!ctx.alive() || (err && err.status === 401)) return;
      if (silent) {
        if (!state.refreshFailed) {
          state.refreshFailed = true;
          showError(err, "Aktualisierung fehlgeschlagen");
        }
        return;
      }
      const retry = h("button", { type: "button", class: "btn small mt-8" }, "Erneut versuchen");
      retry.addEventListener("click", () => {
        box.replaceChildren(loadingEl());
        guarded(ctx, box, load, false);
      });
      box.replaceChildren(notice("danger", errorTitle(err), errorText(err), retry));
    }
  }

  // ------------------------------------------------------------------------
  // view: Übersicht
  // ------------------------------------------------------------------------

  function viewOverview(root, _params, ctx) {
    root.append(pageHead("Übersicht", "Host-Ressourcen und Belegung durch Sandboxen"));
    const body = h("div", null, loadingEl());
    root.append(body);

    async function load() {
      const ov = await api("overview");
      state.overview = ov;
      state.overviewAt = Date.now();
      if (!ctx.alive()) return;
      body.replaceChildren(...renderOverview(ov));
    }
    const refresh = ({ silent = false } = {}) => guarded(ctx, body, load, silent);
    refresh();
    return { refresh };
  }

  function kpi(label, value, unit, foot, href) {
    const inner = [
      h("div", { class: "kpi-label" }, label),
      h("div", { class: "kpi-value" }, value, unit ? h("span", { class: "unit" }, unit) : null),
      foot ? h("div", { class: "kpi-foot" }, foot) : null,
    ];
    return h("div", { class: "panel kpi" }, href ? h("a", { href, class: "kpi-link" }, inner) : inner);
  }

  function fsMetric(label, fs, pathHint) {
    if (!fs) {
      return h("div", { class: "metric" },
        h("div", { class: "metric-head" }, h("span", { class: "metric-label" }, label),
          h("span", { class: "metric-value faint" }, "nicht verfügbar")));
    }
    const used = fs.usedBytes;
    const total = fs.totalBytes;
    return metric(label,
      `${fmtBytes(used)} von ${fmtBytes(total)} (${fmtPct(ratioOf(used, total))})`,
      used, total,
      `Frei: ${fmtBytes(fs.freeBytes)}${pathHint ? " · " + pathHint : ""}`);
  }

  function renderOverview(ov) {
    const host = ov.host || {};
    const sb = ov.sandboxes || {};
    const al = ov.allocated || {};
    const def = ov.defaults || {};
    const out = [];

    if (host.dockerReachable === false) {
      out.push(notice("danger", "Docker ist nicht erreichbar.",
        "Laufzeitstatus und Zuteilungen laufender Sandboxen sind unvollständig."));
    }
    const iso = ov.isolation || {};
    const fw = iso.firewall || {};
    const isolationOk = !!iso.network && iso.interContainerBlocked && (fw.enabled === false || fw.ok);
    if (ov.isolation && !isolationOk) {
      out.push(notice("danger", "Netz-Isolation der Sandboxen ist unvollständig.",
        "Der Watcher repariert das innerhalb einer Minute automatisch. Bleibt der Hinweis, auf dem Host prüfen: "
        + "albert-ai-sandbox-manager firewall status"));
    }
    if (ov.clientsLegacy) {
      out.push(notice("warn", `${fmtNum(ov.clientsLegacy, 0)} Client${ov.clientsLegacy === 1 ? " nutzt" : "s nutzen"} `
        + "noch die alte Anbindung.",
        "MCP und Dateien ihrer Sandboxen sind ohne API-Key erreichbar, der Desktop ohne Einmal-Link. Sobald ALBERT | AI "
        + "das Sicherheitsupdate hat, beim Client „Alte Anbindung erlauben“ ausschalten.",
        h("a", { href: "#/clients" }, "Zu den Clients")));
    }
    if (ov.https && ov.https.active === false) {
      out.push(notice("warn", "HTTPS ist nicht eingerichtet.",
        "API-Keys, Dateien und MCP-Aufrufe gehen unverschlüsselt über das Netz.",
        h("a", { href: "#/settings" }, "Zertifikat einrichten")));
    }

    out.push(h("div", { class: "grid cols-4" + (out.length ? " mt-16" : "") },
      kpi("Clients", fmtNum(ov.clients, 0), null,
        ov.clientsOffline ? `davon ${fmtNum(ov.clientsOffline, 0)} ohne Internet` : "Mandanten mit API-Key", "#/clients"),
      kpi("Sandboxen", fmtNum(sb.total, 0), null,
        `${fmtNum(sb.running, 0)} laufend · ${fmtNum((sb.total || 0) - (sb.running || 0), 0)} gestoppt`, "#/sandboxes"),
      kpi("CPU-Kerne (Host)", fmtNum(host.cpus, 0), null, "logische Kerne"),
      kpi("RAM (Host)", fmtMb(host.memoryMb), null,
        host.dockerReachable === false ? "Docker: nicht erreichbar" : "Docker: erreichbar")));

    // allocation of running sandboxes vs host
    const cpuRatio = ratioOf(al.cpusRunning, host.cpus);
    const memRatio = ratioOf(al.memoryMbRunning, host.memoryMb || null);
    const guard = ov.memoryGuard && ov.memoryGuard.limitMb ? ov.memoryGuard : null;
    const guardRatio = guard ? ratioOf(guard.committedMb, guard.limitMb) : null;
    const overNote = (ratio, what) => {
      if (ratio === null || ratio <= 1 + 1e-9) return null;
      return h("span", { class: "over-note" },
        `Überbucht: ${fmtPct(ratio)} der Host-${what} sind zugeteilt (Faktor ${fmtNum(ratio, 2)}).`);
    };
    const alloc = h("div", { class: "panel" }, h("div", { class: "panel-body" },
      h("div", { class: "panel-title" }, "Zuteilung laufender Sandboxen"),
      metric("CPU-Limits",
        `${fmtNum(al.cpusRunning, 2)} von ${fmtNum(host.cpus, 0)} Kernen (${fmtPct(cpuRatio)})`,
        al.cpusRunning, host.cpus,
        overNote(cpuRatio, "Kerne"),
        "CPU-Überbuchung ist unkritisch, Sandboxen teilen sich dann die Rechenzeit."),
      guard
        ? metric("RAM-Limits",
          `${fmtMb(guard.committedMb)} von ${fmtMb(guard.limitMb)} zugesagt (${fmtPct(guardRatio)})`,
          guard.committedMb, guard.limitMb,
          overNote(memRatio, "RAM"),
          `Grenze: ${fmtNum(guard.factor, 1)} × (RAM ${fmtMb(host.memoryMb)} − Reserve ${fmtMb(guard.reserveMb)}); `
          + "darüber lehnt der Server Anlegen und Starten ab (host_full)."
          + (isNum(guard.availableMb) && guard.availableMb ? ` Jetzt frei: ${fmtMb(guard.availableMb)}.` : ""))
        : metric("RAM-Limits",
          `${fmtMb(al.memoryMbRunning)} von ${fmtMb(host.memoryMb)} (${fmtPct(memRatio)})`,
          al.memoryMbRunning, host.memoryMb || null,
          overNote(memRatio, "RAM"),
          memRatio !== null && memRatio > 1
            ? "Bei RAM-Überbuchung droht der OOM-Killer, wenn mehrere Sandboxen ihr Limit ausschöpfen."
            : "Summe der RAM-Limits; tatsächlicher Verbrauch ist meist geringer.")));

    const storageDir = h("div", { class: "panel" }, h("div", { class: "panel-body" },
      h("div", { class: "panel-title" }, "Dateisysteme"),
      fsMetric("Sandbox-Speicher", host.storage, "Disk-Images der Sandboxen"),
      fsMetric("Docker-Root", host.dockerRoot, "/var/lib/docker – Images und Container-Layer")));

    out.push(h("div", { class: "grid cols-2 mt-16" }, alloc, storageDir));

    // configured disks vs used / vs storage size
    const configuredBytes = (al.diskGbTotal || 0) * GIB;
    const storageTotal = host.storage ? host.storage.totalBytes : null;
    const diskOver = storageTotal ? ratioOf(configuredBytes, storageTotal) : null;
    const disks = h("div", { class: "panel" }, h("div", { class: "panel-body" },
      h("div", { class: "panel-title" }, "Sandbox-Disks"),
      metric("Tatsächlich belegt",
        `${fmtBytes(al.diskUsedBytes)} von ${fmtNum(al.diskGbTotal, 0)} GB konfiguriert (${fmtPct(ratioOf(al.diskUsedBytes, configuredBytes || null))})`,
        al.diskUsedBytes, configuredBytes || null,
        "Gemessen vom Watcher; Legacy-Sandboxen (Docker-Volume) zählen ohne konfigurierte Größe."),
      metric("Konfiguriert vs. Speicherplatz",
        storageTotal
          ? `${fmtNum(al.diskGbTotal, 0)} GB von ${fmtBytes(storageTotal)} (${fmtPct(diskOver)})`
          : `${fmtNum(al.diskGbTotal, 0)} GB`,
        configuredBytes, storageTotal,
        diskOver !== null && diskOver > 1
          ? h("span", { class: "warn-note" },
            "Überbucht: Die Disk-Images sind sparse und belegen nur geschriebene Daten – "
            + "der freie Platz muss trotzdem im Blick bleiben.")
          : "Summe der Disk-Größen aller Sandboxen (laufend und gestoppt).")));

    const defaults = h("div", { class: "panel" }, h("div", { class: "panel-body" },
      h("div", { class: "panel-title" }, "Globale Standardwerte pro Sandbox"),
      h("dl", { class: "kv" },
        h("dt", null, "CPU"), h("dd", null, fmtCores(def.cpus)),
        h("dt", null, "RAM"), h("dd", null, fmtMb(def.memoryMb)),
        h("dt", null, "Disk"), h("dd", null, fmtGb(def.diskGb)),
        h("dt", null, "Öffentliche URL"),
        h("dd", null, safeUrl(ov.publicUrl)
          ? h("a", { href: safeUrl(ov.publicUrl), target: "_blank", rel: "noopener noreferrer", class: "mono" }, ov.publicUrl)
          : h("span", { class: "mono" }, ov.publicUrl || "–"))),
      h("p", { class: "metric-note mt-12" },
        "Gelten für Clients ohne eigenes Limit pro Sandbox (ALBERT_DEFAULT_CPUS / _MEMORY_MB / _DISK_GB).")));

    out.push(h("div", { class: "grid cols-2 mt-16" }, disks, defaults));

    if (ov.isolation) {
      const yesNo = (v) => h("span", { class: ["badge", v ? "ok" : "danger"] }, v ? "aktiv" : "fehlt");
      const offline = iso.offline || {};
      const isolation = h("div", { class: "panel" }, h("div", { class: "panel-body" },
        h("div", { class: "panel-title" }, "Netz-Isolation"),
        h("dl", { class: "kv" },
          h("dt", null, "Eigenes Sandbox-Netz"),
          h("dd", null, yesNo(!!iso.network), " ", h("span", { class: "mono faint" }, iso.subnet || "")),
          h("dt", null, "Sandbox ↔ Sandbox gesperrt"), h("dd", null, yesNo(!!iso.interContainerBlocked)),
          h("dt", null, "Firewall (internes Netz, Host)"),
          h("dd", null, fw.enabled === false ? h("span", { class: "badge warn" }, "abgeschaltet") : yesNo(!!fw.ok)),
          h("dt", null, "Gesperrte Ziele"),
          h("dd", { class: "mono small" }, (iso.blockedNets || []).join(", ") || "–"),
          (iso.allowedNets || []).length ? h("dt", null, "Ausnahmen") : null,
          (iso.allowedNets || []).length ? h("dd", { class: "mono small" }, iso.allowedNets.join(", ")) : null,
          h("dt", null, "Netz ohne Internet"),
          h("dd", null, offline.network ? yesNo(true) : h("span", { class: "faint" }, "wird bei Bedarf angelegt"),
            " ", h("span", { class: "mono faint" }, offline.subnet || ""))),
        h("p", { class: "metric-note mt-12" },
          "Sandboxen erreichen das Internet, aber weder andere Sandboxen noch interne Netze oder Dienste des Hosts "
          + "(ALBERT_BLOCK_NETS / ALBERT_ALLOW_NETS). Sandboxen von Clients ohne Internetzugriff erreichen nur die "
          + "Ausnahmen und haben keine Namensauflösung.")));
      const profilesPanel = h("div", { class: "panel" }, h("div", { class: "panel-body" },
        h("div", { class: "panel-title" }, "Software-Profile"),
        h("dl", { class: "kv" },
          h("dt", null, "Profile"), h("dd", null, h("a", { href: "#/profiles" }, fmtNum(ov.profiles || 0, 0))),
          h("dt", null, "Clients ohne Internet"), h("dd", null, fmtNum(ov.clientsOffline || 0, 0))),
        h("p", { class: "metric-note mt-12" },
          "Ein Profil ergänzt das mitgelieferte Sandbox-Image um Pakete (apt, pip, npm). Es wird einem Client "
          + "zugeordnet; dessen neue Sandboxen entstehen dann aus dem Profil-Image.")));
      out.push(h("div", { class: "grid cols-2 mt-16" }, isolation, profilesPanel));
    }
    return out;
  }

  // ------------------------------------------------------------------------
  // view: Clients
  // ------------------------------------------------------------------------

  function viewClients(root, _params, ctx) {
    const createBtn = h("button", { type: "button", class: "btn primary" }, "Client anlegen");
    createBtn.addEventListener("click", () => openCreateClient());
    root.append(pageHead("Clients", "Mandanten mit je einem API-Key, Limit pro Sandbox und Gesamtkontingent", createBtn));
    const body = h("div", null, loadingEl());
    root.append(body);

    async function load() {
      const data = await api("clients");
      if (!ctx.alive()) return;
      body.replaceChildren(clientsTable(data.clients || []));
    }
    const refresh = ({ silent = false } = {}) => guarded(ctx, body, load, silent);
    refresh();
    getOverview().catch(() => {}); // warm up defaults for the create dialog
    return { refresh };
  }

  function perSandboxText(client) {
    const d = client.defaults || {};
    const e = d.effective || {};
    const own = [d.cpus, d.memoryMb, d.diskGb].filter((v) => v !== null && v !== undefined).length;
    const source = own === 0 ? "globaler Standard" : own === 3 ? "eigene Werte" : "teilweise Standard";
    return h("div", null,
      h("div", { class: "num" }, `${fmtNum(e.cpus, 2)} CPU · ${fmtMb(e.memoryMb)} · ${fmtNum(e.diskGb, 0)} GB`),
      h("div", { class: "cell-sub" }, source));
  }

  function persistentPolicyText(client) {
    const n = (client.usage || {}).persistent || 0;
    const count = n ? `${fmtNum(n, 0)} persistent` : null;
    if (client.allowPersistent) {
      return h("div", null, h("span", { class: "badge ok" }, "erlaubt"), count ? h("div", { class: "cell-sub" }, count) : null);
    }
    return h("div", null, h("span", { class: "badge" }, "gesperrt"),
      count ? h("div", { class: "cell-sub", title: "Vor der Sperre persistent gesetzt; bleiben erhalten" }, count) : null);
  }

  function softwareNetText(client) {
    return h("div", null,
      client.profile
        ? h("a", { href: "#/profiles/" + enc(client.profile.id), title: "Software-Profil" }, client.profile.name)
        : h("span", { class: "muted", title: "Mitgeliefertes Sandbox-Image" }, "Standard"),
      h("div", { class: "cell-sub" }, client.allowInternet === false
        ? h("span", { class: "badge warn" }, "ohne Internet")
        : "mit Internet"),
      (client.allowNets || []).length ? h("div", { class: "cell-sub" }, h("span", {
        class: "badge", title: "Eigene interne Ziele: " + client.allowNets.join(", "),
      }, `${client.allowNets.length} interne${client.allowNets.length === 1 ? "s Ziel" : " Ziele"}`)) : null,
      client.legacyAccess ? h("div", { class: "cell-sub" }, h("span", {
        class: "badge warn", title: "MCP und Dateien ohne API-Key, Desktop ohne Einmal-Link (Client-Einstellungen)",
      }, "alte Anbindung")) : null);
  }

  function clientsTable(list) {
    const head = h("thead", null, h("tr", null,
      h("th", null, "Client"),
      h("th", null, "Angelegt"),
      h("th", null, "Sandboxen"),
      h("th", null, "Laufend"),
      h("th", null, "CPU (laufend)"),
      h("th", null, "RAM (laufend)"),
      h("th", null, "Disk (alle)"),
      h("th", null, "Limit pro Sandbox"),
      h("th", null, "Software · Netz"),
      h("th", null, "Persistenz")));
    const rows = list.map((c) => {
      const u = c.usage || {};
      const q = c.quota || {};
      return h("tr", null,
        h("td", null,
          h("a", { href: "#/clients/" + enc(c.id), class: "cell-main" }, c.label || `Client ${c.id}`),
          h("div", { class: "cell-sub mono" }, "Key " + (c.keyHashPrefix || "") + "…")),
        h("td", null, timeEl(c.createdAt)),
        h("td", null, usageCell(u.sandboxes, q.sandboxes, (n) => fmtNum(n, 0))),
        h("td", null, usageCell(u.running, q.running, (n) => fmtNum(n, 0))),
        h("td", null, usageCell(u.cpus, q.cpus, (n) => fmtNum(n, 2))),
        h("td", null, usageCell(u.memoryMb, q.memoryMb, fmtMb)),
        h("td", null, usageCell(u.diskGb, q.diskGb, (n) => fmtNum(n, 0) + " GB")),
        h("td", null, perSandboxText(c)),
        h("td", null, softwareNetText(c)),
        h("td", null, persistentPolicyText(c)));
    });
    if (!rows.length) {
      rows.push(h("tr", null, h("td", { class: "empty", colspan: "10" },
        "Noch keine Clients angelegt. Mit „Client anlegen“ wird ein Client samt API-Key erzeugt.")));
    }
    return h("div", { class: "table-wrap" }, h("table", { class: "data" }, head, h("tbody", null, rows)));
  }

  async function openCreateClient() {
    const [ov, profiles] = await Promise.all([getOverview().catch(() => null), getProfiles().catch(() => [])]);
    const label = h("input", { type: "text", name: "label", required: true, maxlength: "200", autocomplete: "off" });
    const limits = clientLimitFields(null, ov);
    const policy = persistentPolicyField(null, ov);
    const internet = internetPolicyField(null);
    const legacy = legacyAccessField(null);
    const nets = allowNetsField(null);
    const profile = profileSelectField(null, profiles);
    const created = await modal({
      title: "Client anlegen",
      wide: true,
      submitLabel: "Client anlegen",
      body: [
        h("div", { class: "form-row" }, field("Bezeichnung", label, "z. B. Kunde, Team oder Anwendung"), profile.node,
          policy.node, internet.node),
        h("div", { class: "form-row" }, legacy.node, nets.node),
        limits.nodes,
      ],
      onSubmit: async () => {
        const text = label.value.trim();
        if (!text) {
          label.setCustomValidity("Bitte eine Bezeichnung angeben.");
          label.reportValidity();
          label.setCustomValidity("");
          return false;
        }
        return api("clients", {
          method: "POST",
          body: {
            label: text, ...limits.values(), allowPersistent: policy.value(), allowInternet: internet.value(),
            profileId: profile.value(), legacyAccess: legacy.value(), allowNets: nets.value(),
          },
        });
      },
    });
    if (!created || !created.client) return;
    toast(`Client „${created.client.label}“ wurde angelegt.`, { type: "success" });
    await showKeyDialog(created.apiKey, {
      title: "API-Key für „" + created.client.label + "“",
      intro: "Der Client wurde angelegt. Bitte den API-Key jetzt kopieren und sicher übergeben.",
    });
    location.hash = "#/clients/" + enc(created.client.id);
  }

  // ------------------------------------------------------------------------
  // view: Client-Detail
  // ------------------------------------------------------------------------

  function viewClientDetail(root, params, ctx) {
    const id = Number.parseInt(params.id, 10);
    root.append(h("a", { class: "back-link", href: "#/clients" }, "← Alle Clients"));
    const head = h("div", null, loadingEl());
    const quotaBox = h("div");
    const formBox = h("div");
    const sbxTitle = h("h2", null, "Sandboxen");
    const sbxBox = h("div");
    const sections = h("div", { hidden: true },
      h("section", { class: "section" }, quotaBox),
      h("section", { class: "section" }, h("div", { class: "section-head" }, sbxTitle,
        h("span", { class: "hint" }, "Start und Neustart warten auf den MCP Hub und können bis zu 2 Minuten dauern.")), sbxBox),
      h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", null, "Einstellungen")), formBox));
    root.append(head, sections);

    if (!Number.isInteger(id)) {
      head.replaceChildren(notice("danger", "Ungültige Client-ID."));
      return null;
    }

    let client = null;
    let formRendered = false;

    function renderHead() {
      const rotateBtn = h("button", { type: "button", class: "btn" }, "API-Key erneuern");
      rotateBtn.addEventListener("click", rotateKey);
      const deleteBtn = h("button", { type: "button", class: "btn danger-outline" }, "Client löschen");
      deleteBtn.addEventListener("click", deleteClient);
      setTitle(client.label || `Client ${client.id}`);
      head.replaceChildren(h("div", { class: "page-head" },
        h("div", null,
          h("h1", null, client.label || `Client ${client.id}`),
          h("div", { class: "meta-line" },
            h("span", null, "ID ", h("span", { class: "mono" }, String(client.id))),
            h("span", null, "Key-Hash ", h("span", { class: "mono" }, (client.keyHashPrefix || "") + "…")),
            h("span", null, "angelegt ", timeEl(client.createdAt)),
            client.updatedAt ? h("span", null, "geändert ", timeEl(client.updatedAt)) : null)),
        h("div", { class: "actions" }, rotateBtn, deleteBtn)));
    }

    function renderQuota() {
      const u = client.usage || {};
      const q = client.quota || {};
      const d = client.defaults || {};
      const e = d.effective || {};
      const limitText = (used, limit, fmt) =>
        `${fmt(used)} von ${limit === null || limit === undefined ? "unbegrenzt" : fmt(limit)}`
        + (limit === null || limit === undefined ? "" : ` (${fmtPct(ratioOf(used, limit))})`);
      const src = (v) => (v === null || v === undefined ? h("span", { class: "faint" }, " (Standard)") : null);
      quotaBox.replaceChildren(
        h("div", { class: "section-head" }, h("h2", null, "Kontingent")),
        h("div", { class: "panel" }, h("div", { class: "panel-body" },
          h("div", { class: "grid cols-5" },
            metric("Sandboxen", limitText(u.sandboxes, q.sandboxes, (n) => fmtNum(n, 0)), u.sandboxes, q.sandboxes),
            metric("Laufend", limitText(u.running, q.running, (n) => fmtNum(n, 0)), u.running, q.running),
            metric("CPU (laufend)", limitText(u.cpus, q.cpus, (n) => fmtNum(n, 2)), u.cpus, q.cpus),
            metric("RAM (laufend)", limitText(u.memoryMb, q.memoryMb, fmtMb), u.memoryMb, q.memoryMb),
            metric("Disk (alle)", limitText(u.diskGb, q.diskGb, (n) => fmtNum(n, 0) + " GB"), u.diskGb, q.diskGb)),
          h("p", { class: "metric-note mt-12" },
            "Limit pro Sandbox: ",
            h("strong", null, fmtCores(e.cpus)), src(d.cpus), " · ",
            h("strong", null, fmtMb(e.memoryMb) + " RAM"), src(d.memoryMb), " · ",
            h("strong", null, fmtNum(e.diskGb, 0) + " GB Disk"), src(d.diskGb),
            h("span", { class: "sep" }, "|"),
            "Persistente Sandboxen: ",
            h("strong", null, client.allowPersistent ? "erlaubt" : "gesperrt"),
            u.persistent ? ` (${fmtNum(u.persistent, 0)} persistent${client.allowPersistent ? "" : ", vor der Sperre gesetzt"})` : null,
            h("span", { class: "sep" }, "|"),
            "Software: ",
            client.profile
              ? h("a", { href: "#/profiles/" + enc(client.profile.id) }, h("strong", null, client.profile.name))
              : h("strong", null, "Standard"),
            h("span", { class: "sep" }, "|"),
            "Internet: ",
            h("strong", null, client.allowInternet === false ? "gesperrt" : "erlaubt"),
            h("span", { class: "sep" }, "|"),
            "Anbindung: ",
            h("strong", null, client.legacyAccess ? "alt (ohne API-Key für MCP und Dateien)" : "mit API-Key und Benutzer"),
            h("span", { class: "sep" }, "|"),
            "Interne Ziele: ",
            h("strong", null, (client.allowNets || []).length
              ? "global + " + client.allowNets.join(", ") : "nur die globalen"),
            client.netSubnet ? h("span", { class: "faint" }, ` (eigenes Netz ${client.netSubnet})`) : null))));
    }

    /** Asks before the internet access of a client with sandboxes is switched. */
    async function confirmInternetSwitch(allow) {
      const list = client.sandboxes || [];
      if (!list.length) return true;
      const running = list.filter((s) => s.status === "running").length;
      return confirmDialog({
        title: allow ? "Internetzugriff einschalten" : "Internetzugriff abschalten",
        message: [
          h("p", null, list.length === 1 ? "Die Sandbox von " : `Die ${list.length} Sandboxen von `,
            h("strong", null, client.label || `#${client.id}`),
            list.length === 1 ? " wird sofort umgestellt: " : " werden sofort umgestellt: ",
            allow ? "Verbindungen nach außen und Namensauflösung werden wieder möglich."
              : "Verbindungen nach außen und die Namensauflösung werden gesperrt."),
          notice("ok", "Daten bleiben erhalten.",
            "Die Container werden dafür neu aufgebaut; Home-Verzeichnis, Uploads und Ports bleiben gleich, "
            + "VNC-Passwort und interne Tokens werden erneuert."
            + (running ? ` ${running} laufende Sandbox${running === 1 ? " ist" : "en sind"} dabei kurz nicht erreichbar.` : "")),
        ],
        confirmLabel: "Speichern und umstellen",
      });
    }

    /** The first own target moves the client's sandboxes into an own network - ask first. */
    async function confirmOwnNetwork(list) {
      const sandboxesOf = client.sandboxes || [];
      if (client.netSubnet || !list.length || !sandboxesOf.length) return true;
      const running = sandboxesOf.filter((s) => s.status === "running").length;
      return confirmDialog({
        title: "Eigenes Netz für den Client",
        message: [
          h("p", null, "Für eigene interne Ziele bekommt ",
            h("strong", null, client.label || `#${client.id}`),
            " ein eigenes Netz. ",
            sandboxesOf.length === 1 ? "Die Sandbox wird" : `Die ${sandboxesOf.length} Sandboxen werden`,
            " sofort umgehängt, ohne Neuaufbau."),
          running ? notice("warn", null, `${running} laufende Sandbox${running === 1 ? " ist" : "en sind"} `
            + "dabei für einige Sekunden ohne Verbindung; offene Verbindungen der Sandbox nach außen brechen ab.") : null,
        ],
        confirmLabel: "Speichern und umhängen",
      });
    }

    async function renderForm() {
      formRendered = true;
      const [ov, profiles] = await Promise.all([getOverview().catch(() => null), getProfiles().catch(() => [])]);
      if (!ctx.alive()) return;
      const label = h("input", {
        type: "text", name: "label", required: true, maxlength: "200", autocomplete: "off", value: client.label || "",
      });
      label.dataset.initial = client.label || "";
      const limits = clientLimitFields(client, ov);
      const policy = persistentPolicyField(client, ov);
      const internet = internetPolicyField(client);
      const legacy = legacyAccessField(client);
      const nets = allowNetsField(client);
      const profile = profileSelectField(client, profiles);
      const saveBtn = h("button", { type: "submit", class: "btn primary" }, "Änderungen speichern");
      const resetBtn = h("button", { type: "button", class: "btn" }, "Zurücksetzen");
      const status = h("span", { class: "state" });
      const form = h("form", { class: "panel panel-body" },
        h("div", { class: "form-row" }, field("Bezeichnung", label), profile.node, policy.node, internet.node),
        h("div", { class: "form-row" }, legacy.node, nets.node),
        limits.nodes,
        h("div", { class: "form-actions" }, saveBtn, resetBtn, status));
      resetBtn.addEventListener("click", () => renderForm());
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        if (!form.checkValidity()) {
          form.reportValidity();
          return;
        }
        const payload = limits.changedValues();
        if (policy.changed()) payload.allowPersistent = policy.value();
        if (profile.changed()) payload.profileId = profile.value();
        if (legacy.changed()) payload.legacyAccess = legacy.value();
        if (nets.changed()) {
          if (!(await confirmOwnNetwork(nets.value()))) return;
          payload.allowNets = nets.value();
        }
        if (internet.changed()) {
          if (!(await confirmInternetSwitch(internet.value()))) return;
          payload.allowInternet = internet.value();
        }
        if (changed(label)) {
          const text = label.value.trim();
          if (!text) {
            label.setCustomValidity("Bitte eine Bezeichnung angeben.");
            label.reportValidity();
            label.setCustomValidity("");
            return;
          }
          payload.label = text;
        }
        if (!Object.keys(payload).length) {
          toast("Keine Änderungen.", { type: "info" });
          return;
        }
        saveBtn.disabled = true;
        saveBtn.classList.add("busy");
        try {
          const data = await api("clients/" + id, { method: "PATCH", body: payload });
          if (!ctx.alive()) return;
          client = { ...data.client, sandboxes: client.sandboxes };
          if (data.client.networkPolicy === "applying") {
            toast("Client gespeichert. Die Sandboxen werden jetzt umgestellt – das kann etwas dauern.",
              { type: "success" });
            setTimeout(() => { if (ctx.alive()) refresh({ silent: true }); }, 3000);
          } else {
            toast("Client gespeichert.", { type: "success" });
          }
          renderHead();
          renderQuota();
          renderForm();
          // persistence switches, profile and network state in the sandbox table
          if (["allowPersistent", "profileId", "allowInternet", "allowNets"].some((k) => k in payload)) {
            refresh({ silent: true });
          }
        } catch (err) {
          showError(err, "Speichern fehlgeschlagen");
        } finally {
          saveBtn.disabled = false;
          saveBtn.classList.remove("busy");
        }
      });
      formBox.replaceChildren(form);
    }

    function renderSandboxes() {
      const list = client.sandboxes || [];
      sbxTitle.textContent = `Sandboxen (${list.length})`;
      sbxBox.replaceChildren(sandboxTable(list, {
        showClient: false,
        emptyText: "Dieser Client hat keine Sandboxen. Sandboxen werden über die Client-API (POST /containers) erstellt.",
      }));
    }

    async function load() {
      const data = await api("clients/" + id);
      if (!ctx.alive()) return;
      client = data.client;
      renderHead();
      renderQuota();
      renderSandboxes();
      sections.hidden = false;
      if (!formRendered) renderForm();
    }

    async function rotateKey() {
      const result = await confirmDialog({
        title: "API-Key erneuern",
        message: [
          h("p", null, "Für ", h("strong", null, client.label), " wird ein neuer API-Key erzeugt."),
          notice("warn", "Der bisherige Key wird sofort ungültig.",
            "Anwendungen, die ihn verwenden, verlieren den Zugriff, bis der neue Key hinterlegt ist. "
            + "Bestehende Sandboxen bleiben erhalten."),
        ],
        confirmLabel: "Neuen Key erzeugen",
        danger: true,
        onConfirm: () => api(`clients/${id}/rotate-key`, { method: "POST" }),
      });
      if (!result || !result.apiKey) return;
      await showKeyDialog(result.apiKey, {
        title: "Neuer API-Key für „" + client.label + "“",
        intro: "Der alte Key ist ab sofort ungültig.",
      });
      refresh({ silent: true });
    }

    async function deleteClient() {
      const names = (client.sandboxes || []).map((s) => s.name);
      const confirmWord = (client.label || "").trim() || "LÖSCHEN";
      const result = await confirmDialog({
        title: "Client löschen",
        message: [
          h("p", null, "Der Client ", h("strong", null, client.label || `#${client.id}`),
            " wird dauerhaft gelöscht, sein API-Key wird ungültig."),
          notice("danger", names.length
            ? `Alle ${names.length} Sandbox${names.length === 1 ? "" : "en"} dieses Clients und ihre Daten werden unwiderruflich gelöscht.`
            : "Der Client hat derzeit keine Sandboxen.",
          names.length ? h("ul", null, names.slice(0, 12).map((n) => h("li", { class: "mono" }, n)),
            names.length > 12 ? h("li", null, `… und ${names.length - 12} weitere`) : null) : null,
          names.length ? "Laufende Sandboxen werden dabei gestoppt; Home-Verzeichnisse und Disk-Images werden entfernt." : null),
        ],
        confirmLabel: "Client endgültig löschen",
        danger: true,
        requireText: confirmWord,
        onConfirm: () => api("clients/" + id, { method: "DELETE" }),
      });
      if (!result) {
        refresh({ silent: true });
        return;
      }
      const removed = (result.removedSandboxes || []).length;
      toast(`Client „${client.label}“ gelöscht` + (removed ? ` (${removed} Sandbox${removed === 1 ? "" : "en"} entfernt).` : "."),
        { type: "success" });
      location.hash = "#/clients";
    }

    const refresh = ({ silent = false } = {}) => guarded(ctx, head, load, silent && client !== null);
    refresh();
    return { refresh, rerender: () => { if (client) renderSandboxes(); } };
  }

  // ------------------------------------------------------------------------
  // sandbox table (client detail + all sandboxes)
  // ------------------------------------------------------------------------

  const STATUS = {
    running: ["läuft", "ok"],
    stopped: ["gestoppt", ""],
    creating: ["wird erstellt", "accent busy"],
    removing: ["wird gelöscht", "danger busy"],
    rebuilding: ["wird neu aufgebaut", "accent busy"],
    error: ["Fehler", "danger"],
  };
  const PENDING_LABEL = {
    start: "startet …", stop: "stoppt …", restart: "startet neu …", delete: "wird gelöscht …",
    recreate: "baut neu auf …",
  };
  const OUTDATED_LABEL = {
    image: "älteres Image (Sandbox-Image oder Software-Profil)",
    hardening: "ohne aktuelle Container-Härtung",
    network: "Netz passt nicht zur Client-Einstellung",
    auth: "ohne Zugangsschutz für MCP und Dateien (interne Tokens)",
  };

  function outdatedBadge(s) {
    const reasons = (s.outdated || []).map((r) => OUTDATED_LABEL[r] || r);
    if (!reasons.length) return null;
    return h("div", null, h("span", {
      class: "badge warn",
      title: "Veraltet: " + reasons.join(", ") + ". „Neu aufbauen“ bringt die Sandbox auf den aktuellen Stand "
        + "(Daten und Ports bleiben erhalten, Zugangsdaten werden erneuert).",
    }, "veraltet"));
  }

  function statusLabel(status) {
    return (STATUS[status] || [status || "unbekannt"])[0];
  }

  function statusBadge(status, pending) {
    if (pending) return h("span", { class: "badge accent busy" }, PENDING_LABEL[pending.action] || "…");
    const [label, cls] = STATUS[status] || [status || "unbekannt", "warn"];
    return h("span", { class: ["badge", cls] }, label);
  }

  function limitsCell(s) {
    const r = s.resources || {};
    const legacy = r.storage === "volume";
    const none = (r.cpus === null || r.cpus === undefined)
      && (r.memoryMb === null || r.memoryMb === undefined)
      && (r.diskGb === null || r.diskGb === undefined);
    if (none) {
      return h("div", null, h("span", { class: "muted" }, "ohne Limit"),
        legacy ? h("div", { class: "cell-sub" }, "Legacy (Docker-Volume)") : null);
    }
    const cpu = isNum(r.cpus) ? fmtNum(r.cpus, 2) + " CPU" : "CPU ∞";
    const mem = isNum(r.memoryMb) ? fmtMb(r.memoryMb) : "RAM ∞";
    const disk = isNum(r.diskGb) ? fmtNum(r.diskGb, 0) + " GB" : "Disk ∞";
    return h("div", null,
      h("div", { class: "num" }, `${cpu} · ${mem} · ${disk}`),
      legacy ? h("div", { class: "cell-sub" }, "Legacy (Docker-Volume), Disk ohne Limit") : null);
  }

  function diskCell(s) {
    const u = s.usage || {};
    const r = s.resources || {};
    const used = u.diskUsedBytes;
    const title = (u.updatedAt ? "Gemessen " + fmtDate(u.updatedAt, true) : "Noch nicht gemessen")
      + ". Container-Layer = beschreibbare Schicht außerhalb des Home-Verzeichnisses.";
    const layer = isNum(u.rootfsUsedBytes) ? h("div", { class: "cell-sub" }, "Layer " + fmtBytes(u.rootfsUsedBytes)) : null;
    if (isNum(r.diskGb) && r.diskGb > 0) {
      return h("div", { class: "usage", title },
        h("div", { class: "usage-text" }, fmtBytes(used), h("span", { class: "of" }, " / " + fmtNum(r.diskGb, 0) + " GB")),
        bar(isNum(used) ? used : 0, r.diskGb * GIB),
        layer);
    }
    return h("div", { title }, h("div", { class: "usage-text" }, fmtBytes(used)), layer);
  }

  function activityCell(s) {
    return h("div", { class: "nowrap" },
      h("div", { title: "Letzte Aktivität" }, timeEl(s.lastActivityAt, true)),
      h("div", { class: "cell-sub" }, "angelegt ", timeEl(s.createdAt)));
  }

  function sandboxTable(list, { showClient = false, emptyText = "Keine Sandboxen." } = {}) {
    const cols = ["Sandbox", showClient ? "Client" : null, "Status", "Aktivität", "Persistent",
      "Limits (CPU · RAM · Disk)", "Disk belegt", "Aktionen"].filter(Boolean);
    const head = h("thead", null, h("tr", null, cols.map((c) => h("th", null, c))));
    const rows = list.map((s) => sandboxRow(s, showClient));
    if (!rows.length) rows.push(h("tr", null, h("td", { class: "empty", colspan: String(cols.length) }, emptyText)));
    return h("div", { class: "table-wrap" }, h("table", { class: "data sandboxes" }, head, h("tbody", null, rows)));
  }

  function sandboxRow(s, showClient) {
    const pending = state.pending.get(s.name) || null;
    // The desktop is only reachable while the sandbox runs; it opens with a one-time link.
    let nameEl;
    if (s.status === "running") {
      nameEl = h("a", {
        href: "#", class: "sbx-name", role: "button",
        title: "Desktop in neuem Tab öffnen (Einmal-Link)",
      }, s.name, " ↗");
      nameEl.addEventListener("click", (ev) => {
        ev.preventDefault();
        openDesktop(s.name);
      });
    } else {
      nameEl = h("span", {
        class: "sbx-name",
        title: s.status === "stopped" ? "Desktop erst nach dem Start erreichbar" : null,
      }, s.name);
    }
    const nameCell = h("td", null,
      nameEl,
      (s.profile || s.internet === false || s.user) ? h("div", { class: "cell-tags" },
        s.user ? h("span", { class: "tag", title: "Gebunden an diesen ALBERT-Benutzer (Pseudonym)" },
          "Benutzer " + s.user) : null,
        s.profile ? h("span", { class: "tag", title: "Software-Profil des Clients" }, s.profile.name) : null,
        s.internet === false ? h("span", { class: "tag warn", title: "Kein Internetzugriff, keine Namensauflösung" },
          "ohne Internet") : null) : null,
      s.lastError ? h("div", { class: "err-line", title: s.lastError }, "Fehler: " + s.lastError) : null);

    const clientCell = showClient
      ? h("td", null, s.clientId !== null && s.clientId !== undefined
        ? h("a", { href: "#/clients/" + enc(s.clientId) }, s.clientLabel || `Client ${s.clientId}`)
        : h("span", { class: "faint" }, "–"))
      : null;

    // Switching off is always possible; switching on needs the client's permission.
    const locked = !s.persistent && s.persistentAllowed === false;
    const persistent = h("input", {
      type: "checkbox", class: "switch", checked: !!s.persistent,
      disabled: !!pending || s.status === "removing" || locked,
      "aria-label": `Sandbox ${s.name} persistent`,
      title: locked
        ? "Persistente Sandboxen sind für diesen Client nicht freigegeben (Client-Einstellungen)."
        : "Persistente Sandboxen werden nicht nach Ablauf der maximalen Lebensdauer automatisch gelöscht "
          + "(Stopp bei Inaktivität gilt weiterhin).",
    });
    persistent.addEventListener("change", () => togglePersistent(s, persistent));

    return h("tr", null,
      nameCell,
      clientCell,
      h("td", null, statusBadge(s.status, pending), pending ? null : outdatedBadge(s)),
      h("td", null, activityCell(s)),
      h("td", null, persistent),
      h("td", null, limitsCell(s)),
      h("td", null, diskCell(s)),
      h("td", { class: "actions" }, sandboxActions(s, pending)));
  }

  /** Opens the desktop of a sandbox with a fresh one-time link (valid once, for two minutes). */
  async function openDesktop(name) {
    // Open the tab right away (popup blockers allow it only during the click), then point it to the link.
    const win = window.open("", "_blank");
    if (win) win.opener = null;
    try {
      const data = await api(`sandboxes/${enc(name)}/desktop`, { method: "POST" });
      const url = safeUrl(data.desktop && data.desktop.url);
      if (!url) throw new Error("Kein Desktop-Link erhalten.");
      if (win) win.location.href = url;
      else window.location.href = url;
    } catch (err) {
      if (win) win.close();
      showError(err, "Desktop nicht geöffnet");
    }
  }

  // Inline SVG icons (24x24, stroke = currentColor); built with DOM APIs because of the CSP.
  const ICONS = {
    start: [["path", { d: "M7 4.5v15l12-7.5z" }]],
    stop: [["rect", { x: "6", y: "6", width: "12", height: "12", rx: "1.5" }]],
    restart: [["path", { d: "M20 12a8 8 0 1 1-2.34-5.66" }], ["path", { d: "M20 4v5h-5" }]],
    limits: [["path", { d: "M4 6h8M16 6h4M4 12h3M11 12h9M4 18h10M18 18h2" }],
      ["circle", { cx: "14", cy: "6", r: "2" }], ["circle", { cx: "9", cy: "12", r: "2" }],
      ["circle", { cx: "16", cy: "18", r: "2" }]],
    rebuild: [["path", { d: "M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94"
      + "l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" }]],
    live: [["path", { d: "M3 12h4l3-8 4 16 3-8h4" }]],
    build: [["path", { d: "M21 8 12 3 3 8l9 5 9-5z" }], ["path", { d: "M3 8v8l9 5 9-5V8" }], ["path", { d: "M12 13v8" }]],
    remove: [["path", { d: "M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3" }]],
  };

  function icon(name) {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    for (const [k, v] of Object.entries({
      viewBox: "0 0 24 24", width: "16", height: "16", fill: "none", stroke: "currentColor",
      "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true",
    })) svg.setAttribute(k, v);
    for (const [tag, attrs] of ICONS[name] || []) {
      const el = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
      svg.append(el);
    }
    return svg;
  }

  function sandboxActions(s, pending) {
    const busy = !!pending;
    const transitional = s.status === "creating" || s.status === "removing" || s.status === "rebuilding";
    const running = s.status === "running";
    const mk = (iconName, label, cls, onClick, { disabled = false, busyFor = null, title = null } = {}) => {
      const isBusy = busy && pending.action === busyFor;
      const b = h("button", {
        type: "button",
        class: ["btn small icon-only", cls, isBusy && "busy"],
        disabled: busy || disabled,
        title: title ? `${label} – ${title}` : label,
        "aria-label": label,
      }, isBusy ? null : icon(iconName));
      b.addEventListener("click", onClick);
      return b;
    };
    const buttons = [];
    if (running || (busy && pending.action === "stop")) {
      buttons.push(mk("stop", "Stoppen", "", () => sandboxAction(s, "stop"), { disabled: transitional, busyFor: "stop" }));
    } else {
      buttons.push(mk("start", "Starten", "", () => sandboxAction(s, "start"), {
        disabled: transitional, busyFor: "start", title: "wartet auf den MCP Hub (bis zu ca. 2 Minuten)",
      }));
    }
    buttons.push(mk("restart", "Neu starten", "", () => sandboxAction(s, "restart"), {
      disabled: transitional || !running, busyFor: "restart",
    }));
    buttons.push(mk("limits", "Limits ändern", "", () => openLimitsDialog(s), {
      disabled: transitional || s.status === "error",
    }));
    buttons.push(mk("rebuild", "Neu aufbauen", (s.outdated || []).length || s.status === "error" ? "primary" : "",
      () => recreateSandbox(s), {
        disabled: transitional, busyFor: "recreate",
        title: "Container mit aktuellem Image, Härtung und Netz neu erstellen; Daten und Ports bleiben, "
          + "Zugangsdaten werden erneuert",
      }));
    buttons.push(mk("live", "Live-Werte", "", () => openStatsDialog(s), { title: "CPU, RAM, Netz (docker stats)" }));
    buttons.push(mk("remove", "Löschen", "danger-outline", () => deleteSandbox(s), {
      disabled: s.status === "creating", busyFor: "delete",
    }));
    return h("div", { class: "btn-row" }, buttons);
  }

  async function sandboxAction(s, action) {
    if (state.pending.has(s.name)) return;
    state.pending.set(s.name, { action, since: Date.now() });
    rerenderView();
    const done = { start: "gestartet", stop: "gestoppt", restart: "neu gestartet" }[action];
    try {
      await api(`sandboxes/${enc(s.name)}/${action}`, { method: "POST" });
      toast(`Sandbox ${s.name} ${done}.`, { type: "success" });
    } catch (err) {
      showError(err, `${{ start: "Start", stop: "Stopp", restart: "Neustart" }[action]} fehlgeschlagen`);
    } finally {
      state.pending.delete(s.name);
      rerenderView();
      refreshView();
    }
  }

  async function recreateSandbox(s) {
    if (state.pending.has(s.name)) return;
    const reasons = (s.outdated || []).map((r) => OUTDATED_LABEL[r] || r);
    const ok = await confirmDialog({
      title: "Sandbox neu aufbauen",
      message: [
        h("p", null, "Der Container von ", h("strong", { class: "mono" }, s.name),
          " wird mit dem aktuellen Image des Clients (", s.profile ? `Profil „${s.profile.name}“` : "Standard",
          "), der aktuellen Härtung und im passenden Netz (",
          s.internet === false ? "ohne Internet" : "mit Internet", ") neu erstellt."),
        reasons.length ? h("p", null, "Grund: " + reasons.join(", ") + ".") : null,
        notice("ok", "Daten bleiben erhalten.",
          "Home-Verzeichnis, Uploads, Ports und Desktop-URL bleiben gleich; VNC-Passwort und interne Tokens werden "
          + "erneuert, ein offener Desktop muss neu geöffnet werden. "
          + (s.status === "running"
            ? "Die Sandbox ist dabei kurz nicht erreichbar, laufende Programme im Desktop werden beendet."
            : "Die Sandbox bleibt danach gestoppt.")),
      ],
      confirmLabel: "Neu aufbauen",
    });
    if (!ok) return;
    state.pending.set(s.name, { action: "recreate", since: Date.now() });
    rerenderView();
    try {
      await api(`sandboxes/${enc(s.name)}/recreate`, { method: "POST" });
      toast(`Sandbox ${s.name} neu aufgebaut.`, { type: "success" });
    } catch (err) {
      showError(err, "Neu aufbauen fehlgeschlagen");
    } finally {
      state.pending.delete(s.name);
      rerenderView();
      refreshView();
    }
  }

  async function togglePersistent(s, input) {
    const value = input.checked;
    input.disabled = true;
    try {
      await api("sandboxes/" + enc(s.name), { method: "PATCH", body: { persistent: value } });
      s.persistent = value;
      toast(`Sandbox ${s.name} ist ${value ? "jetzt persistent" : "nicht mehr persistent"}.`, { type: "success" });
    } catch (err) {
      input.checked = !value;
      showError(err, "Änderung fehlgeschlagen");
    } finally {
      input.disabled = false;
    }
  }

  async function openLimitsDialog(s) {
    const ov = await getOverview().catch(() => null);
    const host = (ov && ov.host) || {};
    const r = s.resources || {};
    const legacyVolume = r.storage === "volume";
    const cpus = numInput({
      name: "cpus", value: isNum(r.cpus) ? String(r.cpus) : "", min: "0.1",
      max: isNum(host.cpus) ? host.cpus : undefined, placeholder: isNum(r.cpus) ? "" : "ohne Limit", unit: "Kerne",
    });
    const mem = numInput({
      name: "memory", value: mbToGbInput(r.memoryMb), min: "0.5",
      max: isNum(host.memoryMb) && host.memoryMb > 0 ? Math.ceil((host.memoryMb / 1024) * 1000) / 1000 : undefined,
      placeholder: isNum(r.memoryMb) ? "" : "ohne Limit", unit: "GB",
    });
    const disk = numInput({
      name: "disk", value: isNum(r.diskGb) ? String(r.diskGb) : "", min: String(isNum(r.diskGb) ? r.diskGb : 1),
      step: "1", disabled: legacyVolume, placeholder: legacyVolume ? "ohne Limit" : "", unit: "GB",
    });
    await modal({
      title: "Limits ändern – " + s.name,
      wide: true,
      submitLabel: "Übernehmen",
      body: [
        h("div", { class: "field-grid" },
          field("CPU", cpus.node, isNum(host.cpus) ? `max. ${fmtNum(host.cpus, 0)} (Host)` : null),
          field("RAM", mem.node, "min. 0,5 GB"),
          field("Disk", disk.node, legacyVolume ? "Legacy-Sandbox (Docker-Volume)"
            : `nur vergrößern (aktuell ${fmtNum(r.diskGb, 0)} GB)`)),
        h("p", { class: "muted small mt-12" },
          "CPU und RAM werden sofort übernommen, auch bei laufender Sandbox. Die Disk kann nur vergrößert werden. "
          + "Admins dürfen das Limit pro Sandbox des Clients überschreiten; das Gesamtkontingent wird geprüft."),
        legacyVolume ? notice("warn", null,
          "Legacy-Sandboxen mit Docker-Volume haben kein Disk-Limit. Für ein Limit die Sandbox neu erstellen.") : null,
      ],
      onSubmit: async () => {
        const body = {};
        if (changed(cpus.input) && readNumber(cpus.input) !== null) {
          body.cpus = Math.round(readNumber(cpus.input) * 100) / 100;
        }
        if (changed(mem.input) && readNumber(mem.input) !== null) {
          body.memoryMb = Math.round(readNumber(mem.input) * 1024);
        }
        if (!legacyVolume && changed(disk.input) && readNumber(disk.input) !== null) {
          const gb = Math.round(readNumber(disk.input));
          if (isNum(r.diskGb) && gb < r.diskGb) throw new Error("Die Disk kann nur vergrößert werden.");
          body.diskGb = gb;
        }
        if (!Object.keys(body).length) return true;
        await api("sandboxes/" + enc(s.name), { method: "PATCH", body });
        toast(`Limits von ${s.name} geändert.`, { type: "success" });
        refreshView();
        return true;
      },
    });
  }

  function parsePercent(text) {
    const n = Number.parseFloat(String(text || "").replace("%", "").replace(",", "."));
    return Number.isFinite(n) ? n : null;
  }

  function statTile(label, value, extra) {
    return h("div", { class: "stat" },
      h("div", { class: "stat-label" }, label),
      h("div", { class: "stat-value" }, value),
      extra);
  }

  function renderStats(data, s) {
    const st = data.stats;
    const disk = data.disk;
    const r = s.resources || {};
    const tiles = [];
    if (st) {
      const cpu = parsePercent(st.CPUPerc);
      const cpuHint = isNum(r.cpus) ? `100 % = 1 Kern · Limit ${fmtNum(r.cpus, 2)} = ${fmtNum(r.cpus * 100, 0)} %` : "100 % = 1 Kern";
      tiles.push(statTile("CPU", st.CPUPerc || "–",
        [isNum(r.cpus) && cpu !== null ? bar(cpu, r.cpus * 100) : null, h("div", { class: "metric-note" }, cpuHint)]));
      const memPerc = parsePercent(st.MemPerc);
      tiles.push(statTile("RAM", st.MemUsage || "–",
        [memPerc !== null ? bar(memPerc, 100) : null, h("div", { class: "metric-note" }, (st.MemPerc || "–") + " des Limits")]));
      tiles.push(statTile("Prozesse", st.PIDs || "–"));
      tiles.push(statTile("Netzwerk I/O", st.NetIO || "–", h("div", { class: "metric-note" }, "empfangen / gesendet")));
      tiles.push(statTile("Block I/O", st.BlockIO || "–", h("div", { class: "metric-note" }, "gelesen / geschrieben")));
    }
    if (disk) {
      tiles.push(statTile("Disk (Home)", `${fmtBytes(disk.usedBytes)} / ${fmtBytes(disk.sizeBytes)}`,
        [bar(disk.usedBytes, disk.sizeBytes), h("div", { class: "metric-note" }, fmtPct(ratioOf(disk.usedBytes, disk.sizeBytes)) + " belegt")]));
    } else {
      tiles.push(statTile("Disk (Home)", "–", h("div", { class: "metric-note" },
        r.storage === "volume" ? "Legacy-Volume: keine Live-Messung" : "keine Daten")));
    }
    return [
      st ? null : notice("warn", "Keine Container-Statistik verfügbar.", "Läuft der Container?"),
      h("div", { class: ["stat-grid", st ? null : "mt-12"] }, tiles),
      h("p", { class: "muted small mt-12" }, "Stand: " + new Date().toLocaleTimeString("de-DE")),
    ];
  }

  function openStatsDialog(s) {
    const content = h("div", null, loadingEl("Lade Live-Werte …"));
    const reloadBtn = h("button", { type: "button", class: "btn" }, "Aktualisieren");
    let loading = false;
    async function load() {
      if (loading) return;
      loading = true;
      reloadBtn.disabled = true;
      reloadBtn.classList.add("busy");
      try {
        const data = await api(`sandboxes/${enc(s.name)}/stats`);
        content.replaceChildren(...renderStats(data, s).filter(Boolean));
      } catch (err) {
        if (err.status !== 401) content.replaceChildren(h("div", { class: "form-error" }, errorText(err)));
      } finally {
        loading = false;
        reloadBtn.disabled = false;
        reloadBtn.classList.remove("busy");
      }
    }
    reloadBtn.addEventListener("click", load);
    modal({
      title: "Live-Werte – " + s.name,
      wide: true,
      body: content,
      submitLabel: null,
      cancelLabel: "Schließen",
      extraButtons: [reloadBtn],
    });
    load();
  }

  async function deleteSandbox(s) {
    await confirmDialog({
      title: "Sandbox löschen",
      message: [
        h("p", null, "Die Sandbox ", h("strong", { class: "mono" }, s.name),
          s.clientLabel ? [" von ", h("strong", null, s.clientLabel)] : null,
          " wird gestoppt und mit allen Daten (Home-Verzeichnis, Disk-Image, Route) unwiderruflich gelöscht."),
        s.persistent ? notice("warn", "Diese Sandbox ist als persistent markiert.") : null,
      ],
      confirmLabel: "Endgültig löschen",
      danger: true,
      onConfirm: async () => {
        state.pending.set(s.name, { action: "delete", since: Date.now() });
        rerenderView();
        try {
          await api("sandboxes/" + enc(s.name), { method: "DELETE" });
        } finally {
          state.pending.delete(s.name);
          rerenderView();
        }
        toast(`Sandbox ${s.name} gelöscht.`, { type: "success" });
        refreshView();
        return true;
      },
    });
  }

  // ------------------------------------------------------------------------
  // view: Sandboxen (alle)
  // ------------------------------------------------------------------------

  function viewSandboxes(root, _params, ctx) {
    const search = h("input", {
      type: "search", placeholder: "Filtern nach Name, Client, Status …", value: state.sbxFilter.text,
      "aria-label": "Sandboxen filtern",
    });
    const statusSel = h("select", { "aria-label": "Status filtern" },
      h("option", { value: "" }, "Alle Status"),
      Object.entries(STATUS).map(([key, [label]]) => h("option", { value: key }, label)));
    statusSel.value = state.sbxFilter.status;
    const count = h("span", { class: "count" });
    const tableBox = h("div", null, loadingEl());
    root.append(
      pageHead("Sandboxen", "Alle Sandboxen aller Clients. Start und Neustart können bis zu 2 Minuten dauern."),
      h("div", { class: "toolbar" }, search, statusSel, h("span", { class: "grow" }), count),
      tableBox);

    let list = null;

    function matches(s, tokens) {
      if (state.sbxFilter.status && s.status !== state.sbxFilter.status) return false;
      if (!tokens.length) return true;
      const hay = [s.name, s.clientLabel, s.status, statusLabel(s.status), String(s.clientId),
        s.persistent ? "persistent" : ""].join(" ").toLowerCase();
      return tokens.every((t) => hay.includes(t));
    }

    function rerender() {
      if (!list) return;
      const tokens = state.sbxFilter.text.toLowerCase().split(/\s+/).filter(Boolean);
      const filtered = list.filter((s) => matches(s, tokens));
      const running = list.filter((s) => s.status === "running").length;
      count.textContent = (filtered.length === list.length ? `${list.length} Sandboxen` : `${filtered.length} von ${list.length}`)
        + ` · ${running} laufend`;
      tableBox.replaceChildren(sandboxTable(filtered, {
        showClient: true,
        emptyText: list.length ? "Keine Sandbox passt zum Filter." : "Es gibt noch keine Sandboxen.",
      }));
    }

    async function load() {
      const data = await api("sandboxes");
      if (!ctx.alive()) return;
      list = data.sandboxes || [];
      rerender();
    }

    search.addEventListener("input", () => {
      state.sbxFilter.text = search.value;
      rerender();
    });
    statusSel.addEventListener("change", () => {
      state.sbxFilter.status = statusSel.value;
      rerender();
    });

    const refresh = ({ silent = false } = {}) => guarded(ctx, tableBox, load, silent && list !== null);
    refresh();
    getOverview().catch(() => {});
    return { refresh, rerender };
  }

  // ------------------------------------------------------------------------
  // view: Admins
  // ------------------------------------------------------------------------

  const USERNAME_PATTERN = "[A-Za-z0-9._@\\-]{3,64}";

  function passwordPair() {
    const pw = h("input", { type: "password", required: true, minlength: "8", maxlength: "256", autocomplete: "new-password" });
    const pw2 = h("input", { type: "password", required: true, minlength: "8", maxlength: "256", autocomplete: "new-password" });
    const check = () => pw2.setCustomValidity(pw2.value && pw.value !== pw2.value ? "Die Passwörter stimmen nicht überein." : "");
    pw.addEventListener("input", check);
    pw2.addEventListener("input", check);
    return {
      pw, pw2,
      nodes: [field("Neues Passwort", pw, "mindestens 8 Zeichen"), field("Passwort wiederholen", pw2)],
      valid() {
        check();
        if (!pw2.checkValidity()) {
          pw2.reportValidity();
          return false;
        }
        return true;
      },
    };
  }

  function viewAdmins(root, _params, ctx) {
    const addBtn = h("button", { type: "button", class: "btn primary" }, "Admin hinzufügen");
    root.append(pageHead("Admins", "Benutzer mit Zugriff auf diese Administrationsoberfläche", addBtn));
    const body = h("div", null, loadingEl());
    root.append(body);

    async function load() {
      const data = await api("admins");
      if (!ctx.alive()) return;
      body.replaceChildren(adminsTable(data.admins || []));
    }
    const refresh = ({ silent = false } = {}) => guarded(ctx, body, load, silent);

    function adminsTable(list) {
      const rows = list.map((a) => {
        const pwBtn = h("button", { type: "button", class: "btn small" }, "Passwort ändern");
        pwBtn.addEventListener("click", () => changePassword(a));
        const delBtn = h("button", {
          type: "button", class: "btn small danger-outline", disabled: !!a.self,
          title: a.self ? "Das eigene Konto kann nicht gelöscht werden." : null,
        }, "Löschen");
        delBtn.addEventListener("click", () => deleteAdmin(a));
        return h("tr", null,
          h("td", null, h("span", { class: "cell-main" }, a.username), a.self ? h("span", { class: "muted" }, " (du)") : null),
          h("td", null, timeEl(a.createdAt)),
          h("td", null, a.lastLoginAt ? timeEl(a.lastLoginAt) : h("span", { class: "faint" }, "noch nie")),
          h("td", { class: "actions" }, h("div", { class: "btn-row" }, pwBtn, delBtn)));
      });
      if (!rows.length) rows.push(h("tr", null, h("td", { class: "empty", colspan: "4" }, "Keine Admins.")));
      return h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("thead", null, h("tr", null,
          h("th", null, "Benutzername"), h("th", null, "Angelegt"), h("th", null, "Letzte Anmeldung"), h("th", null, "Aktionen"))),
        h("tbody", null, rows)));
    }

    async function addAdmin() {
      const user = h("input", {
        type: "text", required: true, pattern: USERNAME_PATTERN, maxlength: "64", autocomplete: "off",
        autocapitalize: "off", spellcheck: "false",
        title: "3–64 Zeichen: Buchstaben, Ziffern und . _ @ -",
      });
      const pair = passwordPair();
      const res = await modal({
        title: "Admin hinzufügen",
        submitLabel: "Admin anlegen",
        body: [field("Benutzername", user, "3–64 Zeichen: Buchstaben, Ziffern und . _ @ -"), pair.nodes],
        onSubmit: async () => {
          if (!pair.valid()) return false;
          return api("admins", { method: "POST", body: { username: user.value.trim(), password: pair.pw.value } });
        },
      });
      if (res && res.admin) {
        toast(`Admin ${res.admin.username} angelegt.`, { type: "success" });
        refresh({ silent: true });
      }
    }

    async function changePassword(a) {
      const current = a.self
        ? h("input", { type: "password", required: true, autocomplete: "current-password" })
        : null;
      const pair = passwordPair();
      const res = await modal({
        title: a.self ? "Eigenes Passwort ändern" : `Passwort für ${a.username} setzen`,
        submitLabel: "Passwort speichern",
        body: [
          a.self ? null : h("p", { class: "muted" }, "Das neue Passwort gilt ab sofort; bestehende Sitzungen bleiben angemeldet."),
          current ? field("Aktuelles Passwort", current) : null,
          pair.nodes,
        ],
        onSubmit: async () => {
          if (!pair.valid()) return false;
          const body = { password: pair.pw.value };
          if (current) body.currentPassword = current.value;
          await api(`admins/${enc(a.id)}/password`, { method: "POST", body });
          return true;
        },
      });
      if (res) toast("Passwort geändert.", { type: "success" });
    }

    async function deleteAdmin(a) {
      const res = await confirmDialog({
        title: "Admin löschen",
        message: h("p", null, "Den Admin ", h("strong", null, a.username), " wirklich löschen? Er kann sich danach nicht mehr anmelden."),
        confirmLabel: "Löschen",
        danger: true,
        onConfirm: () => api("admins/" + enc(a.id), { method: "DELETE" }),
      });
      if (res) {
        toast(`Admin ${a.username} gelöscht.`, { type: "success" });
      }
      refresh({ silent: true });
    }

    addBtn.addEventListener("click", addAdmin);
    refresh();
    return { refresh };
  }

  // ------------------------------------------------------------------------
  // view: Profile (extra software on top of the shipped sandbox image)
  // ------------------------------------------------------------------------

  const PROFILE_STATUS = {
    new: ["noch nicht gebaut", "warn"],
    queued: ["wartet auf Bau", "accent busy"],
    building: ["wird gebaut", "accent busy"],
    ready: ["bereit", "ok"],
    outdated: ["veraltet", "warn"],
    failed: ["Bau fehlgeschlagen", "danger"],
  };
  const PROFILE_OUTDATED = {
    config: "Pakete oder Befehle wurden seit dem letzten Bau geändert",
    base: "das mitgelieferte Sandbox-Image wurde aktualisiert",
  };
  const PACKAGE_KINDS = [
    { key: "apt", label: "Systempakete (apt)", placeholder: "libreoffice\ntesseract-ocr-deu",
      hint: "Ubuntu-Pakete, je Zeile eins (oder mit Leerzeichen getrennt), optional mit Version: paket=1.2-3" },
    { key: "pip", label: "Python-Pakete (pip)", placeholder: "pandas\nopenpyxl==3.1.5",
      hint: "z. B. pandas oder openpyxl==3.1.5" },
    { key: "npm", label: "Node-Pakete (npm, global)", placeholder: "typescript",
      hint: "z. B. typescript oder @mermaid-js/mermaid-cli@11.4.2" },
  ];
  const BUILD_ACTIVE = new Set(["queued", "building"]);

  function profileStatusBadge(p) {
    const [label, cls] = PROFILE_STATUS[p.status] || [p.status || "unbekannt", "warn"];
    const reasons = (p.outdatedReasons || []).map((r) => PROFILE_OUTDATED[r] || r);
    return h("span", { class: ["badge", cls], title: reasons.length ? "Veraltet: " + reasons.join("; ") : null }, label);
  }

  function packageList(p) {
    const pk = p.packages || {};
    return PACKAGE_KINDS.flatMap((k) => pk[k.key] || []);
  }

  function softwareCell(p) {
    const pk = p.packages || {};
    const counts = PACKAGE_KINDS.map((k) => [k.key, (pk[k.key] || []).length]).filter(([, n]) => n > 0)
      .map(([k, n]) => `${n} ${k}`);
    if (p.setupScript) counts.push("eigene Befehle");
    const all = packageList(p);
    const preview = all.join(", ");
    return h("div", { title: all.join("\n") || null },
      h("div", null, counts.join(" · ") || h("span", { class: "faint" }, "keine Zusatzsoftware")),
      all.length ? h("div", { class: "cell-sub" }, preview.length > 80 ? preview.slice(0, 79) + "…" : preview) : null);
  }

  function imageCell(p) {
    if (!p.image) return h("span", { class: "faint" }, "–");
    return h("div", null,
      h("div", { class: "mono small" }, p.image),
      h("div", { class: "cell-sub" },
        isNum(p.imageSizeBytes)
          ? h("span", { title: "Gesamtgröße inklusive des mitgelieferten Sandbox-Images, dessen Schichten alle Profile "
            + "gemeinsam nutzen" }, fmtBytes(p.imageSizeBytes) + " inkl. Basis · ")
          : "",
        "gebaut ", timeEl(p.builtAt, true)));
  }

  function clientLinks(list) {
    if (!list || !list.length) return h("span", { class: "faint" }, "keinem Client");
    return h("div", { class: "link-list" }, list.map((c, i) => [i ? ", " : null,
      h("a", { href: "#/clients/" + enc(c.id) }, c.label || `Client ${c.id}`)]));
  }

  async function buildProfile(p, { after = null } = {}) {
    try {
      await api(`profiles/${enc(p.id)}/build`, { method: "POST" });
      toast(`Bau von „${p.name}“ gestartet. Das Protokoll steht auf der Profilseite.`, { type: "success" });
    } catch (err) {
      showError(err, "Bau nicht gestartet");
    } finally {
      state.profilesAt = 0;
      if (after) after();
    }
  }

  async function deleteProfile(p, { after = null } = {}) {
    const ok = await confirmDialog({
      title: "Profil löschen",
      message: [
        h("p", null, "Das Profil ", h("strong", null, p.name), " wird gelöscht."),
        (p.clients || []).length
          ? notice("warn", "Das Profil ist noch Clients zugeordnet.",
            "Vorher bei diesen Clients ein anderes Profil wählen: "
            + p.clients.map((c) => c.label || `#${c.id}`).join(", "))
          : h("p", { class: "muted" }, "Das Image wird beim nächsten Aufräumen entfernt, sobald keine Sandbox es mehr verwendet."),
      ],
      confirmLabel: "Profil löschen",
      danger: true,
      onConfirm: () => api("profiles/" + enc(p.id), { method: "DELETE" }),
    });
    state.profilesAt = 0;
    if (ok) toast(`Profil „${p.name}“ gelöscht.`, { type: "success" });
    if (after) after(!!ok);
  }

  function viewProfiles(root, _params, ctx) {
    const createBtn = h("button", { type: "button", class: "btn primary" }, "Profil anlegen");
    createBtn.addEventListener("click", () => openCreateProfile());
    root.append(pageHead("Profile",
      "Zusatzsoftware auf Basis des mitgelieferten Sandbox-Images – wird einem Client zugeordnet", createBtn));
    const body = h("div", null, loadingEl());
    root.append(body);

    async function load() {
      const data = await api("profiles");
      storeProfiles(data);
      if (!ctx.alive()) return;
      body.replaceChildren(profilesTable(state.profiles, refresh),
        h("p", { class: "metric-note mt-12" },
          "Basis aller Profile: ", h("span", { class: "mono" }, data.baseImage || "–"),
          ". Wird das Sandbox-Image bei einem Update neu gebaut, baut install.sh die Profile automatisch nach; "
          + "die Sandboxen erscheinen danach als veraltet und werden per „Neu aufbauen“ aktualisiert (Daten bleiben)."));
    }
    const refresh = ({ silent = false } = {}) => guarded(ctx, body, load, silent && state.profiles !== null);
    refresh();
    return { refresh };
  }

  function profilesTable(list, refresh) {
    const head = h("thead", null, h("tr", null,
      ["Profil", "Software", "Status", "Image", "Verwendet von", "Aktionen"].map((c) => h("th", null, c))));
    const rows = list.map((p) => {
      const building = BUILD_ACTIVE.has(p.status);
      const buildBtn = h("button", {
        type: "button", class: ["btn small icon-only", (p.status === "outdated" || p.status === "new") && "primary"],
        disabled: building, title: p.image ? "Neu bauen" : "Bauen", "aria-label": p.image ? "Neu bauen" : "Bauen",
      }, icon("build"));
      buildBtn.addEventListener("click", () => buildProfile(p, { after: () => refresh({ silent: true }) }));
      const delBtn = h("button", {
        type: "button", class: "btn small icon-only danger-outline", disabled: building,
        title: "Löschen", "aria-label": "Löschen",
      }, icon("remove"));
      delBtn.addEventListener("click", () => deleteProfile(p, { after: () => refresh({ silent: true }) }));
      return h("tr", null,
        h("td", null,
          h("a", { href: "#/profiles/" + enc(p.id), class: "cell-main" }, p.name),
          p.description ? h("div", { class: "cell-sub" }, p.description) : null),
        h("td", null, softwareCell(p)),
        h("td", null, profileStatusBadge(p)),
        h("td", null, imageCell(p)),
        h("td", null, clientLinks(p.clients)),
        h("td", { class: "actions" }, h("div", { class: "btn-row" }, buildBtn, delBtn)));
    });
    if (!rows.length) {
      rows.push(h("tr", null, h("td", { class: "empty", colspan: "6" },
        "Noch keine Profile. Ein Profil ergänzt das Sandbox-Image um Pakete, z. B. für Kunden ohne Internetzugriff.")));
    }
    return h("div", { class: "table-wrap" }, h("table", { class: "data" }, head, h("tbody", null, rows)));
  }

  async function openCreateProfile() {
    const name = h("input", { type: "text", name: "name", required: true, maxlength: "80", autocomplete: "off" });
    const description = h("input", { type: "text", name: "description", maxlength: "1000", autocomplete: "off" });
    const created = await modal({
      title: "Profil anlegen",
      submitLabel: "Anlegen",
      body: [
        field("Name", name, "z. B. Office + OCR"),
        field("Beschreibung", description, "optional – wofür das Profil gedacht ist"),
        h("p", { class: "muted small mt-12" }, "Pakete und Bau folgen auf der nächsten Seite."),
      ],
      onSubmit: () => api("profiles", {
        method: "POST", body: { name: name.value.trim(), description: description.value.trim() },
      }),
    });
    if (!created || !created.profile) return;
    state.profilesAt = 0;
    location.hash = "#/profiles/" + enc(created.profile.id);
  }

  function viewProfileDetail(root, params, ctx) {
    const id = Number.parseInt(params.id, 10);
    root.append(h("a", { class: "back-link", href: "#/profiles" }, "← Alle Profile"));
    const head = h("div", null, loadingEl());
    const statusBox = h("div");
    const formBox = h("div");
    const logTitle = h("h2", null, "Bau-Protokoll");
    const logHint = h("span", { class: "hint" });
    const logPre = h("pre", { class: "build-log", "aria-live": "polite" });
    const sections = h("div", { hidden: true },
      h("section", { class: "section" }, statusBox),
      h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", null, "Software")), formBox),
      h("section", { class: "section" }, h("div", { class: "section-head" }, logTitle, logHint), logPre));
    root.append(head, sections);
    if (!Number.isInteger(id)) {
      head.replaceChildren(notice("danger", "Ungültige Profil-ID."));
      return null;
    }

    let profile = null;
    let formRendered = false;
    const log = { seq: null, offset: 0, timer: null };

    function renderHead() {
      const building = BUILD_ACTIVE.has(profile.status);
      const buildBtn = h("button", { type: "button", class: "btn primary", disabled: building },
        profile.image ? "Neu bauen" : "Bauen");
      buildBtn.addEventListener("click", () => buildProfile(profile, { after: () => refresh({ silent: true }) }));
      const deleteBtn = h("button", { type: "button", class: "btn danger-outline", disabled: building }, "Profil löschen");
      deleteBtn.addEventListener("click", () => deleteProfile(profile, {
        after: (deleted) => { if (deleted) location.hash = "#/profiles"; },
      }));
      setTitle(profile.name);
      head.replaceChildren(h("div", { class: "page-head" },
        h("div", null,
          h("h1", null, profile.name),
          h("div", { class: "meta-line" },
            h("span", null, "ID ", h("span", { class: "mono" }, String(profile.id))),
            profile.description ? h("span", null, profile.description) : null,
            h("span", null, "angelegt ", timeEl(profile.createdAt)),
            profile.updatedAt ? h("span", null, "geändert ", timeEl(profile.updatedAt)) : null)),
        h("div", { class: "actions" }, buildBtn, deleteBtn)));
    }

    function renderStatus() {
      const b = profile.build || {};
      const reasons = (profile.outdatedReasons || []).map((r) => PROFILE_OUTDATED[r] || r);
      const lastBuild = b.seq
        ? [`#${b.seq} · `, { ok: "erfolgreich", failed: "fehlgeschlagen", queued: "wartet", building: "läuft" }[b.state] || b.state,
          b.finishedAt ? [" · ", timeEl(b.finishedAt, true)] : b.startedAt ? [" · gestartet ", timeEl(b.startedAt, true)] : null,
          b.actor ? h("span", { class: "faint" }, " · " + b.actor) : null]
        : h("span", { class: "faint" }, "noch nie gebaut");
      const out = [
        h("div", { class: "section-head" }, h("h2", null, "Status")),
        h("div", { class: "panel" }, h("div", { class: "panel-body" },
          h("dl", { class: "kv" },
            h("dt", null, "Status"), h("dd", null, profileStatusBadge(profile)),
            h("dt", null, "Image"), h("dd", null, imageCell(profile)),
            h("dt", null, "Letzter Bau"), h("dd", null, lastBuild),
            h("dt", null, "Basis"), h("dd", { class: "mono small" }, state.baseImage || "albert-ai-sandbox:latest"),
            h("dt", null, "Verwendet von"), h("dd", null, clientLinks(profile.clients))))),
      ];
      if (profile.status === "new") {
        out.push(notice("warn", "Das Profil wurde noch nicht gebaut.",
          "Clients mit diesem Profil können erst Sandboxen erstellen, wenn der Bau erfolgreich war (sonst lehnt die API mit "
          + "profile_not_ready ab)."));
      } else if (reasons.length) {
        out.push(notice("warn", "Das Image ist veraltet: " + reasons.join("; ") + ".",
          "Bis zum nächsten erfolgreichen Bau bekommen neue Sandboxen das bisherige Image."));
      }
      if (b.state === "failed" && b.error) {
        out.push(h("div", { class: "notice danger mt-12" },
          h("strong", null, profile.image ? "Der letzte Bau ist fehlgeschlagen – das bisherige Image bleibt in Betrieb."
            : "Der Bau ist fehlgeschlagen."),
          h("pre", { class: "build-log compact" }, b.error)));
      }
      statusBox.replaceChildren(...out);
    }

    function renderForm() {
      formRendered = true;
      const name = h("input", { type: "text", name: "name", required: true, maxlength: "80", autocomplete: "off",
        value: profile.name });
      const description = h("input", { type: "text", name: "description", maxlength: "1000", autocomplete: "off",
        value: profile.description || "" });
      const areas = {};
      const pkgFields = PACKAGE_KINDS.map((k) => {
        const ta = h("textarea", { name: k.key, rows: "7", spellcheck: "false", autocomplete: "off",
          placeholder: k.placeholder, class: "mono" });
        ta.value = ((profile.packages || {})[k.key] || []).join("\n");
        areas[k.key] = ta;
        return field(k.label, ta, k.hint);
      });
      const setup = h("textarea", { name: "setupScript", rows: "8", spellcheck: "false", autocomplete: "off",
        class: "mono", placeholder: "# Beispiel: Paketquelle eines Herstellers einbinden\n"
          + "curl -fsSL https://example.com/key.gpg | gpg --dearmor -o /usr/share/keyrings/example.gpg" });
      setup.value = profile.setupScript || "";
      const advanced = h("details", { class: "advanced", open: !!profile.setupScript },
        h("summary", null, "Zusätzliche Befehle (für Fortgeschrittene)"),
        field("Shell-Befehle", setup,
          "Laufen am Ende des Baus als root im Build-Container (bash, Abbruch beim ersten Fehler), nicht auf dem Host. "
          + "Für alles, was sich nicht als Paket angeben lässt."));
      const dockerfile = h("details", { class: "advanced" },
        h("summary", null, "Erzeugtes Dockerfile"),
        h("pre", { class: "build-log compact" }, profile.dockerfile || ""));
      const saveBtn = h("button", { type: "submit", class: "btn" }, "Speichern");
      const saveBuildBtn = h("button", { type: "button", class: "btn primary" }, "Speichern und bauen");
      const resetBtn = h("button", { type: "button", class: "btn ghost" }, "Zurücksetzen");
      const form = h("form", { class: "panel panel-body" },
        h("div", { class: "form-row" }, field("Name", name), field("Beschreibung", description)),
        h("div", { class: "grid cols-3 mt-12" }, pkgFields),
        advanced,
        dockerfile,
        h("div", { class: "form-actions" }, saveBuildBtn, saveBtn, resetBtn));

      async function save(andBuild) {
        if (!form.checkValidity()) {
          form.reportValidity();
          return;
        }
        const body = {
          name: name.value.trim(),
          description: description.value.trim(),
          packages: Object.fromEntries(PACKAGE_KINDS.map((k) => [k.key, areas[k.key].value])),
          setupScript: setup.value,
        };
        const btn = andBuild ? saveBuildBtn : saveBtn;
        [saveBtn, saveBuildBtn].forEach((b) => { b.disabled = true; });
        btn.classList.add("busy");
        try {
          const data = await api("profiles/" + id, { method: "PATCH", body });
          if (!ctx.alive()) return;
          profile = data.profile;
          state.profilesAt = 0;
          if (andBuild) {
            await api(`profiles/${id}/build`, { method: "POST" });
            toast("Gespeichert, der Bau läuft.", { type: "success" });
          } else {
            toast("Profil gespeichert." + (profile.status === "outdated" ? " Neu bauen, damit es wirkt." : ""),
              { type: "success" });
          }
          await load();
          renderForm();
        } catch (err) {
          showError(err, andBuild ? "Speichern und Bauen fehlgeschlagen" : "Speichern fehlgeschlagen");
        } finally {
          [saveBtn, saveBuildBtn].forEach((b) => { b.disabled = false; });
          btn.classList.remove("busy");
        }
      }
      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        save(false);
      });
      saveBuildBtn.addEventListener("click", () => save(true));
      resetBtn.addEventListener("click", () => renderForm());
      formBox.replaceChildren(form);
    }

    /** Streams the build log (incrementally while a build runs). */
    async function pollLog() {
      clearTimeout(log.timer);
      log.timer = null;
      if (!ctx.alive() || !profile) return;
      const seq = (profile.build || {}).seq;
      if (!seq) {
        logPre.textContent = "";
        logHint.textContent = "noch kein Bau";
        return;
      }
      if (log.seq !== seq) {
        log.seq = seq;
        log.offset = 0;
        logPre.textContent = "";
      }
      try {
        const data = await api(`profiles/${id}/log?seq=${seq}&offset=${log.offset}`);
        if (!ctx.alive()) return;
        const atBottom = logPre.scrollHeight - logPre.scrollTop - logPre.clientHeight < 40;
        if (data.text) logPre.append(data.text);
        log.offset = data.offset;
        if (atBottom) logPre.scrollTop = logPre.scrollHeight;
        logTitle.textContent = `Bau-Protokoll (Bau ${seq})`;
        logHint.textContent = data.running ? "läuft – wird laufend aktualisiert" : "";
        if (data.running) {
          log.timer = setTimeout(pollLog, 2000);
          // "wartet auf Bau" -> "wird gebaut" as soon as the job got the build slot
          if (data.state !== profile.status && data.state && BUILD_ACTIVE.has(data.state)) refresh({ silent: true });
        } else if (BUILD_ACTIVE.has(profile.status)) {
          refresh({ silent: true }); // finished meanwhile: show the result
        }
      } catch (err) {
        if (err && err.status === 401) return;
        log.timer = setTimeout(pollLog, 5000);
      }
    }

    async function load() {
      const data = await api("profiles/" + id);
      if (!ctx.alive()) return;
      profile = data.profile;
      renderHead();
      renderStatus();
      sections.hidden = false;
      if (!formRendered) renderForm();
      if (!log.timer) pollLog();
    }

    const refresh = ({ silent = false } = {}) => guarded(ctx, head, load, silent && profile !== null);
    if (!state.baseImage) getProfiles().catch(() => {});
    refresh();
    return { refresh };
  }

  // ------------------------------------------------------------------------
  // view: Wartung (reconcile + audit log)
  // ------------------------------------------------------------------------

  const RECONCILE_ACTIONS = {
    "remove-sandbox": "Sandbox entfernen",
    "remove-container": "Container entfernen",
    "remove-disk": "Disk-Image entfernen",
    "remove-volume": "Docker-Volume entfernen",
    "remove-route": "nginx-Route entfernen",
    "remove-file": "Datei entfernen",
    "write-route": "nginx-Route schreiben",
    "mount-disk": "Disk einhängen",
    "create-network": "Sandbox-Netz anlegen",
    "apply-firewall": "Firewall-Regeln setzen",
    "isolate-network": "Ins richtige Netz verschieben",
    "rebuild-network": "Neu aufbauen (Netz)",
    "fail-build": "Abgebrochenen Profil-Bau beenden",
    "remove-image": "Profil-Image entfernen",
    "remove-profile-dir": "Build-Dateien entfernen",
  };

  function renderReconcile(res, when) {
    const actions = res.actions || [];
    const errors = res.errors || [];
    const out = [];
    const stamp = " · " + when.toLocaleTimeString("de-DE");
    if (!actions.length && !errors.length) {
      out.push(notice("ok", "Alles konsistent.",
        (res.dryRun ? "Probelauf: keine Aktionen nötig." : "Es war nichts zu tun.") + stamp));
      return out;
    }
    out.push(h("p", { class: "muted mt-12" },
      res.dryRun
        ? `Probelauf – es wurde nichts verändert. ${actions.length} Aktion${actions.length === 1 ? "" : "en"} geplant.`
        : `Aufräumen ausgeführt: ${actions.length} Aktion${actions.length === 1 ? "" : "en"}.`,
      stamp));
    if (actions.length) {
      const rows = actions.map((a) => h("tr", null,
        h("td", { class: "nowrap" }, RECONCILE_ACTIONS[a.action] || a.action, " ", tag(a.action)),
        h("td", { class: "mono" }, a.target),
        h("td", null, a.detail || "")));
      const thead = h("thead", null, h("tr", null,
        h("th", null, "Aktion"), h("th", null, "Ziel"), h("th", null, "Detail")));
      out.push(h("div", { class: "table-wrap mt-8" },
        h("table", { class: "data compact" }, thead, h("tbody", null, rows))));
    }
    if (errors.length) {
      out.push(h("div", { class: "notice danger mt-12" },
        h("strong", null, `${errors.length} Fehler`),
        h("ul", null, errors.map((e) => h("li", null, e)))));
    }
    return out;
  }

  function detailsNode(raw) {
    if (raw === null || raw === undefined || raw === "") return h("span", { class: "faint" }, "–");
    let parsed = raw;
    if (typeof raw === "string") {
      try {
        parsed = JSON.parse(raw);
      } catch (_) {
        parsed = raw;
      }
    }
    if (typeof parsed !== "object" || parsed === null) {
      return h("span", { class: "details-inline" }, String(parsed));
    }
    let truncated = false;
    const summary = Object.entries(parsed).map(([k, v]) => {
      let val = typeof v === "object" && v !== null ? JSON.stringify(v) : String(v);
      if (val.length > 40) {
        val = val.slice(0, 39) + "…";
        truncated = true;
      }
      return `${k}=${val}`;
    }).join(", ");
    if (!truncated && summary.length <= 100) {
      return h("span", { class: "details-inline" }, summary);
    }
    return h("details", { class: "json" }, h("summary", null, summary),
      h("pre", null, JSON.stringify(parsed, null, 2)));
  }

  // ------------------------------------------------------------------------
  // view: Einstellungen (HTTPS certificate, admin addresses)
  // ------------------------------------------------------------------------

  const TLS_MODE = { letsencrypt: "Let's Encrypt", custom: "eigenes Zertifikat" };

  function viewSettings(root, _params, ctx) {
    root.append(pageHead("Einstellungen",
      "HTTPS-Zertifikat des Hosts, interne Ziele der Sandboxen und Zugriff auf diese Verwaltung"));
    const body = h("div", null, loadingEl());
    root.append(body);
    let data = null;
    let poll = null;

    function stopPoll() {
      if (poll) clearTimeout(poll);
      poll = null;
    }

    async function load() {
      const res = await api("settings");
      if (!ctx.alive()) return;
      data = res.settings;
      body.replaceChildren(...renderSettings(data));
      stopPoll();
      if (data.job && data.job.state === "running") {
        poll = setTimeout(() => { if (ctx.alive()) refresh({ silent: true }); }, 2500);
      }
    }

    function renderSettings(st) {
      const cert = st.certificate || {};
      const job = st.job || {};
      const out = [];
      const leBtn = h("button", {
        type: "button", class: "btn", disabled: !st.certbotAvailable || job.state === "running",
        title: st.certbotAvailable ? null : "certbot ist auf dem Host nicht installiert (install.sh installiert es).",
      }, "Let's Encrypt einrichten");
      leBtn.addEventListener("click", () => openLetsEncrypt(st));
      const customBtn = h("button", { type: "button", class: "btn" }, "Eigenes Zertifikat hochladen");
      customBtn.addEventListener("click", () => openCustomCert(st));
      const offBtn = st.active ? h("button", { type: "button", class: "btn danger-outline" }, "HTTPS ausschalten") : null;
      if (offBtn) offBtn.addEventListener("click", () => disableTls(st));
      const daysText = isNum(cert.daysLeft) ? ` (noch ${fmtNum(cert.daysLeft, 0)} Tage)` : "";
      const tlsPanel = h("div", { class: "panel" }, h("div", { class: "panel-body" },
        h("div", { class: "panel-title" }, "HTTPS-Zertifikat"),
        h("dl", { class: "kv" },
          h("dt", null, "Status"),
          h("dd", null, st.active ? h("span", { class: "badge ok" }, "aktiv") : h("span", { class: "badge warn" }, "aus – nur HTTP"),
            !st.active && st.mode ? h("span", { class: "faint" }, " – das hinterlegte Zertifikat wird nicht verwendet") : null),
          st.mode ? h("dt", null, "Art") : null, st.mode ? h("dd", null, TLS_MODE[st.mode] || st.mode) : null,
          st.host ? h("dt", null, "Host-Name") : null, st.host ? h("dd", { class: "mono" }, st.host) : null,
          cert.issuer ? h("dt", null, "Aussteller") : null,
          cert.issuer ? h("dd", { class: "small" }, cert.issuer, cert.selfSigned ? " (selbst signiert)" : null) : null,
          cert.notAfter ? h("dt", null, "Gültig bis") : null,
          cert.notAfter ? h("dd", null, timeEl(cert.notAfter), daysText) : null,
          (cert.names || []).length ? h("dt", null, "Gilt für") : null,
          (cert.names || []).length ? h("dd", { class: "mono small" }, cert.names.join(", ")) : null,
          h("dt", null, "Öffentliche Adresse"), h("dd", { class: "mono" }, st.publicUrl || "–")),
        (st.warnings || []).map((w) => notice("warn", null, w)),
        job.state === "running" ? notice("ok", "Zertifikat wird bei Let's Encrypt angefordert …",
          `Für ${job.host}. Das dauert meist unter einer Minute.`) : null,
        job.state === "failed" ? notice("danger", "Die letzte Anforderung ist fehlgeschlagen.",
          h("pre", { class: "build-log compact" }, job.error || "")) : null,
        h("p", { class: "metric-note mt-12" },
          "Mit HTTPS nimmt der Host API-, MCP- und Datei-Aufrufe nur noch verschlüsselt an; ALBERT | AI muss dann "
          + "„https://“ in der Adresse des Sandbox-Servers verwenden. Diese Verwaltung läuft dann ebenfalls über HTTPS."),
        h("div", { class: "form-actions" }, leBtn, customBtn, offBtn)));

      const allow = h("textarea", {
        class: "mono", rows: "5", name: "allow", spellcheck: "false",
        placeholder: "z. B.\n203.0.113.7\n10.0.0.0/16",
      });
      allow.value = (st.adminAllow || []).join("\n");
      const saveBtn = h("button", { type: "submit", class: "btn primary" }, "Speichern");
      const accessForm = h("form", { class: "panel" }, h("div", { class: "panel-body" },
        h("div", { class: "panel-title" }, "Zugriff auf die Verwaltung"),
        h("dl", { class: "kv" },
          h("dt", null, "Adresse der Verwaltung"),
          h("dd", null, safeUrl(st.adminUrl)
            ? h("a", { href: safeUrl(st.adminUrl), class: "mono" }, st.adminUrl) : h("span", { class: "mono" }, st.adminUrl || "–")),
          h("dt", null, "Deine Adresse"), h("dd", { class: "mono" }, st.yourAddress || "–")),
        field("Erlaubte Adressen", allow,
          "Eine IP-Adresse oder ein Netz (CIDR) pro Zeile. Leer = von überall erreichbar. "
          + "127.0.0.1 (SSH-Tunnel auf dem Host) ist immer erlaubt; deine eigene Adresse muss enthalten sein."),
        h("div", { class: "form-actions" }, saveBtn)));
      accessForm.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        saveBtn.disabled = true;
        saveBtn.classList.add("busy");
        try {
          const res = await api("settings/admin-access", { method: "PUT", body: { allow: allow.value } });
          toast((res.settings.adminAllow || []).length ? "Zugriff eingeschränkt." : "Verwaltung ist von überall erreichbar.",
            { type: "success" });
          if (ctx.alive()) refresh({ silent: true });
        } catch (err) {
          showError(err, "Speichern fehlgeschlagen");
        } finally {
          saveBtn.disabled = false;
          saveBtn.classList.remove("busy");
        }
      });
      out.push(h("div", { class: "grid cols-2" }, tlsPanel, accessForm));
      out.push(allowNetsPanel(st));
      return out;
    }

    function allowNetsPanel(st) {
      const info = st.allowNets || { nets: [], source: "config", clients: [] };
      const area = h("textarea", {
        class: "mono", rows: "5", name: "allowNets", spellcheck: "false", autocomplete: "off",
        placeholder: "z. B.\n10.0.5.20\n10.0.5.0/24",
      });
      area.value = (info.nets || []).join("\n");
      const saveBtn = h("button", { type: "submit", class: "btn primary" }, "Speichern");
      const resetBtn = info.source === "settings"
        ? h("button", { type: "button", class: "btn" }, "Wert aus albert.env verwenden") : null;
      const form = h("form", { class: "panel mt-16" }, h("div", { class: "panel-body" },
        h("div", { class: "panel-title" }, "Interne Ziele für alle Sandboxen"),
        h("p", { class: "metric-note" },
          "Sandboxen erreichen keine internen Netze (",
          h("span", { class: "mono" }, (st.blockedNets || []).join(", ") || "–"),
          "). Hier eingetragene Adressen und Netze sind für alle Sandboxen freigegeben, auch für Clients ohne "
          + "Internetzugriff. Ziele nur für einen Client stehen in dessen Einstellungen."),
        field("Freigegebene Ziele", area,
          "Eine IPv4-Adresse oder ein Netz (CIDR) pro Zeile. Gilt sofort, auch für laufende Sandboxen."
          + (info.source === "config"
            ? " Derzeit gilt der Wert aus albert.env (ALBERT_ALLOW_NETS); Speichern übernimmt ihn hierher."
            : "")),
        (info.clients || []).length ? h("dl", { class: "kv" },
          h("dt", null, "Clients mit eigenen Zielen"),
          h("dd", null, info.clients.map((c, i) => [i ? ", " : null,
            h("a", { href: "#/clients/" + enc(c.id) }, c.label || `Client ${c.id}`),
            h("span", { class: "faint mono" }, " (" + c.nets.join(", ") + ")")]))) : null,
        h("div", { class: "form-actions" }, saveBtn, resetBtn)));

      async function save(body, okText) {
        saveBtn.disabled = true;
        saveBtn.classList.add("busy");
        try {
          await api("settings/allow-nets", { method: "PUT", body });
          toast(okText, { type: "success" });
          if (ctx.alive()) refresh({ silent: true });
        } catch (err) {
          showError(err, "Speichern fehlgeschlagen");
        } finally {
          saveBtn.disabled = false;
          saveBtn.classList.remove("busy");
        }
      }
      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        const list = netLines(area.value);
        save({ allowNets: list }, list.length ? "Interne Ziele gespeichert." : "Keine internen Ziele mehr freigegeben.");
      });
      if (resetBtn) {
        resetBtn.addEventListener("click", () => save({ reset: true }, "Es gilt wieder der Wert aus albert.env."));
      }
      return form;
    }

    /** After HTTPS was switched on or off the admin UI moves (http <-> https); offer the new address. */
    async function showMoved(url, title, text) {
      const target = safeUrl(url);
      await modal({
        title,
        noCancel: true,
        submitLabel: "Zur neuen Adresse",
        body: [h("p", null, text), target ? h("p", { class: "mono" }, target) : null],
      });
      if (target) window.location.href = target;
    }

    function pemField(label, name, required, hint) {
      const area = h("textarea", { class: "mono", rows: "6", name, required, spellcheck: "false",
        placeholder: "-----BEGIN ...-----" });
      const file = h("input", { type: "file", accept: ".pem,.crt,.cer,.key,.txt", class: "file-input",
        "aria-label": label + " aus Datei laden" });
      file.addEventListener("change", () => {
        const f = file.files && file.files[0];
        if (!f) return;
        if (f.size > 65536) {
          toast("Die Datei ist zu groß (max. 64 KB).", { type: "error" });
          return;
        }
        const reader = new FileReader();
        reader.onload = () => { area.value = String(reader.result || ""); };
        reader.readAsText(f);
      });
      return { area, node: field(label, h("div", null, area, h("div", { class: "mt-8" }, file)), hint) };
    }

    async function openCustomCert(st) {
      const host = h("input", { type: "text", name: "host", required: true, autocomplete: "off", spellcheck: "false",
        placeholder: "sandbox.example.com", value: st.host || "" });
      const cert = pemField("Zertifikat", "certificate", true, "PEM; darf die Zwischenzertifikate schon enthalten.");
      const chain = pemField("Zwischenzertifikate (optional)", "chain", false, null);
      const key = pemField("Privater Schlüssel", "privateKey", true,
        "PEM ohne Passwort. Wird nur auf dem Host gespeichert (nur für root lesbar) und nicht wieder angezeigt.");
      const res = await modal({
        title: "Eigenes Zertifikat hochladen",
        wide: true,
        submitLabel: "Prüfen und aktivieren",
        body: [
          field("Host-Name", host, "Unter diesem Namen (oder dieser IP) erreichen ALBERT | AI und die Browser den Host."),
          cert.node, chain.node, key.node,
        ],
        onSubmit: () => api("settings/tls/custom", {
          method: "POST",
          body: { host: host.value.trim(), certificate: cert.area.value, chain: chain.area.value,
            privateKey: key.area.value },
        }),
      });
      if (!res || !res.settings) return;
      await showMoved(res.settings.adminUrl, "HTTPS ist aktiv",
        "Das Zertifikat ist geprüft und aktiv. Die Verwaltung ist jetzt nur noch über HTTPS erreichbar:");
    }

    async function openLetsEncrypt(st) {
      const host = h("input", { type: "text", name: "host", required: true, autocomplete: "off", spellcheck: "false",
        placeholder: "sandbox.example.com", value: st.mode === "letsencrypt" ? st.host || "" : "" });
      const email = h("input", { type: "email", name: "email", required: true, autocomplete: "email",
        placeholder: "it@example.com" });
      const terms = h("input", { type: "checkbox", class: "switch", name: "acceptTerms" });
      const res = await modal({
        title: "Let's Encrypt einrichten",
        submitLabel: "Zertifikat anfordern",
        body: [
          field("Host-Name", host, "Muss im DNS auf diesen Host zeigen; Port 80 muss aus dem Internet erreichbar sein."),
          field("E-Mail-Adresse", email, "Let's Encrypt schickt Hinweise zum Ablauf an diese Adresse."),
          h("label", { class: "switch-field" }, terms, h("span", null,
            h("span", { class: "field-label" }, "Nutzungsbedingungen von Let's Encrypt akzeptieren"),
            h("span", { class: "field-hint" }, h("a", {
              href: "https://letsencrypt.org/repository/", target: "_blank", rel: "noopener noreferrer",
            }, "letsencrypt.org/repository")))),
          notice("ok", null, "Die Erneuerung läuft danach automatisch (certbot-Timer des Hosts)."),
        ],
        onSubmit: () => {
          if (!terms.checked) throw new Error("Bitte die Nutzungsbedingungen von Let's Encrypt akzeptieren.");
          return api("settings/tls/letsencrypt", {
            method: "POST", body: { host: host.value.trim(), email: email.value.trim(), acceptTerms: true },
          });
        },
      });
      if (!res || !res.settings) return;
      toast("Zertifikat wird angefordert …", { type: "info" });
      const target = `https://${host.value.trim()}:${res.settings.adminPort}/manager/admin/#/settings`;
      // Poll until the job is done; once HTTPS is active this page (HTTP) loses its connection.
      for (let i = 0; i < 90 && ctx.alive(); i += 1) {
        await new Promise((r) => setTimeout(r, 2000));
        let now;
        try {
          now = (await api("settings")).settings;
        } catch (_) {
          await showMoved(target, "HTTPS ist aktiv", "Die Verwaltung ist jetzt nur noch über HTTPS erreichbar:");
          return;
        }
        const job = now.job || {};
        if (job.state === "done" || now.active) {
          await showMoved(now.adminUrl, "HTTPS ist aktiv", "Das Zertifikat ist ausgestellt und aktiv. Die Verwaltung "
            + "ist jetzt nur noch über HTTPS erreichbar:");
          return;
        }
        if (job.state === "failed") {
          if (ctx.alive()) refresh({ silent: true });
          showError({ code: "tls_letsencrypt_failed", message: job.error || "Unbekannter Fehler" },
            "Let's Encrypt fehlgeschlagen");
          return;
        }
      }
      if (ctx.alive()) refresh({ silent: true });
    }

    async function disableTls(st) {
      const res = await confirmDialog({
        title: "HTTPS ausschalten",
        message: [
          h("p", null, "Der Host nimmt danach wieder unverschlüsselte Verbindungen an."),
          notice("warn", "API-Keys und Dateien gehen dann wieder im Klartext über das Netz.",
            "ALBERT | AI muss danach „http://“ in der Adresse des Sandbox-Servers verwenden."),
        ],
        confirmLabel: "HTTPS ausschalten",
        danger: true,
        onConfirm: () => api("settings/tls", { method: "DELETE" }),
      });
      if (!res || !res.settings) return;
      await showMoved(res.settings.adminUrl, "HTTPS ist aus", "Die Verwaltung ist jetzt über HTTP erreichbar:");
    }

    const refresh = ({ silent = false } = {}) => guarded(ctx, body, load, silent && data !== null);
    refresh();
    return { refresh };
  }

  function viewMaintenance(root, _params, ctx) {
    const checkBtn = h("button", { type: "button", class: "btn" }, "Aufräumen prüfen");
    const applyBtn = h("button", { type: "button", class: "btn primary" }, "Aufräumen ausführen");
    const result = h("div");
    if (state.lastReconcile) result.replaceChildren(...renderReconcile(state.lastReconcile.res, state.lastReconcile.when));

    const auditFilter = h("input", {
      type: "search", placeholder: "Filtern nach Akteur, Aktion, Ziel …", value: state.auditFilter,
      "aria-label": "Audit-Log filtern",
    });
    const auditCount = h("span", { class: "count" });
    const auditReload = h("button", { type: "button", class: "btn small" }, "Neu laden");
    const auditBox = h("div", null, loadingEl());

    root.append(
      pageHead("Wartung", "Abgleich von Docker, Disk-Images und nginx mit der Datenbank sowie Audit-Log"),
      h("section", { class: "panel" }, h("div", { class: "panel-body" },
        h("div", { class: "section-head" }, h("h2", null, "Aufräumen (Reconcile)"),
          h("div", { class: "btn-group" }, checkBtn, applyBtn)),
        h("p", { class: "muted" },
          "Die Datenbank ist die Referenz: verwaiste Container, Disk-Images, Docker-Volumes und nginx-Routen "
          + "werden entfernt, fehlende Routen geschrieben, Disks eingehängt. Läuft zusätzlich automatisch jede Minute im Watcher."),
        result)),
      h("section", { class: "section" },
        h("div", { class: "section-head" }, h("h2", null, "Audit-Log"), h("span", { class: "hint" }, "letzte 200 Einträge")),
        h("div", { class: "toolbar" }, auditFilter, auditReload, h("span", { class: "grow" }), auditCount),
        auditBox));

    async function runReconcile(apply) {
      const btn = apply ? applyBtn : checkBtn;
      checkBtn.disabled = true;
      applyBtn.disabled = true;
      btn.classList.add("busy");
      try {
        const res = await api("reconcile", { method: apply ? "POST" : "GET" });
        state.lastReconcile = { res, when: new Date() };
        if (!ctx.alive()) return;
        result.replaceChildren(...renderReconcile(res, state.lastReconcile.when));
        if (apply) {
          toast(res.errors && res.errors.length ? "Aufräumen mit Fehlern beendet." : "Aufräumen abgeschlossen.",
            { type: res.errors && res.errors.length ? "warn" : "success" });
          refresh({ silent: true });
        }
      } catch (err) {
        showError(err, "Aufräumen fehlgeschlagen");
      } finally {
        checkBtn.disabled = false;
        applyBtn.disabled = false;
        btn.classList.remove("busy");
      }
    }

    checkBtn.addEventListener("click", () => runReconcile(false));
    applyBtn.addEventListener("click", async () => {
      const last = state.lastReconcile && state.lastReconcile.res.dryRun ? state.lastReconcile.res : null;
      const ok = await confirmDialog({
        title: "Aufräumen ausführen",
        message: [
          h("p", null, "Alle Abweichungen zwischen Datenbank und Host werden jetzt behoben. "
            + "Dabei werden verwaiste Container, Disk-Images und Volumes endgültig gelöscht."),
          last ? h("p", { class: "muted" },
            `Letzter Probelauf: ${last.actions.length} geplante Aktion${last.actions.length === 1 ? "" : "en"}.`)
            : h("p", { class: "muted" }, "Tipp: Vorher „Aufräumen prüfen“ ausführen, um die geplanten Aktionen zu sehen."),
        ],
        confirmLabel: "Jetzt aufräumen",
        danger: true,
      });
      if (ok) runReconcile(true);
    });

    let entries = null;
    function renderAudit() {
      if (!entries) return;
      const tokens = state.auditFilter.toLowerCase().split(/\s+/).filter(Boolean);
      const filtered = entries.filter((e) => {
        if (!tokens.length) return true;
        const hay = [e.actor, e.action, e.target, e.details].join(" ").toLowerCase();
        return tokens.every((t) => hay.includes(t));
      });
      auditCount.textContent = filtered.length === entries.length ? `${entries.length} Einträge` : `${filtered.length} von ${entries.length}`;
      const rows = filtered.map((e) => h("tr", null,
        h("td", { class: "nowrap num" }, timeEl(e.ts ? e.ts : null)),
        h("td", { class: "mono nowrap" }, e.actor || "–"),
        h("td", { class: "nowrap" }, tag(e.action || "–")),
        h("td", { class: "mono" }, e.target || h("span", { class: "faint" }, "–")),
        h("td", null, detailsNode(e.details))));
      if (!rows.length) {
        rows.push(h("tr", null, h("td", { class: "empty", colspan: "5" },
          entries.length ? "Kein Eintrag passt zum Filter." : "Noch keine Einträge.")));
      }
      auditBox.replaceChildren(h("div", { class: "table-wrap" }, h("table", { class: "data compact" },
        h("thead", null, h("tr", null,
          h("th", null, "Zeit"), h("th", null, "Akteur"), h("th", null, "Aktion"), h("th", null, "Ziel"), h("th", null, "Details"))),
        h("tbody", null, rows))));
    }

    async function load() {
      const data = await api("audit?limit=200");
      if (!ctx.alive()) return;
      entries = data.entries || [];
      renderAudit();
    }

    auditFilter.addEventListener("input", () => {
      state.auditFilter = auditFilter.value;
      renderAudit();
    });
    auditReload.addEventListener("click", async () => {
      auditReload.disabled = true;
      auditReload.classList.add("busy");
      await refresh();
      auditReload.disabled = false;
      auditReload.classList.remove("busy");
    });

    const refresh = ({ silent = false } = {}) => guarded(ctx, auditBox, load, silent && entries !== null);
    refresh();
    return { refresh };
  }

  // ------------------------------------------------------------------------
  // boot
  // ------------------------------------------------------------------------

  async function boot() {
    $("#login-form").addEventListener("submit", onLoginSubmit);
    $("#logout-btn").addEventListener("click", logout);
    applyTheme(currentTheme());
    $("#theme-toggle").addEventListener("click", () => applyTheme(THEME_NEXT[currentTheme()]));
    window.addEventListener("hashchange", () => {
      if (state.user) render();
    });
    setInterval(autoRefresh, REFRESH_MS);
    // Unsaved input keeps the view from being refreshed (saving re-renders it without the mark).
    document.addEventListener("input", (ev) => {
      const form = ev.target && ev.target.closest ? ev.target.closest("#view form") : null;
      if (form) form.dataset.dirty = "1";
    });
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && Date.now() - state.lastRefresh > REFRESH_MS) autoRefresh();
    });

    try {
      const data = await api("session", { auth: false });
      setSession(data);
    } catch (err) {
      if (err.status === 401) {
        showLogin(err.data);
        return;
      }
      const retry = h("button", { type: "button", class: "btn mt-12" }, "Erneut versuchen");
      retry.addEventListener("click", () => location.reload());
      $("#view").replaceChildren(h("div", { class: "login" },
        h("div", { class: "login-card" },
          notice("danger", "Verbindung zum Manager fehlgeschlagen.", errorText(err)),
          retry)));
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
