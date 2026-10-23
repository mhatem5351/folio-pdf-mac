import AppKit
import PDFKit

/// What `flatten` burns into the page content.
enum FlattenTarget: String, CaseIterable, Identifiable {
    case annotations
    case forms
    case all

    var id: String { rawValue }

    var title: String {
        switch self {
        case .annotations: "Annotations"
        case .forms: "Form fields"
        case .all: "Both"
        }
    }
}

/// Document & Security tools: passwords, flattening, properties/privacy and
/// the outline. Ops that change the open document go through runEngineOp
/// (one undoable version each); reads run in the background and call back on
/// the main queue.
extension AppStore {
    // MARK: Security

    /// Write a password-protected copy to a location the user picks. The
    /// working document stays unencrypted so every other tool keeps working.
    func protectDocument(
        ownerPassword: String,
        userPassword: String?,
        allow: Set<PDFPermission>,
        encryption: PDFEncryptionMethod
    ) {
        guard let input = currentDocumentURL, let session else {
            toast("Open a PDF first")
            return
        }
        let original = session.originalURL
        // Let the tool sheet close before the save panel runs modally.
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            let panel = NSSavePanel()
            panel.allowedContentTypes = [.pdf]
            panel.directoryURL = original.deletingLastPathComponent()
            panel.nameFieldStringValue = original.deletingPathExtension().lastPathComponent + " (protected).pdf"
            panel.message = "Choose where to save the password-protected copy"
            panel.prompt = "Protect"
            guard panel.runModal() == .OK, let destination = panel.url else { return }
            self.runEngineJob(title: "Protecting PDF", work: { [engine] in
                // Write next to the destination first so a failure never
                // leaves a half-written file in place of an existing one.
                let staging = destination.deletingLastPathComponent()
                    .appendingPathComponent(".pdfstudio-protect-\(UUID().uuidString).pdf")
                defer { try? FileManager.default.removeItem(at: staging) }
                try engine.protect(
                    input: input,
                    output: staging,
                    ownerPassword: ownerPassword,
                    userPassword: userPassword,
                    allow: allow,
                    encryption: encryption
                )
                if FileManager.default.fileExists(atPath: destination.path) {
                    _ = try FileManager.default.replaceItemAt(destination, withItemAt: staging)
                } else {
                    try FileManager.default.moveItem(at: staging, to: destination)
                }
                return destination
            }, onSuccess: { [weak self] url in
                self?.toast("Saved protected copy \(url.lastPathComponent)")
                self?.reveal(url)
            })
        }
    }

    /// Remove encryption from the open document as a new (undoable) version.
    func unlockDocument(password: String, onDone: (() -> Void)? = nil) {
        runEngineOp(title: "Password removed", onDone: onDone) { [engine] input, output in
            try engine.unlock(input: input, output: output, password: password)
        }
    }

    /// Encryption facts for the open document.
    func fetchSecurityInfo(password: String? = nil, completion: @escaping (Result<PDFSecurityInfo, Error>) -> Void) {
        guard let input = currentDocumentURL else {
            completion(.failure(EngineMessageError(message: "Open a PDF first")))
            return
        }
        background({ [engine] in try engine.inspectSecurity(input: input, password: password) }, completion: completion)
    }

    // MARK: Flatten

    func flattenDocument(_ target: FlattenTarget, onDone: (() -> Void)? = nil) {
        let counts = ResultBox<(annotations: Int, fields: Int)>()
        runEngineOp(title: "Flattened", onDone: { [weak self] in
            if let value = counts.value {
                var parts: [String] = []
                if value.annotations > 0 { parts.append(Self.plural(value.annotations, "annotation")) }
                if value.fields > 0 { parts.append(Self.plural(value.fields, "form field")) }
                if !parts.isEmpty { self?.toast("Flattened " + parts.joined(separator: " and ")) }
            }
            onDone?()
        }) { [engine] input, output in
            counts.value = try engine.flatten(input: input, output: output, what: target.rawValue)
        }
    }

    // MARK: Properties & privacy

    func fetchDocumentProperties(completion: @escaping (Result<PDFDocumentProperties, Error>) -> Void) {
        guard let input = currentDocumentURL else {
            completion(.failure(EngineMessageError(message: "Open a PDF first")))
            return
        }
        background({ [engine] in try engine.documentProperties(input: input) }, completion: completion)
    }

    /// Strip the selected hidden data, then write new properties, as one
    /// undoable version. Pass `fields: nil` to leave the properties alone.
    func applyDocumentProperties(fields: PDFMetadataFields?, remove: Set<PDFHiddenData>, onDone: (() -> Void)? = nil) {
        guard fields != nil || !remove.isEmpty else { return }
        let report = ResultBox<PDFSanitizeReport>()
        let title = remove.isEmpty ? "Properties updated" : "Hidden data removed"
        runEngineOp(title: title, onDone: { [weak self] in
            if let removed = report.value {
                self?.toast(removed.total > 0
                            ? "Removed \(Self.plural(removed.total, "hidden item"))" + (fields == nil ? "" : " and updated properties")
                            : "No hidden data found" + (fields == nil ? "" : " — properties updated"))
            }
            onDone?()
        }) { [engine] input, output in
            var source = input
            var staging: URL?
            defer { if let staging { try? FileManager.default.removeItem(at: staging) } }
            if !remove.isEmpty {
                let target: URL
                if fields == nil {
                    target = output
                } else {
                    let temp = output.deletingLastPathComponent()
                        .appendingPathComponent("sanitize-\(UUID().uuidString).pdf")
                    staging = temp
                    target = temp
                }
                report.value = try engine.sanitize(input: source, output: target, remove: remove)
                source = target
            }
            if let fields {
                try engine.setMetadata(input: source, output: output, fields: fields)
            }
        }
    }

    // MARK: Bookmarks

    func fetchOutline(completion: @escaping (Result<[PDFOutlineEntry], Error>) -> Void) {
        guard let input = currentDocumentURL else {
            completion(.failure(EngineMessageError(message: "Open a PDF first")))
            return
        }
        background({ [engine] in try engine.outline(input: input) }, completion: completion)
    }

    /// Replace the outline with `entries` as a new version.
    func saveOutline(_ entries: [PDFOutlineEntry], onDone: (() -> Void)? = nil) {
        let title = entries.isEmpty ? "Bookmarks removed" : "Saved \(Self.plural(entries.count, "bookmark"))"
        runEngineOp(title: title, onDone: onDone) { [engine] input, output in
            try engine.setOutline(input: input, output: output, entries: entries)
        }
    }

    /// Scroll the canvas to a 1-based page.
    func goToPage(_ number: Int) {
        guard let view = pdfViewProxy.pdfView,
              let doc = view.document,
              number >= 1, number <= doc.pageCount,
              let page = doc.page(at: number - 1) else { return }
        view.go(to: page)
    }

    /// A reasonable bookmark title for a page: its first meaningful line of
    /// text, else "Page N".
    func suggestedBookmarkTitle(forPage number: Int) -> String {
        let fallback = "Page \(number)"
        guard let page = document?.page(at: number - 1), let text = page.string else { return fallback }
        for rawLine in text.components(separatedBy: .newlines) {
            let line = rawLine.split(whereSeparator: \.isWhitespace).joined(separator: " ")
            guard line.count >= 3, line.rangeOfCharacter(from: .letters) != nil else { continue }
            return line.count > 60 ? String(line.prefix(57)) + "…" : line
        }
        return fallback
    }

    // MARK: Helpers

    private func background<T>(_ work: @escaping () throws -> T, completion: @escaping (Result<T, Error>) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let result = Result { try work() }
            DispatchQueue.main.async { completion(result) }
        }
    }

    private static func plural(_ count: Int, _ noun: String) -> String {
        "\(count) \(noun)\(count == 1 ? "" : "s")"
    }
}

/// Carries a value out of a background engine closure to its completion.
private final class ResultBox<Value> {
    var value: Value?
}
