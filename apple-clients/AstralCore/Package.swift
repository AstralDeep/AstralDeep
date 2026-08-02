// swift-tools-version: 5.9
// Feature 051 — shared first-party core for the three Apple SDUI clients.
// ZERO third-party dependencies (Constitution V): Foundation, CryptoKit,
// URLSession only. All protocol/transport/auth logic lives here so
// `swift test` covers it headlessly (no Xcode project required).
import CryptoKit
import Foundation
import PackageDescription

// Feature 065 keeps one canonical C0-C6 fixture outside the client trees.
let packageRoot = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
let repositoryRoot =
    packageRoot
    .deletingLastPathComponent()  // apple-clients
    .deletingLastPathComponent()  // repository
let canonicalVoiceFixture = repositoryRoot.appendingPathComponent(
    "backend/tests/fixtures/voice_065/client_conformance.json")
let linkedVoiceFixture = packageRoot.appendingPathComponent(
    "Tests/AstralCoreTests/Fixtures/voice_065/client_conformance.json")
let expectedVoiceFixtureSHA256 =
    "bc98077594fa8d51dd664fadefaa48cf596a94e7fb2a961a972dbabca4f02143"

func sha256(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

do {
    let canonicalBytes = try Data(contentsOf: canonicalVoiceFixture)
    let canonicalDigest = sha256(canonicalBytes)
    guard canonicalDigest == expectedVoiceFixtureSHA256 else {
        fatalError("canonical Feature 065 voice fixture digest changed: \(canonicalDigest)")
    }
    let linkedBytes = try Data(contentsOf: linkedVoiceFixture)
    guard sha256(linkedBytes) == canonicalDigest, linkedBytes == canonicalBytes else {
        fatalError("Feature 065 test-resource link differs from its canonical source")
    }
} catch {
    fatalError("unable to validate canonical Feature 065 voice fixture: \(error)")
}

let package = Package(
    name: "AstralCore",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
        .watchOS(.v10),
    ],
    products: [
        .library(name: "AstralCore", targets: ["AstralCore"])
    ],
    targets: [
        .target(name: "AstralCore", path: "Sources/AstralCore"),
        .testTarget(
            name: "AstralCoreTests",
            dependencies: ["AstralCore"],
            path: "Tests/AstralCoreTests",
            // Existing AstralPrims generator inputs remain source-tree-only.
            // The relative link below has no client-owned JSON bytes; SwiftPM
            // copies its hash-checked canonical target into Bundle.module.
            exclude: [
                "Fixtures/astralprims-fixtures.json",
                "Fixtures/generate_fixtures.py",
            ],
            resources: [.process("Fixtures/voice_065")]
        ),
    ]
)
