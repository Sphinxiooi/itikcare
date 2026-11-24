// Shared UI states for ItikCare (styles in static/css/states.css).
//
//   Loading  -> skeleton placeholders shaped like the page that is coming, shown over <main>
//               while a link/form navigation is slow. duck-loader.js calls showSkeleton()
//               and falls back to its logo overlay on pages with no <main data-skeleton-host>
//               (login, landing...), where there is no page shape to draw.
//   Offline  -> a bar that says what still works. The page already on screen is left alone
//               (that is the cached content); navigation and form submits are held back so
//               the farmer never drops to the browser's "no internet" page or loses what
//               they typed.
//   Error    -> renderError() builds the same what / whose fault / how to fix card (with a
//               wiggle) that includes/error_state.html renders on the server.
//   Empty    -> server-rendered only, see includes/empty_state.html.
//
// Public API: window.ItikStates = { showSkeleton, hideSkeleton, isOffline, renderError, wiggle }
(function () {
    "use strict";

    const scriptEl = document.currentScript;
    const TEXT = {
        loading: (scriptEl && scriptEl.dataset.loadingText) || "Loading…",
        offlineTitle: (scriptEl && scriptEl.dataset.offlineTitle) || "You are offline.",
        offlineBody:
            (scriptEl && scriptEl.dataset.offlineBody) ||
            "What is on screen is the last copy we loaded, so you can keep reading it. Saving, new forecasts and opening other pages need a connection and will work again when you are back online.",
        online: (scriptEl && scriptEl.dataset.onlineText) || "You are back online.",
    };

    // Same ceiling as duck-loader.js: never trap someone behind a skeleton forever (hung
    // request, the browser's Stop button).
    const SAFETY_TIMEOUT_MS = 120000;

    // ---------- helpers ----------

    // Re-trigger the CSS wiggle on an element (removing + re-adding the class restarts it).
    function wiggle(el) {
        if (!el) {
            return;
        }
        el.classList.remove("itik-wiggle");
        void el.offsetWidth; // reflow so the animation restarts
        el.classList.add("itik-wiggle");
    }

    function svg(pathData, extra) {
        return (
            '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
            pathData +
            (extra || "") +
            "</svg>"
        );
    }
    const ICON_ALERT = svg(
        '<path d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z"/>'
    );
    const ICON_WIFI_OFF = svg(
        '<line x1="1" y1="1" x2="23" y2="23"/><path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55"/>' +
            '<path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39"/><path d="M10.71 5.05A16 16 0 0 1 22.58 9"/>' +
            '<path d="M1.42 9a15.91 15.91 0 0 1 4.7-2.88"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/>' +
            '<line x1="12" y1="20" x2="12.01" y2="20"/>'
    );

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) {
            node.className = className;
        }
        if (text !== undefined) {
            node.textContent = text;
        }
        return node;
    }

    // ---------- Loading: skeletons ----------

    const line = (width) => `<span class="itik-skel itik-skel--line" style="width:${width}"></span>`;
    const title = '<span class="itik-skel itik-skel--title"></span>';
    const card = (inner) => `<div class="itik-skel-card">${inner}</div>`;
    const statCard = card(line("45%") + '<span class="itik-skel itik-skel--stat"></span>' + line("30%"));
    const chart = card(line("35%") + '<span class="itik-skel itik-skel--chart"></span>');
    const tableRows = (n) => {
        let rows = "";
        for (let i = 0; i < n; i += 1) {
            rows += `<div style="display:flex;gap:1rem">${line("18%")}${line("14%")}${line("14%")}${line("22%")}${line("12%")}</div>`;
        }
        return rows;
    };

    // One skeleton per kind of page, drawn to roughly match the real layout.
    const SHAPES = {
        dashboard:
            title +
            '<div class="itik-skel-grid itik-skel-grid--3">' + statCard + statCard + statCard + "</div>" +
            chart +
            card(line("30%") + tableRows(4)),
        table: title + card(line("25%") + tableRows(8)),
        forecast:
            title +
            '<div class="itik-skel-grid itik-skel-grid--3">' + statCard + statCard + statCard + "</div>" +
            '<div class="itik-skel-grid itik-skel-grid--2">' + chart + card(line("40%") + line("90%") + line("80%") + line("85%")) + "</div>",
        form:
            title +
            card(
                line("30%") + '<span class="itik-skel itik-skel--input"></span>' +
                line("25%") + '<span class="itik-skel itik-skel--input"></span>' +
                line("35%") + '<span class="itik-skel itik-skel--input"></span>' +
                '<span class="itik-skel itik-skel--input" style="width:9rem"></span>'
            ),
        generic: title + '<div class="itik-skel-grid itik-skel-grid--2">' + statCard + statCard + "</div>" + card(line("90%") + line("75%") + line("85%")),
    };

    // Destination path -> which skeleton to draw. Anything unlisted gets the generic one.
    function shapeFor(pathname) {
        if (pathname === "/") {
            return "dashboard";
        }
        if (pathname === "/farm-records/") {
            return "table";
        }
        if (pathname === "/forecast-recommendations/") {
            return "forecast";
        }
        if (pathname === "/log-daily-data/" || pathname === "/flock/" || /^\/farm-records\/\d+\/(edit|delete)\/$/.test(pathname)) {
            return "form";
        }
        return "generic";
    }

    let skelOverlay = null;
    let skelSafetyTimer = null;
    let skelHideTimer = null;

    function hasHost() {
        return !!document.querySelector("main[data-skeleton-host]");
    }

    // Returns true if a skeleton was shown (the caller then skips its own spinner overlay).
    function showSkeleton(targetUrl) {
        if (!hasHost()) {
            return false;
        }
        let pathname = window.location.pathname;
        try {
            pathname = new URL(targetUrl || window.location.href, window.location.href).pathname;
        } catch (err) {
            // Unparseable target: keep the current path's shape.
        }
        window.clearTimeout(skelHideTimer);
        if (!skelOverlay) {
            skelOverlay = el("div", "itik-skel-overlay app-background");
            skelOverlay.setAttribute("role", "status");
            skelOverlay.setAttribute("aria-live", "polite");
        }
        skelOverlay.innerHTML = `<span class="sr-only">${TEXT.loading.replace(/</g, "&lt;")}</span>` + SHAPES[shapeFor(pathname)];
        if (!skelOverlay.isConnected) {
            document.body.appendChild(skelOverlay);
        }
        void skelOverlay.offsetWidth; // so the fade-in has a hidden state to start from
        skelOverlay.classList.add("is-active");
        window.clearTimeout(skelSafetyTimer);
        skelSafetyTimer = window.setTimeout(hideSkeleton, SAFETY_TIMEOUT_MS);
        return true;
    }

    function hideSkeleton() {
        window.clearTimeout(skelSafetyTimer);
        if (!skelOverlay) {
            return;
        }
        skelOverlay.classList.remove("is-active");
        window.clearTimeout(skelHideTimer);
        skelHideTimer = window.setTimeout(function () {
            if (skelOverlay && !skelOverlay.classList.contains("is-active") && skelOverlay.isConnected) {
                skelOverlay.remove();
            }
        }, 200);
    }

    // Back/forward can restore this page from the bfcache with the skeleton still up.
    window.addEventListener("pageshow", function (event) {
        if (event.persisted) {
            hideSkeleton();
        }
    });

    // ---------- Error ----------

    // Fills `container` with the what / whose fault / how to fix card and wiggles it.
    // opts: { title, what, whose, fix, retryLabel, onRetry }. All text is set via
    // textContent, never innerHTML, so a message built from user input can't inject markup.
    function renderError(container, opts) {
        const box = el("div", "itik-error");
        box.setAttribute("role", "alert");

        const head = el("div", "itik-error__head");
        head.innerHTML = ICON_ALERT;
        head.appendChild(el("p", "itik-error__title", opts.title));
        box.appendChild(head);

        const rows = el("dl", "itik-error__rows");
        [
            [opts.whatLabel || "What went wrong", opts.what],
            [opts.whoseLabel || "Whose fault", opts.whose],
            [opts.fixLabel || "How to fix it", opts.fix],
        ].forEach(function (pair) {
            if (!pair[1]) {
                return;
            }
            const row = el("div");
            row.appendChild(el("dt", "", pair[0]));
            row.appendChild(el("dd", "", pair[1]));
            rows.appendChild(row);
        });
        box.appendChild(rows);

        if (opts.onRetry) {
            const actions = el("div", "itik-error__actions");
            const btn = el("button", "itik-error__btn", opts.retryLabel || "Try again");
            btn.type = "button";
            btn.addEventListener("click", opts.onRetry);
            actions.appendChild(btn);
            box.appendChild(actions);
        }

        container.replaceChildren(box);
        wiggle(box);
        return box;
    }

    // ---------- Offline ----------

    let offlineBar = null;
    let backTimer = null;

    function isOffline() {
        return navigator.onLine === false;
    }

    function buildOfflineBar() {
        if (offlineBar) {
            return offlineBar;
        }
        offlineBar = el("div", "itik-offline");
        offlineBar.setAttribute("role", "status");
        offlineBar.setAttribute("aria-live", "polite");
        return offlineBar;
    }

    function showOfflineBar() {
        window.clearTimeout(backTimer);
        const bar = buildOfflineBar();
        bar.classList.remove("itik-offline--back");
        bar.innerHTML = ICON_WIFI_OFF;
        const textBox = el("div");
        textBox.appendChild(el("p", "itik-offline__title", TEXT.offlineTitle));
        textBox.appendChild(el("p", "itik-offline__body", TEXT.offlineBody));
        bar.appendChild(textBox);
        if (!bar.isConnected) {
            document.body.appendChild(bar);
        }
        void bar.offsetWidth;
        bar.classList.add("is-active");
    }

    function showBackOnline() {
        if (!offlineBar || !offlineBar.classList.contains("is-active")) {
            return;
        }
        offlineBar.classList.add("itik-offline--back");
        offlineBar.innerHTML = "";
        const textBox = el("div");
        textBox.appendChild(el("p", "itik-offline__title", TEXT.online));
        offlineBar.appendChild(textBox);
        window.clearTimeout(backTimer);
        backTimer = window.setTimeout(function () {
            offlineBar.classList.remove("is-active");
        }, 3000);
    }

    function syncConnection() {
        if (isOffline()) {
            // A skeleton can't be left up: nothing is coming.
            hideSkeleton();
            showOfflineBar();
        } else {
            showBackOnline();
        }
    }

    // Offline: hold back anything that would leave the page or hit the server. Registered
    // on `window` in the capture phase so it runs before every other handler on the page
    // (base.html's data-confirm dialog, duck-loader.js...) -- stopImmediatePropagation
    // then keeps them from showing a skeleton or opening a confirm for an action that
    // can't go through.
    function holdBack(event) {
        if (!isOffline()) {
            return false;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        showOfflineBar();
        wiggle(offlineBar);
        return true;
    }

    window.addEventListener(
        "click",
        function (event) {
            const link = event.target.closest && event.target.closest("a[href]");
            if (!link || event.button !== 0) {
                return;
            }
            let url;
            try {
                url = new URL(link.href, window.location.href);
            } catch (err) {
                return;
            }
            if (url.protocol !== "http:" && url.protocol !== "https:") {
                return;
            }
            // In-page jumps (#anchor) still work offline.
            const samePage = url.pathname === window.location.pathname && url.search === window.location.search;
            if (samePage && url.hash) {
                return;
            }
            holdBack(event);
        },
        true
    );
    window.addEventListener(
        "submit",
        function (event) {
            const form = event.target;
            if (form instanceof HTMLFormElement && form.method === "dialog") {
                return;
            }
            holdBack(event);
        },
        true
    );

    window.addEventListener("offline", syncConnection);
    window.addEventListener("online", syncConnection);
    // Page opened (or restored) while already offline.
    function initialSync() {
        if (isOffline()) {
            syncConnection();
        }
    }
    if (document.body) {
        initialSync();
    } else {
        document.addEventListener("DOMContentLoaded", initialSync);
    }

    window.ItikStates = {
        showSkeleton: showSkeleton,
        hideSkeleton: hideSkeleton,
        isOffline: isOffline,
        renderError: renderError,
        wiggle: wiggle,
    };
})();
