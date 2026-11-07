package org.personalhub.metrics

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.X509Certificate
import java.util.Base64
import java.util.concurrent.TimeUnit
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import javax.net.ssl.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

class CredentialStore(context: Context) {
    private val prefs = context.getSharedPreferences("private-connection", Context.MODE_PRIVATE)
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        return (store.getKey("personal-hub-metrics", null) as? SecretKey) ?: KeyGenerator
            .getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
                init(KeyGenParameterSpec.Builder("personal-hub-metrics",
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
            }.generateKey()
    }
    fun save(value: JSONObject) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val data = cipher.iv + cipher.doFinal(value.toString().toByteArray())
        check(prefs.edit().putString("connection", Base64.getEncoder().encodeToString(data)).commit())
    }
    fun load(): JSONObject? {
        val encoded = prefs.getString("connection", null) ?: return null
        val data = Base64.getDecoder().decode(encoded)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, data.copyOfRange(0,12)))
        return JSONObject(String(cipher.doFinal(data.copyOfRange(12,data.size))))
    }
    fun clear() { prefs.edit().clear().apply() }
}

class RootClient(private val connection: JSONObject) {
    private val expected = connection.getString("fingerprint").lowercase()
    private val trust = object : X509TrustManager {
        override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
        override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) = error("not a server")
        override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
            require(chain.isNotEmpty())
            val actual = MessageDigest.getInstance("SHA-256").digest(chain[0].encoded)
                .joinToString("") { "%02x".format(it) }
            if (actual != expected) throw java.security.cert.CertificateException("Root certificate changed")
            chain[0].checkValidity()
        }
    }
    private val client = OkHttpClient.Builder().apply {
        val tls = SSLContext.getInstance("TLS").apply { init(null, arrayOf(trust), SecureRandom()) }
        sslSocketFactory(tls.socketFactory, trust)
        // The pinned certificate establishes root identity; its LAN IP may change.
        hostnameVerifier { _, session ->
            val certificate = session.peerCertificates.first() as X509Certificate
            MessageDigest.getInstance("SHA-256").digest(certificate.encoded)
                .joinToString("") { "%02x".format(it) } == expected
        }
        followRedirects(false); followSslRedirects(false)
        dns(object : okhttp3.Dns {
            override fun lookup(hostname: String): List<java.net.InetAddress> =
            okhttp3.Dns.SYSTEM.lookup(hostname).also { addresses ->
                require(addresses.isNotEmpty() && addresses.all {
                    !it.isAnyLocalAddress && !it.isMulticastAddress &&
                        (it.isSiteLocalAddress || it.isLoopbackAddress || it.isLinkLocalAddress)
                }) { "Choose a root on your local network" }
            }
        })
        connectTimeout(10, TimeUnit.SECONDS); callTimeout(20, TimeUnit.SECONDS)
    }.build()
    fun post(path: String, payload: JSONObject): JSONObject {
        val base = connection.getString("url")
        val uri = java.net.URI(base)
        require(uri.scheme == "https" && uri.userInfo == null && uri.rawQuery == null && uri.rawFragment == null)
        require(uri.path.isNullOrEmpty() || uri.path == "/")
        val request = Request.Builder().url(base.trimEnd('/') + path)
            .post(payload.toString().toRequestBody("application/json".toMediaType()))
            .header("X-Hub-Source", connection.getString("source_id"))
        if (connection.has("credential")) request.header("Authorization", "Bearer " + connection.getString("credential"))
        return client.newCall(request.build()).execute().use { response ->
            check(response.isSuccessful) { "Root rejected transfer (${response.code})" }
            // Bound root responses as carefully as incoming measurements.
            val input = response.body!!.byteStream()
            val output = java.io.ByteArrayOutputStream()
            val buffer = ByteArray(4096)
            while (output.size() <= 65536) {
                val count = input.read(buffer, 0, minOf(buffer.size, 65537-output.size()))
                if (count < 0) break
                output.write(buffer, 0, count)
            }
            val bytes = output.toByteArray()
            check(bytes.size <= 65536)
            JSONObject(String(bytes))
        }
    }
}
