self.addEventListener('push', event => {
  let message = {title: '時間になりました', body: 'タイマーが終了しました', url: '/'};
  if (event.data) {
    try { message = {...message, ...event.data.json()}; } catch (_) {}
  }
  event.waitUntil(Promise.all([
    self.registration.showNotification(message.title, {
      body: message.body,
      data: {url: message.url},
      tag: 'timer-finished',
      renotify: true,
      requireInteraction: true,
      vibrate: [200, 100, 200]
    }),
    clients.matchAll({type: 'window', includeUncontrolled: true}).then(windows => {
      windows.forEach(client => client.postMessage({type: 'timer-finished'}));
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
