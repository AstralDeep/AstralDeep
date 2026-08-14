// AstralPrims — the Swift mirror of the first-party `astralprims` Python
// package (github.com/AstralDeep/AstralPrimitives, mirrored at v0.3.0).
//
// Same philosophy as the original: JSON stays the wire format; these classes
// are the AUTHORING layer. Every primitive serializes with `toDict()` to the
// exact dict `astralprims.<X>(...).to_dict()` produces — pinned by the
// known-answer fixtures in Tests/AstralCoreTests/Fixtures/ (generated from
// the live Python package). The CONSUMING side of the wire stays
// `AstralComponent` (the renderer model): AstralPrims authors, the server's
// webrender/ROTE renders, AstralComponent reads — Constitution II unchanged.
//
// Serialization contract (mirrors astralprims.base.Primitive._serialize):
//   • "type" always present.
//   • Base fields css/id/class/tooltip only when set; an EMPTY css is omitted.
//   • `className` serializes as "class" (Python's `class_name` alias).
//   • A subclass's non-Optional fields are ALWAYS emitted (even empty lists /
//     dicts / empty strings — e.g. Button emits `payload: {}`).
//   • Optional fields are dropped when nil.
//   • `attributes` merge LAST at the top level and may override anything —
//     the same escape hatch as the Python package.
//
// Naming: types are namespaced under `AstralPrims` so `Text`, `Image`,
// `Divider`, `Table`, `List`, `Button` never collide with SwiftUI. Python
// names that dodge Python quirks keep their wire-truth here:
// `List_` → `AstralPrims.List`, `CodeBlock` → type "code",
// `ProgressBar` → "progress", `MetricCard` → "metric", `Grids` → "grid"
// (with the same `Grid` alias the package exports).
import Foundation

public enum AstralPrims {

    // MARK: - Base

    /// Base class for all UI primitives (mirror of `astralprims.Primitive`).
    open class Primitive {
        public let type: String
        public var css: [String: String]?
        public var id: String?
        /// Serialized as `"class"` (the Python `class_name` alias).
        public var className: String?
        public var tooltip: String?
        /// Free-form extras merged into the output at the top level (escape
        /// hatch; merged last so they can override or extend).
        public var attributes: [String: JSONValue]

        public init(type: String) {
            self.type = type
            self.attributes = [:]
        }

        /// Subclass wire fields as (key, value) pairs; nil values are dropped.
        open var ownFields: [(String, JSONValue?)] { [] }

        /// Serialize to the wire dict — the Swift `to_dict()`.
        public final func toDict() -> JSONValue {
            var out: [String: JSONValue] = ["type": .string(type)]
            if let css, !css.isEmpty {
                out["css"] = .object(css.mapValues { .string($0) })
            }
            if let id { out["id"] = .string(id) }
            if let className { out["class"] = .string(className) }
            if let tooltip { out["tooltip"] = .string(tooltip) }
            for (key, value) in ownFields {
                if let value { out[key] = value }
            }
            for (key, value) in attributes { out[key] = value }
            return .object(out)
        }

        /// Serialize to a JSON string — the Swift `to_json()`.
        public final func toJSONString() throws -> String {
            let data = try toDict().encoded()
            return String(decoding: data, as: UTF8.self)
        }

        // Fluent styling modifiers (the Python base fields arrive as kwargs;
        // Swift keeps the per-type inits tight and chains these instead).

        @discardableResult
        public func css(_ css: [String: String]) -> Self {
            self.css = css
            return self
        }

        @discardableResult
        public func id(_ id: String) -> Self {
            self.id = id
            return self
        }

        @discardableResult
        public func className(_ name: String) -> Self {
            self.className = name
            return self
        }

        @discardableResult
        public func tooltip(_ tip: String) -> Self {
            self.tooltip = tip
            return self
        }

        @discardableResult
        public func attributes(_ attrs: [String: JSONValue]) -> Self {
            self.attributes.merge(attrs) { _, new in new }
            return self
        }
    }

    /// Wrap primitives in the standard UI response envelope — the Swift
    /// `create_ui_response()`.
    public static func createUIResponse(_ components: [Primitive]) -> JSONValue {
        .object([
            "_ui_components": .array(components.map { $0.toDict() }),
            "_data": .null,
        ])
    }

    /// Every wire `type` this authoring layer can produce (the package's
    /// registry). The manifest additionally lists renderer-origin types the
    /// package deliberately does NOT author: download_card, generative,
    /// skeleton — pinned by PrimitivesTests.
    public static let allTypes: Set<String> = [
        "alert", "audio", "badge", "bar_chart", "button", "card",
        "chat_history", "code", "collapsible", "color_picker", "container",
        "divider", "file_download", "file_upload", "grid", "hero", "image",
        "input", "keyvalue", "line_chart", "list", "metric", "param_picker",
        "pie_chart", "plotly_chart", "progress", "rating", "table", "tabs",
        "text", "theme_apply", "timeline",
    ]

    // MARK: - Serialization helpers

    static func strings(_ values: [String]) -> JSONValue {
        .array(values.map { .string($0) })
    }

    static func numbers(_ values: [Double]) -> JSONValue {
        .array(values.map { .number($0) })
    }

    // MARK: - Layout

    /// A layout container holding child primitives.
    public final class Container: Primitive {
        public var children: [Primitive]
        public var direction: String?  // e.g. "row" | "column"

        public init(children: [Primitive] = [], direction: String? = nil) {
            self.children = children
            self.direction = direction
            super.init(type: "container")
        }

        /// Append one or more children and return self (chainable).
        @discardableResult
        public func add(_ children: Primitive...) -> Container {
            self.children.append(contentsOf: children)
            return self
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("children", .array(children.map { $0.toDict() })),
                ("direction", direction.map(JSONValue.string)),
            ]
        }
    }

    /// A titled card wrapping child primitives.
    public final class Card: Primitive {
        public var title: String
        public var content: [Primitive]
        public var variant: String

        public init(title: String = "", content: [Primitive] = [], variant: String = "default") {
            self.title = title
            self.content = content
            self.variant = variant
            super.init(type: "card")
        }

        @discardableResult
        public func add(_ content: Primitive...) -> Card {
            self.content.append(contentsOf: content)
            return self
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("title", .string(title)),
                ("content", .array(content.map { $0.toDict() })),
                ("variant", .string(variant)),
            ]
        }
    }

    /// A grid layout with a fixed column count (wire type "grid").
    public final class Grid: Primitive {
        public var columns: Int
        public var children: [Primitive]
        public var gap: Int

        public init(columns: Int = 2, children: [Primitive] = [], gap: Int = 20) {
            self.columns = columns
            self.children = children
            self.gap = gap
            super.init(type: "grid")
        }

        @discardableResult
        public func add(_ children: Primitive...) -> Grid {
            self.children.append(contentsOf: children)
            return self
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("columns", .number(Double(columns))),
                ("children", .array(children.map { $0.toDict() })),
                ("gap", .number(Double(gap))),
            ]
        }
    }

    /// Backwards-compatible alias (the package exports both Grid and Grids).
    public typealias Grids = Grid

    /// A single tab. Not a primitive itself (no `type`); nested in `Tabs`.
    public struct TabItem {
        public var label: String
        public var content: [Primitive]
        public var value: String?

        public init(label: String = "", content: [Primitive] = [], value: String? = nil) {
            self.label = label
            self.content = content
            self.value = value
        }

        public func toDict() -> JSONValue {
            var out: [String: JSONValue] = [
                "label": .string(label),
                "content": .array(content.map { $0.toDict() }),
            ]
            if let value { out["value"] = .string(value) }
            return .object(out)
        }
    }

    /// A tabbed container.
    public final class Tabs: Primitive {
        public var tabs: [TabItem]
        public var variant: String

        public init(tabs: [TabItem] = [], variant: String = "default") {
            self.tabs = tabs
            self.variant = variant
            super.init(type: "tabs")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("tabs", .array(tabs.map { $0.toDict() })),
                ("variant", .string(variant)),
            ]
        }
    }

    /// A collapsible / accordion section.
    public final class Collapsible: Primitive {
        public var title: String
        public var content: [Primitive]
        public var defaultOpen: Bool

        public init(title: String = "", content: [Primitive] = [], defaultOpen: Bool = false) {
            self.title = title
            self.content = content
            self.defaultOpen = defaultOpen
            super.init(type: "collapsible")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("title", .string(title)),
                ("content", .array(content.map { $0.toDict() })),
                ("default_open", .bool(defaultOpen)),
            ]
        }
    }

    /// A horizontal rule / visual separator.
    public final class Divider: Primitive {
        public var variant: String

        public init(variant: String = "solid") {
            self.variant = variant
            super.init(type: "divider")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [("variant", .string(variant))]
        }
    }

    // MARK: - Content & controls

    /// A run of text. `variant` is one of h1, h2, h3, body, caption.
    public final class Text: Primitive {
        public var content: String
        public var variant: String

        public init(content: String = "", variant: String = "body") {
            self.content = content
            self.variant = variant
            super.init(type: "text")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [("content", .string(content)), ("variant", .string(variant))]
        }
    }

    /// A clickable button dispatching an action with an optional payload.
    public final class Button: Primitive {
        public var label: String
        public var action: String
        public var payload: [String: JSONValue]
        public var variant: String

        public init(
            label: String = "", action: String = "",
            payload: [String: JSONValue] = [:], variant: String = "primary"
        ) {
            self.label = label
            self.action = action
            self.payload = payload
            self.variant = variant
            super.init(type: "button")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("label", .string(label)),
                ("action", .string(action)),
                ("payload", .object(payload)),
                ("variant", .string(variant)),
            ]
        }
    }

    /// A single-line form input.
    public final class Input: Primitive {
        public var placeholder: String
        public var name: String
        public var value: String

        public init(placeholder: String = "", name: String = "", value: String = "") {
            self.placeholder = placeholder
            self.name = name
            self.value = value
            super.init(type: "input")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("placeholder", .string(placeholder)),
                ("name", .string(name)),
                ("value", .string(value)),
            ]
        }
    }

    /// Interactive parameter form. Each entry in `fields` is a dict:
    /// `{"name","label","kind","default","options","help","step"}` — see the
    /// Python docstring; `submit_message_template` supports `{field_name}`
    /// and `{__values_json__}` placeholders.
    public final class ParamPicker: Primitive {
        public var title: String
        public var description: String
        public var fields: [JSONValue]
        public var submitLabel: String
        public var submitMessageTemplate: String

        public init(
            title: String = "", description: String = "",
            fields: [JSONValue] = [], submitLabel: String = "Submit",
            submitMessageTemplate: String = ""
        ) {
            self.title = title
            self.description = description
            self.fields = fields
            self.submitLabel = submitLabel
            self.submitMessageTemplate = submitMessageTemplate
            super.init(type: "param_picker")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("title", .string(title)),
                ("description", .string(description)),
                ("fields", .array(fields)),
                ("submit_label", .string(submitLabel)),
                ("submit_message_template", .string(submitMessageTemplate)),
            ]
        }
    }

    /// An image.
    public final class Image: Primitive {
        public var url: String
        public var alt: String?
        public var width: String?
        public var height: String?

        public init(
            url: String = "", alt: String? = nil,
            width: String? = nil, height: String? = nil
        ) {
            self.url = url
            self.alt = alt
            self.width = width
            self.height = height
            super.init(type: "image")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("url", .string(url)),
                ("alt", alt.map(JSONValue.string)),
                ("width", width.map(JSONValue.string)),
                ("height", height.map(JSONValue.string)),
            ]
        }
    }

    /// A syntax-highlighted code block (wire type "code").
    public final class CodeBlock: Primitive {
        public var code: String
        public var language: String
        public var showLineNumbers: Bool

        public init(
            code: String = "", language: String = "text",
            showLineNumbers: Bool = false
        ) {
            self.code = code
            self.language = language
            self.showLineNumbers = showLineNumbers
            super.init(type: "code")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("code", .string(code)),
                ("language", .string(language)),
                ("show_line_numbers", .bool(showLineNumbers)),
            ]
        }
    }

    /// A callout / banner. `variant` is info, success, warning, or error.
    public final class Alert: Primitive {
        public var message: String
        public var variant: String
        public var title: String?

        public init(message: String = "", variant: String = "info", title: String? = nil) {
            self.message = message
            self.variant = variant
            self.title = title
            super.init(type: "alert")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("message", .string(message)),
                ("variant", .string(variant)),
                ("title", title.map(JSONValue.string)),
            ]
        }
    }

    /// A progress bar (wire type "progress").
    public final class ProgressBar: Primitive {
        public var value: Double
        public var label: String?
        public var variant: String
        public var showPercentage: Bool

        public init(
            value: Double = 0.0, label: String? = nil,
            variant: String = "default", showPercentage: Bool = true
        ) {
            self.value = value
            self.label = label
            self.variant = variant
            self.showPercentage = showPercentage
            super.init(type: "progress")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("value", .number(value)),
                ("label", label.map(JSONValue.string)),
                ("variant", .string(variant)),
                ("show_percentage", .bool(showPercentage)),
            ]
        }
    }

    /// A single KPI / metric tile (wire type "metric").
    public final class MetricCard: Primitive {
        public var title: String
        public var value: String
        public var subtitle: String?
        public var icon: String?
        public var variant: String
        public var progress: Double?

        public init(
            title: String = "", value: String = "", subtitle: String? = nil,
            icon: String? = nil, variant: String = "default",
            progress: Double? = nil
        ) {
            self.title = title
            self.value = value
            self.subtitle = subtitle
            self.icon = icon
            self.variant = variant
            self.progress = progress
            super.init(type: "metric")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("title", .string(title)),
                ("value", .string(value)),
                ("subtitle", subtitle.map(JSONValue.string)),
                ("icon", icon.map(JSONValue.string)),
                ("variant", .string(variant)),
                ("progress", progress.map(JSONValue.number)),
            ]
        }
    }

    /// An ordered or unordered list. Items are strings or dicts (the Python
    /// `List_`; wire type "list").
    public final class List: Primitive {
        public var items: [JSONValue]
        public var ordered: Bool
        public var variant: String

        public init(
            items: [JSONValue] = [], ordered: Bool = false,
            variant: String = "default"
        ) {
            self.items = items
            self.ordered = ordered
            self.variant = variant
            super.init(type: "list")
        }

        /// Convenience for the common all-strings case.
        public convenience init(
            items: [String], ordered: Bool = false,
            variant: String = "default"
        ) {
            self.init(items: items.map(JSONValue.string), ordered: ordered, variant: variant)
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("items", .array(items)),
                ("ordered", .bool(ordered)),
                ("variant", .string(variant)),
            ]
        }
    }

    /// A data table with optional pagination + tool re-invocation context.
    public final class Table: Primitive {
        public var headers: [String]
        public var rows: [[JSONValue]]
        public var variant: String
        public var totalRows: Int?
        public var pageSize: Int?
        public var pageOffset: Int?
        public var pageSizes: [Int]
        public var sourceTool: String?
        public var sourceAgent: String?
        public var sourceParams: [String: JSONValue]

        public init(
            headers: [String] = [], rows: [[JSONValue]] = [],
            variant: String = "default", totalRows: Int? = nil,
            pageSize: Int? = nil, pageOffset: Int? = nil,
            pageSizes: [Int] = [], sourceTool: String? = nil,
            sourceAgent: String? = nil, sourceParams: [String: JSONValue] = [:]
        ) {
            self.headers = headers
            self.rows = rows
            self.variant = variant
            self.totalRows = totalRows
            self.pageSize = pageSize
            self.pageOffset = pageOffset
            self.pageSizes = pageSizes
            self.sourceTool = sourceTool
            self.sourceAgent = sourceAgent
            self.sourceParams = sourceParams
            super.init(type: "table")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("headers", AstralPrims.strings(headers)),
                ("rows", .array(rows.map { .array($0) })),
                ("variant", .string(variant)),
                ("total_rows", totalRows.map { .number(Double($0)) }),
                ("page_size", pageSize.map { .number(Double($0)) }),
                ("page_offset", pageOffset.map { .number(Double($0)) }),
                ("page_sizes", .array(pageSizes.map { .number(Double($0)) })),
                ("source_tool", sourceTool.map(JSONValue.string)),
                ("source_agent", sourceAgent.map(JSONValue.string)),
                ("source_params", .object(sourceParams)),
            ]
        }
    }

    // MARK: - Charts

    /// A named series of values. Not a primitive itself.
    public struct ChartDataset {
        public var label: String
        public var data: [Double]
        public var color: String?

        public init(label: String = "", data: [Double] = [], color: String? = nil) {
            self.label = label
            self.data = data
            self.color = color
        }

        public func toDict() -> JSONValue {
            var out: [String: JSONValue] = [
                "label": .string(label),
                "data": AstralPrims.numbers(data),
            ]
            if let color { out["color"] = .string(color) }
            return .object(out)
        }
    }

    /// A bar chart.
    public final class BarChart: Primitive {
        public var title: String
        public var labels: [String]
        public var datasets: [JSONValue]

        public init(title: String = "", labels: [String] = [], datasets: [JSONValue] = []) {
            self.title = title
            self.labels = labels
            self.datasets = datasets
            super.init(type: "bar_chart")
        }

        public convenience init(
            title: String = "", labels: [String] = [],
            datasets: [ChartDataset]
        ) {
            self.init(title: title, labels: labels, datasets: datasets.map { $0.toDict() })
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("title", .string(title)),
                ("labels", AstralPrims.strings(labels)),
                ("datasets", .array(datasets)),
            ]
        }
    }

    /// A line chart.
    public final class LineChart: Primitive {
        public var title: String
        public var labels: [String]
        public var datasets: [JSONValue]

        public init(title: String = "", labels: [String] = [], datasets: [JSONValue] = []) {
            self.title = title
            self.labels = labels
            self.datasets = datasets
            super.init(type: "line_chart")
        }

        public convenience init(
            title: String = "", labels: [String] = [],
            datasets: [ChartDataset]
        ) {
            self.init(title: title, labels: labels, datasets: datasets.map { $0.toDict() })
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("title", .string(title)),
                ("labels", AstralPrims.strings(labels)),
                ("datasets", .array(datasets)),
            ]
        }
    }

    /// A pie chart.
    public final class PieChart: Primitive {
        public var title: String
        public var labels: [String]
        public var data: [Double]
        public var colors: [String]

        public init(
            title: String = "", labels: [String] = [],
            data: [Double] = [], colors: [String] = []
        ) {
            self.title = title
            self.labels = labels
            self.data = data
            self.colors = colors
            super.init(type: "pie_chart")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("title", .string(title)),
                ("labels", AstralPrims.strings(labels)),
                ("data", AstralPrims.numbers(data)),
                ("colors", AstralPrims.strings(colors)),
            ]
        }
    }

    /// An arbitrary Plotly figure (data + layout + config).
    public final class PlotlyChart: Primitive {
        public var title: String
        public var data: [JSONValue]
        public var layout: [String: JSONValue]
        public var config: [String: JSONValue]

        public init(
            title: String = "", data: [JSONValue] = [],
            layout: [String: JSONValue] = [:], config: [String: JSONValue] = [:]
        ) {
            self.title = title
            self.data = data
            self.layout = layout
            self.config = config
            super.init(type: "plotly_chart")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("title", .string(title)),
                ("data", .array(data)),
                ("layout", .object(layout)),
                ("config", .object(config)),
            ]
        }
    }

    // MARK: - Media & I/O

    /// Audio player primitive (inline base64 data, URLs, speech, MIDI). NOTE:
    /// this type's wire keys are camelCase in the package — mirrored exactly.
    public final class Audio: Primitive {
        public var src: String
        public var contentType: String?
        public var autoplay: Bool
        public var loop: Bool
        public var label: String?
        public var showControls: Bool
        public var description: String?

        public init(
            src: String = "", contentType: String? = nil,
            autoplay: Bool = false, loop: Bool = false,
            label: String? = nil, showControls: Bool = true,
            description: String? = nil
        ) {
            self.src = src
            self.contentType = contentType
            self.autoplay = autoplay
            self.loop = loop
            self.label = label
            self.showControls = showControls
            self.description = description
            super.init(type: "audio")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("src", .string(src)),
                ("contentType", contentType.map(JSONValue.string)),
                ("autoplay", .bool(autoplay)),
                ("loop", .bool(loop)),
                ("label", label.map(JSONValue.string)),
                ("showControls", .bool(showControls)),
                ("description", description.map(JSONValue.string)),
            ]
        }
    }

    /// A file upload control.
    public final class FileUpload: Primitive {
        public var label: String
        public var accept: String
        public var action: String

        public init(label: String = "Upload File", accept: String = "*/*", action: String = "") {
            self.label = label
            self.accept = accept
            self.action = action
            super.init(type: "file_upload")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("label", .string(label)),
                ("accept", .string(accept)),
                ("action", .string(action)),
            ]
        }
    }

    /// A file download link/button.
    public final class FileDownload: Primitive {
        public var label: String
        public var url: String
        public var filename: String?

        public init(label: String = "Download File", url: String = "", filename: String? = nil) {
            self.label = label
            self.url = url
            self.filename = filename
            super.init(type: "file_download")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("label", .string(label)),
                ("url", .string(url)),
                ("filename", filename.map(JSONValue.string)),
            ]
        }
    }

    // MARK: - Dashboard & status

    /// A small inline status chip. `variant`: default, success, warning,
    /// error, info, or accent.
    public final class Badge: Primitive {
        public var label: String
        public var variant: String
        public var icon: String?

        public init(label: String = "", variant: String = "default", icon: String? = nil) {
            self.label = label
            self.variant = variant
            self.icon = icon
            super.init(type: "badge")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("label", .string(label)),
                ("variant", .string(variant)),
                ("icon", icon.map(JSONValue.string)),
            ]
        }
    }

    /// A page-level header band: eyebrow, title, subtitle, optional badges.
    /// `variant`: default, gradient, or subtle.
    public final class Hero: Primitive {
        public var title: String
        public var subtitle: String?
        public var eyebrow: String?
        public var icon: String?
        public var variant: String
        public var badges: [String]

        public init(
            title: String = "", subtitle: String? = nil, eyebrow: String? = nil,
            icon: String? = nil, variant: String = "default", badges: [String] = []
        ) {
            self.title = title
            self.subtitle = subtitle
            self.eyebrow = eyebrow
            self.icon = icon
            self.variant = variant
            self.badges = badges
            super.init(type: "hero")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("title", .string(title)),
                ("subtitle", subtitle.map(JSONValue.string)),
                ("eyebrow", eyebrow.map(JSONValue.string)),
                ("icon", icon.map(JSONValue.string)),
                ("variant", .string(variant)),
                ("badges", AstralPrims.strings(badges)),
            ]
        }
    }

    /// A compact label/value fact sheet (wire type "keyvalue"). Each entry in
    /// `items`: `{"label","value","hint"}` — hint optional; `columns` 1–4.
    public final class KeyValue: Primitive {
        public var title: String?
        public var items: [JSONValue]
        public var columns: Int

        public init(title: String? = nil, items: [JSONValue] = [], columns: Int = 2) {
            self.title = title
            self.items = items
            self.columns = columns
            super.init(type: "keyvalue")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("title", title.map(JSONValue.string)),
                ("items", .array(items)),
                ("columns", .number(Double(columns))),
            ]
        }
    }

    /// A vertical sequence of events/appointments/steps. Each entry in
    /// `items`: `{"time","title","description","variant"}`.
    public final class Timeline: Primitive {
        public var title: String?
        public var items: [JSONValue]
        public var variant: String

        public init(title: String? = nil, items: [JSONValue] = [], variant: String = "default") {
            self.title = title
            self.items = items
            self.variant = variant
            super.init(type: "timeline")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("title", title.map(JSONValue.string)),
                ("items", .array(items)),
                ("variant", .string(variant)),
            ]
        }
    }

    /// A star-rating readout.
    public final class Rating: Primitive {
        public var value: Double
        public var maxValue: Int
        public var label: String?
        public var subtitle: String?
        public var showValue: Bool

        public init(
            value: Double = 0.0, maxValue: Int = 5, label: String? = nil,
            subtitle: String? = nil, showValue: Bool = true
        ) {
            self.value = value
            self.maxValue = maxValue
            self.label = label
            self.subtitle = subtitle
            self.showValue = showValue
            super.init(type: "rating")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("value", .number(value)),
                ("max_value", .number(Double(maxValue))),
                ("label", label.map(JSONValue.string)),
                ("subtitle", subtitle.map(JSONValue.string)),
                ("show_value", .bool(showValue)),
            ]
        }
    }

    /// A scannable list of recent conversations. Each entry in `items`:
    /// `{"chat_id","title","preview","time","icon","saved"}` — only chat_id
    /// and title are required; selecting a row dispatches `load_chat`.
    public final class ChatHistory: Primitive {
        public var title: String?
        public var items: [JSONValue]

        public init(title: String? = "Recent chats", items: [JSONValue] = []) {
            self.title = title
            self.items = items
            super.init(type: "chat_history")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("title", title.map(JSONValue.string)),
                ("items", .array(items)),
            ]
        }
    }

    // MARK: - Theming

    /// A color picker bound to a theme color key.
    public final class ColorPicker: Primitive {
        public var label: String
        public var colorKey: String
        public var value: String

        public init(label: String = "", colorKey: String = "", value: String = "#000000") {
            self.label = label
            self.colorKey = colorKey
            self.value = value
            super.init(type: "color_picker")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("label", .string(label)),
                ("color_key", .string(colorKey)),
                ("value", .string(value)),
            ]
        }
    }

    /// Applies a theme preset or individual color change.
    public final class ThemeApply: Primitive {
        public var preset: String?
        public var colors: [String: String]?
        public var colorKey: String?
        public var colorValue: String?
        public var message: String

        public init(
            preset: String? = nil, colors: [String: String]? = nil,
            colorKey: String? = nil, colorValue: String? = nil,
            message: String = ""
        ) {
            self.preset = preset
            self.colors = colors
            self.colorKey = colorKey
            self.colorValue = colorValue
            self.message = message
            super.init(type: "theme_apply")
        }

        override public var ownFields: [(String, JSONValue?)] {
            [
                ("preset", preset.map(JSONValue.string)),
                ("colors", colors.map { .object($0.mapValues(JSONValue.string)) }),
                ("color_key", colorKey.map(JSONValue.string)),
                ("color_value", colorValue.map(JSONValue.string)),
                ("message", .string(message)),
            ]
        }
    }
}
