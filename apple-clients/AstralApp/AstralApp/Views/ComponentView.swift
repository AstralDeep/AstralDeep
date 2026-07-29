import AstralCore
// Feature 051 — the iOS/macOS SDUI renderer (FR-004/FR-025). Native views for
// the full astralprims vocabulary, styled from the shared AstralDeep palette
// (parity with web/Android/Windows), with `emit(action,payload)` round-trips
// for interactive components (buttons, inputs, tables, forms, theme). Anything
// unknown falls back to readable text with a type badge (FR-003).
import SwiftUI

#if os(macOS)
    import AppKit
#endif

struct ComponentView: View {
    let component: AstralComponent
    @Environment(ThemeStore.self) var theme
    @Environment(AppModel.self) var model
    /// Measured width of this component's slot, used to clamp multi-column
    /// layouts on compact screens (0 until the first layout pass).
    @State private var slotWidth: CGFloat = 0

    private var p: AstralPalette { theme.palette }

    /// How many ~150 pt columns actually fit the measured slot, capped at
    /// `authored`. Before the first measurement, fall back to 2 on the
    /// assumption of a compact screen — never the authored maximum.
    private func fittedColumns(authored: Int) -> Int {
        guard slotWidth > 0 else { return min(authored, 2) }
        return min(authored, max(1, Int(slotWidth / 150)))
    }

    /// Invisible width probe: writes the slot width into `slotWidth` without
    /// influencing layout (fixed 0-height overlay on the container).
    private var widthProbe: some View {
        GeometryReader { geo in
            Color.clear
                .onAppear { slotWidth = geo.size.width }
                .onChange(of: geo.size.width) { _, w in
                    if abs(w - slotWidth) > 1 { slotWidth = w }
                }
        }
        .frame(height: 0)
    }

    var body: some View {
        switch component.type {
        case "text":
            textView
        case "alert":
            alertView
        case "card":
            cardView
        case "collapsible":
            CollapsibleComponent(component: component)
        case "container":
            containerView
        case "grid":
            gridView
        case "metric":
            metricView
        case "badge":
            badgeView
        case "hero":
            heroView
        case "list":
            listView
        case "keyvalue":
            keyValueView
        case "timeline":
            timelineView
        case "rating":
            ratingView
        case "table":
            TableComponent(component: component)
        case "code":
            codeView
        case "image":
            imageView
        case "progress":
            progressView
        case "divider":
            Divider().overlay(p.border)
        case "button":
            buttonView
        case "file_upload":
            // Not a live control here — the chat input owns attachment staging.
            // A generic action button would emit a bogus component_action and
            // earn a server error alert.
            fileUploadHint
        case "input":
            InputComponent(component: component)
        case "param_picker":
            ParamPickerComponent(component: component)
        case "tabs":
            TabsComponent(component: component)
        case "color_picker":
            ColorPickerComponent(component: component)
        case "chat_history":
            chatHistoryView
        case "bar_chart", "line_chart", "pie_chart", "plotly_chart":
            ChartComponent(component: component)
        case "file_download", "download_card":
            DownloadComponent(component: component)
        case "skeleton":
            skeletonView
        case "theme_apply":
            Color.clear.frame(height: 0)
                .onAppear { theme.apply(spec: component.raw["attributes"] ?? component.raw) }
        default:
            fallbackView
        }
    }

    // MARK: text

    @ViewBuilder
    private var textView: some View {
        let text = component.textContent ?? component.fallbackText
        if component.variant == "markdown" {
            // The server explicitly declared block content (web parity:
            // block_md) — headings/fences/lists/tables must not stay literal.
            MarkdownBlockView(source: text)
                .foregroundStyle(p.text)
                .textSelection(.enabled)
        } else {
            markdown(text)
                .font(fontForVariant(component.variant))
                .foregroundStyle(component.variant == "caption" ? p.muted : p.text)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func fontForVariant(_ v: String?) -> Font {
        switch v {
        case "h1": return .largeTitle.bold()
        case "h2": return .title.bold()
        case "h3": return .title3.bold()
        case "caption": return .caption
        default: return .body
        }
    }

    // MARK: alert

    private var alertView: some View {
        let color = p.variant(component.variant)
        return HStack(alignment: .top, spacing: 10) {
            Rectangle().fill(color).frame(width: 3)
            Image(systemName: alertIcon).foregroundStyle(color)
            VStack(alignment: .leading, spacing: 2) {
                if let title = component.title, !title.isEmpty {
                    markdown(title).font(.subheadline.bold()).foregroundStyle(color)
                }
                MarkdownBlockView(source: component.message ?? component.fallbackText)
                    .foregroundStyle(p.text)
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(color.opacity(0.12), in: RoundedRectangle(cornerRadius: AstralRadius.sm))
    }

    private var alertIcon: String {
        switch component.variant {
        case "error", "danger": return "xmark.octagon.fill"
        case "warning": return "exclamationmark.triangle.fill"
        case "success": return "checkmark.circle.fill"
        default: return "info.circle.fill"
        }
    }

    // MARK: containers

    private var cardView: some View {
        VStack(alignment: .leading, spacing: 6) {
            titleLine
            childViews
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(p.surface.opacity(0.55), in: RoundedRectangle(cornerRadius: AstralRadius.lg))
        .overlay(RoundedRectangle(cornerRadius: AstralRadius.lg).stroke(p.border))
    }

    @ViewBuilder
    private var containerView: some View {
        let dir = component.raw["direction"]?.stringValue
        if dir == "row" {
            // Not a fixed HStack: N children on a phone would each get
            // screenWidth/N points and wrap character-by-character. Clamp the
            // side-by-side count to how many ~150 pt slots the measured width
            // fits, breaking extra children to the next line.
            let count = fittedColumns(authored: max(1, component.children.count))
            LazyVGrid(
                columns: Array(
                    repeating: GridItem(.flexible(), spacing: 8, alignment: .top),
                    count: count),
                alignment: .leading, spacing: 8
            ) { childViews }
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(alignment: .top) { widthProbe }
        } else {
            VStack(alignment: .leading, spacing: 8) { childViews }
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var gridView: some View {
        // The authored column count is a wide-screen hint. Honoring it verbatim
        // on a phone gives each cell screenWidth/N points and wraps content
        // character-by-character — clamp to the columns the measured width
        // actually fits, so a 4-up grid becomes 2×2 on compact widths.
        let authored = max(1, Int(component.raw["columns"]?.numberValue ?? 2))
        let count = fittedColumns(authored: authored)
        let kids = component.children
        return VStack(alignment: .leading, spacing: 6) {
            titleLine
            LazyVGrid(
                columns: Array(
                    repeating: GridItem(.flexible(), spacing: 8, alignment: .top),
                    count: count),
                alignment: .leading, spacing: 8
            ) {
                ForEach(Array(kids.enumerated()), id: \.offset) { _, child in
                    ComponentView(component: child)
                }
            }
        }
        .overlay(alignment: .top) { widthProbe }
    }

    // MARK: metric / badge / hero

    private var metricView: some View {
        let color = p.variant(component.variant)
        return HStack(spacing: 10) {
            Rectangle().fill(color).frame(width: 3)
            VStack(alignment: .leading, spacing: 2) {
                markdown(component.title ?? component.label ?? "")
                    .font(.caption).foregroundStyle(p.muted)
                    .textCase(.uppercase)
                Text(component.value ?? "—").font(.title.bold()).foregroundStyle(p.text)
                if let sub = component.raw["subtitle"]?.stringValue, !sub.isEmpty {
                    markdown(sub).font(.caption).foregroundStyle(p.muted)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(p.surface.opacity(0.55), in: RoundedRectangle(cornerRadius: AstralRadius.md))
        .overlay(RoundedRectangle(cornerRadius: AstralRadius.md).stroke(p.border))
    }

    private var badgeView: some View {
        let color = p.variant(component.variant)
        return Text(component.label ?? component.fallbackText)
            .font(.caption.bold())
            .foregroundStyle(color)
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(color.opacity(0.18), in: Capsule())
    }

    /// Hero variants match the web renderer: default = surface + soft border,
    /// `gradient` = subtle 135° wash (primary 18% → secondary 8%) with a 3 pt
    /// top accent bar, `subtle` = 2% text wash — never a full-strength banner.
    private var heroView: some View {
        let variant = component.variant ?? "default"
        return VStack(alignment: .leading, spacing: 0) {
            if variant == "gradient" {
                Rectangle().fill(p.gradient).frame(height: 3)
            }
            VStack(alignment: .leading, spacing: 6) {
                if let eyebrow = component.raw["eyebrow"]?.stringValue, !eyebrow.isEmpty {
                    Text(eyebrow).font(.caption.bold()).foregroundStyle(p.primary).textCase(.uppercase)
                }
                markdown(component.raw["heading"]?.stringValue ?? component.title ?? "")
                    .font(.title.bold()).foregroundStyle(p.text)
                if let sub = component.raw["subtitle"]?.stringValue ?? component.raw["subheading"]?.stringValue {
                    markdown(sub).foregroundStyle(p.muted)
                }
                let badges = component.raw["badges"]?.arrayValue ?? []
                if !badges.isEmpty {
                    HStack {
                        ForEach(Array(badges.enumerated()), id: \.offset) { _, b in
                            Text(b["label"]?.stringValue ?? b.displayText)
                                .font(.caption.bold())
                                .padding(.horizontal, 8).padding(.vertical, 3)
                                .background(p.primary.opacity(0.18), in: Capsule())
                                .foregroundStyle(p.text)
                        }
                    }
                }
            }
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(heroBackground(variant), in: RoundedRectangle(cornerRadius: AstralRadius.lg))
        .overlay(RoundedRectangle(cornerRadius: AstralRadius.lg).stroke(p.border))
        .clipShape(RoundedRectangle(cornerRadius: AstralRadius.lg))
    }

    private func heroBackground(_ variant: String) -> AnyShapeStyle {
        switch variant {
        case "gradient":
            return AnyShapeStyle(
                LinearGradient(
                    colors: [p.primary.opacity(0.18), p.secondary.opacity(0.08)],
                    startPoint: .topLeading, endPoint: .bottomTrailing))
        case "subtle":
            return AnyShapeStyle(p.text.opacity(0.02))
        default:
            return AnyShapeStyle(p.surface.opacity(0.55))
        }
    }

    // MARK: list / keyvalue / timeline / rating

    private var listView: some View {
        let ordered = component.raw["ordered"]?.boolValue ?? false
        let items = component.listItems
        return VStack(alignment: .leading, spacing: 4) {
            titleLine
            if items.isEmpty {
                childViews
            } else {
                ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                    HStack(alignment: .top, spacing: 6) {
                        Text(ordered ? "\(index + 1)." : "•").foregroundStyle(p.muted)
                        markdown(item).foregroundStyle(p.text)
                        Spacer(minLength: 0)
                    }
                }
            }
        }
    }

    private var keyValueView: some View {
        VStack(alignment: .leading, spacing: 4) {
            titleLine
            ForEach(Array(component.keyValuePairs.enumerated()), id: \.offset) { _, pair in
                HStack(alignment: .top) {
                    Text(pair.0).foregroundStyle(p.muted)
                    Spacer(minLength: 12)
                    Text(pair.1).foregroundStyle(p.text)
                }
                .font(.callout)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(p.surface.opacity(0.4), in: RoundedRectangle(cornerRadius: AstralRadius.md))
    }

    private var timelineView: some View {
        let items = component.raw["items"]?.arrayValue ?? []
        return VStack(alignment: .leading, spacing: 6) {
            titleLine
            ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                HStack(alignment: .top, spacing: 8) {
                    Circle().fill(p.variant(item["variant"]?.stringValue))
                        .frame(width: 8, height: 8).padding(.top, 5)
                    VStack(alignment: .leading, spacing: 1) {
                        if let time = item["time"]?.stringValue, !time.isEmpty {
                            Text(time).font(.caption2).foregroundStyle(p.muted)
                        }
                        markdown(item["title"]?.stringValue ?? item["label"]?.stringValue ?? item.displayText)
                            .font(.callout).foregroundStyle(p.text)
                        if let desc = item["description"]?.stringValue, !desc.isEmpty {
                            markdown(desc).font(.caption).foregroundStyle(p.muted)
                        }
                    }
                    Spacer(minLength: 0)
                }
            }
        }
    }

    private var ratingView: some View {
        let rawValue = component.raw["value"]?.numberValue ?? 0
        let value = Int(rawValue.rounded())
        let maxValue = Int(component.raw["max_value"]?.numberValue ?? component.raw["max"]?.numberValue ?? 5)
        return VStack(alignment: .leading, spacing: 2) {
            if let label = component.label ?? component.title, !label.isEmpty {
                markdown(label).font(.caption).foregroundStyle(p.muted)
            }
            HStack(spacing: 2) {
                ForEach(0..<max(maxValue, 1), id: \.self) { i in
                    Image(systemName: i < value ? "star.fill" : "star")
                        .foregroundStyle(p.accent)
                }
                // The stars round — the number is the honest value (web shows
                // it by default; 3.5/5 must not read as four stars flat).
                if component.raw["show_value"]?.boolValue != false {
                    Text("\(rawValue.formatted(.number.precision(.fractionLength(0...1))))/\(maxValue)")
                        .font(.caption.weight(.semibold)).foregroundStyle(p.text)
                        .padding(.leading, 4)
                }
            }
            if let sub = component.raw["subtitle"]?.stringValue, !sub.isEmpty {
                markdown(sub).font(.caption).foregroundStyle(p.muted)
            }
        }
    }

    // MARK: code / image / progress

    private var codeView: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Text(component.textContent ?? component.raw["code"]?.stringValue ?? "")
                .font(.callout.monospaced())
                .textSelection(.enabled)
                .padding(12)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.black.opacity(0.45), in: RoundedRectangle(cornerRadius: AstralRadius.sm))
        .foregroundStyle(Color(hex: 0x8BE9A0))
    }

    @ViewBuilder
    private var imageView: some View {
        if let url = (component.url ?? component.raw["src"]?.stringValue).flatMap(URL.init(string:)) {
            VStack(alignment: .leading, spacing: 4) {
                AsyncImage(url: url) { image in
                    image.resizable().scaledToFit()
                } placeholder: {
                    ProgressView().tint(p.primary)
                }
                .frame(maxHeight: 360)
                .clipShape(RoundedRectangle(cornerRadius: AstralRadius.md))
                if let caption = component.raw["caption"]?.stringValue ?? component.raw["alt"]?.stringValue,
                    !caption.isEmpty
                {
                    Text(caption).font(.caption).foregroundStyle(p.muted)
                }
            }
        }
    }

    private var progressView: some View {
        VStack(alignment: .leading, spacing: 3) {
            // The wire caption field is `label` (progress has no `title`).
            if let label = component.label ?? component.title, !label.isEmpty {
                HStack {
                    markdown(label).font(.caption).foregroundStyle(p.muted)
                    Spacer(minLength: 8)
                    if component.raw["show_percentage"]?.boolValue != false {
                        Text("\(Int((progressFraction * 100).rounded()))%")
                            .font(.caption).foregroundStyle(p.muted)
                    }
                }
            }
            ProgressView(value: progressFraction).tint(p.primary)
        }
    }

    private var progressFraction: Double {
        let value = component.raw["value"]?.numberValue ?? 0
        // Web/others use 0–1; tolerate 0–100.
        return value > 1 ? min(value / 100, 1) : min(max(value, 0), 1)
    }

    // MARK: interactive

    private var buttonView: some View {
        let label = component.label ?? component.title ?? "Continue"
        let variant = component.variant ?? "primary"
        return Button {
            let action = component.raw["action"]?.stringValue ?? "component_action"
            model.emit(action, payload: component.raw["payload"]?.objectValue ?? [:])
        } label: {
            Text(label).frame(maxWidth: variant == "primary" ? .infinity : nil)
        }
        .buttonStyle(AstralButtonStyle(palette: p, variant: variant))
    }

    private var chatHistoryView: some View {
        let items = component.raw["items"]?.arrayValue ?? component.raw["chats"]?.arrayValue ?? []
        return VStack(alignment: .leading, spacing: 4) {
            titleLine
            ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                if let chatId = item["chat_id"]?.stringValue ?? item["id"]?.stringValue {
                    Button {
                        model.emit("load_chat", payload: ["chat_id": .string(chatId)])
                    } label: {
                        VStack(alignment: .leading, spacing: 1) {
                            HStack(spacing: 4) {
                                Text(item["title"]?.stringValue ?? "Chat").foregroundStyle(p.text)
                                if isTruthy(item["saved"]) {
                                    Image(systemName: "star.fill")
                                        .font(.caption2).foregroundStyle(p.accent)
                                        .accessibilityLabel("Has saved components")
                                }
                                Spacer(minLength: 6)
                                if let time = item["time"]?.stringValue, !time.isEmpty {
                                    Text(time).font(.caption2).foregroundStyle(p.muted)
                                }
                            }
                            if let preview = item["preview"]?.stringValue, !preview.isEmpty {
                                Text(preview).font(.caption).foregroundStyle(p.muted).lineLimit(1)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                        .background(p.surface.opacity(0.4), in: RoundedRectangle(cornerRadius: AstralRadius.md))
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private var skeletonView: some View {
        let count = max(1, min(Int(component.raw["count"]?.numberValue ?? 3), 6))
        return VStack(alignment: .leading, spacing: 6) {
            ForEach(0..<count, id: \.self) { _ in
                RoundedRectangle(cornerRadius: AstralRadius.sm)
                    .fill(p.surface.opacity(0.5))
                    .frame(height: 12)
                    .frame(maxWidth: .infinity)
            }
        }
    }

    private var fallbackView: some View {
        VStack(alignment: .leading, spacing: 2) {
            markdown(component.fallbackText).foregroundStyle(p.text)
                .fixedSize(horizontal: false, vertical: true)
            Text(component.type).font(.caption2).foregroundStyle(p.muted.opacity(0.7))
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(p.surface.opacity(0.3), in: RoundedRectangle(cornerRadius: AstralRadius.sm))
    }

    // MARK: shared pieces

    @ViewBuilder
    private var titleLine: some View {
        if let title = component.title, !title.isEmpty {
            HStack(spacing: 6) {
                Rectangle().fill(p.gradient).frame(width: 3, height: 16)
                markdown(title).font(.headline).foregroundStyle(p.text)
            }
        }
    }

    /// FR-033-style redirect: file upload lives in the chat input, so this
    /// component is informational here — never a dead button.
    private var fileUploadHint: some View {
        VStack(alignment: .leading, spacing: 3) {
            if let label = component.label ?? component.title, !label.isEmpty {
                markdown(label).foregroundStyle(p.text)
            }
            Label(
                "Attach files with the paperclip in the chat input",
                systemImage: "paperclip"
            )
            .font(.caption).foregroundStyle(p.muted)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(p.surface.opacity(0.4), in: RoundedRectangle(cornerRadius: AstralRadius.md))
    }

    @ViewBuilder
    private var childViews: some View {
        ForEach(Array(component.children.enumerated()), id: \.offset) { _, child in
            ComponentView(component: child)
        }
    }

    private func markdown(_ string: String) -> Text {
        Text(InlineMarkdown.attributed(string))
    }

    private func isTruthy(_ value: JSONValue?) -> Bool {
        guard let value else { return false }
        return value.boolValue == true
            || (value.numberValue ?? 0) > 0
            || (value.stringValue?.isEmpty == false)
    }
}

// MARK: - Button style (parity with the web primary/secondary/ghost)

struct AstralButtonStyle: ButtonStyle {
    let palette: AstralPalette
    let variant: String

    func makeBody(configuration: Configuration) -> some View {
        let label = configuration.label
            .font(.callout.weight(.semibold))
            .padding(.horizontal, 14).padding(.vertical, 9)
        switch variant {
        case "secondary", "ghost":
            return AnyView(
                label.foregroundStyle(palette.text)
                    .background(palette.surface.opacity(0.5), in: RoundedRectangle(cornerRadius: AstralRadius.sm))
                    .overlay(RoundedRectangle(cornerRadius: AstralRadius.sm).stroke(palette.border))
                    .opacity(configuration.isPressed ? 0.7 : 1))
        case "danger":
            return AnyView(
                label.foregroundStyle(.white)
                    .background(palette.error, in: RoundedRectangle(cornerRadius: AstralRadius.sm))
                    .opacity(configuration.isPressed ? 0.8 : 1))
        default:
            return AnyView(
                label.foregroundStyle(.white)
                    .background(palette.gradient, in: RoundedRectangle(cornerRadius: AstralRadius.sm))
                    .opacity(configuration.isPressed ? 0.85 : 1))
        }
    }
}

// MARK: - Interactive component subviews

/// Authenticated file download (file_download / download_card). The web's
/// anchor click carries the session cookie; the native twin must fetch with
/// the Bearer token (root-relative `/api/download/...` URLs resolve against
/// the configured server) and then hand the file to the platform: a share
/// sheet on iOS/iPadOS, a save panel on macOS. Off-origin URLs (e.g. GitHub
/// release assets) are fetched without credentials.
struct DownloadComponent: View {
    let component: AstralComponent
    @Environment(ThemeStore.self) var theme
    @Environment(AppModel.self) var model
    @State private var phase = Phase.idle
    private var p: AstralPalette { theme.palette }

    enum Phase: Equatable {
        case idle, fetching
        case done(URL)
        case failed(String)
    }

    private var urlString: String? {
        // The unavailable variant ships download_url:"" — an empty URL is no URL.
        (component.raw["download_url"]?.stringValue ?? component.url)
            .flatMap { $0.isEmpty ? nil : $0 }
    }
    private var filename: String? { component.raw["filename"]?.stringValue }
    private var label: String {
        component.label ?? filename ?? component.title ?? "Download"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            // The card's context (what this is, why it's trustworthy, which
            // platform) — web parity; the button alone says none of it.
            if let title = component.title, !title.isEmpty {
                Text(InlineMarkdown.attributed(title))
                    .font(.subheadline.bold()).foregroundStyle(p.text)
            }
            if let desc = component.raw["description"]?.stringValue, !desc.isEmpty {
                Text(InlineMarkdown.attributed(desc))
                    .font(.caption).foregroundStyle(p.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            let meta = [
                component.raw["version"]?.stringValue,
                component.raw["platform"]?.stringValue,
            ]
            .compactMap { $0 }.filter { !$0.isEmpty }
            if !meta.isEmpty {
                Text(meta.joined(separator: " • "))
                    .font(.caption2).foregroundStyle(p.muted)
            }
            switch phase {
            case .idle:
                Button {
                    download()
                } label: {
                    Label(label, systemImage: "arrow.down.circle")
                }
                .buttonStyle(AstralButtonStyle(palette: p, variant: "secondary"))
                .disabled(urlString == nil)
                .accessibilityLabel("Download \(filename ?? label)")
            case .fetching:
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Downloading…").font(.callout).foregroundStyle(p.muted)
                }
            case .done(let file):
                #if os(macOS)
                    HStack(spacing: 8) {
                        Image(systemName: "checkmark.circle.fill").foregroundStyle(p.success)
                        Text("Saved \(file.lastPathComponent)")
                            .font(.callout).foregroundStyle(p.text)
                        Button("Show in Finder") {
                            NSWorkspace.shared.activateFileViewerSelecting([file])
                        }
                        .font(.callout)
                        .tint(p.primary)
                    }
                #else
                    ShareLink(item: file) {
                        Label(
                            "Save or share \(file.lastPathComponent)",
                            systemImage: "square.and.arrow.up")
                    }
                    .buttonStyle(AstralButtonStyle(palette: p, variant: "primary"))
                #endif
            case .failed(let why):
                HStack(spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(p.warning)
                    Text(why).font(.caption).foregroundStyle(p.muted)
                    Button("Retry") { download() }
                        .font(.callout).tint(p.primary)
                }
            }
            if urlString == nil,
                let page = component.raw["html_url"]?.stringValue,
                let pageURL = URL(string: page), !page.isEmpty
            {
                Link("Open the releases page", destination: pageURL)
                    .font(.caption).tint(p.primary)
            }
        }
    }

    private func download() {
        guard let urlString else { return }
        phase = .fetching
        let rest = model.rest
        let suggested = filename
        Task {
            do {
                let file = try await rest.downloadFile(
                    from: urlString,
                    suggestedFilename: suggested)
                await MainActor.run { finish(with: file) }
            } catch {
                await MainActor.run {
                    phase = .failed("Download failed — check your connection and try again.")
                }
            }
        }
    }

    @MainActor
    private func finish(with file: URL) {
        #if os(macOS)
            let panel = NSSavePanel()
            panel.nameFieldStringValue = file.lastPathComponent
            panel.canCreateDirectories = true
            if panel.runModal() == .OK, let destination = panel.url {
                do {
                    if FileManager.default.fileExists(atPath: destination.path) {
                        try FileManager.default.removeItem(at: destination)
                    }
                    try FileManager.default.copyItem(at: file, to: destination)
                    phase = .done(destination)
                } catch {
                    phase = .failed(error.localizedDescription)
                }
            } else {
                phase = .idle  // user cancelled the save panel
            }
        #else
            phase = .done(file)
        #endif
    }
}

/// Table with server-driven pagination (emits `table_paginate`).
struct TableComponent: View {
    let component: AstralComponent
    @Environment(ThemeStore.self) var theme
    @Environment(AppModel.self) var model
    private var p: AstralPalette { theme.palette }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let title = component.title, !title.isEmpty {
                Text(InlineMarkdown.attributed(title)).font(.headline).foregroundStyle(p.text)
            }
            ScrollView(.horizontal, showsIndicators: false) {
                Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 4) {
                    if !component.tableHeaders.isEmpty {
                        GridRow {
                            ForEach(Array(component.tableHeaders.enumerated()), id: \.offset) { _, header in
                                Text(header).font(.caption.bold()).foregroundStyle(p.muted)
                            }
                        }
                        Divider().overlay(p.border)
                    }
                    ForEach(Array(component.tableRows.enumerated()), id: \.offset) { _, row in
                        GridRow {
                            ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                                Text(cell).font(.callout).foregroundStyle(p.text)
                            }
                        }
                    }
                }
            }
            pager
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(p.surface.opacity(0.4), in: RoundedRectangle(cornerRadius: AstralRadius.md))
    }

    @ViewBuilder
    private var pager: some View {
        if let total = component.raw["total_rows"]?.numberValue,
            let size = component.raw["page_size"]?.numberValue, size > 0
        {
            let offset = component.raw["page_offset"]?.numberValue ?? 0
            let start = Int(offset) + 1
            let end = min(Int(offset + size), Int(total))
            HStack {
                Button("‹ Prev") { paginate(offset: max(offset - size, 0), size: size) }
                    .disabled(offset <= 0)
                Spacer()
                Text("rows \(start)–\(end) of \(Int(total))").font(.caption).foregroundStyle(p.muted)
                Spacer()
                Button("Next ›") { paginate(offset: offset + size, size: size) }
                    .disabled(end >= Int(total))
            }
            .font(.caption)
            .tint(p.primary)
            .padding(.top, 4)
        }
    }

    private func paginate(offset: Double, size: Double) {
        guard let cid = component.componentId else { return }
        model.emit(
            "table_paginate",
            payload: [
                "component_id": .string(cid),
                "params": .object(["page_offset": .number(offset), "page_size": .number(size)]),
            ])
    }
}

/// Single-line input that submits its value through the standard event path.
struct InputComponent: View {
    let component: AstralComponent
    @Environment(ThemeStore.self) var theme
    @Environment(AppModel.self) var model
    @State private var value = ""
    private var p: AstralPalette { theme.palette }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            if let label = component.label, !label.isEmpty {
                Text(label).font(.caption).foregroundStyle(p.muted)
            }
            HStack {
                TextField(component.raw["placeholder"]?.stringValue ?? "", text: $value)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(submit)
                Button("Send", action: submit).tint(p.primary)
            }
        }
        .onAppear { value = component.raw["value"]?.stringValue ?? "" }
    }

    private func submit() {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        if let action = component.raw["action"]?.stringValue {
            model.emit(action, payload: ["value": .string(trimmed)])
        } else {
            model.emit("chat_message", payload: ["message": .string(trimmed)])
        }
    }
}

/// Multi-field form (param_picker) — text/boolean/select — submitting either a
/// templated chat message or a `submit_action` with `{fields:{…}}`.
struct ParamPickerComponent: View {
    let component: AstralComponent
    @Environment(ThemeStore.self) var theme
    @Environment(AppModel.self) var model
    @State private var values: [String: String] = [:]
    @State private var flags: [String: Bool] = [:]
    private var p: AstralPalette { theme.palette }

    private var fields: [JSONValue] { component.raw["fields"]?.arrayValue ?? [] }
    private var actions: [JSONValue] { component.raw["actions"]?.arrayValue ?? [] }
    private var hasLLMSave: Bool {
        component.raw["submit_action"]?.stringValue == "chrome_llm_save"
            || actions.contains { $0["action"]?.stringValue == "chrome_llm_save" }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let title = component.title, !title.isEmpty {
                Text(InlineMarkdown.attributed(title))
                    .font(.headline).foregroundStyle(p.text)
                    .accessibilityIdentifier(
                        hasLLMSave ? "llm-provider-form-title" : "param-picker-form-title")
            }
            // The form's operative instructions live here (web parity).
            if let desc = component.raw["description"]?.stringValue, !desc.isEmpty {
                Text(InlineMarkdown.attributed(desc))
                    .font(.caption).foregroundStyle(p.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(Array(fields.enumerated()), id: \.offset) { _, field in
                fieldView(field)
            }
            if actions.isEmpty {
                let action = component.raw["submit_action"]?.stringValue
                paramButton(
                    label: component.raw["submit_label"]?.stringValue ?? "Submit",
                    action: action,
                    variant: "primary",
                    payload: component.raw["submit_payload"]?.objectValue ?? [:])
            } else {
                HStack(spacing: 8) {
                    ForEach(Array(actions.enumerated()), id: \.offset) { _, definition in
                        paramButton(
                            label: definition["label"]?.stringValue ?? "Submit",
                            action: definition["action"]?.stringValue,
                            variant: definition["variant"]?.stringValue ?? "secondary",
                            payload: definition["payload"]?.objectValue ?? [:])
                    }
                }
            }
            if hasLLMSave, let operation = model.llmFirstLoginOperation {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    if operation.isLoading {
                        ProgressView().controlSize(.small).tint(p.primary)
                            .accessibilityHidden(true)
                    }
                    // The accessibility contract lives on the Text itself, with
                    // no `.accessibilityElement(children:)` wrapper: wrapping —
                    // even a Text — mints a generic AXGroup, and macOS AXGroups
                    // drop AXValue, so XCUIElement.value (and VoiceOver's value
                    // readout) read as empty on macOS.
                    Text(operation.presentedLabel)
                        .font(.caption)
                        .foregroundStyle(operation.errorCode == nil ? p.muted : p.error)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("llm-save-status")
                        .accessibilityLabel("AI provider setup status")
                        .accessibilityValue(operation.presentedLabel)
                        .accessibilityAddTraits(.updatesFrequently)
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(p.surface.opacity(0.5), in: RoundedRectangle(cornerRadius: AstralRadius.lg))
        .overlay(RoundedRectangle(cornerRadius: AstralRadius.lg).stroke(p.border))
    }

    @ViewBuilder
    private func fieldView(_ field: JSONValue) -> some View {
        if fieldIsVisible(field) { fieldBody(field) }
    }

    /// Server-declared conditional visibility (063): a field may carry
    /// `visible_when: {field, equals, default}` — hidden unless the named
    /// controller field's current value matches. Fields without the attribute
    /// are always visible, so servers can emit it freely for older clients.
    private func fieldIsVisible(_ field: JSONValue) -> Bool {
        guard let vw = field["visible_when"],
            let controller = vw["field"]?.stringValue,
            let expected = vw["equals"]?.stringValue
        else { return true }
        let current = values[controller] ?? vw["default"]?.stringValue ?? ""
        return current == expected
    }

    @ViewBuilder
    private func fieldBody(_ field: JSONValue) -> some View {
        let name = field["name"]?.stringValue ?? ""
        let label = field["label"]?.stringValue ?? name
        let kind = field["kind"]?.stringValue ?? field["type"]?.stringValue ?? "text"
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption).foregroundStyle(p.muted)
            switch kind {
            case "boolean", "checkbox":
                Toggle(
                    "",
                    isOn: Binding(
                        get: { flags[name] ?? (field["default"]?.boolValue ?? false) },
                        set: { flags[name] = $0 })
                )
                .labelsHidden().tint(p.primary)
                .accessibilityIdentifier("param-field-\(name)")
                .accessibilityLabel(label)
                .accessibilityValue(
                    flags[name] ?? (field["default"]?.boolValue ?? false)
                        ? "Enabled" : "Disabled")
            case "select":
                let options =
                    field["options"]?.arrayValue?.compactMap { $0.stringValue ?? $0["value"]?.stringValue } ?? []
                Picker(
                    label,
                    selection: Binding(
                        get: { values[name] ?? options.first ?? "" },
                        set: { values[name] = $0 })
                ) {
                    ForEach(options, id: \.self) { Text($0).tag($0) }
                }
                .pickerStyle(.menu).tint(p.primary)
                .accessibilityIdentifier("param-field-\(name)")
                .accessibilityLabel(label)
                .accessibilityValue(values[name] ?? options.first ?? "Not selected")
            case "checklist":
                let options =
                    field["options"]?.arrayValue?.compactMap { $0.stringValue ?? $0["value"]?.stringValue } ?? []
                ForEach(options, id: \.self) { option in
                    Toggle(
                        option,
                        isOn: Binding(
                            get: { flags["\(name).\(option)"] ?? false },
                            set: { flags["\(name).\(option)"] = $0 })
                    )
                    .font(.callout).tint(p.primary)
                    .accessibilityIdentifier("param-field-\(name)-\(option)")
                    .accessibilityLabel(option)
                    .accessibilityValue(
                        flags["\(name).\(option)"] == true ? "Selected" : "Not selected")
                }
            case "number":
                TextField(
                    field["help"]?.stringValue ?? "",
                    text: Binding(
                        get: {
                            values[name]
                                ?? (field["default"]?.stringValue
                                    ?? field["default"]?.numberValue.map { String($0) } ?? "")
                        },
                        set: { values[name] = $0 })
                )
                .textFieldStyle(.roundedBorder)
                #if os(iOS)
                    .keyboardType(.decimalPad)
                #endif
                .accessibilityIdentifier("param-field-\(name)")
                .accessibilityLabel(label)
            case "password":
                SecureField(
                    field["help"]?.stringValue ?? "",
                    text: Binding(
                        get: { values[name] ?? "" },
                        set: { values[name] = $0 })
                )
                .textFieldStyle(.roundedBorder)
                .accessibilityIdentifier("param-field-\(name)")
                .accessibilityLabel(label)
            case "textarea":
                TextEditor(
                    text: Binding(
                        get: { values[name] ?? (field["default"]?.stringValue ?? "") },
                        set: { values[name] = $0 })
                )
                .frame(minHeight: 80)
                // Param fields hold identifiers/keys (usernames, PEMs), never
                // prose — autocap/autocorrect would corrupt them (063).
                .autocorrectionDisabled(true)
                #if os(iOS)
                    .textInputAutocapitalization(.never)
                #endif
                .accessibilityIdentifier("param-field-\(name)")
                .accessibilityLabel(label)
            default:
                TextField(
                    field["help"]?.stringValue ?? "",
                    text: Binding(
                        get: { values[name] ?? (field["default"]?.stringValue ?? "") },
                        set: { values[name] = $0 })
                )
                .textFieldStyle(.roundedBorder)
                .autocorrectionDisabled(true)
                #if os(iOS)
                    .textInputAutocapitalization(.never)
                #endif
                .accessibilityIdentifier("param-field-\(name)")
                .accessibilityLabel(label)
            }
        }
    }

    @ViewBuilder
    private func paramButton(
        label: String,
        action: String?,
        variant: String,
        payload: [String: JSONValue]
    ) -> some View {
        Button(label) {
            submit(action: action, payload: payload)
        }
        .buttonStyle(AstralButtonStyle(palette: p, variant: variant))
        .disabled(
            action == "chrome_llm_save"
                && (model.llmFirstLoginOperation?.isLoading ?? false)
        )
        .accessibilityIdentifier(
            action == "chrome_llm_save" ? "llm-save-button" : "param-action-\(action ?? "message")"
        )
        .accessibilityLabel(label)
        .accessibilityValue(
            action == "chrome_llm_save" && (model.llmFirstLoginOperation?.isLoading ?? false)
                ? "Submitting"
                : "Ready")
    }

    private func submit(action: String?, payload: [String: JSONValue]) {
        var collected: [String: JSONValue] = [:]
        for field in fields {
            guard let name = field["name"]?.stringValue else { continue }
            let kind = field["kind"]?.stringValue ?? field["type"]?.stringValue ?? "text"
            if kind == "checklist" {
                let options =
                    field["options"]?.arrayValue?.compactMap { $0.stringValue ?? $0["value"]?.stringValue } ?? []
                let chosen = options.filter { flags["\(name).\($0)"] == true }
                collected[name] = .array(chosen.map { .string($0) })
            } else if let flag = flags[name] {
                collected[name] = .bool(flag)
            } else if let value = values[name] {
                collected[name] = .string(value)
            } else if let def = field["default"] {
                collected[name] = def
            }
        }
        if let action {
            _ = model.submitParamPicker(action: action, fields: collected, payload: payload)
        } else if let template = component.raw["submit_message_template"]?.stringValue {
            var message = template
            // Whole-form placeholder first (web parity: client.js substitutes
            // {__values_json__} with the full state) — the classify training
            // template relies on it; per-field {key} replacement can't fill it.
            if message.contains("{__values_json__}") {
                let json =
                    (try? JSONValue.object(collected).encoded())
                    .flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
                message = message.replacingOccurrences(of: "{__values_json__}", with: json)
            }
            for (key, value) in collected {
                message = message.replacingOccurrences(of: "{\(key)}", with: value.displayText)
            }
            model.emit("chat_message", payload: ["message": .string(message)])
        }
    }
}

/// Tabs with local selection; renders the selected tab's children.
struct TabsComponent: View {
    let component: AstralComponent
    @Environment(ThemeStore.self) var theme
    @State private var selection = 0
    private var p: AstralPalette { theme.palette }

    private var tabs: [JSONValue] { component.raw["tabs"]?.arrayValue ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    ForEach(Array(tabs.enumerated()), id: \.offset) { index, tab in
                        Button(tab["label"]?.stringValue ?? "Tab \(index + 1)") { selection = index }
                            .font(.callout.weight(selection == index ? .bold : .regular))
                            .foregroundStyle(selection == index ? p.primary : p.muted)
                    }
                }
            }
            if tabs.indices.contains(selection) {
                let content = tabs[selection]["content"] ?? tabs[selection]["children"]
                ForEach(Array(AstralComponent.list(from: content).enumerated()), id: \.offset) { _, child in
                    ComponentView(component: child)
                }
            }
        }
    }
}

/// Collapsible disclosure (parity with web `<details>`).
struct CollapsibleComponent: View {
    let component: AstralComponent
    @Environment(ThemeStore.self) var theme
    @State private var expanded: Bool
    private var p: AstralPalette { theme.palette }

    init(component: AstralComponent) {
        self.component = component
        _expanded = State(initialValue: component.raw["default_open"]?.boolValue ?? false)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation { expanded.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right").foregroundStyle(p.muted)
                    // astralprims always serializes `title` (default "") — treat
                    // empty as missing like the web does, never a blank header.
                    Text(
                        InlineMarkdown.attributed(
                            (component.title?.isEmpty == false) ? component.title! : "Details")
                    )
                    .font(.headline).foregroundStyle(p.text)
                    Spacer(minLength: 0)
                }
            }
            .buttonStyle(.plain)
            if expanded {
                ForEach(Array(component.children.enumerated()), id: \.offset) { _, child in
                    ComponentView(component: child)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(p.surface.opacity(0.4), in: RoundedRectangle(cornerRadius: AstralRadius.md))
    }
}

/// Color picker → live restyle + `save_theme` (feature 044 US5 parity).
struct ColorPickerComponent: View {
    let component: AstralComponent
    @Environment(ThemeStore.self) var theme
    @Environment(AppModel.self) var model
    private var p: AstralPalette { theme.palette }

    private let presets = ["#6366F1", "#8B5CF6", "#06B6D4", "#22C55E", "#F59E0B", "#EF4444"]

    var body: some View {
        HStack(spacing: 8) {
            Text(component.label ?? "Color").foregroundStyle(p.text)
            Spacer(minLength: 0)
            Menu {
                ForEach(presets, id: \.self) { hex in
                    Button(hex) { choose(hex) }
                }
            } label: {
                RoundedRectangle(cornerRadius: 6)
                    .fill(Color(cssHex: component.raw["value"]?.stringValue ?? "#6366F1") ?? p.primary)
                    .frame(width: 28, height: 20)
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(p.border))
            }
            .accessibilityLabel("Choose \(component.label ?? "theme") color")
        }
    }

    private func choose(_ hex: String) {
        guard let key = component.raw["color_key"]?.stringValue else { return }
        theme.apply(spec: .object(["color_key": .string(key), "color_value": .string(hex)]))
        model.emit(
            "save_theme",
            payload: [
                "theme": .object([
                    "color_key": .string(key), "color_value": .string(hex),
                ])
            ])
    }
}

/// Native bar/line/pie chart (parity with the other clients' canvas draws).
struct ChartComponent: View {
    let component: AstralComponent
    @Environment(ThemeStore.self) var theme
    private var p: AstralPalette { theme.palette }

    private typealias Series = (name: String?, values: [Double])

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let title = component.title, !title.isEmpty {
                Text(InlineMarkdown.attributed(title)).font(.headline).foregroundStyle(p.text)
            }
            let series = allSeries
            if series.isEmpty {
                Text("[\(component.type)]").font(.caption).foregroundStyle(p.muted)
            } else {
                chart(series)
                    .frame(height: 160)
                    .frame(maxWidth: .infinity)
                legend(series)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(p.surface.opacity(0.4), in: RoundedRectangle(cornerRadius: AstralRadius.md))
    }

    private var seriesColors: [Color] {
        [p.primary, p.secondary, p.accent, p.success, p.warning, p.error]
    }

    @ViewBuilder
    private func chart(_ series: [Series]) -> some View {
        switch component.type {
        case "pie_chart":
            pie(series.first?.values ?? [])
        case "line_chart":
            lines(series)
        default:
            bars(series)
        }
    }

    /// Named multi-trace charts get a legend (the weather daily forecast is a
    /// two-trace High/Low bar chart — unnamed single traces stay clean). The
    /// pie draws only the first trace, so a multi-trace legend would name
    /// series that aren't on screen.
    @ViewBuilder
    private func legend(_ series: [Series]) -> some View {
        if series.count > 1 && component.type != "pie_chart" {
            HStack(spacing: 10) {
                ForEach(Array(series.enumerated()), id: \.offset) { index, s in
                    HStack(spacing: 4) {
                        Circle().fill(seriesColors[index % seriesColors.count])
                            .frame(width: 7, height: 7)
                        Text(s.name ?? "Series \(index + 1)")
                            .font(.caption2).foregroundStyle(p.muted)
                    }
                }
            }
        }
    }

    /// Bars grow away from a TRUE zero baseline (negatives hang below it),
    /// normalized across every series — mixed-sign data renders honestly and
    /// can never produce a negative frame height. Missing indices in ragged
    /// traces draw nothing, never a fabricated zero bar.
    private func bars(_ series: [Series]) -> some View {
        let all = series.flatMap(\.values)
        let top = max(all.max() ?? 0, 0)
        let bottom = min(all.min() ?? 0, 0)
        let range = top - bottom
        let baseline = range > 0 ? (0 - bottom) / range : 0
        let groups = series.map(\.values.count).max() ?? 0
        return GeometryReader { geo in
            HStack(alignment: .bottom, spacing: 3) {
                ForEach(0..<max(groups, 1), id: \.self) { index in
                    HStack(alignment: .bottom, spacing: 1) {
                        ForEach(Array(series.enumerated()), id: \.offset) { s, trace in
                            if index < trace.values.count, range > 0 {
                                let fraction = (trace.values[index] - bottom) / range
                                let lower = min(max(min(fraction, baseline), 0), 1)
                                let upper = min(max(max(fraction, baseline), 0), 1)
                                RoundedRectangle(cornerRadius: 3)
                                    .fill(
                                        series.count > 1
                                            ? AnyShapeStyle(seriesColors[s % seriesColors.count])
                                            : AnyShapeStyle(p.gradient)
                                    )
                                    .frame(height: max(geo.size.height * CGFloat(upper - lower), 1))
                                    .padding(.bottom, geo.size.height * CGFloat(lower))
                                    .frame(maxWidth: .infinity)
                            } else {
                                Color.clear.frame(maxWidth: .infinity)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
            }
            .frame(maxHeight: .infinity, alignment: .bottom)
        }
    }

    private func lines(_ series: [Series]) -> some View {
        let all = series.flatMap(\.values)
        let maxV = all.max() ?? 1
        let minV = all.min() ?? 0
        let range = maxV - minV
        return GeometryReader { geo in
            ForEach(Array(series.enumerated()), id: \.offset) { s, trace in
                Path { path in
                    for (i, v) in trace.values.enumerated() {
                        let x =
                            trace.values.count > 1
                            ? geo.size.width * CGFloat(i) / CGFloat(trace.values.count - 1) : 0
                        let y =
                            range > 0
                            ? geo.size.height * (1 - CGFloat((v - minV) / range))
                            : geo.size.height / 2
                        if i == 0 { path.move(to: CGPoint(x: x, y: y)) } else { path.addLine(to: CGPoint(x: x, y: y)) }
                    }
                }
                .stroke(
                    series.count > 1 ? seriesColors[s % seriesColors.count] : p.primary,
                    style: StrokeStyle(lineWidth: 2, lineJoin: .round))
            }
        }
    }

    private func pie(_ values: [Double]) -> some View {
        let positive = values.filter { $0 > 0 }
        let total = positive.reduce(0, +)
        let colors = seriesColors
        return Canvas { context, size in
            let radius = min(size.width, size.height) / 2
            let center = CGPoint(x: size.width / 2, y: size.height / 2)
            var start = Angle.degrees(-90)
            for (i, v) in positive.enumerated() where total > 0 {
                let sweep = Angle.degrees(360 * v / total)
                var path = Path()
                path.move(to: center)
                path.addArc(
                    center: center, radius: radius, startAngle: start,
                    endAngle: start + sweep, clockwise: false)
                context.fill(path, with: .color(colors[i % colors.count]))
                start = start + sweep
            }
        }
    }

    /// Every series in the payload — simple values, chart.js-style datasets,
    /// or plotly traces (`y` for bar/line/scatter, `values` for pie).
    private var allSeries: [Series] {
        if let values = component.raw["values"]?.arrayValue {
            let nums = values.compactMap { $0.numberValue }
            return nums.isEmpty ? [] : [(nil, nums)]
        }
        if let datasets = component.raw["datasets"]?.arrayValue {
            let traces: [Series] = datasets.compactMap { ds in
                guard let data = ds["data"]?.arrayValue else { return nil }
                let nums = data.compactMap { $0.numberValue }
                guard !nums.isEmpty else { return nil }
                return (ds["label"]?.stringValue ?? ds["name"]?.stringValue, nums)
            }
            if !traces.isEmpty { return traces }
        }
        if let data = component.raw["data"]?.arrayValue {
            if data.first?.numberValue != nil {
                return [(nil, data.compactMap { $0.numberValue })]
            }
            let traces: [Series] = data.compactMap { trace in
                guard let vals = trace["y"]?.arrayValue ?? trace["values"]?.arrayValue else { return nil }
                let nums = vals.compactMap { $0.numberValue }
                guard !nums.isEmpty else { return nil }
                return (trace["name"]?.stringValue, nums)
            }
            if !traces.isEmpty { return traces }
        }
        return []
    }
}
