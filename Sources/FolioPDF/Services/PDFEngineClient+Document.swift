import Foundation

// MARK: - Models

/// Permission names understood by the engine's `protect --allow`.
enum PDFPermission: String, CaseIterable, Identifiable, Codable {
    case print
    case printHQ = "print-hq"
    case copy
    case modify
    case annotate
    case forms
    case assemble
    case accessibility

    var id: String { rawValue }

    var title: String {
        switch self {
        case .print: "Printing"
        case .printHQ: "High-quality printing"
        case .copy: "Copying text and images"
        case .modify: "Editing content"
        case .annotate: "Commenting and annotating"
        case .forms: "Filling form fields"
        case .assemble: "Assembling pages"
        case .accessibility: "Screen readers"
        }
    }

    var systemImage: String {
        switch self {
        case .print: "printer"
        case .printHQ: "printer.filled.and.paper"
        case .copy: "doc.on.doc"
        case .modify: "pencil"
        case .annotate: "text.bubble"
        case .forms: "rectangle.and.pencil.and.ellipsis"
        case .assemble: "square.stack.3d.up"
        case .accessibility: "accessibility"
        }
    }
}

enum PDFEncryptionMethod: String, CaseIterable, Identifiable {
    case aes256
    case aes128

    var id: String { rawValue }

    var title: String {
        switch self {
        case .aes256: "AES-256 (recommended)"
        case .aes128: "AES-128 (older readers)"
        }
    }
}

/// Reply of `inspect-security`.
struct PDFSecurityInfo: Decodable, Equatable {
    let encrypted: Bool
    let needsPass: Bool
    let locked: Bool
    let method: String?
    let permissions: [String]
    let restricted: [String]
    let authenticatedAs: String?

    enum CodingKeys: String, CodingKey {
        case encrypted, locked, method, permissions, restricted
        case needsPass = "needs_pass"
        case authenticatedAs = "authenticated_as"
    }

    var allowed: [PDFPermission] { permissions.compactMap(PDFPermission.init(rawValue:)) }
    var denied: [PDFPermission] { restricted.compactMap(PDFPermission.init(rawValue:)) }
}

/// Reply of `get-metadata`.
struct PDFDocumentProperties: Decodable, Equatable {
    var title: String
    var author: String
    var subject: String
    var keywords: String
    var creator: String
    var producer: String
    let creationDateISO: String?
    let modDateISO: String?
    let pages: Int
    let fileSize: Int
    let pdfVersion: String?
    let encrypted: Bool
    let hasXMP: Bool
    let hasJavaScript: Bool
    let embeddedFiles: Int
    let hasForms: Bool
    let annotations: Int
    let bookmarks: Int
    let pageWidth: Double?
    let pageHeight: Double?

    enum CodingKeys: String, CodingKey {
        case title, author, subject, keywords, creator, producer, pages, encrypted, annotations, bookmarks
        case creationDateISO, modDateISO
        case fileSize = "file_size"
        case pdfVersion = "pdf_version"
        case hasXMP = "has_xmp"
        case hasJavaScript = "has_javascript"
        case embeddedFiles = "embedded_files"
        case hasForms = "has_forms"
        case pageWidth = "page_width"
        case pageHeight = "page_height"
    }

    var creationDate: Date? { creationDateISO.flatMap(Self.parseISO) }
    var modificationDate: Date? { modDateISO.flatMap(Self.parseISO) }

    private static func parseISO(_ text: String) -> Date? {
        let withZone = ISO8601DateFormatter()
        if let date = withZone.date(from: text) { return date }
        let local = DateFormatter()
        local.locale = Locale(identifier: "en_US_POSIX")
        local.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return local.date(from: text)
    }
}

/// Editable info-dictionary fields for `set-metadata`.
struct PDFMetadataFields: Equatable {
    var title = ""
    var author = ""
    var subject = ""
    var keywords = ""
    var creator = ""
    var producer = ""

    init() {}

    init(_ properties: PDFDocumentProperties) {
        title = properties.title
        author = properties.author
        subject = properties.subject
        keywords = properties.keywords
        creator = properties.creator
        producer = properties.producer
    }
}

/// Kinds of hidden data `sanitize` can remove.
enum PDFHiddenData: String, CaseIterable, Identifiable {
    case metadata
    case xmp
    case javascript
    case embeddedFiles = "embedded-files"
    case links
    case annotations
    case hiddenText = "hidden-text"
    case thumbnails

    var id: String { rawValue }
    /// Key used in the engine's `removed` report.
    var reportKey: String { rawValue.replacingOccurrences(of: "-", with: "_") }

    var title: String {
        switch self {
        case .metadata: "Document properties"
        case .xmp: "XMP metadata"
        case .javascript: "JavaScript"
        case .embeddedFiles: "Attachments"
        case .links: "Links"
        case .annotations: "Comments & markup"
        case .hiddenText: "Hidden text"
        case .thumbnails: "Page thumbnails"
        }
    }

    var detail: String {
        switch self {
        case .metadata: "Title, author, subject, keywords, app names"
        case .xmp: "Extended metadata stream (edit history, tool info)"
        case .javascript: "Scripts that run when the file opens or on actions"
        case .embeddedFiles: "Embedded files and file-attachment comments"
        case .links: "Web and in-document links"
        case .annotations: "Highlights, notes, stamps, drawings"
        case .hiddenText: "Invisible text, such as OCR layers"
        case .thumbnails: "Embedded page preview images"
        }
    }
}

/// Reply of `sanitize`.
struct PDFSanitizeReport: Decodable {
    let removed: [String: Int]
    let total: Int
}

/// One outline (bookmark) row. `x`/`y` are the destination point in engine
/// coordinates (top-left origin), carried through so saved bookmarks keep
/// their scroll target.
struct PDFOutlineEntry: Codable, Identifiable, Equatable {
    var id = UUID()
    var level: Int
    var title: String
    var page: Int
    var x: Double?
    var y: Double?

    enum CodingKeys: String, CodingKey {
        case level, title, page, x, y
    }
}

private struct OutlineReply: Decodable {
    let outline: [PDFOutlineEntry]
}

private struct FlattenReply: Decodable {
    let annotations: Int
    let fields: Int
}

/// Engine failure reduced to its human-readable message.
struct EngineMessageError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

// MARK: - Commands

extension PDFEngineClient {
    func protect(
        input: URL,
        output: URL,
        ownerPassword: String,
        userPassword: String?,
        allow: Set<PDFPermission>,
        encryption: PDFEncryptionMethod
    ) throws {
        var args = [
            "protect",
            "--input", input.path,
            "--output", output.path,
            "--owner-password", ownerPassword,
            "--encryption", encryption.rawValue,
            "--allow", allow.isEmpty ? "none" : PDFPermission.allCases.filter(allow.contains).map(\.rawValue).joined(separator: ",")
        ]
        if let userPassword, !userPassword.isEmpty {
            args.append(contentsOf: ["--user-password", userPassword])
        }
        _ = try runDocument(args)
    }

    func unlock(input: URL, output: URL, password: String) throws {
        _ = try runDocument(["unlock", "--input", input.path, "--output", output.path, "--password", password])
    }

    func inspectSecurity(input: URL, password: String? = nil) throws -> PDFSecurityInfo {
        var args = ["inspect-security", "--input", input.path]
        if let password, !password.isEmpty {
            args.append(contentsOf: ["--password", password])
        }
        return try runDocumentJSON(args)
    }

    /// `what`: "annotations", "forms" or "all". Returns how many annotations
    /// and form fields were flattened.
    @discardableResult
    func flatten(input: URL, output: URL, what: String) throws -> (annotations: Int, fields: Int) {
        let reply: FlattenReply = try runDocumentJSON(["flatten", "--input", input.path, "--output", output.path, "--what", what])
        return (reply.annotations, reply.fields)
    }

    func documentProperties(input: URL) throws -> PDFDocumentProperties {
        try runDocumentJSON(["get-metadata", "--input", input.path])
    }

    func setMetadata(input: URL, output: URL, fields: PDFMetadataFields) throws {
        _ = try runDocument([
            "set-metadata",
            "--input", input.path,
            "--output", output.path,
            "--title", fields.title,
            "--author", fields.author,
            "--subject", fields.subject,
            "--keywords", fields.keywords,
            "--creator", fields.creator,
            "--producer", fields.producer
        ])
    }

    func sanitize(input: URL, output: URL, remove: Set<PDFHiddenData>) throws -> PDFSanitizeReport {
        var args = ["sanitize", "--input", input.path, "--output", output.path]
        for item in PDFHiddenData.allCases where remove.contains(item) {
            args.append("--\(item.rawValue)")
        }
        return try runDocumentJSON(args)
    }

    func outline(input: URL) throws -> [PDFOutlineEntry] {
        let reply: OutlineReply = try runDocumentJSON(["get-outline", "--input", input.path])
        return reply.outline
    }

    func setOutline(input: URL, output: URL, entries: [PDFOutlineEntry]) throws {
        let jsonURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("folio-outline-\(UUID().uuidString).json")
        let data = try JSONEncoder().encode(entries)
        try data.write(to: jsonURL, options: .atomic)
        defer { try? FileManager.default.removeItem(at: jsonURL) }
        _ = try runDocument(["set-outline", "--input", input.path, "--output", output.path, "--json", jsonURL.path])
    }

    // MARK: Error shaping

    /// `run(_:)`, but an engine `{"ok": false, "message": …}` reply surfaces
    /// as just its message instead of the raw command line and JSON.
    private func runDocument(_ args: [String]) throws -> EngineCommandResult {
        do {
            return try run(args)
        } catch PDFEngineError.commandFailed(_, _, let stdout, _) {
            if let data = stdout.data(using: .utf8),
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let message = object["message"] as? String, !message.isEmpty {
                throw EngineMessageError(message: message.prefix(1).uppercased() + message.dropFirst())
            }
            throw EngineMessageError(message: "The engine could not complete \(args.first ?? "the command").")
        }
    }

    private func runDocumentJSON<T: Decodable>(_ args: [String]) throws -> T {
        let result = try runDocument(args)
        do {
            return try JSONDecoder().decode(T.self, from: Data(result.stdout.utf8))
        } catch {
            throw PDFEngineError.invalidOutput("Could not decode \(args.first ?? "engine") reply: \(error.localizedDescription)")
        }
    }
}
