// Feedback layer for ItikCare forms and sessions. Styles: static/css/itik-feedback.css.
// Strings and URLs come from window.ItikFeedbackConfig (templates/includes/feedback_assets.html),
// so the wording follows the English/Filipino switch and no URL is hard-coded here.
//
// Three jobs, each independent of the others:
//
// 1. INLINE VALIDATION. A form that can't be submitted is NOT sent: the exact fields
//    that are wrong turn red, each gets a message under it saying what's wrong and how to
//    fix it, the fields wiggle, and the first one takes focus. Messages the server already
//    rendered (Django form errors) are upgraded the same way, so a field looks identical
//    whether the browser or the server caught the mistake.
//
// 2. DRAFTS. Forms marked data-draft="<name>" are copied to localStorage as the farmer
//    types (only when they differ from what the page loaded with). If the session ends, or
//    the page is left before saving, the entry is put back the next time that form opens,
//    with a "restored" notice and a Discard button.
//
// 3. SESSION. The page asks the server how long the login has left. Five minutes before the
//    end it offers "Stay signed in"; once it has ended it saves every draft form and shows
//    a dialog whose button goes to the login page and then back to the same page.
//
// Opt-outs: data-no-validate on a form; formnovalidate on a submit button.
(function () {
    "use strict";

    var cfg = window.ItikFeedbackConfig || {};
    var strings = cfg.i18n || {};
    var reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    // ---------- small helpers ----------

    function t(key, vars) {
        var text = strings[key] || key;
        return text.replace(/\{(\w+)\}/g, function (match, name) {
            return vars && vars[name] !== undefined ? vars[name] : match;
        });
    }

    var ICON_ALERT =
        '<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" ' +
        'd="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16ZM9 6a1 1 0 0 1 2 0v4a1 1 0 1 1-2 0V6Zm1 8a1 1 0 1 0 0-2 1 1 0 0 0 0 2Z" clip-rule="evenodd"/></svg>';
    var ICON_CHECK =
        '<svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" ' +
        'd="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm3.7-9.3a1 1 0 0 0-1.4-1.4L9 10.6 7.7 9.3a1 1 0 0 0-1.4 1.4l2 2a1 1 0 0 0 1.4 0l4-4Z" clip-rule="evenodd"/></svg>';

    var uid = 0;
    function nextId(prefix) {
        uid += 1;
        return prefix + "-" + uid;
    }

    function isCheckable(el) {
        return el.type === "checkbox" || el.type === "radio";
    }

    // A control the validator should look at: a visible, enabled, named-or-labelled field.
    function isField(el) {
        return el && el.willValidate && el.type !== "hidden" && el.type !== "submit" && el.type !== "button";
    }

    // ---------- 1. inline validation ----------

    function labelFor(el) {
        var label = el.id ? document.querySelector('label[for="' + el.id + '"]') : null;
        if (!label && el.closest("label")) {
            label = el.closest("label");
        }
        var text = label ? label.textContent : el.getAttribute("aria-label") || el.name || "";
        // "Temperature (°C)" reads better in a sentence as "Temperature"; drop "*" markers too.
        return text.replace(/\([^)]*\)/g, " ").replace(/\*/g, "").replace(/\s+/g, " ").trim();
    }

    function prettyLimit(el, raw) {
        if (el.type === "date" && raw) {
            var parsed = new Date(raw + "T00:00:00");
            if (!isNaN(parsed.getTime())) {
                return parsed.toLocaleDateString(document.documentElement.lang || undefined, {
                    year: "numeric", month: "short", day: "numeric",
                });
            }
        }
        return raw;
    }

    // What's wrong with this field, and how to fix it -- or "" if it's fine.
    function messageFor(el) {
        var v = el.validity;
        if (v.valid) {
            return "";
        }
        var label = labelFor(el) || t("thisField");
        var min = prettyLimit(el, el.getAttribute("min"));
        var max = prettyLimit(el, el.getAttribute("max"));
        var isDate = el.type === "date";

        if (v.valueMissing) {
            if (el.type === "checkbox") { return t("requiredCheck"); }
            if (el.tagName === "SELECT" || el.type === "radio") { return t("requiredChoice", { label: label }); }
            if (isDate) { return t("requiredDate", { label: label }); }
            if (el.type === "number") { return t("requiredNumber", { label: label }); }
            return t("required", { label: label });
        }
        if (v.badInput) {
            return isDate ? t("badDate", { label: label }) : t("badNumber", { label: label });
        }
        if (v.rangeUnderflow) {
            return isDate ? t("dateTooEarly", { label: label, min: min }) : t("tooLow", { label: label, min: min });
        }
        if (v.rangeOverflow) {
            return isDate ? t("dateTooLate", { label: label, max: max }) : t("tooHigh", { label: label, max: max });
        }
        if (v.stepMismatch) {
            var step = el.getAttribute("step");
            return !step || step === "1" ? t("wholeNumber", { label: label }) : t("stepMismatch", { label: label, step: step });
        }
        if (v.typeMismatch) {
            if (el.type === "email") { return t("badEmail"); }
            return t("badFormat", { label: label });
        }
        if (v.tooShort) {
            return t("tooShort", { label: label, min: el.minLength, more: el.minLength - el.value.length });
        }
        if (v.tooLong) {
            return t("tooLong", { label: label, max: el.maxLength, over: el.value.length - el.maxLength });
        }
        if (v.patternMismatch) {
            return el.title ? t("badFormatHint", { label: label, hint: el.title }) : t("badFormat", { label: label });
        }
        return t("generic", { label: label });
    }

    function describeBy(el, id, add) {
        var ids = (el.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
        var has = ids.indexOf(id) !== -1;
        if (add && !has) { ids.push(id); }
        if (!add && has) { ids.splice(ids.indexOf(id), 1); }
        if (ids.length) { el.setAttribute("aria-describedby", ids.join(" ")); } else { el.removeAttribute("aria-describedby"); }
    }

    function wiggle(el) {
        if (reducedMotion) { return; }
        el.classList.remove("itik-wiggle");
        void el.offsetWidth; // restart the animation if it's already running
        el.classList.add("itik-wiggle");
        el.addEventListener("animationend", function done() {
            el.removeEventListener("animationend", done);
            el.classList.remove("itik-wiggle");
        });
    }

    // Where the message goes: right under the field (or under its wrapper, e.g. the
    // password box with its show/hide button; or after the checkbox's label row).
    function anchorFor(el) {
        var parent = el.parentElement;
        if (parent && (parent.classList.contains("relative") || parent.tagName === "LABEL")) {
            return parent;
        }
        return el;
    }

    function setError(el, message, options) {
        options = options || {};
        el.classList.add("itik-invalid");
        el.setAttribute("aria-invalid", "true");
        var box = el._itikError;
        if (!box) {
            box = document.createElement("p");
            box.id = (el.id || nextId("field")) + "-error";
            anchorFor(el).insertAdjacentElement("afterend", box);
            el._itikError = box;
        }
        box.className = "itik-field-error";
        box.innerHTML = ICON_ALERT + "<span></span>";
        box.lastChild.textContent = message;
        describeBy(el, box.id, true);
        if (options.wiggle) { wiggle(el); }
    }

    function clearError(el) {
        el.classList.remove("itik-invalid");
        el.removeAttribute("aria-invalid");
        if (el._itikError) {
            describeBy(el, el._itikError.id, false);
            el._itikError.remove();
            el._itikError = null;
        }
    }

    // Server-rendered errors: <p class="... text-red-600 ..."> inside a <form>. Next to a
    // field => that field's error; with no field beside it => an error for the whole form.
    var CONTROL_SELECTOR = "input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea";

    function adoptServerErrors(root) {
        var adopted = [];
        (root || document).querySelectorAll("form p.text-red-600:not([data-itik-adopted])").forEach(function (p) {
            p.setAttribute("data-itik-adopted", "1");
            var holder = p.parentElement;
            // A message sitting directly in the <form> (e.g. "wrong password") belongs to the
            // whole form, not to whichever input happens to come first.
            var control = holder && holder.tagName !== "FORM" ? holder.querySelector(CONTROL_SELECTOR) : null;
            if (!control) {
                p.className = "itik-form-error";
                p.setAttribute("role", "alert");
                p.insertAdjacentHTML("afterbegin", ICON_ALERT);
                var text = document.createElement("span");
                while (p.childNodes.length > 1) { text.appendChild(p.childNodes[1]); }
                p.appendChild(text);
                return;
            }
            p.id = (control.id || nextId("field")) + "-error";
            p.className = "itik-field-error";
            var message = p.textContent.trim();
            p.innerHTML = ICON_ALERT + "<span></span>";
            p.lastChild.textContent = message;
            control.classList.add("itik-invalid");
            control.setAttribute("aria-invalid", "true");
            describeBy(control, p.id, true);
            control._itikError = p;
            adopted.push(control);
        });
        return adopted;
    }

    function focusFirst(el) {
        if (!el) { return; }
        try {
            el.focus({ preventScroll: true });
            el.scrollIntoView({ block: "center", behavior: reducedMotion ? "auto" : "smooth" });
        } catch (err) { /* focus is a nicety */ }
    }

    // "Fix 2 highlighted fields to continue." above the submit button.
    function updateSummary(form) {
        var count = form.querySelectorAll(".itik-invalid").length;
        var summary = form._itikSummary;
        if (!count) {
            if (summary) { summary.remove(); form._itikSummary = null; }
            return;
        }
        if (!summary) {
            var submit = form.querySelector('button[type="submit"]:not([name="edit"])');
            if (!submit) { return; }
            summary = document.createElement("p");
            summary.className = "itik-form-summary";
            summary.setAttribute("role", "alert");
            summary.style.gridColumn = "1 / -1";
            (submit.parentElement.matches("form") ? submit : submit.parentElement)
                .insertAdjacentElement("beforebegin", summary);
            form._itikSummary = summary;
        }
        summary.innerHTML = ICON_ALERT;
        summary.firstChild.style.cssText = "width:1.125rem;height:1.125rem;flex:none";
        var label = document.createElement("span");
        label.textContent = count === 1 ? t("fixOne") : t("fixMany", { n: count });
        summary.appendChild(label);
    }

    // Validate every field in the form; returns the invalid controls (first = top-most).
    function validateForm(form) {
        var invalid = [];
        Array.prototype.forEach.call(form.elements, function (el) {
            if (!isField(el)) { return; }
            var message = messageFor(el);
            if (message) {
                setError(el, message, { wiggle: true });
                invalid.push(el);
            } else if (el._itikError || el.classList.contains("itik-invalid")) {
                clearError(el);
            }
        });
        return invalid;
    }

    function shouldValidate(event) {
        var form = event.target;
        if (!(form instanceof HTMLFormElement) || form.hasAttribute("data-no-validate")) { return false; }
        if (form.method && form.method.toLowerCase() === "get") { return false; }
        var submitter = event.submitter;
        if (submitter && (submitter.hasAttribute("formnovalidate") || submitter.name === "edit")) { return false; }
        return true;
    }

    // The browser's own validation bubbles would pre-empt ours; switch them off for every
    // form we manage (our message says the same thing, in the right place).
    function disableNativeBubbles(root) {
        (root || document).querySelectorAll("form").forEach(function (form) {
            if (!form.hasAttribute("data-no-validate") && !(form.method && form.method.toLowerCase() === "get")) {
                form.noValidate = true;
            }
        });
    }

    // Live feedback once a field has been flagged: fix it and the error disappears.
    document.addEventListener("input", function (event) {
        var el = event.target;
        if (!isField(el) || !el.classList.contains("itik-invalid")) { return; }
        var message = messageFor(el);
        if (message) { setError(el, message); } else { clearError(el); }
        if (el.form) { updateSummary(el.form); }
    });
    document.addEventListener("change", function (event) {
        var el = event.target;
        if (isField(el) && el.classList.contains("itik-invalid")) {
            var message = messageFor(el);
            if (message) { setError(el, message); } else { clearError(el); }
            if (el.form) { updateSummary(el.form); }
        }
    });
    // Leaving a field that holds something wrong flags it right away (empty fields wait
    // until submit -- nobody wants to be told off for tabbing through).
    document.addEventListener("focusout", function (event) {
        var el = event.target;
        if (!isField(el) || !el.form || el.form.hasAttribute("data-no-validate")) { return; }
        if (el.form.method && el.form.method.toLowerCase() === "get") { return; }
        if (isCheckable(el) || el.value === "" || el.classList.contains("itik-invalid")) { return; }
        var message = messageFor(el);
        if (message) { setError(el, message, { wiggle: true }); }
    });

    // ---------- 2. drafts ----------

    var DRAFT_MAX_AGE_MS = 24 * 60 * 60 * 1000;
    var SKIP_NAMES = { csrfmiddlewaretoken: 1, confirmed: 1, edit: 1, next: 1 };
    var SKIP_TYPES = { password: 1, file: 1, submit: 1, button: 1, reset: 1, image: 1 };

    function storage() {
        try { return window.localStorage; } catch (err) { return null; }
    }

    function draftKey(form) {
        return "itik:draft:" + (cfg.userId || "anon") + ":" + window.location.pathname + ":" + form.getAttribute("data-draft");
    }

    // name -> value for everything worth keeping. A confirm-step form carries the whole
    // entry as hidden inputs, so it opts in with data-draft-hidden.
    function collect(form) {
        var data = {};
        var allowHidden = form.hasAttribute("data-draft-hidden");
        Array.prototype.forEach.call(form.elements, function (el) {
            if (!el.name || el.disabled || SKIP_NAMES[el.name] || SKIP_TYPES[el.type]) { return; }
            if (el.type === "hidden" && !allowHidden) { return; }
            if (el.type === "checkbox") {
                data[el.name] = el.checked;
            } else if (el.type === "radio") {
                if (el.checked) { data[el.name] = el.value; }
            } else {
                data[el.name] = el.value;
            }
        });
        return data;
    }

    function sameData(a, b) {
        var keys = Object.keys(a).concat(Object.keys(b));
        return keys.every(function (k) { return a[k] === b[k]; });
    }

    function readDraft(form) {
        var store = storage();
        if (!store) { return null; }
        try {
            var saved = JSON.parse(store.getItem(draftKey(form)));
            if (saved && saved.data && Date.now() - saved.at < DRAFT_MAX_AGE_MS) { return saved; }
        } catch (err) { /* corrupt draft: treat as none */ }
        return null;
    }

    function clearDraft(form) {
        var store = storage();
        if (!store || !form.hasAttribute("data-draft")) { return; }
        try { store.removeItem(draftKey(form)); } catch (err) { /* ignore */ }
    }

    // Save the form if it holds something the page didn't start with. Returns true if a
    // draft is now stored.
    function saveDraft(form) {
        var store = storage();
        if (!store) { return false; }
        var data = collect(form);
        var unchanged = !form.hasAttribute("data-draft-force") && form._itikPristine && sameData(data, form._itikPristine);
        try {
            if (unchanged) {
                // Back to how the page loaded. If the farmer typed and then undid it, there's
                // nothing to keep. If they never touched the form on this page (e.g. it was
                // re-opened with "Edit" after a review step), leave any existing draft alone:
                // the page's values may only exist in that draft.
                if (form._itikTouched) {
                    store.removeItem(draftKey(form));
                    return false;
                }
                return store.getItem(draftKey(form)) !== null;
            }
            store.setItem(draftKey(form), JSON.stringify({ at: Date.now(), data: data }));
            return true;
        } catch (err) {
            return false; // private mode / quota: drafts are best-effort
        }
    }

    function flushAllDrafts() {
        var saved = 0;
        document.querySelectorAll("form[data-draft]").forEach(function (form) {
            if (saveDraft(form)) { saved += 1; }
        });
        return saved;
    }

    function applyData(form, data, onlyVisible) {
        Array.prototype.forEach.call(form.elements, function (el) {
            if (!el.name || !(el.name in data) || SKIP_TYPES[el.type]) { return; }
            if (onlyVisible && el.type === "hidden") { return; }
            if (el.type === "checkbox") {
                el.checked = !!data[el.name];
            } else if (el.type === "radio") {
                el.checked = el.value === data[el.name];
            } else {
                el.value = data[el.name];
            }
        });
    }

    function initDraft(form) {
        if (form._itikDraftReady) { return; }
        form._itikDraftReady = true;
        form._itikPristine = collect(form);

        var hasServerErrors = !!form.querySelector(".itik-field-error, .itik-form-error");
        var saved = form.hasAttribute("data-draft-hidden") ? null : readDraft(form);
        if (saved && !hasServerErrors && !sameData(saved.data, form._itikPristine)) {
            applyData(form, saved.data, true);
            var notice = document.createElement("div");
            notice.className = "itik-draft-notice";
            notice.setAttribute("role", "status");
            var text = document.createElement("span");
            text.textContent = t("draftRestored");
            var discard = document.createElement("button");
            discard.type = "button";
            discard.textContent = t("draftDiscard");
            discard.addEventListener("click", function () {
                applyData(form, form._itikPristine, true);
                clearDraft(form);
                notice.remove();
            });
            notice.appendChild(text);
            notice.appendChild(discard);
            form.insertAdjacentElement("beforebegin", notice);
        }

        var timer = null;
        function later() {
            form._itikTouched = true;
            window.clearTimeout(timer);
            timer = window.setTimeout(function () { saveDraft(form); }, 400);
        }
        form.addEventListener("input", later);
        form.addEventListener("change", later);
    }

    // Leaving the page or switching tabs: capture whatever's in the forms right now (this
    // also catches values set by script, e.g. the weather "Use these values" button).
    window.addEventListener("pagehide", flushAllDrafts);
    document.addEventListener("visibilitychange", function () {
        if (document.visibilityState === "hidden") { flushAllDrafts(); }
    });

    // A draft is removed only after the server confirms the save (the success card shows
    // up on the next page). Which draft? The one for the form we submitted last.
    var PENDING_KEY = "itik:pending-draft";
    function settlePendingDraft() {
        var store = storage();
        if (!store) { return; }
        try {
            var pending = store.getItem(PENDING_KEY);
            store.removeItem(PENDING_KEY);
            if (pending && document.querySelector(".itik-success")) {
                store.removeItem(pending);
            }
        } catch (err) { /* ignore */ }
    }

    function clearUserDrafts() {
        var store = storage();
        if (!store) { return; }
        try {
            var prefix = "itik:draft:" + (cfg.userId || "anon") + ":";
            Object.keys(store).forEach(function (key) {
                if (key.indexOf(prefix) === 0) { store.removeItem(key); }
            });
        } catch (err) { /* ignore */ }
    }

    // ---------- 3. session ----------

    var WARN_AT_SECONDS = 300;
    var POLL_MS = 60000;
    var expiresAt = null;      // ms timestamp the session ends at, from the server's last answer
    var expiredShown = false;
    var toast = null;
    var warnDismissed = false;
    var countdownTimer = null;
    var lastPoll = 0;

    function csrfToken() {
        var input = document.querySelector("input[name=csrfmiddlewaretoken]");
        return input ? input.value : "";
    }

    function loginUrl() {
        return cfg.loginUrl + "?next=" + encodeURIComponent(window.location.pathname + window.location.search);
    }

    function fetchStatus() {
        var controller = window.AbortController ? new AbortController() : null;
        var timeout = window.setTimeout(function () { if (controller) { controller.abort(); } }, 4000);
        return fetch(cfg.statusUrl, {
            credentials: "same-origin",
            cache: "no-store",
            headers: { "X-Requested-With": "XMLHttpRequest" },
            signal: controller ? controller.signal : undefined,
        })
            .then(function (response) { return response.ok ? response.json() : null; })
            .catch(function () { return null; }) // offline / slow: say nothing, try again later
            .then(function (body) { window.clearTimeout(timeout); return body; });
    }

    function showExpiredDialog() {
        if (expiredShown) { return; }
        expiredShown = true;
        hideToast();
        var saved = flushAllDrafts();

        var dialog = document.createElement("dialog");
        dialog.className = "itik-session";
        dialog.setAttribute("aria-labelledby", "itik-session-title");
        dialog.innerHTML =
            '<div class="itik-session__card">' +
            '<h2 class="itik-session__title" id="itik-session-title"></h2>' +
            '<p class="itik-session__body"></p>' +
            '<p class="itik-session__saved">' + ICON_CHECK + "<span></span></p>" +
            '<div class="itik-session__actions"><button type="button" class="itik-btn itik-btn--primary"></button></div>' +
            "</div>";
        dialog.querySelector(".itik-session__title").textContent = t("expiredTitle");
        dialog.querySelector(".itik-session__body").textContent = t("expiredBody");
        dialog.querySelector(".itik-session__saved span").textContent = saved ? t("expiredSaved") : t("expiredNothing");
        var button = dialog.querySelector("button");
        button.textContent = t("expiredButton");
        button.addEventListener("click", function () {
            button.disabled = true;
            window.location.href = loginUrl();
        });
        // Escape must not dismiss it: there's nothing to go back to once signed out.
        dialog.addEventListener("cancel", function (event) { event.preventDefault(); });
        document.body.appendChild(dialog);
        if (typeof dialog.showModal === "function") { dialog.showModal(); } else { dialog.setAttribute("open", ""); }
        button.focus();
    }

    function hideToast() {
        window.clearInterval(countdownTimer);
        if (toast) { toast.remove(); toast = null; }
    }

    function formatTime(seconds) {
        var m = Math.floor(seconds / 60);
        var s = seconds % 60;
        return m + ":" + (s < 10 ? "0" : "") + s;
    }

    function showToast(secondsLeft) {
        if (toast || expiredShown) { return; }
        // "Not now" is respected until the last minute, when it's worth interrupting again.
        if (warnDismissed && secondsLeft > 60) { return; }
        toast = document.createElement("div");
        toast.className = "itik-session-toast";
        toast.setAttribute("role", "alertdialog");
        toast.setAttribute("aria-label", t("warnTitle", { time: "" }));
        toast.innerHTML =
            "<strong></strong><span></span>" +
            '<div class="itik-session-toast__actions">' +
            '<button type="button" class="itik-btn itik-btn--primary" data-act="stay"></button>' +
            '<button type="button" class="itik-btn itik-btn--ghost" data-act="dismiss"></button></div>';
        toast.querySelector("span").textContent = t("warnBody");
        toast.querySelector('[data-act="stay"]').textContent = t("warnStay");
        toast.querySelector('[data-act="dismiss"]').textContent = t("warnDismiss");
        document.body.appendChild(toast);

        function renderCountdown() {
            var left = Math.max(0, Math.round((expiresAt - Date.now()) / 1000));
            toast.querySelector("strong").textContent = t("warnTitle", { time: formatTime(left) });
            if (left <= 0) { hideToast(); confirmExpiry(); }
        }
        renderCountdown();
        countdownTimer = window.setInterval(renderCountdown, 1000);

        toast.addEventListener("click", function (event) {
            var act = event.target.getAttribute && event.target.getAttribute("data-act");
            if (act === "dismiss") { warnDismissed = true; hideToast(); }
            if (act === "stay") { extendSession(event.target); }
        });
    }

    function applyStatus(body) {
        if (!body) { return; }
        if (!body.authenticated || body.seconds_left <= 0) {
            showExpiredDialog();
            return;
        }
        expiresAt = Date.now() + body.seconds_left * 1000;
        if (body.seconds_left <= WARN_AT_SECONDS) {
            showToast(body.seconds_left);
        } else {
            warnDismissed = false; // session was extended: warn again next time it runs low
            hideToast();
        }
    }

    // The local clock says "over": ask the server before declaring it (clocks drift).
    function confirmExpiry() {
        fetchStatus().then(function (body) {
            if (body && (!body.authenticated || body.seconds_left <= 0)) { showExpiredDialog(); } else { applyStatus(body); }
        });
    }

    function extendSession(button) {
        button.disabled = true;
        fetch(cfg.extendUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: { "X-CSRFToken": csrfToken(), "X-Requested-With": "XMLHttpRequest" },
        })
            .then(function (response) {
                if (response.status === 401 || response.status === 403) { showExpiredDialog(); return null; }
                return response.ok ? response.json() : Promise.reject(new Error("extend failed"));
            })
            .then(function (body) { if (body) { hideToast(); applyStatus(body); } })
            .catch(function () {
                button.disabled = false;
                var note = toast && toast.querySelector("span");
                if (note) { note.textContent = t("warnFailed"); }
            });
    }

    function pollSession() {
        if (expiredShown || !cfg.authenticated) { return; }
        lastPoll = Date.now();
        fetchStatus().then(applyStatus);
    }

    // The farmer is submitting a form that's worth protecting: confirm the login is still
    // good first, so the data never travels to a login page that discards it.
    function guardSubmit(event) {
        var form = event.target;
        if (!cfg.authenticated || !(form.hasAttribute("data-draft") || form.hasAttribute("data-session-guard"))) {
            return false;
        }
        if (form._itikSessionOk) {
            form._itikSessionOk = false;
            return false; // second pass, after the check below passed
        }
        var submitter = event.submitter;
        flushAllDrafts();
        // Remember which draft to drop once the server confirms the save.
        var store = storage();
        if (store && form.hasAttribute("data-draft")) {
            try { store.setItem(PENDING_KEY, draftKey(form)); } catch (err) { /* ignore */ }
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        fetchStatus().then(function (body) {
            // body === null means the check itself failed (offline): don't block the farmer.
            if (body && (!body.authenticated || body.seconds_left <= 0)) {
                showExpiredDialog();
                return;
            }
            form._itikSessionOk = true;
            if (typeof form.requestSubmit === "function") { form.requestSubmit(submitter || undefined); } else { form.submit(); }
        });
        return true;
    }

    // ---------- submit hook (runs before every other submit listener on the page) ----------

    window.addEventListener("submit", function (event) {
        if (!(event.target instanceof HTMLFormElement)) { return; }
        if (shouldValidate(event)) {
            var form = event.target;
            var invalid = validateForm(form);
            if (invalid.length) {
                event.preventDefault();
                event.stopImmediatePropagation();
                updateSummary(form);
                focusFirst(invalid[0]);
                return;
            }
            updateSummary(form);
        }
        guardSubmit(event);
    }, true);

    // ---------- start-up ----------

    function scan(root) {
        var scope = root || document;
        disableNativeBubbles(scope);
        var flagged = adoptServerErrors(scope);
        flagged.forEach(function (el) { wiggle(el); });
        (scope.querySelectorAll ? scope.querySelectorAll("form[data-draft]") : []).forEach(initDraft);
        if (scope.matches && scope.matches("form[data-draft]")) { initDraft(scope); }
        return flagged;
    }

    function start() {
        settlePendingDraft();
        var flagged = scan(document);
        if (flagged.length) { focusFirst(flagged[0]); }

        document.querySelectorAll(".itik-success__close").forEach(function (btn) {
            btn.addEventListener("click", function () { btn.closest(".itik-success").remove(); });
        });

        if (cfg.authenticated && cfg.statusUrl) {
            pollSession();
            window.setInterval(pollSession, POLL_MS);
            // Coming back to a tab that sat in the background (a phone left in a pocket).
            document.addEventListener("visibilitychange", function () {
                if (document.visibilityState === "visible" && Date.now() - lastPoll > 5000) { pollSession(); }
            });
            window.addEventListener("focus", function () {
                if (Date.now() - lastPoll > 5000) { pollSession(); }
            });
        }
    }

    window.ItikFeedback = {
        scan: scan,
        clearDraft: clearDraft,
        clearUserDrafts: clearUserDrafts,
        // Called by code that discovers the session is gone mid-request (an AJAX save).
        sessionExpired: showExpiredDialog,
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})();
