package org.personalhub.metrics

import android.content.Context
import androidx.room.*
import org.json.JSONObject

@Entity(tableName = "readings")
data class Reading(
    @PrimaryKey val recordId: String,
    val payload: String,
    val revision: Long,
    val pending: Boolean = true,
)

@Dao
abstract class ReadingDao {
    @Query("SELECT * FROM readings WHERE recordId = :id")
    abstract suspend fun get(id: String): Reading?
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    abstract suspend fun put(reading: Reading)
    @Query("SELECT * FROM readings WHERE pending = 1 ORDER BY recordId LIMIT 1000")
    abstract suspend fun pending(): List<Reading>
    @Query("SELECT * FROM readings ORDER BY recordId LIMIT :limit OFFSET :offset")
    abstract suspend fun page(limit: Int, offset: Int): List<Reading>
    @Query("SELECT count(*) FROM readings WHERE pending = 1")
    abstract suspend fun pendingCount(): Int
    @Query("UPDATE readings SET pending = 0 WHERE recordId = :id AND revision = :revision")
    abstract suspend fun acknowledge(id: String, revision: Long)
    @Transaction
    open suspend fun record(payload: JSONObject) {
        val id = payload.getString("record_id")
        val previous = get(id)
        // Stable serialization from Wire.steps makes equal samples byte-identical.
        val old = previous?.let { JSONObject(it.payload).apply { remove("revision") }.toString() }
        val comparable = JSONObject(payload.toString()).apply { remove("revision") }.toString()
        if (old == comparable) return
        val revision = (previous?.revision ?: 0) + 1
        payload.put("revision", revision)
        put(Reading(id, payload.toString(), revision))
    }
}

@Database(entities = [Reading::class], version = 1, exportSchema = false)
abstract class QueueDatabase : RoomDatabase() {
    abstract fun readings(): ReadingDao
    companion object {
        @Volatile private var instance: QueueDatabase? = null
        fun get(context: Context): QueueDatabase = instance ?: synchronized(this) {
            instance ?: Room.databaseBuilder(context.applicationContext, QueueDatabase::class.java,
                "personal-hub-metrics.db").build().also { instance = it }
        }
    }
}
