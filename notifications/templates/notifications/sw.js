{% load static %}// ItikCare service worker: shows phone notifications and opens the app when one is tapped.
// It deliberately caches nothing, so farmers always see live farm data.

self.addEventListener("install", function () {
    self.skipWaiting();
});

self.addEventListener("activate", function (event) {
    event.waitUntil(self.clients.claim());
});

// A message from notifications/push.py arrived
self.addEventListener("push", function (event) {
    var data = {};
    try {
        data = event.data ? event.data.json() : {};
    } catch (error) {
        data = {};
    }
    event.waitUntil(self.registration.showNotification(data.title || "ItikCare", {
        body: data.body || "",
        tag: data.tag,
        icon: "{% static 'images/icon-192.png' %}",
        // Small white silhouette shown in Android's status bar
        badge: "{% static 'images/badge-96.png' %}",
        data: { url: data.url || "{{ fallback_url|escapejs }}" }
    }));
});

// Tapping the notification opens the log form
self.addEventListener("notificationclick", function (event) {
    event.notification.close();
    var target = new URL(event.notification.data.url, self.location.origin).href;
    event.waitUntil(
        self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (windows) {
            // Reuse a window already on that page; never navigate away from a half-typed form
            var open = windows.find(function (client) { return client.url === target; });
            return open ? open.focus() : self.clients.openWindow(target);
        })
    );
});
