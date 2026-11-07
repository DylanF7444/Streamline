package org.personalhub.metrics

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.IOException
import java.time.LocalDate
import java.time.ZoneId
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class CollectionTest {
    private val context = InstrumentationRegistry.getInstrumentation().targetContext
    private val dao = QueueDatabase.get(context).readings()
    private val day = LocalDate.of(2026, 11, 1)
    private val prefs = context.getSharedPreferences("metrics", Context.MODE_PRIVATE)
    private class FakeSteps(var value: Long? = 100, var allowed: Boolean = true) : StepAccess {
        override suspend fun requireAccess(background: Boolean) {
            if (!allowed) throw SecurityException("permission denied")
        }
        override suspend fun total(day: LocalDate, zone: ZoneId): Long? = value
    }
    @Before fun reset() = runBlocking(Dispatchers.IO) {
        QueueDatabase.get(context).clearAllTables()
        prefs.edit().clear().putString("source", "test-phone").putString("timezone", "UTC")
            .putString("reconciled", day.toString()).putString("mode", "snapshot").commit()
    }
    @Test fun correctionsReplaceDailyValueAndQueueSurvivesReopen() = runBlocking(Dispatchers.IO) {
        val access = FakeSteps()
        Collector(context, access, day).run()
        Collector(context, access, day).run()
        assertEquals(1, dao.pendingCount())
        assertEquals(1L, dao.pending().first().revision)
        access.value = 150
        Collector(context, access, day).run()
        assertEquals(2L, QueueDatabase.get(context).readings().pending().first().revision)
    }
    @Test fun deniedPermissionsLeaveQueueUntouched() = runBlocking(Dispatchers.IO) {
        val access = FakeSteps()
        Collector(context, access, day).run()
        access.allowed = false
        try { Collector(context, access, day).run(background = true); fail("permission required") }
        catch (_: SecurityException) { }
        assertEquals(1, dao.pendingCount())
    }
    @Test fun offlineTransferRetainsQueueAndAcknowledgmentClearsOnlyPendingFlag() = runBlocking(Dispatchers.IO) {
        prefs.edit().putString("mode", "lan").commit()
        val access = FakeSteps()
        try { Collector(context, access, day) { _, _ -> throw IOException("offline") }.run(); fail("offline") }
        catch (_: IOException) { }
        assertEquals(1, dao.pendingCount())
        Collector(context, access, day) { _, _ -> }.run()
        assertEquals(0, dao.pendingCount())
        assertEquals(1, dao.page(1000, 0).size) // snapshots retain acknowledged history
    }
    @Test fun lateAcknowledgmentCannotClearNewerRevision() = runBlocking(Dispatchers.IO) {
        val access = FakeSteps()
        Collector(context, access, day).run()
        val old = dao.pending().first()
        access.value = 200
        Collector(context, access, day).run()
        dao.acknowledge(old.recordId, old.revision)
        assertEquals(1, dao.pendingCount())
    }
}
