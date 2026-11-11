package com.finfam.companion.service;

import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.ComponentName;
import android.content.Context;

import com.finfam.companion.api.ApiClient;
import com.finfam.companion.crypto.SecurePrefs;
import com.finfam.companion.parser.ParsedNotification;
import com.finfam.companion.queue.QueueStore;

/** Drena uma vez, sem polling infinito; falhas de rede deixam a fila para o próximo evento/job. */
public final class SyncCoordinator {
    private static final int JOB_ID = 8137;
    private static final Object DRAIN_LOCK = new Object();
    private static final long JOB_MIN_LATENCY_MS = 1000L;
    private static final long JOB_DEADLINE_MS = 30L * 60L * 1000L;

    private SyncCoordinator() { }

    public static void enqueue(Context context, ParsedNotification event) {
        Context app = context.getApplicationContext();
        new QueueStore(app).append(event);
        schedule(app);
    }

    public static void drainOnce(Context context) {
        Context app = context.getApplicationContext();
        synchronized (DRAIN_LOCK) {
            SecurePrefs prefs = new SecurePrefs(app);
            String token = prefs.getToken();
            if (token == null || token.trim().isEmpty()) return;
            QueueStore queue = new QueueStore(app);
            ApiClient client = new ApiClient();
            for (ParsedNotification event : queue.snapshot()) {
                ApiClient.ReceiveResult result = client.receive(token, event);
                if (result.accepted) {
                    queue.removeEvent(event.getEventId());
                } else if (result.httpCode == 401) {
                    // Token revogado: não envia nem retém eventos antigos para um reparo posterior.
                    prefs.clearSession();
                    return;
                } else if (result.httpCode == 400 || result.httpCode == 413 || result.httpCode == 422) {
                    // Erro permanente deste evento não pode bloquear os seguintes.
                    // Não registrar o payload nem o texto da notificação.
                    queue.removeEvent(event.getEventId());
                } else {
                    // Offline, timeout ou resposta não esperada: parar esta drenagem finita.
                    return;
                }
            }
        }
    }

    public static void schedule(Context context) {
        JobScheduler scheduler = (JobScheduler) context.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        if (scheduler == null) return;
        JobInfo info = new JobInfo.Builder(JOB_ID,
                new ComponentName(context, PendingSyncJobService.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setMinimumLatency(JOB_MIN_LATENCY_MS)
                .setOverrideDeadline(JOB_DEADLINE_MS)
                .setPersisted(false)
                .build();
        scheduler.schedule(info);
    }
}
