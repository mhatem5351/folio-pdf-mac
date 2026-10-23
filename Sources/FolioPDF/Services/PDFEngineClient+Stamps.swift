import Foundation

// MARK: - Options

/// Named anchor on the visible page. `tile` is watermark-only; the middle
/// row is not offered for headers, footers, or Bates numbers.
enum StampPosition: String, CaseIterable, Identifiable {
    case topLeft = "top-left"
    case topCenter = "top-center"
    case topRight = "top-right"
    case middleLeft = "middle-left"
    case center
    case middleRight = "middle-right"
    case bottomLeft = "bottom-left"
    case bottomCenter = "bottom-center"
    case bottomRight = "bottom-right"
    case tile

    var id: String { rawValue }

    /// 3x3 grid order, row by row.
    static let grid: [StampPosition] = [
        .topLeft, .topCenter, .topRight,
        .middleLeft, .center, .middleRight,
        .bottomLeft, .bottomCenter, .bottomRight
    ]

    /// Positions valid for header/footer slots and Bates numbers.
    static let edgeSlots: [StampPosition] = [.topLeft, .topCenter, .topRight, .bottomLeft, .bottomCenter, .bottomRight]

    var title: String {
        switch self {
        case .topLeft: "Top left"
        case .topCenter: "Top center"
        case .topRight: "Top right"
        case .middleLeft: "Middle left"
        case .center: "Center"
        case .middleRight: "Middle right"
        case .bottomLeft: "Bottom left"
        case .bottomCenter: "Bottom center"
        case .bottomRight: "Bottom right"
        case .tile: "Tile"
        }
    }

    /// 0 = leading/top, 0.5 = middle, 1 = trailing/bottom.
    var unitX: Double {
        switch self {
        case .topLeft, .middleLeft, .bottomLeft: 0
        case .topRight, .middleRight, .bottomRight: 1
        default: 0.5
        }
    }

    var unitY: Double {
        switch self {
        case .topLeft, .topCenter, .topRight: 0
        case .bottomLeft, .bottomCenter, .bottomRight: 1
        default: 0.5
        }
    }
}

enum WatermarkLayer: String, CaseIterable, Identifiable {
    case over, under
    var id: String { rawValue }
    var title: String { self == .over ? "Over content" : "Behind content" }
}

struct WatermarkOptions {
    enum Content {
        case text(String)
        case image(URL)
    }

    var content: Content = .text("CONFIDENTIAL")
    var fontSize: Double = 60
    var colorHex: String = "#808080"
    /// 0...1
    var opacity: Double = 0.3
    /// Degrees counter-clockwise as seen on screen.
    var rotation: Double = 45
    var position: StampPosition = .center
    var layer: WatermarkLayer = .over
    /// Image width as a fraction of the page width.
    var scale: Double = 0.5
    var pages: String = "all"
    /// helv | tiro | cour | path to a .ttf. Non-Latin text falls back to an
    /// installed font that covers it (e.g. SF Arabic) automatically.
    var font: String = "helv"
}

/// The six running-text slots, in page order.
enum HeaderFooterSlot: String, CaseIterable, Identifiable {
    case headerLeft = "header-left"
    case headerCenter = "header-center"
    case headerRight = "header-right"
    case footerLeft = "footer-left"
    case footerCenter = "footer-center"
    case footerRight = "footer-right"

    var id: String { rawValue }
    var isHeader: Bool { rawValue.hasPrefix("header") }

    var title: String {
        switch self {
        case .headerLeft, .footerLeft: "Left"
        case .headerCenter, .footerCenter: "Center"
        case .headerRight, .footerRight: "Right"
        }
    }
}

struct HeaderFooterOptions {
    var slots: [HeaderFooterSlot: String] = [:]
    var font: String = "helv"
    var fontSize: Double = 10
    var colorHex: String = "#333333"
    /// Distance from the page edge, in points.
    var margin: Double = 28
    var startNumber: Int = 1
    var pages: String = "all"
    /// strftime format for {date}.
    var dateFormat: String = "%Y-%m-%d"
    /// Value for {filename}; the working copy's name is meaningless to users.
    var filename: String?
    /// Value for {title}; defaults to the PDF title, then the file stem.
    var title: String?

    var hasContent: Bool {
        slots.values.contains { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }
}

struct BatesOptions {
    var prefix: String = ""
    var suffix: String = ""
    var start: Int = 1
    var digits: Int = 6
    var position: StampPosition = .bottomRight
    var font: String = "helv"
    var fontSize: Double = 10
    var colorHex: String = "#000000"
    var margin: Double = 24
    var pages: String = "all"

    func identifier(_ number: Int) -> String {
        let digitsText = String(number)
        let padded = String(repeating: "0", count: max(0, digits - digitsText.count)) + digitsText
        return prefix + padded + suffix
    }
}

enum SplitMode: String, CaseIterable, Identifiable {
    case ranges, every, bookmarks, size
    var id: String { rawValue }

    var title: String {
        switch self {
        case .ranges: "Page ranges"
        case .every: "Every N pages"
        case .bookmarks: "Bookmarks"
        case .size: "File size"
        }
    }

    var systemImage: String {
        switch self {
        case .ranges: "text.line.first.and.arrowtriangle.forward"
        case .every: "square.grid.3x1.below.line.grid.1x2"
        case .bookmarks: "bookmark"
        case .size: "externaldrive"
        }
    }
}

struct SplitOptions {
    var mode: SplitMode = .every
    /// "1-3, 4-6, 7-" — each comma-separated range becomes one file.
    var ranges: String = ""
    var every: Int = 1
    var maxMB: Double = 10
    /// File name stem for the parts (<stem>_part-01.pdf).
    var basename: String?
}

// MARK: - Replies

struct WatermarkResult: Decodable {
    let output: String
    let pages: Int
    let stamps: Int
    let font: String?
}

struct StampLabelsResult: Decodable {
    let output: String
    let pages: Int
    let labels: Int
}

struct BatesResult: Decodable {
    let output: String
    let pages: Int
    let first: String
    let last: String
    let nextNumber: Int

    enum CodingKeys: String, CodingKey {
        case output, pages, first, last
        case nextNumber = "next_number"
    }
}

struct SplitPart: Decodable, Identifiable {
    let path: String
    let pages: Int
    let first: Int
    let last: Int
    let bytes: Int?
    let title: String?
    let oversize: Bool?

    var id: String { path }
    var url: URL { URL(fileURLWithPath: path) }
}

struct SplitPlanPart: Decodable {
    let first: Int
    let last: Int
    let pages: Int
    let title: String?
}

struct SplitResult: Decodable {
    let outputDir: String?
    let outputs: [SplitPart]?
    let plan: [SplitPlanPart]?
    let parts: Int

    enum CodingKeys: String, CodingKey {
        case outputDir = "output_dir"
        case outputs, plan, parts
    }
}

// MARK: - Commands

extension PDFEngineClient {
    /// `--key=value` keeps values that start with "-" (e.g. "- DRAFT -") from
    /// being parsed as options by argparse.
    private static func pair(_ key: String, _ value: String) -> String {
        "--\(key)=\(value)"
    }

    private static func number(_ value: Double) -> String {
        String(format: "%.3f", value)
    }

    func watermark(input: URL, output: URL, options: WatermarkOptions) throws -> WatermarkResult {
        var args = [
            "watermark", "--input", input.path, "--output", output.path,
            Self.pair("font-size", Self.number(options.fontSize)),
            Self.pair("color", options.colorHex),
            Self.pair("opacity", Self.number(min(max(options.opacity, 0.01), 1))),
            Self.pair("rotation", Self.number(options.rotation)),
            Self.pair("position", options.position.rawValue),
            Self.pair("layer", options.layer.rawValue),
            Self.pair("scale", Self.number(options.scale)),
            Self.pair("pages", options.pages),
            Self.pair("font", options.font)
        ]
        switch options.content {
        case .text(let text): args.append(Self.pair("text", text))
        case .image(let url): args.append(Self.pair("image", url.path))
        }
        return try runJSON(args, as: WatermarkResult.self)
    }

    func headerFooter(input: URL, output: URL, options: HeaderFooterOptions) throws -> StampLabelsResult {
        var args = [
            "header-footer", "--input", input.path, "--output", output.path,
            Self.pair("font", options.font),
            Self.pair("font-size", Self.number(options.fontSize)),
            Self.pair("color", options.colorHex),
            Self.pair("margin", Self.number(options.margin)),
            Self.pair("start-number", String(options.startNumber)),
            Self.pair("pages", options.pages),
            Self.pair("date-format", options.dateFormat)
        ]
        for slot in HeaderFooterSlot.allCases {
            let text = options.slots[slot]?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if !text.isEmpty {
                args.append(Self.pair(slot.rawValue, text))
            }
        }
        if let filename = options.filename, !filename.isEmpty {
            args.append(Self.pair("filename", filename))
        }
        if let title = options.title, !title.isEmpty {
            args.append(Self.pair("title", title))
        }
        return try runJSON(args, as: StampLabelsResult.self)
    }

    func bates(input: URL, output: URL, options: BatesOptions) throws -> BatesResult {
        let args = [
            "bates", "--input", input.path, "--output", output.path,
            Self.pair("prefix", options.prefix),
            Self.pair("suffix", options.suffix),
            Self.pair("start", String(options.start)),
            Self.pair("digits", String(options.digits)),
            Self.pair("position", options.position.rawValue),
            Self.pair("font", options.font),
            Self.pair("font-size", Self.number(options.fontSize)),
            Self.pair("color", options.colorHex),
            Self.pair("margin", Self.number(options.margin)),
            Self.pair("pages", options.pages)
        ]
        return try runJSON(args, as: BatesResult.self)
    }

    /// Split into several files in `outputDirectory`. With `dryRun` nothing
    /// is written and the reply carries `plan` instead of `outputs`.
    func splitAdvanced(input: URL, outputDirectory: URL, options: SplitOptions, dryRun: Bool = false) throws -> SplitResult {
        var args = [
            "split-advanced", "--input", input.path,
            "--output-dir", outputDirectory.path,
            Self.pair("mode", options.mode.rawValue)
        ]
        switch options.mode {
        case .ranges: args.append(Self.pair("ranges", options.ranges))
        case .every: args.append(Self.pair("every", String(max(options.every, 1))))
        case .size: args.append(Self.pair("max-mb", Self.number(options.maxMB)))
        case .bookmarks: break
        }
        if let basename = options.basename, !basename.isEmpty {
            args.append(Self.pair("basename", basename))
        }
        if dryRun {
            args.append("--dry-run")
        }
        return try runJSON(args, as: SplitResult.self)
    }
}
