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

  const ERROR_TITLES = {
    quota_exceeded: "Kontingent überschritten",
    sandbox_limit_exceeded: "Limit pro Sandbox überschritten",
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
    csrf: "Sicherheits-Token ungültig",
    throttled: "Zu viele Fehlversuche",
    network: "Keine Verbindung",
  };

  function errorTitle(err) {
    return (err && ERROR_TITLES[err.code]) || "Fehler";
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
    { key: "quotaSandboxes", group: "quota", label: "max. Sandboxen", unit: "", kind: "int", min: 0 },
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

  /**
   * "Limit pro Sandbox" + "Gesamtkontingent" fieldsets.
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
      return field(spec.label, ni.node);
    };
    const defaults = CLIENT_FIELDS.filter((f) => f.group === "default").map(build);
    const quotas = CLIENT_FIELDS.filter((f) => f.group === "quota").map(build);
    const nodes = [
      h("fieldset", null,
        h("legend", null, "Limit pro Sandbox"),
        h("p", { class: "legend-hint" },
          "Obergrenze für jede einzelne Sandbox; per API darf weniger angefordert werden. "
          + "Leer = globaler Standard."),
        h("div", { class: "field-grid" }, defaults)),
      h("fieldset", null,
        h("legend", null, "Gesamtkontingent"),
        h("p", { class: "legend-hint" },
          "Leer = unbegrenzt. CPU und RAM zählen die Limits laufender Sandboxen, "
          + "Disk die konfigurierte Größe aller Sandboxen."),
        h("div", { class: "field-grid cols-4" }, quotas)),
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
    $("#topbar").hidden = true;
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

  function handleUnauthorized(data) {
    if (!state.user) return;
    showLogin(data || { hasAdmins: true });
    toast("Die Sitzung ist abgelaufen. Bitte erneut anmelden.", { type: "warn" });
  }

  function setSession(data) {
    state.user = data.user;
    state.csrf = data.csrf;
    state.refreshFailed = false;
    $("#login").hidden = true;
    $("#topbar").hidden = false;
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
    admins: { title: "Admins", nav: "admins", fn: viewAdmins },
    maintenance: { title: "Wartung", nav: "maintenance", fn: viewMaintenance },
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

  async function autoRefresh() {
    if (!state.user || document.hidden || state.refreshing) return;
    if (document.querySelector("dialog[open]")) return;
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

    out.push(h("div", { class: "grid cols-4" + (out.length ? " mt-16" : "") },
      kpi("Clients", fmtNum(ov.clients, 0), null, "Mandanten mit API-Key", "#/clients"),
      kpi("Sandboxen", fmtNum(sb.total, 0), null,
        `${fmtNum(sb.running, 0)} laufend · ${fmtNum((sb.total || 0) - (sb.running || 0), 0)} gestoppt`, "#/sandboxes"),
      kpi("CPU-Kerne (Host)", fmtNum(host.cpus, 0), null, "logische Kerne"),
      kpi("RAM (Host)", fmtMb(host.memoryMb), null,
        host.dockerReachable === false ? "Docker: nicht erreichbar" : "Docker: erreichbar")));

    // allocation of running sandboxes vs host
    const cpuRatio = ratioOf(al.cpusRunning, host.cpus);
    const memRatio = ratioOf(al.memoryMbRunning, host.memoryMb || null);
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
      metric("RAM-Limits",
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

  function clientsTable(list) {
    const head = h("thead", null, h("tr", null,
      h("th", null, "Client"),
      h("th", null, "Angelegt"),
      h("th", null, "Sandboxen"),
      h("th", null, "CPU (laufend)"),
      h("th", null, "RAM (laufend)"),
      h("th", null, "Disk (alle)"),
      h("th", null, "Limit pro Sandbox")));
    const rows = list.map((c) => {
      const u = c.usage || {};
      const q = c.quota || {};
      return h("tr", null,
        h("td", null,
          h("a", { href: "#/clients/" + enc(c.id), class: "cell-main" }, c.label || `Client ${c.id}`),
          h("div", { class: "cell-sub mono" }, "Key " + (c.keyHashPrefix || "") + "…")),
        h("td", null, timeEl(c.createdAt)),
        h("td", null, usageCell(u.sandboxes, q.sandboxes, (n) => fmtNum(n, 0), `${fmtNum(u.running, 0)} laufend`)),
        h("td", null, usageCell(u.cpus, q.cpus, (n) => fmtNum(n, 2))),
        h("td", null, usageCell(u.memoryMb, q.memoryMb, fmtMb)),
        h("td", null, usageCell(u.diskGb, q.diskGb, (n) => fmtNum(n, 0) + " GB")),
        h("td", null, perSandboxText(c)));
    });
    if (!rows.length) {
      rows.push(h("tr", null, h("td", { class: "empty", colspan: "7" },
        "Noch keine Clients angelegt. Mit „Client anlegen“ wird ein Client samt API-Key erzeugt.")));
    }
    return h("div", { class: "table-wrap" }, h("table", { class: "data" }, head, h("tbody", null, rows)));
  }

  async function openCreateClient() {
    const ov = await getOverview().catch(() => null);
    const label = h("input", { type: "text", name: "label", required: true, maxlength: "200", autocomplete: "off" });
    const limits = clientLimitFields(null, ov);
    const created = await modal({
      title: "Client anlegen",
      wide: true,
      submitLabel: "Client anlegen",
      body: [field("Bezeichnung", label, "z. B. Kunde, Team oder Anwendung"), limits.nodes],
      onSubmit: async () => {
        const text = label.value.trim();
        if (!text) {
          label.setCustomValidity("Bitte eine Bezeichnung angeben.");
          label.reportValidity();
          label.setCustomValidity("");
          return false;
        }
        return api("clients", { method: "POST", body: { label: text, ...limits.values() } });
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
          h("div", { class: "grid cols-4" },
            metric("Sandboxen", limitText(u.sandboxes, q.sandboxes, (n) => fmtNum(n, 0)), u.sandboxes, q.sandboxes,
              `${fmtNum(u.running, 0)} laufend`),
            metric("CPU (laufend)", limitText(u.cpus, q.cpus, (n) => fmtNum(n, 2)), u.cpus, q.cpus),
            metric("RAM (laufend)", limitText(u.memoryMb, q.memoryMb, fmtMb), u.memoryMb, q.memoryMb),
            metric("Disk (alle Sandboxen)", limitText(u.diskGb, q.diskGb, (n) => fmtNum(n, 0) + " GB"), u.diskGb, q.diskGb)),
          h("p", { class: "metric-note mt-16" },
            "Limit pro Sandbox: ",
            h("strong", null, fmtCores(e.cpus)), src(d.cpus), " · ",
            h("strong", null, fmtMb(e.memoryMb) + " RAM"), src(d.memoryMb), " · ",
            h("strong", null, fmtNum(e.diskGb, 0) + " GB Disk"), src(d.diskGb)))));
    }

    async function renderForm() {
      formRendered = true;
      const ov = await getOverview().catch(() => null);
      if (!ctx.alive()) return;
      const label = h("input", {
        type: "text", name: "label", required: true, maxlength: "200", autocomplete: "off", value: client.label || "",
      });
      label.dataset.initial = client.label || "";
      const limits = clientLimitFields(client, ov);
      const saveBtn = h("button", { type: "submit", class: "btn primary" }, "Änderungen speichern");
      const resetBtn = h("button", { type: "button", class: "btn" }, "Zurücksetzen");
      const status = h("span", { class: "state" });
      const form = h("form", { class: "panel panel-body" },
        field("Bezeichnung", label),
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
          toast("Client gespeichert.", { type: "success" });
          renderHead();
          renderQuota();
          renderForm();
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
  };
  const PENDING_LABEL = {
    start: "startet …", stop: "stoppt …", restart: "startet neu …", delete: "wird gelöscht …",
  };

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
    const title = u.updatedAt ? "Gemessen " + fmtDate(u.updatedAt, true) : "Noch nicht gemessen";
    if (isNum(r.diskGb) && r.diskGb > 0) {
      return h("div", { class: "usage", title },
        h("div", { class: "usage-text" }, fmtBytes(used), h("span", { class: "of" }, " / " + fmtNum(r.diskGb, 0) + " GB")),
        bar(isNum(used) ? used : 0, r.diskGb * GIB));
    }
    return h("div", { class: "usage-text", title }, fmtBytes(used));
  }

  function sandboxTable(list, { showClient = false, emptyText = "Keine Sandboxen." } = {}) {
    const cols = ["Sandbox", showClient ? "Client" : null, "Status", "Angelegt", "Letzte Aktivität", "Persistent",
      "Limits (CPU · RAM · Disk)", "Disk belegt", "Container-Layer", "Aktionen"].filter(Boolean);
    const head = h("thead", null, h("tr", null, cols.map((c) => h("th", null, c))));
    const rows = list.map((s) => sandboxRow(s, showClient));
    if (!rows.length) rows.push(h("tr", null, h("td", { class: "empty", colspan: String(cols.length) }, emptyText)));
    return h("div", { class: "table-wrap" }, h("table", { class: "data" }, head, h("tbody", null, rows)));
  }

  function sandboxRow(s, showClient) {
    const pending = state.pending.get(s.name) || null;
    const url = safeUrl(s.urls && s.urls.desktop);
    const nameCell = h("td", null,
      url
        ? h("a", {
          href: url, target: "_blank", rel: "noopener noreferrer", class: "sbx-name",
          title: "Desktop in neuem Tab öffnen",
        }, s.name, " ↗")
        : h("span", { class: "sbx-name" }, s.name),
      s.lastError ? h("div", { class: "err-line", title: s.lastError }, "Fehler: " + s.lastError) : null);

    const clientCell = showClient
      ? h("td", null, s.clientId !== null && s.clientId !== undefined
        ? h("a", { href: "#/clients/" + enc(s.clientId) }, s.clientLabel || `Client ${s.clientId}`)
        : h("span", { class: "faint" }, "–"))
      : null;

    const persistent = h("input", {
      type: "checkbox", class: "switch", checked: !!s.persistent,
      disabled: !!pending || s.status === "removing",
      "aria-label": `Sandbox ${s.name} persistent`,
      title: "Persistente Sandboxen werden nicht nach Ablauf der maximalen Lebensdauer automatisch gelöscht "
        + "(Stopp bei Inaktivität gilt weiterhin).",
    });
    persistent.addEventListener("change", () => togglePersistent(s, persistent));

    return h("tr", null,
      nameCell,
      clientCell,
      h("td", null, statusBadge(s.status, pending)),
      h("td", null, timeEl(s.createdAt)),
      h("td", null, timeEl(s.lastActivityAt, true)),
      h("td", null, persistent),
      h("td", null, limitsCell(s)),
      h("td", null, diskCell(s)),
      h("td", { class: "num", title: "Beschreibbare Container-Schicht (außerhalb des Home-Verzeichnisses)" },
        fmtBytes((s.usage || {}).rootfsUsedBytes)),
      h("td", { class: "actions" }, sandboxActions(s, pending)));
  }

  function sandboxActions(s, pending) {
    const busy = !!pending;
    const transitional = s.status === "creating" || s.status === "removing";
    const running = s.status === "running";
    const mk = (label, cls, onClick, { disabled = false, busyFor = null, title = null } = {}) => {
      const isBusy = busy && pending.action === busyFor;
      const b = h("button", {
        type: "button",
        class: ["btn small", cls, isBusy && "busy"],
        disabled: busy || disabled,
        title,
      }, isBusy ? PENDING_LABEL[busyFor] : label);
      b.addEventListener("click", onClick);
      return b;
    };
    const buttons = [];
    if (running || (busy && pending.action === "stop")) {
      buttons.push(mk("Stop", "", () => sandboxAction(s, "stop"), { disabled: transitional, busyFor: "stop" }));
    } else {
      buttons.push(mk("Start", "", () => sandboxAction(s, "start"), {
        disabled: transitional, busyFor: "start", title: "Wartet auf den MCP Hub (bis zu ca. 2 Minuten)",
      }));
    }
    buttons.push(mk("Neustart", "", () => sandboxAction(s, "restart"), {
      disabled: transitional || !running, busyFor: "restart",
    }));
    buttons.push(mk("Limits", "", () => openLimitsDialog(s), { disabled: transitional, title: "Limits ändern" }));
    buttons.push(mk("Live", "", () => openStatsDialog(s), { title: "Live-Werte (docker stats)" }));
    buttons.push(mk("Löschen", "danger-outline", () => deleteSandbox(s), {
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
    window.addEventListener("hashchange", () => {
      if (state.user) render();
    });
    setInterval(autoRefresh, REFRESH_MS);
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
