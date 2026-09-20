// Minimal logo loading overlay (the ItikCare mark inside a spinning ring) shown while the browser navigates to another page
// (link click or form submit). Styles live in static/css/duck-loader.css.
//
// Automatic triggers (nothing to wire up per page):
//   - clicking an ordinary same-site link
//   - submitting a form that actually goes to the server
// Both skip anything another handler already cancelled (the data-confirm dialog, the
// AJAX account-settings save, the signup confirm step...), new-tab links, downloads,
// same-page #anchors, modified clicks (ctrl/cmd/shift/alt) and external sites.
//
// Manual API, for code that navigates without firing those events -- e.g. a form
// confirmed through a dialog and then sent with form.submit(), which skips "submit":
//     window.ItikDuckLoader.show("Signing you out…");
//
// Optional per-element message: <form data-loader-text="Signing you in…"> (also honoured
// on links and on the clicked submit button). Add data-no-loader to opt an element out.
(function () {
    "use strict";

    const DEFAULT_TEXT = "Loading…";
    // If the page never actually leaves (a hung request, the user pressing the browser's
    // Stop button) don't trap them behind the overlay forever. Deliberately generous: on a
    // slow connection or a cold-starting host a real navigation can take well over 30s,
    // and hiding the overlay mid-request would make the page look frozen.
    const SAFETY_TIMEOUT_MS = 120000;

    // The logo <img> src comes from the include (data-logo on this script tag) so the
    // {% static %} URL is resolved by Django rather than hard-coded here.
    const scriptEl = document.currentScript;
    const LOGO_URL = scriptEl ? scriptEl.dataset.logo : "";

    // A ring with one bright arc (circumference of r=46 is ~289, arc = quarter of it)
    // spinning around the logo mark, which gently breathes.
    const MARKUP = `
<div class="itik-loader__mark">
    <svg class="itik-loader__ring" viewBox="0 0 100 100" aria-hidden="true" focusable="false">
        <circle class="itik-loader__track" cx="50" cy="50" r="46"/>
        <circle class="itik-loader__arc" cx="50" cy="50" r="46" stroke-dasharray="72 217"/>
    </svg>
    <img class="itik-loader__logo" alt="" width="64" height="64">
</div>
<p class="itik-loader__text"></p>`;

    let overlay = null;
    let textEl = null;
    let safetyTimer = null;
    let hideTimer = null;

    function build() {
        if (overlay) {
            return;
        }
        overlay = document.createElement("div");
        overlay.className = "itik-loader";
        overlay.setAttribute("role", "status");
        overlay.setAttribute("aria-live", "polite");
        overlay.setAttribute("aria-hidden", "true");
        // A popover lives in the browser's top layer, which is what lets the overlay sit
        // above an open <dialog> (the profile modal's AJAX save). z-index alone can't
        // beat the top layer. Browsers without popover support just use z-index.
        overlay.setAttribute("popover", "manual");
        overlay.innerHTML = MARKUP;
        const logo = overlay.querySelector(".itik-loader__logo");
        if (LOGO_URL) {
            logo.src = LOGO_URL;
        } else {
            logo.remove(); // no URL supplied: the ring alone still works as a spinner
        }
        textEl = overlay.querySelector(".itik-loader__text");
        document.body.appendChild(overlay);
    }

    // (Re-)enter the top layer so the overlay is stacked above whatever dialog was opened
    // most recently -- the top layer is ordered by when each element entered it.
    function raiseToTopLayer() {
        if (typeof overlay.showPopover !== "function") {
            return;
        }
        try {
            if (overlay.matches(":popover-open")) {
                overlay.hidePopover();
            }
            overlay.showPopover();
        } catch (err) {
            // Falls back to plain z-index stacking.
        }
    }

    function show(text) {
        build();
        window.clearTimeout(hideTimer);
        textEl.textContent = text || DEFAULT_TEXT;
        overlay.setAttribute("aria-hidden", "false");
        raiseToTopLayer();
        // Force a reflow so the fade-in has a hidden state to transition from even when
        // the overlay was just created.
        void overlay.offsetWidth;
        overlay.classList.add("is-active");
        window.clearTimeout(safetyTimer);
        safetyTimer = window.setTimeout(hide, SAFETY_TIMEOUT_MS);
    }

    function hide() {
        window.clearTimeout(safetyTimer);
        if (!overlay) {
            return;
        }
        overlay.classList.remove("is-active");
        overlay.setAttribute("aria-hidden", "true");
        // Leave the top layer once the fade-out has finished, so a dialog opened later
        // isn't stacked under an invisible overlay.
        window.clearTimeout(hideTimer);
        hideTimer = window.setTimeout(function () {
            if (!overlay.classList.contains("is-active") && typeof overlay.hidePopover === "function") {
                try {
                    overlay.hidePopover();
                } catch (err) {
                    // Already closed.
                }
            }
        }, 250);
    }

    window.ItikDuckLoader = { show: show, hide: hide };

    // Build the (hidden) overlay up front rather than on first show, so the logo image is
    // already downloaded by the time it's needed -- on a slow connection a lazily-fetched
    // logo would leave the ring spinning around an empty middle.
    if (document.body) {
        build();
    } else {
        document.addEventListener("DOMContentLoaded", build);
    }

    function isPlainLeftClick(event) {
        return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
    }

    function isNavigatingLink(link) {
        if ((link.target && link.target !== "_self") || link.hasAttribute("download")) {
            return false;
        }
        if (link.hasAttribute("data-no-loader")) {
            return false;
        }
        const rawHref = link.getAttribute("href");
        if (!rawHref || rawHref.charAt(0) === "#") {
            return false;
        }
        let url;
        try {
            url = new URL(link.href, window.location.href);
        } catch (err) {
            return false;
        }
        if ((url.protocol !== "http:" && url.protocol !== "https:") || url.origin !== window.location.origin) {
            return false;
        }
        // Same page + a hash is just an in-page jump, not a load.
        const samePage = url.pathname === window.location.pathname && url.search === window.location.search;
        return !(samePage && url.hash);
    }

    // Links: decided after the click has finished bubbling, so any handler that cancels
    // it (the profile-avatar modal, the sidebar's double-click guard...) is respected.
    document.addEventListener("click", function (event) {
        const link = event.target.closest && event.target.closest("a[href]");
        if (!link || !isPlainLeftClick(event)) {
            return;
        }
        window.setTimeout(function () {
            if (!event.defaultPrevented && isNavigatingLink(link)) {
                show(link.dataset.loaderText);
            }
        }, 0);
    });

    // Forms: same deferred check -- a form held back by the data-confirm dialog, the
    // signup confirm step, or the AJAX save is defaultPrevented, so it never shows.
    document.addEventListener("submit", function (event) {
        const form = event.target;
        if (!(form instanceof HTMLFormElement)) {
            return;
        }
        window.setTimeout(function () {
            if (event.defaultPrevented || form.hasAttribute("data-no-loader")) {
                return;
            }
            if ((form.target && form.target !== "_self") || form.method === "dialog") {
                return;
            }
            const submitter = event.submitter;
            show((submitter && submitter.dataset.loaderText) || form.dataset.loaderText);
        }, 0);
    });

    // Back/forward can restore this page from the bfcache with the overlay still up.
    window.addEventListener("pageshow", function (event) {
        if (event.persisted) {
            hide();
        }
    });
})();
