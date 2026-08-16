self.addEventListener('install', event => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
  event.waitUntil(clients.claim());
});

self.addEventListener('push', event => {
  let message = {type: 'timer-finished', title: '時間になりました', body: 'タイマーが終了しました', url: '/', tag: 'timer-finished'};
  if (event.data) {
    try { message = {...message, ...event.data.json()}; } catch (_) {}
  }
  event.waitUntil(Promise.all([
    self.registration.showNotification(message.title, {
      body: message.body,
      data: {url: message.url},
      tag: message.tag,
      renotify: true,
      requireInteraction: true,
      silent: false,
      vibrate: [200, 100, 200]
    }),
    clients.matchAll({type: 'window', includeUncontrolled: true}).then(windows => {
      const client = windows.find(windowClient => windowClient.focused)
        || windows.find(windowClient => windowClient.visibilityState === 'visible')
        || windows[0];
      if (client) client.postMessage({type: message.type});
    })
  ]));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(clients.matchAll({type: 'window', includeUncontrolled: true}).then(windows => {
    for (const client of windows) {
      if ('focus' in client) {
        client.navigate(url);
        return client.focus();
      }
    }
    return clients.openWindow(url);
  }));
});
