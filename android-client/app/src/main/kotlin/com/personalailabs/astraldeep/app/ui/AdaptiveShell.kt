package com.personalailabs.astraldeep.app.ui

import android.Manifest
import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.VerticalDivider
import androidx.compose.material3.adaptive.currentWindowAdaptiveInfo
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.window.core.layout.WindowWidthSizeClass
import com.personalailabs.astraldeep.app.R
import com.personalailabs.astraldeep.app.render.CanvasChrome
import com.personalailabs.astraldeep.app.render.CanvasHost
import com.personalailabs.astraldeep.app.render.MarkdownText
import com.personalailabs.astraldeep.app.render.Renderer
import com.personalailabs.astraldeep.app.transport.markMicrophonePermissionRequested
import com.personalailabs.astraldeep.app.transport.RuntimeVoiceCapability
import com.personalailabs.astraldeep.app.transport.runtimeVoiceCapability
import com.personalailabs.astraldeep.app.ui.theme.AstralColors
import com.personalailabs.astraldeep.app.voice.VoiceMediaCapability
import com.personalailabs.astraldeep.app.voice.VoiceTerminalNotice
import com.personalailabs.astraldeep.app.voice.VoiceUiState
import com.personalailabs.astraldeep.core.protocol.VoiceControl
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

/** How the chat + canvas are arranged for the current window width. */
enum class LayoutMode { Stacked, Split }

/**
 * The single adaptive rule (pure → unit-tested): a compact width (phone portrait)
 * stacks chat over canvas; medium/expanded (tablet, foldable open, landscape)
 * splits into a chat rail + canvas. One UI, reflowing by width.
 */
fun layoutModeFor(width: WindowWidthSizeClass): LayoutMode =
    if (width == WindowWidthSizeClass.COMPACT) LayoutMode.Stacked else LayoutMode.Split

@Composable
fun AdaptiveShell(
    vm: AppViewModel,
    renderer: Renderer,
) {
    val state by vm.state.collectAsStateWithLifecycle()
    val voice by vm.voiceState.collectAsStateWithLifecycle()
    val width = currentWindowAdaptiveInfo().windowSizeClass.windowWidthSizeClass
    when (layoutModeFor(width)) {
        LayoutMode.Stacked -> StackedShell(state, voice, renderer, vm)
        LayoutMode.Split -> SplitShell(state, voice, renderer, vm)
    }
}

/**
 * Phone layout, top→bottom: the SDUI canvas (the dominant ~85% area), a
 * collapsible "Messages" panel stickied above the input, and the input bar
 * (mic + paperclip). The canvas persists across turns and is only replaced when
 * a new final SDUI commits (see [AppViewModel]).
 */
@Composable
private fun StackedShell(
    state: UiState,
    voice: VoiceUiState,
    renderer: Renderer,
    vm: AppViewModel,
) {
    Column(modifier = Modifier.fillMaxSize()) {
        CanvasArea(
            state = state,
            renderer = renderer,
            onSelectSnapshot = vm::viewCanvasSnapshot,
            onBackToLive = vm::backToLiveCanvas,
            modifier = Modifier.fillMaxWidth().weight(1f),
        )
        if (state.turnActive) StepTrail(state.stepTrail)
        MessagesPanel(turns = state.visibleTurns, statusText = state.workingStatusText, renderer = renderer)
        InputBar(
            staged = state.staged,
            readOnly = state.mutationsLocked,
            voice = voice,
            onVoiceControl = vm::invokeVoiceControl,
            onSend = vm::sendChat,
            onStageFile = vm::stageAttachment,
            onRemoveAttachment = vm::removeAttachment,
            onOpenAttachments = { vm.openSurface("attachments") },
        )
    }
}

/**
 * Tablet / foldable / landscape layout: a persistent conversation rail beside the
 * canvas. Same input + timeline affordances, reflowed to the wider window.
 */
@Composable
private fun SplitShell(
    state: UiState,
    voice: VoiceUiState,
    renderer: Renderer,
    vm: AppViewModel,
) {
    // 066 canvas-first parity: the canvas leads and the conversation rail sits
    // on the trailing edge — the same arrangement as the web split mode and the
    // Windows splitter.
    Row(modifier = Modifier.fillMaxSize()) {
        CanvasArea(
            state = state,
            renderer = renderer,
            onSelectSnapshot = vm::viewCanvasSnapshot,
            onBackToLive = vm::backToLiveCanvas,
            modifier = Modifier.weight(1f).fillMaxHeight(),
        )
        VerticalDivider()
        Column(modifier = Modifier.width(360.dp).fillMaxHeight()) {
            PanelHeader("Conversation")
            ChatList(state.visibleTurns, Modifier.fillMaxWidth().weight(1f), renderer)
            if (state.turnActive) StepTrail(state.stepTrail)
            InputBar(
                staged = state.staged,
                readOnly = state.mutationsLocked,
                voice = voice,
                onVoiceControl = vm::invokeVoiceControl,
                onSend = vm::sendChat,
                onStageFile = vm::stageAttachment,
                onRemoveAttachment = vm::removeAttachment,
                onOpenAttachments = { vm.openSurface("attachments") },
            )
        }
    }
}

// ---------------------------------------------------------------------------
// Canvas area: skeleton / empty-state / live canvas + working + timeline chrome
// ---------------------------------------------------------------------------

@Composable
private fun CanvasArea(
    state: UiState,
    renderer: Renderer,
    onSelectSnapshot: (Int) -> Unit,
    onBackToLive: () -> Unit,
    modifier: Modifier = Modifier,
) {
    var showTimeline by remember { mutableStateOf(false) }
    Column(modifier = modifier.background(MaterialTheme.colorScheme.background)) {
        // A read-only banner (history), else a thin progress line for any
        // non-skeleton stretch of a turn: in-place turns, and a replacing query
        // once its first live content lands (055 — the skeleton is only the
        // loading state until then). Status text lives only in the Messages bar.
        if (state.isViewingHistory) {
            ReadOnlyBanner(
                label = state.canvasHistory.getOrNull(state.viewingIndex ?: -1)?.label,
                onBackToLive = onBackToLive,
            )
        } else if (state.hasActiveWork && !state.showSkeleton) {
            WorkingBar()
        }

        Box(modifier = Modifier.fillMaxWidth().weight(1f)) {
            when {
                state.showSkeleton -> SkeletonCanvas(Modifier.fillMaxSize())
                state.visibleCanvas.isEmpty() -> EmptyCanvasHint(Modifier.fillMaxSize())
                else ->
                    CanvasHost(
                        components = state.visibleCanvas,
                        renderer = renderer,
                        modifier = Modifier.fillMaxSize(),
                        // Refine pauses on ANY read-only view — the server timeline
                        // (mutationsLocked) and the client-side canvas snapshots.
                        chrome =
                            CanvasChrome(
                                chatId = state.activeChatId,
                                mutationsLocked = state.mutationsLocked || state.isViewingHistory,
                            ),
                    )
            }

            // Timeline entry point — only when previous canvases exist and we're live.
            if (state.canvasHistory.isNotEmpty() && !state.isViewingHistory) {
                TimelinePill(
                    count = state.canvasHistory.size,
                    onClick = { showTimeline = true },
                    modifier = Modifier.align(Alignment.TopEnd).padding(12.dp),
                )
            }

            if (showTimeline) {
                CanvasTimelineOverlay(
                    history = state.canvasHistory,
                    onSelect = { idx ->
                        onSelectSnapshot(idx)
                        showTimeline = false
                    },
                    onDismiss = { showTimeline = false },
                )
            }
        }
    }
}

/** A slim, text-free activity line for in-place turns (component actions). */
@Composable
private fun WorkingBar() {
    LinearProgressIndicator(
        modifier = Modifier.fillMaxWidth(),
        color = AstralColors.Purple,
        trackColor = AstralColors.SurfaceVariant,
    )
}

@Composable
private fun ReadOnlyBanner(
    label: String?,
    onBackToLive: () -> Unit,
) {
    Surface(color = AstralColors.Indigo.copy(alpha = 0.16f), modifier = Modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Icon(
                painter = painterResource(R.drawable.ic_history),
                contentDescription = null,
                tint = AstralColors.Indigo,
                modifier = Modifier.size(16.dp),
            )
            Column(Modifier.weight(1f)) {
                Text(
                    "Viewing a previous canvas",
                    color = MaterialTheme.colorScheme.onSurface,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.SemiBold,
                )
                if (!label.isNullOrBlank()) {
                    Text(label, color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp, maxLines = 1)
                }
            }
            Surface(
                color = AstralColors.Indigo,
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier.clickable(onClick = onBackToLive),
            ) {
                Text(
                    "Back to live",
                    color = Color.White,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Medium,
                    modifier = Modifier.padding(horizontal = 12.dp, vertical = 6.dp),
                )
            }
        }
    }
}

@Composable
private fun TimelinePill(
    count: Int,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Surface(
        color = MaterialTheme.colorScheme.surface.copy(alpha = 0.92f),
        shape = RoundedCornerShape(18.dp),
        tonalElevation = 4.dp,
        modifier = modifier.clickable(onClick = onClick),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 7.dp),
        ) {
            Icon(
                painter = painterResource(R.drawable.ic_history),
                contentDescription = null,
                tint = MaterialTheme.colorScheme.onSurface,
                modifier = Modifier.size(14.dp),
            )
            Text(
                "History ($count)",
                color = MaterialTheme.colorScheme.onSurface,
                fontSize = 12.sp,
                fontWeight = FontWeight.Medium,
            )
        }
    }
}

/** A scrim + card listing prior turns' canvases; tapping opens one read-only. */
@Composable
private fun CanvasTimelineOverlay(
    history: List<CanvasSnapshot>,
    onSelect: (Int) -> Unit,
    onDismiss: () -> Unit,
) {
    Box(
        modifier =
            Modifier.fillMaxSize().background(Color.Black.copy(alpha = 0.5f)).clickable(onClick = onDismiss),
        contentAlignment = Alignment.BottomCenter,
    ) {
        Surface(
            color = MaterialTheme.colorScheme.surface,
            shape = RoundedCornerShape(topStart = 18.dp, topEnd = 18.dp),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Text(
                    "Previous canvases",
                    color = MaterialTheme.colorScheme.onSurface,
                    fontSize = 16.sp,
                    fontWeight = FontWeight.SemiBold,
                )
                Text(
                    "Read-only snapshots from earlier turns in this chat.",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    fontSize = 12.sp,
                )
                Spacer(Modifier.height(10.dp))
                LazyColumn(
                    modifier = Modifier.fillMaxWidth().heightIn(max = 340.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    // Most-recent first.
                    val indexed = history.indices.reversed().toList()
                    items(indexed) { idx ->
                        val snap = history[idx]
                        Surface(
                            color = MaterialTheme.colorScheme.surfaceVariant,
                            shape = RoundedCornerShape(10.dp),
                            modifier = Modifier.fillMaxWidth().clickable { onSelect(idx) },
                        ) {
                            Row(
                                modifier = Modifier.fillMaxWidth().padding(14.dp),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(10.dp),
                            ) {
                                Column(Modifier.weight(1f)) {
                                    Text(
                                        snap.label.ifBlank { "Canvas ${idx + 1}" },
                                        color = MaterialTheme.colorScheme.onSurface,
                                        fontSize = 14.sp,
                                        maxLines = 1,
                                    )
                                    Text(
                                        "${snap.components.size} component${if (snap.components.size == 1) "" else "s"}",
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                        fontSize = 12.sp,
                                    )
                                }
                                Text("›", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 20.sp)
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun EmptyCanvasHint(modifier: Modifier = Modifier) {
    Box(modifier = modifier.padding(32.dp), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text("✨", fontSize = 40.sp)
            Spacer(Modifier.height(12.dp))
            Text(
                "Your generated interface appears here",
                color = MaterialTheme.colorScheme.onSurface,
                fontSize = 16.sp,
                fontWeight = FontWeight.SemiBold,
                textAlign = TextAlign.Center,
            )
            Spacer(Modifier.height(6.dp))
            Text(
                "Ask something below and AstralDeep will build a live interface for it.",
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                fontSize = 13.sp,
                textAlign = TextAlign.Center,
            )
        }
    }
}

// ---------------------------------------------------------------------------
// Messages panel (stacked): collapsible bar stickied above the input
// ---------------------------------------------------------------------------

/**
 * The text-only conversation, collapsed by default to a single "Messages" bar
 * that sits right on top of the input bar. It appears as soon as the chat has
 * any content; tapping the bar expands the transcript up over the canvas.
 */
@Composable
private fun MessagesPanel(
    turns: List<ChatTurn>,
    statusText: String?,
    renderer: Renderer,
) {
    val visible = turns.filter { it.hasVisibleContent }
    if (visible.isEmpty()) return
    // Appears expanded when the chat first has content; the user can collapse it
    // "down to just a bar" to give the canvas the full screen.
    var expanded by rememberSaveable { mutableStateOf(true) }
    Column(Modifier.fillMaxWidth()) {
        if (expanded) {
            HorizontalDivider(color = MaterialTheme.colorScheme.outline)
            ChatList(
                turns,
                Modifier
                    .fillMaxWidth()
                    .heightIn(max = 320.dp)
                    .background(MaterialTheme.colorScheme.background),
                renderer,
            )
        }
        Surface(color = MaterialTheme.colorScheme.surface, tonalElevation = 3.dp) {
            Row(
                modifier =
                    Modifier
                        .fillMaxWidth()
                        .clickable { expanded = !expanded }
                        .padding(horizontal = 16.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text(if (expanded) "▼" else "▲", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp)
                Text(
                    "Messages",
                    color = MaterialTheme.colorScheme.onSurface,
                    fontSize = 14.sp,
                    fontWeight = FontWeight.Medium,
                )
                Text("(${visible.size})", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp)
                Spacer(Modifier.weight(1f))
                if (!expanded && !statusText.isNullOrBlank()) {
                    Text(
                        statusText,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        fontSize = 12.sp,
                        maxLines = 1,
                    )
                }
            }
        }
    }
}

/**
 * The running turn's execution trail (chat_step/tool_progress) — a few small
 * muted lines by the status indicator while the orchestrator works (T021).
 */
@Composable
private fun StepTrail(lines: List<String>) {
    if (lines.isEmpty()) return
    Column(modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp)) {
        lines.takeLast(4).forEach { line ->
            Text(
                line,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                fontSize = 11.sp,
                maxLines = 1,
            )
        }
    }
}

@Composable
private fun PanelHeader(title: String) {
    Surface(color = MaterialTheme.colorScheme.surface) {
        Text(
            text = title.uppercase(),
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            fontSize = 11.sp,
            fontWeight = FontWeight.Bold,
            modifier = Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 8.dp),
        )
    }
}

@Composable
private fun ChatList(
    turns: List<ChatTurn>,
    modifier: Modifier,
    renderer: Renderer,
) {
    val visible = turns.filter { it.hasVisibleContent }
    LazyColumn(
        modifier = modifier.padding(horizontal = 12.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
        reverseLayout = true,
    ) {
        items(visible.reversed()) { turn -> ChatBubble(turn, renderer) }
    }
}

@Composable
private fun ChatBubble(
    turn: ChatTurn,
    renderer: Renderer,
) {
    if (turn.role == "reasoning") {
        ReasoningSnippet(turn.text)
        return
    }
    val isUser = turn.role == "user"
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = if (isUser) Arrangement.End else Arrangement.Start,
    ) {
        // User bubbles follow the shared tinted convention (web `.msg-user`,
        // Apple clients): a translucent primary fill + hairline primary border.
        Surface(
            color =
                if (isUser) {
                    MaterialTheme.colorScheme.primary.copy(alpha = 0.20f)
                } else {
                    MaterialTheme.colorScheme.surfaceVariant
                },
            contentColor = MaterialTheme.colorScheme.onSurface,
            shape = RoundedCornerShape(if (isUser) 10.dp else 16.dp),
            border =
                if (isUser) {
                    BorderStroke(1.dp, MaterialTheme.colorScheme.primary.copy(alpha = 0.30f))
                } else {
                    null
                },
            modifier = if (isUser) Modifier.widthIn(max = 300.dp) else Modifier.fillMaxWidth(0.96f),
        ) {
            Column(modifier = Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
                if (turn.segments.isEmpty()) {
                    if (isUser) Text(turn.text, fontSize = 14.sp) else MarkdownText(turn.text)
                } else {
                    turn.segments.forEach { segment ->
                        if (segment.kind == ChatSegmentKind.COMPONENTS) {
                            segment.components.forEach { component -> renderer.render(component) }
                        } else if (isUser) {
                            Text(segment.text, fontSize = 14.sp)
                        } else {
                            MarkdownText(segment.text)
                        }
                    }
                }
                turn.attachments.forEach { attachment ->
                    val label =
                        listOf("filename", "name", "label")
                            .firstNotNullOfOrNull { key ->
                                (attachment[key] as? JsonPrimitive)?.contentOrNull?.takeIf { it.isNotBlank() }
                            } ?: "Attachment"
                    Text(
                        text = "📎 $label",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        fontSize = 12.sp,
                        modifier = Modifier.padding(top = 6.dp),
                    )
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Input bar: server-owned conversation controls + typed fallback + attachments
// ---------------------------------------------------------------------------

/** Model reasoning shown in the chat window as a collapsed, expandable snippet. */
@Composable
private fun ReasoningSnippet(text: String) {
    var expanded by remember { mutableStateOf(false) }
    Surface(
        color = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f),
        shape = RoundedCornerShape(12.dp),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth().clickable { expanded = !expanded },
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                Text(if (expanded) "▼" else "▶", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 11.sp)
                Text(
                    "Reasoning",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Medium,
                )
            }
            if (expanded) {
                Spacer(Modifier.height(6.dp))
                MarkdownText(text)
            }
        }
    }
}

@Composable
internal fun InputBar(
    staged: List<StagedAttachment>,
    readOnly: Boolean,
    voice: VoiceUiState,
    onVoiceControl: (VoiceControl, VoiceMediaCapability) -> Unit,
    onSend: (String) -> Unit,
    onStageFile: (String, String?, ByteArray) -> Unit,
    onRemoveAttachment: (Long) -> Unit,
    onOpenAttachments: () -> Unit,
) {
    var input by rememberSaveable { mutableStateOf("") }
    var attachMenuOpen by remember { mutableStateOf(false) }
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var permissionControl by remember { mutableStateOf<VoiceControl?>(null) }

    val microphonePermission =
        rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { _ ->
            val control = permissionControl
            permissionControl = null
            if (control != null) {
                onVoiceControl(control, runtimeVoiceCapability(context).toVoiceMediaCapability())
            }
        }
    val filePicker =
        rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
            if (uri != null) {
                scope.launch(Dispatchers.IO) {
                    val picked = readPickedFile(context, uri) ?: return@launch
                    val mime = context.contentResolver.getType(uri)
                    withContext(Dispatchers.Main) { onStageFile(picked.first, mime, picked.second) }
                }
            }
        }

    fun invokeVoice(control: VoiceControl) {
        val capability = runtimeVoiceCapability(context)
        val startsSession = control.action in setOf("voice_session_start", "voice_session_takeover")
        if (startsSession && capability.hasMicrophone && capability.microphonePermission == "not_determined") {
            permissionControl = control
            markMicrophonePermissionRequested(context)
            microphonePermission.launch(Manifest.permission.RECORD_AUDIO)
        } else {
            onVoiceControl(control, capability.toVoiceMediaCapability())
        }
    }

    fun doSend() {
        if (input.isBlank() && staged.none { it.state == "ready" }) return
        onSend(input)
        input = ""
    }

    Surface(color = MaterialTheme.colorScheme.surface, tonalElevation = 2.dp) {
        Column(modifier = Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 8.dp)) {
            // Viewing the read-only timeline pauses composing (T041).
            if (readOnly) {
                Text(
                    "Viewing history — messaging is paused. Return to the live view to continue.",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    fontSize = 12.sp,
                    modifier = Modifier.padding(horizontal = 6.dp, vertical = 4.dp),
                )
            }
            if (staged.isNotEmpty()) {
                AttachmentChips(staged, onRemoveAttachment)
                Spacer(Modifier.height(6.dp))
            }
            if (voice.phase != "off" || voice.message != null || voice.transcriptPreview != null) {
                VoiceFeedback(voice)
                Spacer(Modifier.height(4.dp))
            }
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                voice.composer?.controls.orEmpty().filter { it.visible }.forEach { control ->
                    VoiceControlButton(
                        control = control,
                        phase = voice.phase,
                        enabled = !readOnly && control.enabled,
                        onClick = { invokeVoice(control) },
                    )
                }
                OutlinedTextField(
                    value = input,
                    onValueChange = { input = it },
                    modifier = Modifier.weight(1f).testTag("chat-input"),
                    enabled = !readOnly,
                    placeholder = { Text("Message AstralDeep…") },
                    maxLines = 4,
                    shape = RoundedCornerShape(22.dp),
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Default),
                )
                // Paperclip → a menu mirroring the web: Upload a file, or Choose
                // from your files (opens the attachments surface, T047).
                Box {
                    GlyphButton(iconRes = R.drawable.ic_paperclip, contentDescription = "Attach a file", enabled = !readOnly) {
                        attachMenuOpen = true
                    }
                    DropdownMenu(expanded = attachMenuOpen, onDismissRequest = { attachMenuOpen = false }) {
                        DropdownMenuItem(
                            text = { Text("Upload a file") },
                            onClick = {
                                attachMenuOpen = false
                                filePicker.launch("*/*")
                            },
                        )
                        DropdownMenuItem(
                            text = { Text("Choose from your files") },
                            onClick = {
                                attachMenuOpen = false
                                onOpenAttachments()
                            },
                        )
                    }
                }
                SendButton(enabled = !readOnly && (input.isNotBlank() || staged.any { it.state == "ready" }), onClick = ::doSend)
            }
        }
    }
}

private fun RuntimeVoiceCapability.toVoiceMediaCapability(): VoiceMediaCapability =
    VoiceMediaCapability(
        hasMicrophone = hasMicrophone,
        hasAudioOutput = hasAudioOutput,
        microphonePermission = microphonePermission,
        fullDuplex = fullDuplex,
    )

@Composable
internal fun VoiceFeedback(voice: VoiceUiState) {
    voice.terminalNotice?.let {
        VoiceTerminalNoticeCard(it)
        return
    }
    val text = voice.transcriptPreview ?: voice.message ?: return
    val error = voice.phase == "error" || voice.reason in setOf("permission_denied", "permission_restricted")
    Text(
        text = text,
        color = if (error) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant,
        fontSize = 12.sp,
        maxLines = 2,
        modifier = Modifier.padding(horizontal = 8.dp).testTag("voice-feedback"),
    )
}

@Composable
private fun VoiceTerminalNoticeCard(notice: VoiceTerminalNotice) {
    val prominentWarning = notice.isRequestFailure || notice.speechUnavailable
    val containerColor =
        if (prominentWarning) {
            MaterialTheme.colorScheme.errorContainer
        } else {
            MaterialTheme.colorScheme.tertiaryContainer
        }
    val contentColor =
        if (prominentWarning) {
            MaterialTheme.colorScheme.onErrorContainer
        } else {
            MaterialTheme.colorScheme.onTertiaryContainer
        }
    val marker = if (prominentWarning) "!" else "i"
    Surface(
        color = containerColor,
        contentColor = contentColor,
        shape = RoundedCornerShape(12.dp),
        border = BorderStroke(2.dp, contentColor),
        modifier =
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 4.dp)
                .testTag("voice-terminal-notice")
                .semantics(mergeDescendants = true) {
                    liveRegion = LiveRegionMode.Assertive
                    stateDescription = notice.accessibilityText
                },
    ) {
        Row(
            modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            verticalAlignment = Alignment.Top,
        ) {
            Surface(
                color = Color.Transparent,
                contentColor = contentColor,
                shape = RoundedCornerShape(50),
                border = BorderStroke(2.dp, contentColor),
                modifier = Modifier.size(30.dp),
            ) {
                Box(contentAlignment = Alignment.Center) {
                    Text(
                        text = marker,
                        fontSize = 16.sp,
                        fontWeight = FontWeight.Bold,
                    )
                }
            }
            Column(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(3.dp),
            ) {
                Text(
                    text = notice.title,
                    fontSize = 14.sp,
                    fontWeight = FontWeight.Bold,
                )
                notice.serverMessage?.takeIf { it.isNotBlank() }?.let { safeMessage ->
                    Text(
                        text = safeMessage,
                        fontSize = 13.sp,
                    )
                }
                Text(
                    text = notice.guidance,
                    fontSize = 13.sp,
                    fontWeight = FontWeight.Medium,
                )
            }
        }
    }
}

@Composable
internal fun VoiceControlButton(
    control: VoiceControl,
    phase: String,
    enabled: Boolean,
    onClick: () -> Unit,
) {
    val icon =
        when (control.action) {
            "voice_session_end", "voice_speech_stop" -> R.drawable.ic_stop
            "voice_speech_mute_set" -> R.drawable.ic_volume
            "voice_visible_chat_update" -> R.drawable.ic_chat
            "voice_sensitive_recap_request" -> R.drawable.ic_sparkle
            else -> R.drawable.ic_mic
        }
    val description =
        buildString {
            append(control.label)
            if (control.busy) append(", busy")
            else if (control.pressed) append(", on")
            append(", $phase")
        }
    IconButton(
        onClick = onClick,
        enabled = enabled,
        modifier =
            Modifier
                .testTag("voice-control-${control.action}")
                .semantics { stateDescription = description },
    ) {
        Icon(
            painter = painterResource(icon),
            contentDescription = control.label,
            tint =
                when {
                    !enabled -> MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.4f)
                    control.pressed -> AstralColors.Indigo
                    else -> MaterialTheme.colorScheme.onSurfaceVariant
                },
            modifier = Modifier.size(22.dp),
        )
    }
}

@Composable
private fun GlyphButton(
    iconRes: Int,
    contentDescription: String,
    enabled: Boolean = true,
    onClick: () -> Unit,
) {
    val base = MaterialTheme.colorScheme.onSurfaceVariant
    IconButton(onClick = onClick, enabled = enabled) {
        Icon(
            painter = painterResource(iconRes),
            contentDescription = contentDescription,
            tint = if (enabled) base else base.copy(alpha = 0.4f),
            modifier = Modifier.size(22.dp),
        )
    }
}

@Composable
private fun SendButton(
    enabled: Boolean,
    onClick: () -> Unit,
) {
    val bg = if (enabled) AstralColors.Indigo else AstralColors.SurfaceVariant
    Box(
        modifier =
            Modifier
                .size(44.dp)
                .clip(RoundedCornerShape(22.dp))
                .background(bg)
                .clickable(enabled = enabled, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Icon(
            painter = painterResource(R.drawable.ic_send),
            contentDescription = "Send",
            tint = if (enabled) Color.White else MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.size(20.dp),
        )
    }
}

@Composable
private fun AttachmentChips(
    staged: List<StagedAttachment>,
    onRemove: (Long) -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        staged.forEach { att ->
            Surface(
                color = MaterialTheme.colorScheme.surfaceVariant,
                shape = RoundedCornerShape(14.dp),
            ) {
                Row(
                    modifier = Modifier.padding(start = 10.dp, end = 6.dp, top = 5.dp, bottom = 5.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    val marker =
                        when (att.state) {
                            "uploading" -> "…"
                            "failed" -> "⚠"
                            else -> "📄"
                        }
                    Text(marker, fontSize = 12.sp)
                    Column {
                        Text(
                            att.filename,
                            color = MaterialTheme.colorScheme.onSurface,
                            fontSize = 12.sp,
                            maxLines = 1,
                            modifier = Modifier.widthIn(max = 160.dp),
                        )
                        if (!att.note.isNullOrBlank()) {
                            Text(att.note, color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 10.sp, maxLines = 1)
                        }
                    }
                    Text(
                        "×",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        fontSize = 16.sp,
                        modifier = Modifier.clickable { onRemove(att.uid) }.padding(horizontal = 4.dp),
                    )
                }
            }
        }
    }
}

/** Read a picked file's display name + bytes off the ContentResolver (IO thread). */
private fun readPickedFile(
    context: Context,
    uri: Uri,
): Pair<String, ByteArray>? =
    runCatching {
        val name =
            context.contentResolver.query(uri, null, null, null, null)?.use { c ->
                val idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (idx >= 0 && c.moveToFirst()) c.getString(idx) else null
            } ?: uri.lastPathSegment ?: "file"
        val bytes = context.contentResolver.openInputStream(uri)?.use { it.readBytes() } ?: return@runCatching null
        name to bytes
    }.getOrNull()
