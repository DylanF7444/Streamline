package org.personalhub.metrics

import org.junit.Assert.*
import org.junit.Test
import java.time.LocalDate
import java.time.ZoneId
import java.time.Instant
import java.time.Duration

class WireTest {
    @Test fun stepDaysFollowDstAndIdentityIsStable() {
        val zone = ZoneId.of("America/Chicago")
        val record = Wire.steps(LocalDate.of(2026,11,1), zone, 100)
        assertEquals("steps:America/Chicago:2026-11-01", record.getString("record_id"))
        assertEquals(25L, Duration.between(Instant.parse(record.getString("start")), Instant.parse(record.getString("end"))).toHours())
    }
    @Test fun zeroIsExplicit() {
        assertEquals(0L, Wire.steps(LocalDate.of(2026,11,1), ZoneId.of("UTC"), 0).getLong("value"))
    }
    @Test fun cipherMatchesPythonFixture() {
        val fixture = org.json.JSONObject(javaClass.classLoader!!.getResource("metrics-v1.fixture.json")!!.readText())
        val encrypted = org.json.JSONObject(String(Wire.encrypt(fixture.getString("plaintext").toByteArray(),
            fixture.getString("passphrase"), ByteArray(16) { it.toByte() }, ByteArray(12) { it.toByte() })))
        assertEquals(fixture.getJSONObject("envelope").getString("ciphertext"), encrypted.getString("ciphertext"))
    }
}
