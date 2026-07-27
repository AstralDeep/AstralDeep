package com.personalailabs.astraldeep.app.render.renderers

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import com.personalailabs.astraldeep.app.render.Download
import com.personalailabs.astraldeep.app.render.Emit
import com.personalailabs.astraldeep.app.render.Renderer
import com.personalailabs.astraldeep.app.render.ThemeSink
import com.personalailabs.astraldeep.app.ui.theme.channelSwatchOptions
import com.personalailabs.astraldeep.app.ui.theme.hexToColor
import com.personalailabs.astraldeep.core.sdui.Component
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject

/** How long a ParamPicker shows "Saving…" before restoring its buttons when no
 *  surface re-render arrives (the server normally replies well within this). */
private const val SUBMIT_FAILSAFE_MS = 12_000L

/** Register the input/code/file primitives (US2). */
fun Renderer.registerInputRenderers(): Renderer =
    apply {
        register("input") { c -> InputPrimitive(c, emit) }
        register("param_picker") { c -> ParamPickerPrimitive(c, emit) }
        register("color_picker") { c -> ColorPickerPrimitive(c, emit, theme) }
        register("theme_apply") { c -> ThemeApplyPrimitive(c, theme) } // US5: apply the emitted palette live
        register("code") { c -> CodePrimitive(c) }
        register("file_upload") { c -> FileActionButton(c, emit, c.str("label") ?: "Upload") }
        register("file_download") { c -> FileDownloadPrimitive(c, download) }
        register("download_card") { c -> FileDownloadPrimitive(c, download) }
    }

@Composable
private fun InputPrimitive(
    c: Component,
    emit: Emit,
) {
    var value by remember { mutableStateOf(c.str("value").orEmpty()) }
    val action = c.str("action")
    OutlinedTextField(
        value = value,
        onValueChange = { value = it },
        modifier = Modifier.fillMaxWidth(),
        label = c.str("label")?.let { { Text(it) } },
        singleLine = true,
        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
        keyboardActions =
            KeyboardActions(
                onDone = {
                    dispatchInputDone(action, value, emit) {
                        defaultKeyboardAction(ImeAction.Done)
                    }
                },
            ),
    )
}

/** Emit the server-owned action, then preserve Compose's native IME dismissal. */
internal fun dispatchInputDone(
    action: String?,
    value: String,
    emit: Emit,
    defaultKeyboardAction: () -> Unit,
) {
    try {
        if (action != null) {
            emit.event(action, buildJsonObject { put("value", value) })
        }
    } finally {
        defaultKeyboardAction()
    }
}

// --- param_picker field rules (pure — JVM unit-tested) ----------------------

/** One field attribute as a string; absent / non-primitive reads as null. */
internal fun fieldStr(
    f: JsonObject,
    key: String,
): String? = (f[key] as? JsonPrimitive)?.contentOrNull

internal fun fieldKind(f: JsonObject): String = fieldStr(f, "kind") ?: "text"

/**
 * The option KEYS a `select`/`checklist` offers. The catalog is server-owned (the
 * LLM provider presets, for one) — the client never invents, relabels or reorders
 * options, and submits the key it was handed verbatim.
 */
internal fun fieldOptions(f: JsonObject): List<String> =
    (f["options"] as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull } ?: emptyList()

/** A `select` only becomes a dropdown when the server actually gave it options —
 *  an empty menu is a dead control, so it degrades to the free-text field. */
internal fun rendersAsDropdown(f: JsonObject): Boolean = fieldKind(f) == "select" && fieldOptions(f).isNotEmpty()

/**
 * 063.1 declarative visibility: a field marked `visible_when {field, equals,
 * default}` shows only while the named controller select's current value
 * (typed-or-`default`) equals `equals`. Fields without the marker — and whole
 * payloads from servers that predate it — are always visible, so the server can
 * emit the attribute freely for older clients.
 */
internal fun fieldIsVisible(
    f: JsonObject,
    texts: Map<String, String>,
): Boolean {
    val vw = f["visible_when"] as? JsonObject ?: return true
    val controller = fieldStr(vw, "field") ?: return true
    val expected = fieldStr(vw, "equals") ?: return true
    val current = texts[controller] ?: fieldStr(vw, "default") ?: ""
    return current == expected
}

/**
 * The initially selected key of a `select`: the server default when it is on the
 * menu, else the first option — a dropdown always shows *something*, and that
 * something is what an untouched Save submits (parity with `<select>`, which
 * selects its first `<option>` when none is marked selected). With no options the
 * field is a text box, so the raw default carries through.
 */
internal fun selectInitial(
    default: String?,
    options: List<String>,
): String =
    when {
        options.isEmpty() -> default.orEmpty()
        default != null && default in options -> default
        else -> options.first()
    }

/** The initially checked keys of a `checklist`: the server's default list ∩ its
 *  options (an option-less default could never be unchecked, nor submitted). */
internal fun checklistInitial(
    default: JsonElement?,
    options: List<String>,
): Set<String> =
    (default as? JsonArray)
        ?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull }
        ?.filterTo(mutableSetOf()) { it in options } ?: emptySet()

/** Seed state for every field that holds a string: text/password/textarea/number
 *  and `select` (which holds the selected option KEY). */
internal fun initialTexts(fields: List<JsonObject>): Map<String, String> =
    fields.mapNotNull { f ->
        val name = fieldStr(f, "name") ?: return@mapNotNull null
        when (fieldKind(f)) {
            "boolean", "checklist" -> null
            "select" -> name to selectInitial(fieldStr(f, "default"), fieldOptions(f))
            else -> name to (fieldStr(f, "default") ?: "")
        }
    }.toMap()

internal fun initialBools(fields: List<JsonObject>): Map<String, Boolean> =
    fields.mapNotNull { f ->
        val name = fieldStr(f, "name") ?: return@mapNotNull null
        if (fieldKind(f) == "boolean") name to ((f["default"] as? JsonPrimitive)?.booleanOrNull ?: false) else null
    }.toMap()

internal fun initialChecks(fields: List<JsonObject>): Map<String, Set<String>> =
    fields.mapNotNull { f ->
        val name = fieldStr(f, "name") ?: return@mapNotNull null
        if (fieldKind(f) == "checklist") name to checklistInitial(f["default"], fieldOptions(f)) else null
    }.toMap()

/**
 * The `{fields: {...}}` submit payload (plus the action's extra payload). Each kind
 * keeps its WIRE TYPE, because that is what the `chrome_*` handlers parse (web
 * parity: client.js `collectFields`): boolean → bool, checklist → array of keys in
 * server order, everything else → string — a `select` submits the option KEY, not
 * its label, so the handlers keep working untouched.
 */
internal fun collectFields(
    fields: List<JsonObject>,
    texts: Map<String, String>,
    bools: Map<String, Boolean>,
    checks: Map<String, Set<String>>,
    extra: JsonObject = JsonObject(emptyMap()),
): JsonObject =
    buildJsonObject {
        putJsonObject("fields") {
            fields.forEach { f ->
                val name = fieldStr(f, "name") ?: return@forEach
                when (fieldKind(f)) {
                    "boolean" -> put(name, bools[name] ?: false)
                    "checklist" -> {
                        val on = checks[name] ?: emptySet()
                        putJsonArray(name) { fieldOptions(f).filter { it in on }.forEach { add(it) } }
                    }
                    else -> put(name, texts[name] ?: "")
                }
            }
        }
        extra.forEach { (k, v) -> put(k, v) }
    }

/**
 * Feature 043 — a settings form. Renders each field (text / password / textarea /
 * number / boolean / select / checklist) with collected state and one or more action
 * buttons that post the SAME `{fields: {...}}` payload to a `chrome_*` handler
 * (action-submit), or the single `submit_action`. Falls back to the legacy
 * single-action button.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun ParamPickerPrimitive(
    c: Component,
    emit: Emit,
) {
    val fields = c.arr("fields")?.mapNotNull { it as? JsonObject } ?: emptyList()
    val texts = remember(c) { mutableStateMapOf<String, String>().apply { putAll(initialTexts(fields)) } }
    val bools = remember(c) { mutableStateMapOf<String, Boolean>().apply { putAll(initialBools(fields)) } }
    val checks = remember(c) { mutableStateMapOf<String, Set<String>>().apply { putAll(initialChecks(fields)) } }

    fun collect(extra: JsonObject) = collectFields(fields, texts, bools, checks, extra)

    Card(modifier = Modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(14.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            c.str("title")?.takeIf { it.isNotBlank() }?.let {
                Text(it, style = MaterialTheme.typography.titleSmall)
            }
            fields.forEach { f ->
                val name = fieldStr(f, "name") ?: return@forEach
                // 063.1: a hidden field keeps its state and still submits — it only
                // stops rendering. Reading `texts` here re-evaluates visibility on
                // every controller change (SnapshotStateMap subscription).
                if (!fieldIsVisible(f, texts)) return@forEach
                val label = fieldStr(f, "label") ?: name
                val kind = fieldKind(f)
                when {
                    kind == "boolean" ->
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Switch(checked = bools[name] ?: false, onCheckedChange = { bools[name] = it })
                            Text(label, modifier = Modifier.padding(start = 8.dp))
                        }
                    kind == "checklist" ->
                        ChecklistField(
                            label = label,
                            options = fieldOptions(f),
                            selected = checks[name] ?: emptySet(),
                            onToggle = { opt ->
                                val on = checks[name] ?: emptySet()
                                checks[name] = if (opt in on) on - opt else on + opt
                            },
                        )
                    rendersAsDropdown(f) ->
                        SelectField(
                            label = label,
                            options = fieldOptions(f),
                            selected = texts[name] ?: "",
                            onSelect = { texts[name] = it },
                        )
                    else ->
                        OutlinedTextField(
                            value = texts[name] ?: "",
                            onValueChange = { texts[name] = it },
                            modifier = Modifier.fillMaxWidth(),
                            label = { Text(label) },
                            singleLine = kind != "textarea",
                            visualTransformation =
                                if (kind == "password") PasswordVisualTransformation() else VisualTransformation.None,
                        )
                }
                fieldStr(f, "help")?.let {
                    Text(
                        it,
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            val actions = c.arr("actions")?.mapNotNull { it as? JsonObject } ?: emptyList()
            // The submit is fire-and-forget (the server re-pushes the surface on
            // success, replacing this component). Until then show a transient
            // "Saving…" so a tap is never silent (T039); a new surface resets it
            // (SurfaceContent keys items per delivery), and a lost/failed
            // re-render restores the buttons after a bounded wait — the form
            // must never be stuck spinner-only with no way to resubmit.
            var submitting by remember(c) { mutableStateOf(false) }
            if (submitting) {
                LaunchedEffect(Unit) {
                    kotlinx.coroutines.delay(SUBMIT_FAILSAFE_MS)
                    submitting = false
                }
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                    Text("Saving…", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            } else {
                // Wrap, never overflow: three action buttons (LLM's Load / Test /
                // Save) must not squeeze the last one into a vertical letter
                // stack on a phone width — extra buttons flow to the next line.
                FlowRow(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    if (actions.isNotEmpty()) {
                        actions.forEach { a ->
                            val action = (a["action"] as? JsonPrimitive)?.contentOrNull ?: return@forEach
                            val alabel = (a["label"] as? JsonPrimitive)?.contentOrNull ?: "Submit"
                            val extra = (a["payload"] as? JsonObject) ?: JsonObject(emptyMap())
                            Button(onClick = {
                                submitting = true
                                emit.event(action, collect(extra))
                            }) { Text(alabel) }
                        }
                    } else {
                        c.str("submit_action")?.let { sa ->
                            val extra = (c.attributes["submit_payload"] as? JsonObject) ?: JsonObject(emptyMap())
                            Button(onClick = {
                                submitting = true
                                emit.event(sa, collect(extra))
                            }) {
                                Text(c.str("submit_label") ?: "Save")
                            }
                        }
                    }
                }
            }
        }
    }
}

/**
 * A `select` field — an exposed dropdown, NOT a free-text box (the LLM provider
 * setup made the difference user-visible: you picked a provider on every other
 * client and typed `openai` by hand here). Shows the current option and opens the
 * server's list on tap; the picked KEY is what gets submitted. Same
 * Box + [DropdownMenu] idiom as [ColorPickerPrimitive].
 */
@Composable
private fun SelectField(
    label: String,
    options: List<String>,
    selected: String,
    onSelect: (String) -> Unit,
) {
    var open by remember { mutableStateOf(false) }
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(
            label,
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Box {
            Row(
                modifier =
                    Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(4.dp))
                        .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(4.dp))
                        .clickable(role = Role.DropdownList) { open = true }
                        .padding(horizontal = 12.dp, vertical = 14.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text(selected, modifier = Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium)
                Text("▾", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
                options.forEach { opt ->
                    DropdownMenuItem(
                        text = { Text(opt, color = MaterialTheme.colorScheme.onSurface) },
                        onClick = {
                            open = false
                            onSelect(opt)
                        },
                    )
                }
            }
        }
    }
}

/**
 * A `checklist` field — toggle chips (web parity: the aria-pressed chip row), so the
 * submit carries a LIST of keys. As a text field it submitted a String and every
 * handler expecting a list broke on Android alone.
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun ChecklistField(
    label: String,
    options: List<String>,
    selected: Set<String>,
    onToggle: (String) -> Unit,
) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(
            label,
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        if (options.isEmpty()) {
            Text(
                "(no options provided)",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        } else {
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                options.forEach { opt ->
                    FilterChip(
                        selected = opt in selected,
                        onClick = { onToggle(opt) },
                        label = { Text(opt) },
                    )
                }
            }
        }
    }
}

/**
 * Feature 044 US5 — an INTERACTIVE theme channel picker. Shows the channel's
 * swatch + hex; tapping opens a menu of on-brand choices (the presets' values for
 * this channel). Picking one restyles the app instantly ([ThemeSink]) AND persists
 * it (`save_theme {theme:{color_key, color_value}}`), matching the web round-trip.
 */
@Composable
private fun ColorPickerPrimitive(
    c: Component,
    emit: Emit,
    theme: ThemeSink,
) {
    val key = c.str("color_key").orEmpty()
    val label = c.str("label") ?: key
    var current by remember(c) { mutableStateOf(c.str("value") ?: "") }
    var open by remember { mutableStateOf(false) }
    Box {
        Row(
            modifier =
                Modifier
                    .fillMaxWidth()
                    .clickable(enabled = key.isNotBlank()) { open = true }
                    .padding(vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            ColorSwatch(current)
            Text(label, modifier = Modifier.weight(1f), style = MaterialTheme.typography.bodyMedium)
            Text(
                current,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            channelSwatchOptions(key, current).forEach { hex ->
                DropdownMenuItem(
                    text = {
                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            ColorSwatch(hex)
                            Text(hex, color = MaterialTheme.colorScheme.onSurface)
                        }
                    },
                    onClick = {
                        open = false
                        current = hex
                        val spec =
                            buildJsonObject {
                                put("color_key", key)
                                put("color_value", hex)
                            }
                        theme.apply(spec) // restyle live
                        emit.event("save_theme", buildJsonObject { put("theme", spec) }) // persist
                    },
                )
            }
        }
    }
}

/** A small rounded color chip; falls back to the surface tint for a bad hex. */
@Composable
private fun ColorSwatch(hex: String) {
    val color = hexToColor(hex) ?: MaterialTheme.colorScheme.surfaceVariant
    Box(
        modifier =
            Modifier
                .size(18.dp)
                .clip(RoundedCornerShape(4.dp))
                .background(color)
                .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(4.dp)),
    )
}

/**
 * Feature 044 US5 — `theme_apply` is a side-effect component: when it appears it
 * pushes its palette spec (preset|colors|color_key+value) to the [ThemeSink] so the
 * app restyles live. It renders no visible UI.
 */
@Composable
private fun ThemeApplyPrimitive(
    c: Component,
    theme: ThemeSink,
) {
    LaunchedEffect(c) { theme.apply(c.attributes) }
}

@Composable
private fun CodePrimitive(c: Component) {
    Surface(color = MaterialTheme.colorScheme.surfaceVariant, modifier = Modifier.fillMaxWidth()) {
        Text(
            text = c.str("content") ?: c.str("code").orEmpty(),
            fontFamily = FontFamily.Monospace,
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.padding(12.dp),
        )
    }
}

@Composable
private fun FileActionButton(
    c: Component,
    emit: Emit,
    label: String,
) {
    val action = c.str("action")
    Button(
        onClick = { if (action != null) emit.event(action, c.payload()) },
        enabled = action != null,
    ) { Text(label) }
}

/** Feature — a download button that fetches an authed backend file to the device. */
@Composable
private fun FileDownloadPrimitive(
    c: Component,
    download: Download,
) {
    val url = c.str("url") ?: c.str("download_url")
    val filename = c.str("filename") ?: c.str("title") ?: "download"
    val label = c.str("label") ?: "Download $filename"
    Button(
        onClick = { if (url != null) download.file(url, filename) },
        enabled = url != null,
    ) { Text(label) }
}
