import Foundation

// Stable, non-secret installation identity used only to bind an authenticated
// UI connection to voice-control REST calls. This is not an authorization key.
enum WatchVoiceDeviceIdentity {
    static let defaultsKey = "voice.device-id.v1"

    static func load(defaults: UserDefaults = .standard) -> String {
        if let existing = defaults.string(forKey: defaultsKey), isCanonicalUUID4(existing) {
            return existing
        }
        let fresh = UUID().uuidString.lowercased()
        defaults.set(fresh, forKey: defaultsKey)
        return fresh
    }

    private static func isCanonicalUUID4(_ value: String) -> Bool {
        value.range(
            of: "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            options: .regularExpression) != nil
    }
}
