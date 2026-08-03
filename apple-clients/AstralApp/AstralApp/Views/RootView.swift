import AstralCore
// Feature 051 — the app shell, a 1:1 match to the Android RootScaffold: a
// minimal top bar (square brand mark · New pill · Recent · server-owned chrome
// actions · Settings gear whose dropdown is built ENTIRELY from the server
// `chrome_menu` model), a connection strip + dismissible banner, and the
// navigable surfaces (Chat / Agents / History / Audit / Surface).
import SwiftUI

struct RootView: View {
    @Environment(AppModel.self) var model
    @Environment(ThemeStore.self) var theme

    private var p: AstralPalette { theme.palette }

    var body: some View {
        Group {
            if !model.signedIn {
                SignInView()
            } else {
                signedIn
            }
        }
        .background(rootBackground.ignoresSafeArea())
    }

    /// The web/Windows signature ambient glows: secondary 10% top-right,
    /// primary 8% bottom-left over the flat bg (astral.css body layers).
    private var rootBackground: some View {
        ZStack {
            p.bg
            RadialGradient(
                colors: [p.secondary.opacity(0.10), .clear],
                center: .topTrailing, startRadius: 0, endRadius: 500)
            RadialGradient(
                colors: [p.primary.opacity(0.08), .clear],
                center: .bottomLeading, startRadius: 0, endRadius: 500)
        }
    }

    private var signedIn: some View {
        VStack(spacing: 0) {
            AstralTopBar()
            if let label = model.connectionStripLabel {
                ConnectionStrip(label: label)
            }
            if let banner = model.errorBanner {
                BannerBar(text: banner, isError: model.bannerIsError) { model.dismissBanner() }
            }
            surface
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(p.bg.ignoresSafeArea())
        // T030: rotation / iPad Split View / macOS resize → update_device so
        // ROTE re-derives the layout for this socket.
        .background(
            GeometryReader { geo in
                Color.clear
                    .onAppear {
                        model.viewportChanged(
                            width: Int(geo.size.width),
                            height: Int(geo.size.height))
                    }
                    .onChange(of: geo.size) { _, size in
                        model.viewportChanged(
                            width: Int(size.width),
                            height: Int(size.height))
                    }
            }
        )
    }

    @ViewBuilder
    private var surface: some View {
        switch model.screen {
        case .chat: ChatShell()
        case .agents: AgentsView()
        case .history: HistoryView()
        case .audit: AuditView()
        case .surface: SurfaceView()
        }
    }
}

// MARK: - Top bar

struct AstralTopBar: View {
    @Environment(AppModel.self) var model
    @Environment(ThemeStore.self) var theme
    private var p: AstralPalette { theme.palette }

    var body: some View {
        HStack(spacing: 6) {
            Image("AstralIcon")
                .resizable().scaledToFit()
                .frame(width: 28, height: 28)
                .clipShape(RoundedRectangle(cornerRadius: AstralRadius.sm))

            Spacer()

            // 054 first-run gate: while the server pins a mandatory surface,
            // every navigation control is hidden — only the Settings gear
            // stays, reduced to its sign-out affordance (FR-013).
            if !model.mandatorySurface {
                newButton

                Button {
                    model.goTo(.history)
                } label: {
                    Image(systemName: "bubble.left.and.bubble.right")
                        .font(.system(size: 18)).foregroundStyle(p.text)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Recent chats")

                // Server-owned top-bar actions (pulse / timeline), rendered from the model.
                ForEach(model.chromeMenu?.topbarActions ?? []) { control in
                    if let action = control.action, !action.surface.isEmpty {
                        Button {
                            model.openSurface(action.surface, params: action.params)
                        } label: {
                            Image(systemName: topBarIcon(control.icon))
                                .font(.system(size: 18)).foregroundStyle(p.text)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(control.label ?? action.surface)
                    }
                }
            }

            settingsMenu
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(p.surface)
    }

    private var newButton: some View {
        Button {
            model.newChat()
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "plus").font(.caption2.bold())
                Text("New").font(.caption.bold())
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 11).padding(.vertical, 7)
            .background(p.gradient, in: Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("New chat")
    }

    private var settingsMenu: some View {
        Menu {
            // 054: server menu items are navigation — suppressed while the
            // mandatory surface is pinned; sign-out below always remains.
            if !model.mandatorySurface {
                ForEach(model.chromeMenu?.menu ?? []) { group in
                    Section(group.label) {
                        ForEach(group.items) { item in
                            Button(item.label) { model.openMenuItem(item) }
                        }
                    }
                }
                Divider()
            }
            if !model.accountName.isEmpty {
                Text(model.accountName)
            }
            Button(role: .destructive) {
                Task { await model.signOut() }
            } label: {
                Label(
                    model.chromeMenu?.signout.label ?? "Sign out",
                    systemImage: "rectangle.portrait.and.arrow.right")
            }
        } label: {
            Image(systemName: "gearshape").font(.system(size: 18)).foregroundStyle(p.text)
        }
        .accessibilityLabel("Settings")
    }

    // P11: keyed on the server model's own icon names (sparkle/history/gear —
    // menu_model.py), the same rule the Windows fix pinned after its map was
    // keyed on names the server never sends.
    private func topBarIcon(_ icon: String?) -> String {
        switch icon {
        case "sparkle": return "sparkles"
        case "history": return "clock.arrow.circlepath"
        case "gear": return "gearshape"
        default: return "ellipsis.circle"
        }
    }
}

// MARK: - Strips

struct ConnectionStrip: View {
    @Environment(ThemeStore.self) var theme
    let label: String
    var body: some View {
        Text(label)
            .font(.caption)
            .foregroundStyle(theme.palette.muted)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14).padding(.vertical, 5)
            .background(theme.palette.surface2)
    }
}

struct BannerBar: View {
    @Environment(ThemeStore.self) var theme
    let text: String
    let isError: Bool
    let onDismiss: () -> Void

    var body: some View {
        let color = isError ? theme.palette.error : theme.palette.info
        HStack(spacing: 8) {
            Text(text).font(.footnote).foregroundStyle(theme.palette.text)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button(action: onDismiss) {
                Image(systemName: "xmark").font(.caption).foregroundStyle(theme.palette.muted)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
        .background(color.opacity(0.16))
    }
}

// MARK: - Sign in (logo + SSO only; server/realm come from AstralConfig)

struct SignInView: View {
    @Environment(AppModel.self) var model

    var body: some View {
        VStack(spacing: 28) {
            Spacer()
            Image("AstralDeepLogo")
                .resizable().scaledToFit()
                .frame(maxWidth: 320)
                .accessibilityLabel("AstralDeep")
            Spacer()
            Button {
                model.signIn()
            } label: {
                Label("Sign in with SSO", systemImage: "person.badge.key")
                    .font(.headline)
                    .frame(maxWidth: 320)
                    .padding(.vertical, 6)
            }
            .buttonStyle(.borderedProminent)
            .accessibilityLabel("Sign in with single sign-on")
            if let error = model.signInError {
                Text(error).font(.footnote).foregroundStyle(.red).multilineTextAlignment(.center)
            }
            Spacer().frame(height: 48)
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private func previewTopBarAction(_ key: String, _ icon: String, _ label: String, _ surface: String) -> JSONValue {
    var o: [String: JSONValue] = [:]
    o["key"] = .string(key)
    o["kind"] = .string("action")
    o["icon"] = .string(icon)
    o["label"] = .string(label)
    o["action"] = .object(["surface": .string(surface)])
    return .object(o)
}

private func previewMenuItem(_ key: String, _ label: String, _ surface: String) -> JSONValue {
    .object(["key": .string(key), "label": .string(label), "surface": .string(surface)])
}

private func previewChrome() -> ChromeMenuModel? {
    let topbar: [JSONValue] = [
        previewTopBarAction("pulse", "sparkle", "Pulse", "pulse"),
        previewTopBarAction("timeline", "history", "Timeline", "workspace_timeline"),
        .object(["key": .string("settings"), "kind": .string("menu")]),
    ]
    let account: JSONValue = .object([
        "key": .string("account"), "label": .string("Account"),
        "items": .array([
            previewMenuItem("agents", "Agents & permissions", "agents"),
            previewMenuItem("llm", "Model settings", "llm"),
            previewMenuItem("theme", "Appearance", "theme"),
        ]),
    ])
    let help: JSONValue = .object([
        "key": .string("help"), "label": .string("Help"),
        "items": .array([previewMenuItem("audit", "Activity log", "audit")]),
    ])
    var root: [String: JSONValue] = [:]
    root["version"] = .number(1)
    root["topbar"] = .array(topbar)
    root["menu"] = .array([account, help])
    root["signout"] = .object(["label": .string("Sign out")])
    return ChromeMenuModel.fromJSON(.object(root))
}

private func previewCanvas() -> [AstralComponent] {
    // Authored with AstralPrims (the Swift astralprims mirror).
    [
        AstralPrims.Hero(
            title: "Q3 Sales",
            subtitle: "Revenue up 12% quarter over quarter",
            variant: "gradient"),
        AstralPrims.MetricCard(title: "Revenue", value: "$1.2M", subtitle: "+12%"),
    ].compactMap { AstralComponent(json: $0.toDict()) }
}

#Preview("Signed-in shell") {
    let model = AppModel()
    model.signedIn = true
    model.connected = true
    model.everConnected = true
    model.accountName = "Sam"
    model.turns = [.init(id: "u0", role: "user", text: "Show me Q3 sales")]
    model.canvas = previewCanvas()
    model.chromeMenu = previewChrome()
    return RootView()
        .environment(model)
        .environment(model.themeStore)
        .preferredColorScheme(.dark)
}
