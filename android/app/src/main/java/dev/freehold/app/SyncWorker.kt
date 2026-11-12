package dev.freehold.app

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

/**
 * Android decides when we run, and may stop us at any moment. That suits
 * Freehold: `sync` takes a time budget, stops cleanly when it runs out, and
 * the next window resumes rather than starting over, because chunks are
 * content-addressed and already-uploaded ones are never sent twice.
 */
class SyncWorker(context: Context, params: WorkerParameters) :
    CoroutineWorker(context, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        if (!Freehold.isEnrolled(applicationContext)) return@withContext Result.success()
        try {
            val outcome = Freehold.sync(applicationContext, budgetSeconds = 240)
            if (!outcome.optBoolean("ok")) return@withContext Result.retry()
            // "partial" means the budget ran out with work left; ask for
            // another window rather than treating it as a failure.
            when (outcome.optString("state")) {
                "partial" -> Result.retry()
                "failed" -> Result.retry()
                else -> Result.success()
            }
        } catch (e: Exception) {
            Result.retry()
        }
    }

    companion object {
        private const val NAME = "freehold-sync"

        /** Deliberately no network constraint: Freehold decides for itself
         *  whether the current link is acceptable, so that the policy lives
         *  in one place rather than two. */
        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<SyncWorker>(
                1, TimeUnit.HOURS
            ).build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                NAME, ExistingPeriodicWorkPolicy.KEEP, request
            )
        }

        fun cancel(context: Context) {
            WorkManager.getInstance(context).cancelUniqueWork(NAME)
        }
    }
}
