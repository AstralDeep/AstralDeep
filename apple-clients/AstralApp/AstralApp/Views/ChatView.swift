import AstralCore
// Feature 051 — the adaptive chat shell, a 1:1 match to the Android AdaptiveShell:
// a canvas-dominant area (skeleton while a replacing turn is in flight, empty-
// state hint, live working bar, read-only timeline banner + snapshot overlay), a
// collapsible "Messages" panel with reasoning snippets, the execution step trail,
// and an input bar (mic · attachment chips · rounded field · paperclip · send).
// Compact widths stack; regular widths (iPad/landscape/macOS) split into a rail.
import SwiftUI
import UniformTypeIdentifiers

#if os(iOS)
    import PhotosUI
#endif

struct ChatShell: View {
    @Environment(AppModel.self) var model
    #if os(iOS)
        @Environment(\.horizontalSizeClass) private var hSize
    #endif
    private var isSplit: Bool {
        #if os(iOS)
            return hSize == .regular
        #else
            return true
        #endif
    }
    var body: some View {
        Group {
            if isSplit { SplitShell() } else { StackedShell() }
        }
        #if os(macOS)
            // T033/FR-017: Finder drag-and-drop stages chips exactly like the
            // file dialog (Windows-client parity).
            .dropDestination(for: URL.self) { urls, _ in
                guard !model.mutationsLocked, !urls.isEmpty else { return false }
                for url in urls {
                    model.stageFile(url: url)
                }
                return true
            }
        #endif
    }
}

// MARK: - Layouts

private struct StackedShell: View {
    @Environment(AppModel.self) var model
    var body: some View {
        VStack(spacing: 0) {
            CanvasArea().frame(maxWidth: .infinity, maxHeight: .infinity)
            if model.turnActive { StepTrailView(lines: model.stepTrail) }
            MessagesPanel()
            InputBar()
        }
    }
}

private struct SplitShell: View {
    @Environment(AppModel.self) var model
    @Environment(ThemeStore.self) var theme
    var body: some View {
        HStack(spacing: 0) {
            VStack(spacing: 0) {
                PanelHeader(title: "Conversation")
                // Same layout firewall as CanvasArea: without it the rail
                // VStack's flexible rounds measure the full transcript per
                // proposal (063 livelock class — this is the macOS/iPad shape).
                GeometryReader { geo in
                    ChatList().frame(width: geo.size.width, height: geo.size.height)
                }
                if model.turnActive { StepTrailView(lines: model.stepTrail) }
                InputBar()
            }
            .frame(width: 360)
            Divider().overlay(theme.palette.border)
            CanvasArea().frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

// MARK: - Canvas

private struct CanvasArea: View {
    @Environment(AppModel.self) var model
    @Environment(ThemeStore.self) var theme
    @State private var showTimeline = false
    @State private var refineTarget: RefineTarget?
    private var p: AstralPalette { theme.palette }

    private var canvasItems: [(key: String, comp: AstralComponent)] {
        model.visibleCanvas.enumerated().map { index, comp in
            (comp.componentId ?? "anon-\(index)", comp)
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            if model.isViewingHistory {
                ReadOnlyBanner(label: model.viewingIndex.flatMap { model.canvasHistory[safe: $0]?.label }) {
                    model.backToLiveCanvas()
                }
            } else if model.turnActive && model.statusShowsActivity && !model.showSkeleton {
                if ContinuousActivityPresentation.allowsAnimatedIndicators {
                    ProgressView().progressViewStyle(.linear).tint(p.secondary)
                } else {
                    Rectangle()
                        .fill(p.secondary.opacity(0.65))
                        .frame(height: 2)
                        .accessibilityHidden(true)
                }
            }
            ZStack(alignment: .topTrailing) {
                // GeometryReader is a layout firewall: it answers every parent
                // proposal in O(1) and lays the scroll content out ONCE at the
                // final concrete size. Without it, the shell VStack's flexible-
                // space rounds ask this subtree for its ideal height, and a
                // vertical ScrollView answers that by realizing + measuring its
                // ENTIRE LazyVStack — one component of the combinatorial layout
                // pass behind the 063 stuck-canvas livelock (same class as the
                // shimmer trigger fixed earlier; see StepTrailView/MessagesPanel).
                GeometryReader { geo in
                    Group {
                        if model.showSkeleton {
                            SkeletonCanvas()
                        } else if model.visibleCanvas.isEmpty {
                            EmptyCanvasHint()
                        } else {
                            ScrollView {
                                LazyVStack(alignment: .leading, spacing: 12) {
                                    // Keyed by component identity so a `remove` op
                                    // doesn't shift every later component onto a new
                                    // SwiftUI identity (resetting tabs/collapsibles
                                    // and scroll anchors — FR-013).
                                    ForEach(canvasItems, id: \.key) { item in
                                        // 055 US4/US5 chrome: provenance badge +
                                        // refine/export context menu (top-level only).
                                        ComponentChrome(
                                            component: item.comp,
                                            interactive: !model.isViewingHistory,
                                            onRefine: { refineTarget = $0 })
                                    }
                                }
                                .padding(16)
                            }
                            .scrollDismissesKeyboard(.immediately)
                        }
                    }
                    .frame(width: geo.size.width, height: geo.size.height)
                }

                if !model.isViewingHistory {
                    HStack(spacing: 8) {
                        // 055 US5 (T045): canvas HTML export, opened in the
                        // system browser (session-authed route).
                        if !model.visibleCanvas.isEmpty, !model.showSkeleton,
                            let exportURL = model.exportCanvasURL()
                        {
                            CanvasExportPill(url: exportURL)
                        }
                        if !model.canvasHistory.isEmpty {
                            TimelinePill(count: model.canvasHistory.count) { showTimeline = true }
                        }
                    }
                    .padding(12)
                }
            }
        }
        .background(p.bg)
        .sheet(isPresented: $showTimeline) {
            CanvasTimelineOverlay(history: model.canvasHistory) { idx in
                model.viewCanvasSnapshot(idx)
                showTimeline = false
            }
        }
        .sheet(item: $refineTarget) { target in
            RefineSheet(target: target)
        }
    }
}

private struct CanvasExportPill: View {
    @Environment(ThemeStore.self) var theme
    let url: URL
    private var p: AstralPalette { theme.palette }
    var body: some View {
        Link(destination: url) {
            HStack(spacing: 6) {
                Image(systemName: "square.and.arrow.up").font(.caption2)
                Text("Export").font(.caption.weight(.medium))
            }
            .foregroundStyle(p.text)
            .padding(.horizontal, 12).padding(.vertical, 7)
            .background(p.surface.opacity(0.92), in: Capsule())
            .overlay(Capsule().stroke(p.border))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Export this canvas as HTML")
    }
}

private struct SkeletonCanvas: View {
    @Environment(ThemeStore.self) var theme
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(0..<4, id: \.self) { i in
                RoundedRectangle(cornerRadius: AstralRadius.md)
                    .fill(theme.palette.surface.opacity(0.5))
                    .frame(height: i == 0 ? 90 : 60)
                    .frame(maxWidth: .infinity)
                    .activityShimmer()
            }
            Spacer()
        }
        .padding(16)
    }
}

private struct EmptyCanvasHint: View {
    @Environment(ThemeStore.self) var theme
    private var p: AstralPalette { theme.palette }
    var body: some View {
        VStack(spacing: 8) {
            Text("✨").font(.system(size: 40))
            Text("Your generated interface appears here")
                .font(.headline).foregroundStyle(p.text).multilineTextAlignment(.center)
            Text("Ask something below and AstralDeep will build a live interface for it.")
                .font(.subheadline).foregroundStyle(p.muted).multilineTextAlignment(.center)
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct ReadOnlyBanner: View {
    @Environment(ThemeStore.self) var theme
    let label: String?
    let onBackToLive: () -> Void
    private var p: AstralPalette { theme.palette }
    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "clock.arrow.circlepath").foregroundStyle(p.primary)
            VStack(alignment: .leading, spacing: 1) {
                Text("Viewing a previous canvas").font(.footnote.weight(.semibold)).foregroundStyle(p.text)
                if let label, !label.isEmpty {
                    Text(label).font(.caption).foregroundStyle(p.muted).lineLimit(1)
                }
            }
            Spacer(minLength: 8)
            Button("Back to live", action: onBackToLive)
                .font(.caption.weight(.medium))
                .foregroundStyle(.white)
                .padding(.horizontal, 12).padding(.vertical, 6)
                .background(p.primary, in: Capsule())
                .buttonStyle(.plain)
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(p.primary.opacity(0.16))
    }
}

private struct TimelinePill: View {
    @Environment(ThemeStore.self) var theme
    let count: Int
    let onClick: () -> Void
    private var p: AstralPalette { theme.palette }
    var body: some View {
        Button(action: onClick) {
            HStack(spacing: 6) {
                Image(systemName: "clock.arrow.circlepath").font(.caption2)
                Text("History (\(count))").font(.caption.weight(.medium))
            }
            .foregroundStyle(p.text)
            .padding(.horizontal, 12).padding(.vertical, 7)
            .background(p.surface.opacity(0.92), in: Capsule())
            .overlay(Capsule().stroke(p.border))
        }
        .buttonStyle(.plain)
    }
}

private struct CanvasTimelineOverlay: View {
    @Environment(ThemeStore.self) var theme
    let history: [AppModel.CanvasSnapshot]
    let onSelect: (Int) -> Void
    private var p: AstralPalette { theme.palette }
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Previous canvases").font(.headline).foregroundStyle(p.text)
            Text("Read-only snapshots from earlier turns in this chat.")
                .font(.caption).foregroundStyle(p.muted)
            ScrollView {
                LazyVStack(spacing: 8) {
                    ForEach(Array(history.enumerated()).reversed(), id: \.offset) { idx, snap in
                        Button {
                            onSelect(idx)
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(snap.label.isEmpty ? "Canvas \(idx + 1)" : snap.label)
                                        .foregroundStyle(p.text).lineLimit(1)
                                    Text("\(snap.components.count) component\(snap.components.count == 1 ? "" : "s")")
                                        .font(.caption).foregroundStyle(p.muted)
                                }
                                Spacer()
                                Text("›").foregroundStyle(p.muted)
                            }
                            .padding(14)
                            .background(p.surface2, in: RoundedRectangle(cornerRadius: AstralRadius.md))
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
        .padding(16)
        .background(p.bg.ignoresSafeArea())
        .presentationDetents([.medium, .large])
    }
}

// MARK: - Messages / rail

private struct StepTrailView: View {
    @Environment(ThemeStore.self) var theme
    let lines: [String]
    var body: some View {
        if lines.isEmpty {
            EmptyView()
        } else {
            // One Text, not a ForEach of rows: the trail updates on every
            // chat_step during a live turn, and per-row flexible layout fed the
            // shell's flexible-space rounds (063 livelock). A single bounded
            // Text is one cheap measure — and it can't hit the duplicate-
            // identity hazard `ForEach(id: \.self)` had when a step repeats
            // (two `✗ run_job` lines in one turn).
            Text(lines.suffix(4).joined(separator: "\n"))
                .font(.caption2).foregroundStyle(theme.palette.muted)
                .lineLimit(4)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 16).padding(.vertical, 4)
        }
    }
}

private struct MessagesPanel: View {
    @Environment(AppModel.self) var model
    @Environment(ThemeStore.self) var theme
    @State private var expanded = true
    private var p: AstralPalette { theme.palette }
    private var visible: [AppModel.ChatTurn] {
        model.visibleTurns.filter { !$0.text.isEmpty || !$0.components.isEmpty }
    }

    var body: some View {
        if visible.isEmpty {
            EmptyView()
        } else {
            VStack(spacing: 0) {
                if expanded {
                    Divider().overlay(p.border)
                    // A CONCRETE height, not maxHeight: shrink-to-fit required
                    // measuring the whole transcript (a vertical ScrollView's
                    // ideal height realizes every LazyVStack row, including the
                    // long markdown bubbles) on every flexible-space round of
                    // the shell VStack — the core multiplier of the 063
                    // stuck-canvas layout livelock. Fixed height = O(1) answer.
                    ChatList().frame(height: 320).background(p.bg)
                }
                Button {
                    withAnimation { expanded.toggle() }
                } label: {
                    HStack(spacing: 8) {
                        Text(expanded ? "▼" : "▲").font(.caption2).foregroundStyle(p.muted)
                        Text("Messages").font(.subheadline.weight(.medium)).foregroundStyle(p.text)
                        Text("(\(visible.count))").font(.caption).foregroundStyle(p.muted)
                        Spacer()
                        if !expanded, let status = model.statusText {
                            Text(status).font(.caption).foregroundStyle(p.muted).lineLimit(1)
                        }
                    }
                    .padding(.horizontal, 16).padding(.vertical, 10)
                    .background(p.surface)
                }
                .buttonStyle(.plain)
            }
        }
    }
}

private struct PanelHeader: View {
    @Environment(ThemeStore.self) var theme
    let title: String
    var body: some View {
        Text(title.uppercased())
            .font(.caption2.bold()).foregroundStyle(theme.palette.muted)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14).padding(.vertical, 8)
            .background(theme.palette.surface)
    }
}

private struct ChatList: View {
    @Environment(AppModel.self) var model
    private var visible: [AppModel.ChatTurn] {
        model.visibleTurns.filter { !$0.text.isEmpty || !$0.components.isEmpty }
    }

    @ViewBuilder
    private var rows: some View {
        ForEach(visible) { turn in ChatBubble(turn: turn) }
        if let status = model.statusText {
            StatusLine(text: status, showsActivity: model.statusShowsActivity)
                .id("status")
        }
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                if TranscriptLayoutPresentation.usesLazyRows {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        rows
                    }
                    .padding(.horizontal, 12).padding(.vertical, 8)
                } else {
                    // AppKit's lazy placement engine can fail to converge when
                    // a voice turn replaces pending rows with committed rows
                    // while the status row disappears. An eager stack has a
                    // deterministic content height and avoids that graph loop.
                    VStack(alignment: .leading, spacing: 8) {
                        rows
                    }
                    .padding(.horizontal, 12).padding(.vertical, 8)
                }
            }
            .accessibilityIdentifier("conversation-message-scroll")
            .scrollDismissesKeyboard(.immediately)
            .onChange(of: visible.count) { oldCount, newCount in
                guard newCount > oldCount, let lastID = visible.last?.id else { return }
                // Do not mutate scroll geometry inside the same AttributeGraph
                // transaction that inserted the row. Even an unanimated
                // synchronous scroll can feed AppKit's anchor translation back
                // into lazy placement before that transaction settles.
                Task { @MainActor in
                    await Task.yield()
                    guard !Task.isCancelled else { return }
                    proxy.scrollTo(lastID, anchor: .bottom)
                }
            }
        }
    }
}

private struct StatusLine: View {
    @Environment(ThemeStore.self) var theme
    let text: String
    let showsActivity: Bool
    var body: some View {
        HStack(spacing: 6) {
            if showsActivity {
                if ContinuousActivityPresentation.allowsAnimatedIndicators {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "ellipsis")
                        .font(.caption2.weight(.semibold))
                        .accessibilityHidden(true)
                }
            }
            Text(text).font(.caption).foregroundStyle(theme.palette.muted)
        }
    }
}

private struct ChatBubble: View {
    @Environment(ThemeStore.self) var theme
    let turn: AppModel.ChatTurn
    private var p: AstralPalette { theme.palette }
    var body: some View {
        if turn.role == "reasoning" {
            ReasoningSnippet(text: turn.text)
        } else {
            let isUser = turn.role == "user"
            HStack {
                if isUser { Spacer(minLength: 40) }
                VStack(alignment: .leading, spacing: 8) {
                    if !turn.text.isEmpty {
                        if isUser {
                            Text(turn.text).foregroundStyle(p.text)
                        }
                        // Assistant narrative (incl. doc cards diverted into the
                        // transcript) carries block markdown — headings, fences,
                        // lists and tables must render, not show their syntax.
                        else {
                            MarkdownBlockView(source: turn.text).foregroundStyle(p.text)
                        }
                    }
                    ForEach(Array(turn.components.enumerated()), id: \.offset) { _, component in
                        ComponentView(component: component)
                    }
                }
                .font(.subheadline)
                .padding(.horizontal, 14).padding(.vertical, 10)
                // User turns are the web's 20% primary tint + 30% border —
                // not a saturated pill (cross-client bubble convention).
                .background(
                    isUser ? AnyShapeStyle(p.primary.opacity(0.20)) : AnyShapeStyle(p.surface2),
                    in: RoundedRectangle(cornerRadius: AstralRadius.md)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: AstralRadius.md)
                        .stroke(isUser ? p.primary.opacity(0.30) : .clear)
                )
                .frame(maxWidth: isUser ? 300 : .infinity, alignment: isUser ? .trailing : .leading)
                if !isUser { Spacer(minLength: 20) }
            }
            .frame(maxWidth: .infinity, alignment: isUser ? .trailing : .leading)
        }
    }
}

private struct ReasoningSnippet: View {
    @Environment(ThemeStore.self) var theme
    let text: String
    @State private var expanded = false
    private var p: AstralPalette { theme.palette }
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation { expanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Text(expanded ? "▼" : "▶").font(.caption2).foregroundStyle(p.muted)
                    Text("Reasoning").font(.caption.weight(.medium)).foregroundStyle(p.muted)
                    Spacer(minLength: 0)
                }
            }
            .buttonStyle(.plain)
            if expanded {
                Text(text).font(.caption).foregroundStyle(p.text)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(p.surface2.opacity(0.5), in: RoundedRectangle(cornerRadius: 12))
    }
}

// MARK: - Input bar

private struct InputBar: View {
    @Environment(AppModel.self) var model
    @Environment(ThemeStore.self) var theme
    @State private var input = ""
    @State private var showImporter = false
    #if os(iOS)
        @State private var showPhotoPicker = false
        @State private var photoItem: PhotosPickerItem?
    #endif
    @FocusState private var focused: Bool
    private var p: AstralPalette { theme.palette }
    private let slashCommands = ["/help", "/agents", "/summarize", "/research", "/weather"]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if model.mutationsLocked {
                Text("Viewing history — messaging is paused. Return to the live view to continue.")
                    .font(.caption).foregroundStyle(p.muted)
                    .padding(.horizontal, 6)
            }
            if !model.staged.isEmpty {
                AttachmentChips(staged: model.staged) { model.removeAttachment($0) }
            }
            if input.hasPrefix("/") && !input.contains(" ") {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(slashCommands.filter { $0.hasPrefix(input) }, id: \.self) { cmd in
                            Button(cmd) { input = cmd + " " }
                                .font(.caption.monospaced()).foregroundStyle(p.primary)
                        }
                    }
                }
            }
            VoiceComposerControls()
            HStack(spacing: 6) {
                TextField("Message AstralDeep…", text: $input, axis: .vertical)
                    .textFieldStyle(.plain)
                    .accessibilityIdentifier("chat-composer-input")
                    .accessibilityLabel("Message AstralDeep")
                    .submitLabel(.send)
                    .lineLimit(1...4)
                    .disabled(model.mutationsLocked)
                    .focused($focused)
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .background(p.surface2, in: RoundedRectangle(cornerRadius: 22))
                    .overlay(RoundedRectangle(cornerRadius: 22).stroke(p.border))
                    .onSubmit(send)
                Menu {
                    Button("Upload a file") { showImporter = true }
                    #if os(iOS)
                        Button("Choose a photo") { showPhotoPicker = true }
                    #endif
                    Button("Choose from your files") { model.openSurface("attachments") }
                } label: {
                    Image(systemName: "paperclip").font(.system(size: 18)).foregroundStyle(p.muted)
                }
                .disabled(model.mutationsLocked)
                .accessibilityLabel("Attach a file")
                SendButton(enabled: canSend) { send() }
            }
        }
        .padding(.horizontal, 8).padding(.vertical, 8)
        .background(p.surface)
        .fileImporter(
            isPresented: $showImporter, allowedContentTypes: [.item],
            allowsMultipleSelection: true
        ) { result in
            guard case .success(let urls) = result else { return }
            for url in urls {
                model.stageFile(url: url)
            }
        }
        #if os(iOS)
            .photosPicker(isPresented: $showPhotoPicker, selection: $photoItem, matching: .images)
            .onChange(of: photoItem) { _, item in
                guard let item else { return }
                Task {
                    if let data = try? await item.loadTransferable(type: Data.self) {
                        let ext = item.supportedContentTypes.first?.preferredFilenameExtension ?? "jpg"
                        let mime = item.supportedContentTypes.first?.preferredMIMEType
                        model.stageAttachment(
                            filename: "photo-\(UUID().uuidString.prefix(8)).\(ext)",
                            mimeType: mime, data: data)
                    }
                    photoItem = nil
                }
            }
        #endif
    }

    private var canSend: Bool {
        !model.mutationsLocked
            && (!input.trimmingCharacters(in: .whitespaces).isEmpty || model.staged.contains { $0.state == "ready" })
    }

    private func send() {
        guard canSend else { return }
        let submittedInput = input
        focused = false  // resign native keyboard focus before model-driven re-rendering
        input = ""
        model.sendChat(submittedInput)
    }
}

/// The order, visibility, labels, pressed state, and enabled state all come
/// from the server-owned composer model. This view contributes presentation
/// only; it cannot invent a local voice mutation or bypass REST authorization.
private struct VoiceComposerControls: View {
    @Environment(AppModel.self) var model
    @Environment(ThemeStore.self) var theme
    private var p: AstralPalette { theme.palette }

    var body: some View {
        let controls = model.voice.composer?.controls.filter(\.visible) ?? []
        if !controls.isEmpty || model.voice.active || model.voice.terminalNotice != nil {
            VStack(alignment: .leading, spacing: 5) {
                if let notice = model.voice.terminalNotice {
                    VoiceTerminalNoticeView(notice: notice)
                }
                if !controls.isEmpty {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 6) {
                            ForEach(controls) { control in
                                Button {
                                    Task { await model.performVoiceControl(control.action) }
                                } label: {
                                    HStack(spacing: 5) {
                                        if control.busy {
                                            if ContinuousActivityPresentation.allowsAnimatedIndicators {
                                                ProgressView().controlSize(.small)
                                            } else {
                                                Image(systemName: "ellipsis")
                                                    .accessibilityHidden(true)
                                            }
                                        } else {
                                            Image(systemName: symbol(control.icon))
                                        }
                                        Text(control.label).lineLimit(1)
                                    }
                                    .font(.caption.weight(.semibold))
                                    .padding(.horizontal, 10).padding(.vertical, 6)
                                    .foregroundStyle(control.pressed ? p.surface : p.primary)
                                    .background(
                                        control.pressed ? p.primary : p.surface2,
                                        in: Capsule()
                                    )
                                    .overlay(Capsule().stroke(p.border))
                                }
                                .buttonStyle(.plain)
                                .disabled(!control.enabled || control.busy)
                                .accessibilityIdentifier("voice-control-\(control.key)")
                                .accessibilityLabel(control.label)
                                .accessibilityValue(
                                    control.busy ? "In progress" : (control.pressed ? "On" : "Off"))
                            }
                        }
                    }
                }
                if model.voice.active, let message = model.voice.message, !message.isEmpty {
                    HStack(spacing: 5) {
                        Image(systemName: model.voice.mediaConnected ? "waveform" : "waveform.slash")
                        Text(message).lineLimit(2)
                        if model.voice.awaitingAcceptance > 0 {
                            if ContinuousActivityPresentation.allowsAnimatedIndicators {
                                ProgressView().controlSize(.mini)
                            } else {
                                Image(systemName: "ellipsis")
                                    .font(.caption2.weight(.semibold))
                                    .accessibilityHidden(true)
                            }
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(p.muted)
                    .accessibilityElement(children: .combine)
                    .accessibilityIdentifier("voice-conversation-status")
                }
            }
        }
    }

    private func symbol(_ serverIcon: String) -> String {
        switch serverIcon {
        case "microphone": "mic.fill"
        case "device-transfer": "arrow.triangle.2.circlepath"
        case "stop": "stop.fill"
        case "speaker-stop": "speaker.slash.fill"
        case "speaker-muted": "speaker.slash"
        case "speaker-consent": "speaker.wave.2.bubble"
        case "chat": "bubble.left.and.bubble.right"
        default: "waveform"
        }
    }
}

/// A visible and VoiceOver-readable alert anchored to the chat composer. Its
/// icon and explicit title preserve meaning independently of theme color, and
/// all server text is rendered by `Text` as inert plain content.
private struct VoiceTerminalNoticeView: View {
    @Environment(ThemeStore.self) var theme
    let notice: VoiceTerminalNotice
    private var p: AstralPalette { theme.palette }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.body.weight(.semibold))
                .foregroundStyle(p.error)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(notice.title)
                    .font(.caption.weight(.bold))
                    .foregroundStyle(p.text)
                Text(notice.serverMessage)
                    .font(.caption)
                    .foregroundStyle(p.text)
                if let guidance = notice.guidance {
                    Text(guidance)
                        .font(.caption)
                        .foregroundStyle(p.text)
                }
            }
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(p.error.opacity(0.14), in: RoundedRectangle(cornerRadius: AstralRadius.md))
        .overlay(
            RoundedRectangle(cornerRadius: AstralRadius.md)
                .stroke(p.error.opacity(0.75), lineWidth: 1)
        )
        .accessibilityElement(children: .ignore)
        .accessibilityIdentifier("voice-request-terminal-notice")
        .accessibilityLabel("Voice request alert")
        .accessibilityValue(notice.accessibilityLabel)
        .accessibilityAddTraits(.isStaticText)
        .accessibilityAddTraits(.updatesFrequently)
    }
}

private struct AttachmentChips: View {
    @Environment(ThemeStore.self) var theme
    let staged: [AppModel.StagedAttachment]
    let onRemove: (Int) -> Void
    private var p: AstralPalette { theme.palette }
    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(staged) { att in
                    HStack(spacing: 6) {
                        Text(marker(att.state)).font(.caption2)
                        VStack(alignment: .leading, spacing: 0) {
                            Text(att.filename).font(.caption).foregroundStyle(p.text)
                                .lineLimit(1).frame(maxWidth: 160, alignment: .leading)
                            if let note = att.note, !note.isEmpty {
                                Text(note).font(.caption2).foregroundStyle(p.muted).lineLimit(1)
                            }
                        }
                        Button {
                            onRemove(att.uid)
                        } label: {
                            Image(systemName: "xmark").font(.caption2).foregroundStyle(p.muted)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Remove \(att.filename)")
                    }
                    .padding(.horizontal, 10).padding(.vertical, 5)
                    .background(p.surface2, in: RoundedRectangle(cornerRadius: 14))
                }
            }
        }
    }
    private func marker(_ state: String) -> String {
        switch state {
        case "uploading": return "…"
        case "failed": return "⚠"
        default: return "📄"
        }
    }
}

private struct GlyphButton: View {
    @Environment(ThemeStore.self) var theme
    let system: String
    var enabled: Bool = true
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Image(systemName: system).font(.system(size: 18))
                .foregroundStyle(theme.palette.muted.opacity(enabled ? 1 : 0.4))
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
    }
}

private struct SendButton: View {
    @Environment(ThemeStore.self) var theme
    let enabled: Bool
    let action: () -> Void
    private var p: AstralPalette { theme.palette }
    var body: some View {
        Button(action: action) {
            Image(systemName: "arrow.up").font(.system(size: 18, weight: .bold)).foregroundStyle(.white)
                .frame(width: 44, height: 44)
                .background(enabled ? AnyShapeStyle(p.primary) : AnyShapeStyle(p.surface2), in: Circle())
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .accessibilityLabel("Send message")
    }
}

// MARK: - shimmer + safe index

enum TranscriptLayoutPresentation {
    /// AppKit's `LazyVStack` placement can remain inside one AttributeGraph
    /// transaction when transcript identities and heights change together.
    /// iOS keeps lazy rows for long mobile transcripts; macOS uses bounded,
    /// eager placement inside the rail's concrete viewport.
    static var usesLazyRows: Bool {
        #if os(macOS)
            false
        #else
            true
        #endif
    }
}

enum ContinuousActivityPresentation {
    /// AppKit's indeterminate progress views and animation timelines can feed
    /// their ticks back through the transcript LazyVStack. On macOS that can
    /// keep the main view graph permanently dirty, starving websocket/media
    /// work and growing memory without bound. Static busy affordances retain
    /// the visible state while limiting layout to real model changes.
    static var allowsAnimatedIndicators: Bool {
        #if os(macOS)
            false
        #else
            true
        #endif
    }
}

extension View {
    func shimmer() -> some View { modifier(ShimmerModifier()) }

    @ViewBuilder
    func activityShimmer() -> some View {
        if ContinuousActivityPresentation.allowsAnimatedIndicators {
            shimmer()
        } else {
            self
        }
    }
}

struct ShimmerModifier: ViewModifier {
    /// The moving highlight must never dirty the view graph outside this
    /// overlay. The previous `repeatForever` animation on an `@State` phase
    /// forced a FULL layout pass of the hosting view on every animation
    /// frame; once the chat screen's layout cost exceeded one frame
    /// interval, the main thread livelocked in back-to-back layout and the
    /// @MainActor frame reducer starved — delivered `conversation_snapshot`
    /// / `chat_status done` frames were never reduced and the skeleton
    /// latched forever (the 063 stuck-canvas defect; same class as the 061
    /// grid-clamp hang). `TimelineView` scopes each tick's invalidation to
    /// the gradient subtree, so outer layout runs only when real state
    /// changes.
    func body(content: Content) -> some View {
        content.overlay(
            GeometryReader { geo in
                TimelineView(.animation) { context in
                    let cycle =
                        context.date.timeIntervalSinceReferenceDate
                        .truncatingRemainder(dividingBy: 1.3) / 1.3
                    LinearGradient(
                        colors: [.clear, .white.opacity(0.18), .clear],
                        startPoint: .leading, endPoint: .trailing
                    )
                    .frame(width: geo.size.width * 0.6)
                    .offset(x: geo.size.width * ShimmerModifier.phase(cycle: cycle))
                }
            }
            .allowsHitTesting(false)
        )
        .clipped()
    }

    /// Pure sweep curve: cycle ∈ [0, 1) → offset multiplier in [-1, 1.6),
    /// the same left-to-right pass the animated @State produced.
    static func phase(cycle: Double) -> CGFloat {
        CGFloat(-1 + cycle * 2.6)
    }
}

extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}

#Preview("Chat shell") {
    let model = AppModel()
    model.turns = [
        .init(id: "u0", role: "user", text: "Show me Q3 sales"),
        .init(id: "a0", role: "assistant", text: "Here's a **live summary** of Q3."),
    ]
    // Authored with AstralPrims (the Swift astralprims mirror) — the same
    // wire dicts a Python agent would produce.
    model.canvas = [
        AstralPrims.Hero(
            title: "Q3 Sales",
            subtitle: "Revenue up 12% quarter over quarter",
            variant: "gradient"),
        AstralPrims.Grid(columns: 2).add(
            AstralPrims.MetricCard(title: "Revenue", value: "$1.2M", subtitle: "+12%"),
            AstralPrims.MetricCard(title: "New users", value: "3,401", variant: "success")),
    ].compactMap { AstralComponent(json: $0.toDict()) }
    return ChatShell()
        .environment(model)
        .environment(model.themeStore)
        .preferredColorScheme(.dark)
}
