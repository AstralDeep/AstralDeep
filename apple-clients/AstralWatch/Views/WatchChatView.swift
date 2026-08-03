import AstralCore
// Feature 051 US4 — the conversation on the wrist: crown-scrollable adapted
// components, dictation-first input with confirm-before-send, and speech
// controls (stop/replay; navigation away stops playback).
import SwiftUI

struct WatchChatView: View {
    @Environment(WatchModel.self) var model

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(model.visibleEntries) { entry in
                        entryView(entry).id(entry.id)
                    }
                    // The live canvas: identity-keyed in the MODEL (upserts
                    // morph components in place); watch views are stateless,
                    // so positional ForEach identity is safe here.
                    ForEach(Array(model.visibleCanvas.enumerated()), id: \.offset) { _, comp in
                        WatchComponentView(component: comp)
                    }
                    .id("canvas")
                    if let status = model.statusText {
                        let accessibility = WatchAccessibility060.operationStatus(status)
                        HStack(spacing: 4) {
                            if model.statusShowsActivity {
                                ProgressView().controlSize(.mini)
                            }
                            Text(InlineMarkdown.attributed(status))
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                        .accessibilityElement(children: .ignore)
                        .accessibilityIdentifier(accessibility.identifier)
                        .accessibilityLabel(accessibility.name)
                        .accessibilityValue(accessibility.state)
                        .accessibilityAddTraits(.updatesFrequently)
                    }
                    if let banner = model.errorBanner {
                        Label(banner, systemImage: "exclamationmark.triangle")
                            .font(.footnote)
                            .foregroundStyle(WatchBrand.warning)
                    }
                    inputArea
                }
            }
            .onChange(of: model.visibleEntries.count) { _, _ in
                if let last = model.visibleEntries.last {
                    withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                }
            }
            .onChange(of: model.visibleCanvas.count) { _, count in
                if count > 0 { withAnimation { proxy.scrollTo("canvas", anchor: .bottom) } }
            }
        }
        .navigationTitle("Chat")
        .toolbar {
            // Speech controls (FR-030). Explicit white glyphs: the app-wide
            // indigo tint colors the bottom-bar button circles, and a tinted
            // glyph on a tinted circle disappears into a plain dot.
            ToolbarItemGroup(placement: .bottomBar) {
                Button {
                    model.speaker.replay()
                } label: {
                    Image(systemName: "arrow.counterclockwise")
                        .foregroundStyle(.white)
                }
                .accessibilityIdentifier(WatchAccessibility060.replay.identifier)
                .accessibilityLabel(WatchAccessibility060.replay.name)
                .accessibilityValue(WatchAccessibility060.replay.state)
                .disabled(model.voiceSession != nil)
                Spacer()
                let stopAccessibility = WatchAccessibility060.stop(
                    isSpeaking: model.speaker.isSpeaking)
                Button {
                    model.speaker.stop()
                } label: {
                    Image(
                        systemName: model.speaker.isSpeaking
                            ? "speaker.slash.fill" : "speaker.wave.2"
                    )
                    .foregroundStyle(.white)
                }
                .accessibilityIdentifier(stopAccessibility.identifier)
                .accessibilityLabel(stopAccessibility.name)
                .accessibilityValue(stopAccessibility.state)
                .disabled(model.voiceSession != nil)
            }
        }
        .onDisappear { model.speaker.stop() }  // navigation stops playback
    }

    @ViewBuilder
    private func entryView(_ entry: WatchModel.Entry) -> some View {
        switch entry {
        case .user(_, let text, let attachments):
            VStack(alignment: .trailing, spacing: 3) {
                if !text.isEmpty {
                    Text(text)
                        .font(.footnote)
                        .padding(6)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                        .background(
                            WatchBrand.primary.opacity(0.25),
                            in: RoundedRectangle(cornerRadius: 8))
                }
                // Read-only name chips (FR-033): no upload affordance exists
                // on the watch — these only mirror what the turn carried.
                ForEach(attachments, id: \.self) { name in
                    Label(name, systemImage: "paperclip")
                        .font(.caption2)
                        .lineLimit(1)
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(.gray.opacity(0.25), in: Capsule())
                }
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
        case .status(_, let text):
            // Loaded assistant narrative arrives as raw markdown (parity with
            // the phone's ChatBubble) — flatten blocks and parse inline spans;
            // never show asterisks or `##`/fence syntax.
            Text(InlineMarkdown.attributed(MarkdownBlocks.plainText(text)))
                .font(.footnote).foregroundStyle(.secondary)
        case .turn(_, let components):
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(components.enumerated()), id: \.offset) { _, comp in
                    WatchComponentView(component: comp)
                }
            }
        }
    }

    /// Dictation-first (TextFieldLink opens the system dictation/scribble
    /// sheet); the dictated text lands in a pending row with explicit
    /// Send/Discard — garbled dictation never auto-sends (FR-029).
    @ViewBuilder
    private var inputArea: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let notice = model.voiceTerminalNotice {
                WatchVoiceTerminalNoticeView(notice: notice)
            }
            voiceConversationControls
            if model.pendingDictation.isEmpty {
                TextFieldLink(prompt: Text("Dictate one message")) {
                    Label("Dictate", systemImage: "text.bubble")
                        .frame(maxWidth: .infinity)
                } onSubmit: { text in
                    model.pendingDictation = text
                }
                .accessibilityIdentifier(WatchAccessibility060.dictate.identifier)
                .accessibilityLabel(WatchAccessibility060.dictate.name)
                .accessibilityValue(WatchAccessibility060.dictate.state)
            } else {
                VStack(alignment: .leading, spacing: 4) {
                    Text("“\(model.pendingDictation)”")
                        .font(.footnote)
                        .italic()
                    HStack {
                        Button("Send") { model.sendPending() }
                            .buttonStyle(.borderedProminent)
                            .accessibilityIdentifier(WatchAccessibility060.send.identifier)
                            .accessibilityLabel(WatchAccessibility060.send.name)
                            .accessibilityValue(WatchAccessibility060.send.state)
                        Button("Discard", role: .destructive) {
                            model.pendingDictation = ""
                        }
                        .accessibilityIdentifier(WatchAccessibility060.discard.identifier)
                        .accessibilityLabel(WatchAccessibility060.discard.name)
                        .accessibilityValue(WatchAccessibility060.discard.state)
                    }
                    .font(.footnote)
                }
            }
        }
    }

    @ViewBuilder
    private var voiceConversationControls: some View {
        if model.voiceComposer == nil, model.voiceTerminalNotice == nil {
            // 066/P5: before the first composer_state of a connection (and
            // after a reset clears it) the server model is absent — show a
            // disabled default mic instead of nothing, matching web's
            // pre-rendered voice-start control. The first real frame
            // replaces it. Gate on FRAME PRESENCE (composer), not on a
            // visible primary control: an owning-but-suspended session has a
            // real composer with zero visible session controls, and it must
            // read "suspended", never "checking availability".
            Button {
            } label: {
                Label("Start voice conversation", systemImage: "mic.fill")
                    .lineLimit(2)
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(WatchBrand.primary)
            .disabled(true)
            .accessibilityIdentifier("voice.conversation.primary")
            .accessibilityLabel("Start voice conversation")
            .accessibilityValue("Checking voice availability")
            Text("Checking voice availability…")
                .font(.caption2)
                .foregroundStyle(.secondary)
        } else if model.primaryVoiceControl == nil, model.voiceComposer != nil {
            // Composer present but no visible primary (e.g. this watch owns a
            // suspended session): surface the honest state label instead of
            // nothing.
            Text(model.voiceStatusLabel)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .accessibilityIdentifier("voice.conversation.state")
                .accessibilityLabel("Voice conversation")
                .accessibilityValue(model.voiceStatusLabel)
        }
        if let primary = model.primaryVoiceControl {
            Button {
                model.performPrimaryVoiceAction()
            } label: {
                Label(primary.label, systemImage: voiceIcon(primary.icon))
                    .lineLimit(2)
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .tint(model.voiceState.active ? WatchBrand.warning : WatchBrand.primary)
            .disabled(!primary.enabled || primary.busy || model.voiceActivationBusy)
            .accessibilityIdentifier("voice.conversation.primary")
            .accessibilityLabel(primary.label)
            .accessibilityValue(model.voiceStatusLabel)

            if model.voiceState.active || model.voiceState == .suspended {
                HStack(spacing: 4) {
                    ForEach(
                        model.visibleVoiceControls.filter {
                            [
                                "voice_microphone_set", "voice_speech_stop",
                                "voice_speech_mute_set", "voice_visible_chat_update",
                                "voice_sensitive_recap_request",
                            ].contains($0.action)
                        },
                        id: \.key
                    ) { control in
                        Button {
                            model.performVoiceAction(control.action)
                        } label: {
                            Image(systemName: voiceIcon(control.icon))
                        }
                        .disabled(!control.enabled || control.busy)
                        .accessibilityIdentifier("voice.conversation.\(control.key)")
                        .accessibilityLabel(control.label)
                        .accessibilityValue(control.pressed ? "On" : "Off")
                    }
                }
            }

            HStack(spacing: 4) {
                if [.connecting, .speechDetected, .transcribing, .processing, .reconnecting]
                    .contains(model.voiceState)
                {
                    ProgressView().controlSize(.mini)
                }
                Text(model.voiceStatusLabel)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityIdentifier("voice.conversation.state")
            .accessibilityLabel("Voice conversation")
            .accessibilityValue(model.voiceStatusLabel)

            if let partial = model.voicePartialTranscript, !partial.isEmpty {
                Text(partial)
                    .font(.caption2)
                    .italic()
                    .lineLimit(3)
                    .accessibilityLabel("Voice transcript: \(partial)")
            }
        }
    }

    // P11: one SF Symbol per server icon semantic, identical to the
    // iOS/macOS map in ChatView.swift (and the same glyph semantics as
    // Windows' _CONTROL_GLYPHS and web's VOICE_ICONS).
    private func voiceIcon(_ serverIcon: String) -> String {
        switch serverIcon {
        case "microphone": return "mic.fill"
        case "device-transfer": return "arrow.triangle.2.circlepath"
        case "stop": return "stop.fill"
        case "speaker-stop": return "speaker.slash.fill"
        case "speaker-muted": return "speaker.slash"
        case "speaker-consent": return "speaker.wave.2.bubble"
        case "chat": return "bubble.left.and.bubble.right"
        default: return "waveform"
        }
    }
}

/// Compact wrist presentation for the shared terminal voice notice. The
/// triangle and explicit title are non-color cues; validated server text stays
/// inert in `Text`, and dictation controls remain independent below it.
private struct WatchVoiceTerminalNoticeView: View {
    let notice: VoiceTerminalNotice

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Label(notice.title, systemImage: "exclamationmark.triangle.fill")
                .font(.caption.bold())
                .foregroundStyle(WatchBrand.error)
            Text(notice.serverMessage)
                .font(.caption2)
            if let guidance = notice.guidance {
                Text(guidance)
                    .font(.caption2)
            }
        }
        .padding(7)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            WatchBrand.error.opacity(0.14),
            in: RoundedRectangle(cornerRadius: 8)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(WatchBrand.error.opacity(0.8), lineWidth: 1)
        )
        .accessibilityElement(children: .ignore)
        .accessibilityIdentifier("voice.request.terminal.notice")
        .accessibilityLabel("Voice request alert")
        .accessibilityValue(notice.accessibilityLabel)
        .accessibilityAddTraits(.isStaticText)
        .accessibilityAddTraits(.updatesFrequently)
    }
}
