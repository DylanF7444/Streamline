package org.personalhub.metrics

import java.time.LocalDate
import java.time.ZoneId
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec
import org.bouncycastle.crypto.generators.SCrypt
import org.json.JSONArray
import org.json.JSONObject
import java.security.SecureRandom

object Wire {
    const val encryptedFormat = "personal-hub-metrics-encrypted-v1"
    fun steps(day: LocalDate, zone: ZoneId, total: Long): JSONObject = JSONObject().apply {
        require(total >= 0)
        put("record_id", "steps:${zone.id}:$day")
        put("metric_id", "steps")
        put("revision", 1)
        put("value", total)
        put("unit", "count")
        put("start", day.atStartOfDay(zone).toInstant().toString())
        put("end", day.plusDays(1).atStartOfDay(zone).toInstant().toString())
        put("timezone", zone.id)
        put("reporting_date", day.toString())
    }
    fun batch(source: String, readings: List<Reading>) = JSONObject().apply {
        put("format", "personal-hub-metrics-v1")
        put("source_id", source)
        put("records", JSONArray(readings.map { JSONObject(it.payload) }))
    }
    fun snapshot(source: String, readings: List<Reading>) = JSONObject().apply {
        put("format", "personal-hub-metrics-snapshot-v1")
        put("source", JSONObject().put("source_id", source).put("name", "Android phone")
            .put("metric_ids", JSONArray(listOf("steps"))))
        put("definitions", JSONArray().put(JSONObject().put("metric_id", "steps").put("name", "Steps")
            .put("unit", "count").put("kind", "interval").put("aggregation", "sum")))
        put("batches", JSONArray().put(batch(source, readings)))
    }
    fun encrypt(raw: ByteArray, passphrase: String,
                salt: ByteArray = random(16), nonce: ByteArray = random(12)): ByteArray {
        require(passphrase.codePointCount(0, passphrase.length) in 12..1024)
        require(raw.size <= 16 * 1024 * 1024 && salt.size == 16 && nonce.size == 12)
        val key = SCrypt.generate(passphrase.toByteArray(Charsets.UTF_8), salt, 16384, 8, 1, 32)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, nonce))
        cipher.updateAAD(encryptedFormat.toByteArray())
        val encrypted = cipher.doFinal(raw)
        key.fill(0)
        return JSONObject().put("format", encryptedFormat)
            .put("salt", Base64.getEncoder().encodeToString(salt))
            .put("nonce", Base64.getEncoder().encodeToString(nonce))
            .put("ciphertext", Base64.getEncoder().encodeToString(encrypted)).toString().toByteArray()
    }
    private fun random(size: Int) = ByteArray(size).also { SecureRandom().nextBytes(it) }
}
