package org.personalhub.metrics

import android.os.Bundle
import android.os.Build
import android.os.ext.SdkExtensions
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.HealthConnectFeatures
import androidx.health.connect.client.PermissionController
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.StepsRecord
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.*
import org.json.JSONObject
import java.time.ZoneId
import java.util.UUID

class MainActivity : ComponentActivity() {
    private val prefs by lazy { getSharedPreferences("metrics", MODE_PRIVATE) }
    private var status by mutableStateOf("Choose step permissions, then collect or transfer readings.")
    private var busy by mutableStateOf(false)
    private var exportOffset = 0
    private var exportPassphrase = ""
    private var exportBytes: ByteArray? = null
    private val permission = registerForActivityResult(PermissionController.createRequestPermissionResultContract()) {
        status = "Permissions updated. You can collect with the permissions you granted."
    }
    private val createDocument = registerForActivityResult(ActivityResultContracts.CreateDocument("application/octet-stream")) { uri ->
        if (uri == null) {
            exportBytes = null; exportPassphrase = ""; status = "Export cancelled; queued readings are preserved."
        } else {
            action {
                contentResolver.openOutputStream(uri, "w")!!.use { it.write(exportBytes!!) }
                exportBytes = null
                exportOffset += 1000
                prepareExport()
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        if (!prefs.contains("source")) prefs.edit().putString("source", UUID.randomUUID().toString())
            .putString("timezone", ZoneId.systemDefault().id).apply()
        Collector.schedule(this)
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.RESUMED) {
                while (isActive) {
                    delay(60_000)
                    if (!busy) action { status = Collector(this@MainActivity).run() }
                }
            }
        }
        setContent {
            var enrollment by remember { mutableStateOf("") }
            var passphrase by remember { mutableStateOf("") }
            var mode by remember { mutableStateOf(prefs.getString("mode", "snapshot")!!) }
            MaterialTheme {
                Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(20.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    Text("Personal Hub Metrics", style = MaterialTheme.typography.headlineMedium)
                    Text("Steps stay on your phone until you export a snapshot or enable transfer to your paired Windows root.")
                    Text("Step access: read-only. Background reading is optional. No sleep or location access is requested.")
                    Text("Source ID: ${prefs.getString("source", "")}")
                    Button(onClick = {
                        val clipboard = getSystemService(CLIPBOARD_SERVICE) as android.content.ClipboardManager
                        clipboard.setPrimaryClip(android.content.ClipData.newPlainText("Metrics source ID", prefs.getString("source", "")))
                    }) { Text("Copy source ID") }
                    Text("Reporting timezone: ${prefs.getString("timezone", "UTC")}")
                    Text(if (Build.VERSION.SDK_INT >= 34 && SdkExtensions.getExtensionVersion(Build.VERSION_CODES.UPSIDE_DOWN_CAKE) >= 20)
                        "Built-in Health Connect phone step tracking is available on this Android version."
                    else "Configure a step-recording app to write into Health Connect on this phone.")
                    Button(enabled = !busy, onClick = {
                        if (HealthConnectClient.getSdkStatus(this@MainActivity) == HealthConnectClient.SDK_AVAILABLE)
                            permission.launch(setOf(HealthPermission.getReadPermission(StepsRecord::class)))
                        else status = "Install or update Health Connect, then configure a step-recording source."
                    }) { Text("Allow step reading") }
                    Button(enabled = !busy, onClick = {
                        if (HealthConnectClient.getSdkStatus(this@MainActivity) == HealthConnectClient.SDK_AVAILABLE &&
                            HealthConnectClient.getOrCreate(this@MainActivity).features.getFeatureStatus(
                                HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND) == HealthConnectFeatures.FEATURE_STATUS_AVAILABLE)
                            permission.launch(setOf("android.permission.health.READ_HEALTH_DATA_IN_BACKGROUND"))
                        else status = "Background health reading is unavailable; open the app to collect."
                    }) { Text("Allow optional background reading") }
                    Text("Transfer mode")
                    Row {
                        RadioButton(mode == "snapshot", onClick = { mode = "snapshot"; prefs.edit().putString("mode", mode).apply() })
                        Text("Manual encrypted snapshots")
                    }
                    Row {
                        RadioButton(mode == "lan", onClick = { mode = "lan"; prefs.edit().putString("mode", mode).apply() })
                        Text("Paired local network")
                    }
                    OutlinedTextField(enrollment, { enrollment = it }, label = { Text("Paste root pairing string") }, modifier = Modifier.fillMaxWidth())
                    Button(enabled = !busy, onClick = {
                        val text = enrollment; enrollment = ""
                        action {
                            val connection = JSONObject(text)
                            require(connection.getString("source_id") == prefs.getString("source", ""))
                            val claim = JSONObject().put("token", connection.getString("token"))
                                .put("source_id", connection.getString("source_id"))
                            val response = RootClient(connection).post("/v1/pair", claim)
                            connection.remove("token")
                            connection.put("credential", response.getString("credential"))
                            CredentialStore(this@MainActivity).save(connection)
                            status = "Root paired. Select local network mode to transfer automatically."
                        }
                    }) { Text("Pair with approved root") }
                    Button(enabled = !busy, onClick = { action { status = Collector(this@MainActivity).run() } }) { Text("Collect / Sync now") }
                    Button(enabled = !busy, onClick = { CredentialStore(this@MainActivity).clear(); status = "Root credentials removed. Readings remain on this phone." }) { Text("Forget root connection") }
                    OutlinedTextField(passphrase, { passphrase = it }, label = { Text("Snapshot passphrase (12+ characters)") },
                        visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
                    Button(enabled = !busy, onClick = {
                        exportPassphrase = passphrase; passphrase = ""; exportOffset = 0
                        action { require(exportPassphrase.length in 12..1024); prepareExport() }
                    }) { Text("Export encrypted snapshot parts") }
                    Text("Last successful transfer: ${prefs.getString("last_transfer", "never")}")
                    Text(status)
                    Text("Background checks run about every 15 minutes when Android permits. The open app refreshes about once a minute. The phone queues readings while the root is closed or unreachable.")
                    Text("Privacy: local app storage and encrypted transfer only. Windows can show, export, and delete accepted readings. Deleting on Windows does not erase Health Connect originals.")
                }
            }
        }
    }

    private fun action(block: suspend () -> Unit) {
        if (busy) return
        busy = true
        lifecycleScope.launch {
            try { withContext(Dispatchers.IO) { block() } }
            catch (_: Exception) {
                exportPassphrase = ""; exportBytes = null
                status = "Could not complete the operation. Check permissions, pairing, storage, and local connectivity. Pending readings are preserved."
            } finally { busy = false }
        }
    }

    private suspend fun prepareExport() {
        val records = QueueDatabase.get(this).readings().page(1000, exportOffset)
        if (records.isEmpty()) {
            exportPassphrase = ""
            status = if (exportOffset == 0) "No collected readings to export." else "Snapshot export complete. Queued readings are preserved."
            return
        }
        val raw = Wire.snapshot(prefs.getString("source", "")!!, records).toString().toByteArray()
        exportBytes = Wire.encrypt(raw, exportPassphrase)
        withContext(Dispatchers.Main) {
            createDocument.launch("metrics-${prefs.getString("source", "")}-${exportOffset/1000+1}.phmetrics")
        }
    }
}
