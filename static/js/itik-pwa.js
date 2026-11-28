// "ItikCare on your phone" card on Account Settings (templates/account/_phone_app_card.html):
// the install button and the phone-notification switch.
(function () {
    var card = document.getElementById("phone-app-card");
    if (!card) { return; }

    var toggle = document.getElementById("push-toggle");
    var message = document.getElementById("push-message");
    var testButton = document.getElementById("push-test");
    var installButton = document.getElementById("app-install");
    var installHelp = document.getElementById("app-install-help");
    var installedNote = document.getElementById("app-installed");
    var csrfToken = card.querySelector("input[name=csrfmiddlewaretoken]").value;
    var publicKey = card.dataset.publicKey;

    // ---------- install ----------
    function showInstalled() {
        installButton.classList.add("hidden");
        installHelp.classList.add("hidden");
        installedNote.classList.remove("hidden");
    }

    function offerInstall() {
        if (window.itikInstallPrompt) { installButton.classList.remove("hidden"); }
    }

    if (window.matchMedia("(display-mode: standalone)").matches) {
        showInstalled();
    } else {
        offerInstall();
        window.addEventListener("itik:installable", offerInstall);
        window.addEventListener("appinstalled", showInstalled);
    }

    installButton.addEventListener("click", function () {
        var prompt = window.itikInstallPrompt;
        if (!prompt) { return; }
        // Chrome allows each saved prompt to be used once
        window.itikInstallPrompt = null;
        installButton.classList.add("hidden");
        prompt.prompt();
    });

    // ---------- notifications ----------
    function show(key) {
        message.textContent = key ? card.dataset[key] : "";
    }

    function setOn(on) {
        toggle.setAttribute("aria-checked", on ? "true" : "false");
        testButton.classList.toggle("hidden", !on);
        show(on ? "msgOn" : "msgOff");
    }

    function post(url, data) {
        return fetch(url, {
            method: "POST",
            body: new URLSearchParams(data),
            headers: { "X-CSRFToken": csrfToken, "X-Requested-With": "XMLHttpRequest" }
        }).then(function (response) {
            if (!response.ok) { throw new Error("HTTP " + response.status); }
            return response.json();
        });
    }

    // The server key is URL-safe base64; the browser wants raw bytes
    function keyBytes(text) {
        var base64 = text.replace(/-/g, "+").replace(/_/g, "/");
        var raw = atob(base64 + "=".repeat((4 - base64.length % 4) % 4));
        return Uint8Array.from(raw, function (character) { return character.charCodeAt(0); });
    }

    // False after the server's key pair changes: that old subscription can't be used any more
    function madeWithCurrentKey(subscription) {
        var used = new Uint8Array(subscription.options.applicationServerKey || []);
        var current = keyBytes(publicKey);
        return used.length === current.length && used.every(function (byte, i) { return byte === current[i]; });
    }

    function currentSubscription() {
        return navigator.serviceWorker.ready.then(function (registration) {
            return registration.pushManager.getSubscription();
        });
    }

    function turnOn() {
        return Notification.requestPermission().then(function (permission) {
            if (permission !== "granted") {
                setOn(false);
                if (permission === "denied") {
                    toggle.disabled = true;
                    show("msgBlocked");
                }
                return;
            }
            return navigator.serviceWorker.ready.then(function (registration) {
                return registration.pushManager.getSubscription().then(function (old) {
                    if (old && !madeWithCurrentKey(old)) { return old.unsubscribe(); }
                }).then(function () {
                    // Returns the existing subscription if there still is one
                    return registration.pushManager.subscribe({
                        userVisibleOnly: true,
                        applicationServerKey: keyBytes(publicKey)
                    });
                });
            }).then(function (subscription) {
                var data = subscription.toJSON();
                return post(card.dataset.subscribeUrl, {
                    endpoint: data.endpoint, p256dh: data.keys.p256dh, auth: data.keys.auth
                });
            }).then(function () {
                setOn(true);
            });
        });
    }

    function turnOff() {
        return currentSubscription().then(function (subscription) {
            if (!subscription) {
                setOn(false);
                return;
            }
            return post(card.dataset.unsubscribeUrl, { endpoint: subscription.endpoint })
                .then(function () { return subscription.unsubscribe(); })
                .then(function () { setOn(false); });
        });
    }

    if (!/Android/i.test(navigator.userAgent)) {
        show("msgAndroidOnly");
        return;
    }
    if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
        show("msgUnsupported");
        return;
    }
    if (!publicKey) {
        show("msgNotConfigured");
        return;
    }
    if (Notification.permission === "denied") {
        show("msgBlocked");
        return;
    }

    // The switch shows "on" only if this browser is subscribed AND the server has it for this farmer
    currentSubscription().then(function (subscription) {
        if (!subscription || !madeWithCurrentKey(subscription)) { return false; }
        return post(card.dataset.statusUrl, { endpoint: subscription.endpoint })
            .then(function (data) { return data.subscribed; });
    }).then(function (on) {
        setOn(on);
        toggle.disabled = false;
    }).catch(function () {
        show("msgFailed");
        toggle.disabled = false;
    });

    toggle.addEventListener("click", function () {
        var turningOn = toggle.getAttribute("aria-checked") !== "true";
        toggle.disabled = true;
        show("msgWorking");
        (turningOn ? turnOn() : turnOff()).then(function () {
            toggle.disabled = Notification.permission === "denied";
        }).catch(function () {
            toggle.disabled = false;
            show("msgFailed");
        });
    });

    testButton.addEventListener("click", function () {
        testButton.disabled = true;
        show("msgWorking");
        currentSubscription().then(function (subscription) {
            return post(card.dataset.testUrl, { endpoint: subscription.endpoint });
        }).then(function () {
            show("msgTestSent");
        }).catch(function () {
            show("msgFailed");
        }).then(function () {
            testButton.disabled = false;
        });
    });
})();
