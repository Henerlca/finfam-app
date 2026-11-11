package com.finfam.companion.service;

import android.app.Notification;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;
import android.os.Bundle;

import com.finfam.companion.parser.NotificationParser;
import com.finfam.companion.parser.ParsedNotification;
import com.finfam.companion.queue.PackageAllowlist;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Listener permitido somente após seleção explícita de packages e concessão manual do Android. */
public final class FinfamNotificationListenerService extends NotificationListenerService {
    private ExecutorService executor;

    @Override public void onCreate() {
        super.onCreate();
        executor = Executors.newSingleThreadExecutor();
    }

    @Override public void onListenerConnected() {
        super.onListenerConnected();
        submitDrain();
    }

    @Override public void onListenerDisconnected() {
        super.onListenerDisconnected();
    }

    @Override public void onNotificationPosted(StatusBarNotification sbn) {
        if (sbn == null || executor == null) return;
        String sourcePackage = sbn.getPackageName();
        if (!new PackageAllowlist(this).isAllowed(sourcePackage)) return;

        Notification notification = sbn.getNotification();
        if (notification == null) return;
        Bundle extras = notification.extras;
        if (extras == null) return;
        CharSequence title = extras.getCharSequence(Notification.EXTRA_TITLE);
        CharSequence text = extras.getCharSequence(Notification.EXTRA_BIG_TEXT);
        if (text == null) text = extras.getCharSequence(Notification.EXTRA_TEXT);

        // A extração termina no objeto normalizado; title/text nunca entram na fila, log ou HTTP.
        ParsedNotification event = NotificationParser.parse(
                sourcePackage,
                sbn.getKey(),
                sbn.getPostTime(),
                title == null ? "" : title.toString(),
                text == null ? "" : text.toString());
        if (event == null) return;
        SyncCoordinator.enqueue(this, event);
        submitDrain();
    }

    private void submitDrain() {
        if (executor != null && !executor.isShutdown()) {
            executor.submit(() -> SyncCoordinator.drainOnce(this));
        }
    }

    @Override public void onDestroy() {
        if (executor != null) executor.shutdownNow();
        super.onDestroy();
    }
}
