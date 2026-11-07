package org.personalhub.metrics

import android.content.Context
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.time.TimeRangeFilter
import androidx.work.*
import java.time.LocalDate
import java.time.ZoneId
import java.util.concurrent.TimeUnit

interface StepAccess {
    suspend fun requireAccess(background: Boolean)
    suspend fun total(day: LocalDate, zone: ZoneId): Long?
}

class HealthStepAccess(private val context: Context) : StepAccess {
    override suspend fun requireAccess(background: Boolean) {
        check(HealthConnectClient.getSdkStatus(context) == HealthConnectClient.SDK_AVAILABLE) {
            "Install or update Health Connect and configure a step-recording source."
        }
        val grants = HealthConnectClient.getOrCreate(context).permissionController.getGrantedPermissions()
        if (HealthPermission.getReadPermission(StepsRecord::class) !in grants)
            throw SecurityException("Step read permission is required")
        if (background && "android.permission.health.READ_HEALTH_DATA_IN_BACKGROUND" !in grants)
            throw SecurityException("Background read permission is required")
    }
    override suspend fun total(day: LocalDate, zone: ZoneId): Long? {
        val response = HealthConnectClient.getOrCreate(context).aggregate(AggregateRequest(
            metrics = setOf(StepsRecord.COUNT_TOTAL),
            timeRangeFilter = TimeRangeFilter.between(day.atStartOfDay(zone).toInstant(), day.plusDays(1).atStartOfDay(zone).toInstant())))
        return response[StepsRecord.COUNT_TOTAL]
    }
}

class Collector(private val context: Context,
                private val access: StepAccess = HealthStepAccess(context),
                private val fixedToday: LocalDate? = null,
                private val transfer: (suspend (String, List<Reading>) -> Unit)? = null) {
    private val prefs = context.getSharedPreferences("metrics", Context.MODE_PRIVATE)
    private val dao = QueueDatabase.get(context).readings()
    suspend fun run(background: Boolean = false): String {
        access.requireAccess(background)
        val zone = ZoneId.of(prefs.getString("timezone", ZoneId.systemDefault().id))
        val today = fixedToday ?: LocalDate.now(zone)
        val reconcile = prefs.getString("reconciled", "") != today.toString()
        for (daysAgo in (if (reconcile) 29 else 0) downTo 0) {
            val day = today.minusDays(daysAgo.toLong())
            val total = access.total(day, zone)
            if (total != null) dao.record(Wire.steps(day, zone, total))
        }
        prefs.edit().putString("reconciled", today.toString()).apply()
        if (prefs.getString("mode", "snapshot") == "lan") {
            val connection = if (transfer == null) CredentialStore(context).load() else null
            if (transfer == null && connection == null) return "Steps queued. Pair the root device to transfer."
            val client = connection?.let { RootClient(it) }
            while (true) {
                val pending = dao.pending()
                if (pending.isEmpty()) break
                val source = prefs.getString("source", "")!!
                if (transfer != null) transfer.invoke(source, pending)
                else client!!.post("/v1/measurements/batches", Wire.batch(source, pending))
                pending.forEach { dao.acknowledge(it.recordId, it.revision) }
            }
            prefs.edit().putString("last_transfer", java.time.Instant.now().toString()).apply()
        }
        return "Collection complete; ${dao.pendingCount()} pending records."
    }
    companion object {
        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<CollectWorker>(15, TimeUnit.MINUTES)
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork("metrics-collection", ExistingPeriodicWorkPolicy.KEEP, request)
        }
    }
}

class CollectWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        return try {
            Collector(applicationContext).run(background = true)
            Result.success()
        } catch (_: SecurityException) {
            Result.success() // A user decision requires foreground permission UI.
        } catch (_: IllegalStateException) {
            Result.success()
        } catch (_: Exception) {
            Result.retry() // Queue stays durable across connectivity and storage failures.
        }
    }
}
