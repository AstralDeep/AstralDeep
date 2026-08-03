import AVFoundation
import AstralCore
import Foundation
// Feature 051 — iOS (twin of Android, US1) + macOS (twin of Windows, US2)
// in one multiplatform SwiftUI target on the shared AstralCore package.
import SwiftUI

#if os(iOS)
    import UIKit
#else
    import AppKit
#endif

@main
struct AstralApp: App {
    @State private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase
    private let unitTestHost =
        ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil

    init() {
        NoStoreHTTP.prepareForLaunch()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
                .environment(model.themeStore)
                .tint(model.themeStore.palette.primary)
                .preferredColorScheme(.dark)
                .task {
                    #if DEBUG
                        if let scenario = FirstLoginUITestFixture.requestedScenario() {
                            FirstLoginUITestFixture.install(scenario, on: model)
                            return
                        }
                    #endif
                    // A macOS unit-test bundle is injected into the app host.
                    // Starting the real login bootstrap there can block on a
                    // developer login-keychain prompt before XCTest begins.
                    // UI-test apps are separate processes and do not carry
                    // XCTestConfigurationFilePath, so their real launch path
                    // remains unchanged.
                    if !unitTestHost { await model.bootstrap() }
                }
                .onChange(of: scenePhase) { _, phase in
                    switch phase {
                    case .active: model.voiceSceneBecameActive()
                    case .inactive, .background: model.voiceSceneBecameInactive()
                    @unknown default: model.voiceSceneBecameInactive()
                    }
                }
                #if os(iOS)
                    .onReceive(
                        NotificationCenter.default.publisher(
                            for: AVAudioSession.interruptionNotification)
                    ) { notification in
                        guard
                            let raw = notification.userInfo?[AVAudioSessionInterruptionTypeKey]
                                as? UInt,
                            let type = AVAudioSession.InterruptionType(rawValue: raw)
                        else { return }
                        if type == .began {
                            model.voiceAudioSessionInterrupted()
                        } else {
                            model.voiceAudioSessionInterruptionEnded()
                        }
                    }
                    .onReceive(
                        NotificationCenter.default.publisher(
                            for: AVAudioSession.routeChangeNotification)
                    ) { _ in
                        model.voiceAudioRouteChanged()
                    }
                    .onReceive(
                        NotificationCenter.default.publisher(
                            for: UIApplication.protectedDataWillBecomeUnavailableNotification)
                    ) { _ in
                        model.voiceSessionLocked()
                    }
                    .onReceive(
                        NotificationCenter.default.publisher(
                            for: UIApplication.protectedDataDidBecomeAvailableNotification)
                    ) { _ in
                        model.voiceSessionUnlocked()
                    }
                    .onReceive(
                        NotificationCenter.default.publisher(
                            for: UIApplication.willTerminateNotification)
                    ) { _ in
                        model.voiceApplicationWillTerminate()
                    }
                #else
                    .onReceive(
                        NSWorkspace.shared.notificationCenter.publisher(
                            for: NSWorkspace.sessionDidResignActiveNotification)
                    ) { _ in
                        model.voiceSessionLocked()
                    }
                    .onReceive(
                        NSWorkspace.shared.notificationCenter.publisher(
                            for: NSWorkspace.sessionDidBecomeActiveNotification)
                    ) { _ in
                        model.voiceSessionUnlocked()
                    }
                    .onReceive(
                        NotificationCenter.default.publisher(
                            for: .AVAudioEngineConfigurationChange)
                    ) { _ in
                        model.voiceAudioEngineConfigurationChanged()
                    }
                    .onReceive(
                        NotificationCenter.default.publisher(
                            for: NSApplication.willTerminateNotification)
                    ) { _ in
                        model.voiceApplicationWillTerminate()
                    }
                #endif
                #if os(macOS)
                    .frame(minWidth: 900, minHeight: 600)
                #endif
        }
        #if os(macOS)
            .windowStyle(.titleBar)
            // 066: a fresh window opens wide enough for the split layout
            // (canvas leading, rail trailing); the ≥1024pt breakpoint matches
            // the web client. Users can still resize down to the 900pt
            // minimum, where the collapsed (floating-composer) mode takes over.
            .defaultSize(width: 1280, height: 820)
        #endif
    }
}
