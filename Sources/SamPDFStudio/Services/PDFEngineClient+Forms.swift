import CoreGraphics
import Foundation

// MARK: - Models

/// Kinds of AcroForm fields the engine creates and reports
/// (`Engine/ext/forms.py`, `FIELD_TYPES`).
enum PDFFormFieldKind: String, CaseIterable, Identifiable, Codable {
    case text, multiline, checkbox, radio, combo, list, signature, button

    var id: String { rawValue }

    var title: String {
        switch self {
        case .text: "Text Field"
        case .multiline: "Multiline Text"
        case .checkbox: "Checkbox"
        case .radio: "Radio Group"
        case .combo: "Dropdown"
        case .list: "List Box"
        case .signature: "Signature"
        case .button: "Button"
        }
    }

    var systemImage: String {
        switch self {
        case .text: "character.cursor.ibeam"
        case .multiline: "text.alignleft"
        case .checkbox: "checkmark.square"
        case .radio: "smallcircle.filled.circle"
        case .combo: "chevron.up.chevron.down"
        case .list: "list.bullet.rectangle"
        case .signature: "signature"
        case .button: "hand.tap"
        }
    }

    /// Size used when a field is placed without a drag (points).
    var defaultSize: CGSize {
        switch self {
        case .text: CGSize(width: 180, height: 22)
        case .multiline: CGSize(width: 260, height: 72)
        case .checkbox: CGSize(width: 14, height: 14)
        case .radio: CGSize(width: 150, height: 16)
        case .combo: CGSize(width: 160, height: 22)
        case .list: CGSize(width: 150, height: 64)
        case .signature: CGSize(width: 200, height: 44)
        case .button: CGSize(width: 96, height: 24)
        }
    }

    /// Fields whose value is one of a fixed set of options.
    var hasOptions: Bool { self == .combo || self == .list || self == .radio }
    /// Fields that draw text and honor a font size.
    var hasTextStyle: Bool { self == .text || self == .multiline || self == .combo || self == .list }
    /// Fields a user can type or pick a value for.
    var isFillable: Bool { self != .signature && self != .button }

    /// Options a freshly placed choice field starts with (editable later).
    var defaultOptions: [String] {
        switch self {
        case .radio: ["Option 1", "Option 2", "Option 3"]
        case .combo, .list: ["Option 1", "Option 2", "Option 3"]
        default: []
        }
    }
}

/// A field value as the engine reports it: checkboxes are booleans, every
/// other kind is a string ("" = empty / nothing selected).
enum PDFFormFieldValue: Codable, Hashable {
    case text(String)
    case bool(Bool)

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .text("")
        } else if let flag = try? container.decode(Bool.self) {
            self = .bool(flag)
        } else if let string = try? container.decode(String.self) {
            self = .text(string)
        } else if let number = try? container.decode(Double.self) {
            self = .text(String(number))
        } else {
            self = .text("")
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .text(let string): try container.encode(string)
        case .bool(let flag): try container.encode(flag)
        }
    }

    var stringValue: String {
        switch self {
        case .text(let string): string
        case .bool(let flag): flag ? "true" : "false"
        }
    }

    var boolValue: Bool {
        switch self {
        case .bool(let flag): flag
        case .text(let string): ["1", "true", "yes", "on"].contains(string.lowercased())
        }
    }

    /// JSON-serializable form for `fill-fields`.
    var jsonObject: Any {
        switch self {
        case .text(let string): string
        case .bool(let flag): flag
        }
    }
}

/// One widget (on-page appearance) of a field. Radio groups have several.
struct PDFFormWidget: Decodable, Hashable {
    let page: Int
    let rect: [Double]
    let state: String?

    var engineRect: CGRect { CGRect(engineRectArray: rect) }
}

/// A form field from `list-fields`. Rects are engine coordinates (points,
/// top-left origin of the unrotated CropBox, y down).
struct PDFFormField: Decodable, Identifiable, Hashable {
    let name: String
    let type: String
    let page: Int
    let rect: [Double]
    let value: PDFFormFieldValue
    let options: [String]
    let required: Bool
    let readonly: Bool
    let tooltip: String
    let fontSize: Double
    let maxLen: Int
    let widgets: [PDFFormWidget]

    var id: String { name }
    var kind: PDFFormFieldKind? { PDFFormFieldKind(rawValue: type) }
    var engineRect: CGRect { CGRect(engineRectArray: rect) }

    enum CodingKeys: String, CodingKey {
        case name, type, page, rect, value, options, required, readonly, tooltip, widgets
        case fontSize = "font_size"
        case maxLen = "max_len"
    }
}

/// A likely field found by `detect-fields`.
struct PDFFormFieldSuggestion: Decodable, Hashable {
    let page: Int
    let rect: [Double]
    let type: String
    let nameHint: String
    let source: String
    let name: String?

    var kind: PDFFormFieldKind? { PDFFormFieldKind(rawValue: type) }
    var engineRect: CGRect { CGRect(engineRectArray: rect) }

    enum CodingKeys: String, CodingKey {
        case page, rect, type, source, name
        case nameHint = "name_hint"
    }
}

/// Everything `add-field` accepts.
struct PDFFormFieldSpec {
    var kind: PDFFormFieldKind
    var name: String?
    var page: Int
    var rect: CGRect
    var options: [String] = []
    var value: String?
    var fontSize: Double = 0
    var required = false
    var readonly = false
    var tooltip: String?
    var maxLen = 0
    /// Radio only: join (or create) this group instead of making a new one.
    var group: String?
}

/// Changes for `update-field`; nil means "leave as is".
struct PDFFormFieldChanges: Equatable {
    var newName: String?
    var value: String?
    var options: [String]?
    var rect: CGRect?
    var fontSize: Double?
    var required: Bool?
    var readonly: Bool?
    var tooltip: String?
    var maxLen: Int?

    var isEmpty: Bool { self == PDFFormFieldChanges() }
}

struct PDFAddFormFieldReply: Decodable {
    let name: String
    let renamed: Bool
    let type: String
    let options: [String]?
}

struct PDFFillFormFieldsReply: Decodable {
    let filled: [String]
    let missing: [String]
    let invalid: [String: String]
}

struct PDFDetectFormFieldsReply: Decodable {
    let suggestions: [PDFFormFieldSuggestion]
    let count: Int
    let created: [String]?
}

fileprivate extension CGRect {
    /// Rect from an engine `[x0, y0, x1, y1]` array.
    init(engineRectArray values: [Double]) {
        guard values.count == 4 else {
            self = .zero
            return
        }
        self.init(
            x: min(values[0], values[2]),
            y: min(values[1], values[3]),
            width: abs(values[2] - values[0]),
            height: abs(values[3] - values[1])
        )
    }

    /// Engine `--rect` argument ("x0,y0,x1,y1").
    var engineRectArgument: String {
        func format(_ value: CGFloat) -> String { String(format: "%.2f", Double(value)) }
        return [minX, minY, maxX, maxY].map(format).joined(separator: ",")
    }
}

// MARK: - Engine commands

extension PDFEngineClient {
    private struct FieldListReply: Decodable { let fields: [PDFFormField] }

    func listFormFields(input: URL) throws -> [PDFFormField] {
        try runJSON(["list-fields", "--input", input.path], as: FieldListReply.self).fields
    }

    @discardableResult
    func addFormField(input: URL, output: URL, spec: PDFFormFieldSpec) throws -> PDFAddFormFieldReply {
        // "--flag=value" keeps user text that starts with "-" from being
        // parsed as an option.
        var args = [
            "add-field",
            "--input", input.path,
            "--output", output.path,
            "--page", String(spec.page),
            "--rect=\(spec.rect.engineRectArgument)",
            "--type", spec.kind.rawValue,
            "--font-size", String(max(0, spec.fontSize))
        ]
        if let name = spec.name?.trimmingCharacters(in: .whitespacesAndNewlines), !name.isEmpty {
            args.append("--name=\(name)")
        }
        let options = spec.options.map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        if !options.isEmpty {
            args.append("--options=\(options.joined(separator: "|"))")
        }
        if let value = spec.value, !value.isEmpty { args.append("--value=\(value)") }
        if spec.required { args.append("--required") }
        if spec.readonly { args.append("--readonly") }
        if let tooltip = spec.tooltip, !tooltip.isEmpty { args.append("--tooltip=\(tooltip)") }
        if spec.maxLen > 0 { args.append(contentsOf: ["--max-len", String(spec.maxLen)]) }
        if let group = spec.group, !group.isEmpty { args.append("--group=\(group)") }
        return try runJSON(args, as: PDFAddFormFieldReply.self)
    }

    func updateFormField(input: URL, output: URL, name: String, changes: PDFFormFieldChanges) throws {
        var args = ["update-field", "--input", input.path, "--output", output.path, "--name=\(name)"]
        if let newName = changes.newName { args.append("--new-name=\(newName)") }
        if let value = changes.value { args.append("--value=\(value)") }
        if let options = changes.options { args.append("--options=\(options.joined(separator: "|"))") }
        if let rect = changes.rect { args.append("--rect=\(rect.engineRectArgument)") }
        if let fontSize = changes.fontSize { args.append(contentsOf: ["--font-size", String(max(0, fontSize))]) }
        if let required = changes.required { args.append(required ? "--required" : "--no-required") }
        if let readonly = changes.readonly { args.append(readonly ? "--readonly" : "--no-readonly") }
        if let tooltip = changes.tooltip { args.append("--tooltip=\(tooltip)") }
        if let maxLen = changes.maxLen { args.append(contentsOf: ["--max-len", String(max(0, maxLen))]) }
        _ = try run(args)
    }

    func deleteFormField(input: URL, output: URL, name: String) throws {
        _ = try run(["delete-field", "--input", input.path, "--output", output.path, "--name=\(name)"])
    }

    @discardableResult
    func fillFormFields(input: URL, output: URL, values: [String: PDFFormFieldValue]) throws -> PDFFillFormFieldsReply {
        let json = FileManager.default.temporaryDirectory
            .appendingPathComponent("pdfstudio-fill-\(UUID().uuidString).json")
        let object = values.mapValues { $0.jsonObject }
        let data = try JSONSerialization.data(withJSONObject: object, options: [])
        try data.write(to: json, options: .atomic)
        defer { try? FileManager.default.removeItem(at: json) }
        return try runJSON(
            ["fill-fields", "--input", input.path, "--output", output.path, "--json", json.path],
            as: PDFFillFormFieldsReply.self
        )
    }

    func detectFormFields(input: URL, pages: String?) throws -> [PDFFormFieldSuggestion] {
        var args = ["detect-fields", "--input", input.path]
        if let pages, !pages.isEmpty { args.append(contentsOf: ["--pages", pages]) }
        return try runJSON(args, as: PDFDetectFormFieldsReply.self).suggestions
    }

    @discardableResult
    func applyDetectedFormFields(input: URL, output: URL, pages: String?) throws -> PDFDetectFormFieldsReply {
        var args = ["detect-fields", "--input", input.path, "--apply", "--output", output.path]
        if let pages, !pages.isEmpty { args.append(contentsOf: ["--pages", pages]) }
        return try runJSON(args, as: PDFDetectFormFieldsReply.self)
    }
}
