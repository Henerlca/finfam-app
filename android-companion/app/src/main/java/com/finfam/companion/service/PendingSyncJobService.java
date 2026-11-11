package com.finfam.companion.service;

import android.app.job.JobParameters;
import android.app.job.JobService;

/** Trabalho curto, disparado por rede; não é um processo de polling permanente. */
public final class PendingSyncJobService extends JobService {
    @Override public boolean onStartJob(JobParameters params) {
        new Thread(() -> {
            SyncCoordinator.drainOnce(this);
            jobFinished(params, false);
        }, "finfam-sync-job").start();
        return true;
    }

    @Override public boolean onStopJob(JobParameters params) {
        return true;
    }
}
