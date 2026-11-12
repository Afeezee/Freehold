package dev.freehold.app

import android.os.Bundle
import android.os.Environment
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import dev.freehold.app.databinding.ActivityMainBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * One screen. Enrol with the key from your desktop, point it at a folder,
 * and sync. Everything heavy happens in Python on a background thread.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var ui: ActivityMainBinding

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        ui = ActivityMainBinding.inflate(layoutInflater)
        setContentView(ui.root)

        lifecycleScope.launch { Freehold.start(applicationContext) }

        ui.enrolButton.setOnClickListener { enrol() }
        ui.backendButton.setOnClickListener { addGitHubBackend() }
        ui.folderButton.setOnClickListener { addFolder() }
        ui.syncButton.setOnClickListener { syncNow() }
        ui.autoSwitch.setOnCheckedChangeListener { _, on ->
            if (on) SyncWorker.schedule(applicationContext)
            else SyncWorker.cancel(applicationContext)
        }

        ui.folderInput.setText(
            Environment.getExternalStoragePublicDirectory(
                Environment.DIRECTORY_DCIM
            ).absolutePath
        )
        refresh()
    }

    private fun background(label: String, work: () -> JSONObject) {
        ui.statusText.text = "$label…"
        lifecycleScope.launch {
            val reply = withContext(Dispatchers.IO) {
                try {
                    work()
                } catch (e: Exception) {
                    JSONObject().put("ok", false)
                        .put("error", e.message ?: e.toString())
                }
            }
            if (!reply.optBoolean("ok")) {
                ui.statusText.text = "$label failed: ${reply.optString("error")}"
                Toast.makeText(this@MainActivity,
                    reply.optString("error"), Toast.LENGTH_LONG).show()
            } else {
                refresh()
            }
        }
    }

    private fun enrol() {
        val key = ui.keyInput.text.toString().trim()
        if (key.length != 64) {
            Toast.makeText(this, "A Freehold key is 64 hex characters",
                Toast.LENGTH_LONG).show()
            return
        }
        background("Enrolling") {
            Freehold.enrol(applicationContext, key, android.os.Build.MODEL)
        }
    }

    private fun addGitHubBackend() {
        val owner = ui.ownerInput.text.toString().trim()
        val token = ui.tokenInput.text.toString().trim()
        if (owner.isEmpty() || token.isEmpty()) {
            Toast.makeText(this, "GitHub username and token are both needed",
                Toast.LENGTH_LONG).show()
            return
        }
        background("Adding backend") {
            Freehold.setCredential(applicationContext,
                "FREEHOLD_GITHUB_TOKEN", token)
            Freehold.addBackend(applicationContext, "gh", "github",
                JSONObject().put("owner", owner))
        }
    }

    private fun addFolder() {
        val path = ui.folderInput.text.toString().trim()
        if (path.isEmpty()) return
        background("Adding folder") {
            Freehold.addFolder(applicationContext, path)
        }
    }

    private fun syncNow() {
        background("Backing up") { Freehold.sync(applicationContext) }
    }

    private fun refresh() {
        lifecycleScope.launch {
            val reply = withContext(Dispatchers.IO) {
                try {
                    if (!Freehold.isEnrolled(applicationContext)) null
                    else Freehold.status(applicationContext)
                } catch (e: Exception) {
                    null
                }
            }
            if (reply == null || !reply.optBoolean("ok")) {
                ui.statusText.text = getString(R.string.not_enrolled)
                ui.setupGroup.visibility = android.view.View.VISIBLE
                return@launch
            }
            ui.setupGroup.visibility = android.view.View.GONE
            val folders = reply.optJSONArray("folders")
            val backends = reply.optJSONArray("backends")
            val totals = reply.optJSONObject("totals")
            val lines = StringBuilder()
            lines.append("Device: ").append(reply.optString("device")).append('\n')
            lines.append("Folders: ").append(folders?.length() ?: 0).append('\n')
            lines.append("Backends: ").append(backends?.length() ?: 0).append('\n')
            if (totals != null) {
                lines.append("Blocks stored: ")
                    .append(totals.optInt("chunks")).append('\n')
            }
            val last = reply.opt("last_backup")
            lines.append("Last backup: ")
                .append(if (last == null || last == JSONObject.NULL) "never"
                        else java.text.SimpleDateFormat(
                            "yyyy-MM-dd HH:mm", java.util.Locale.getDefault()
                        ).format(java.util.Date(reply.optLong("last_backup") * 1000)))
            ui.statusText.text = lines.toString()
        }
    }
}
