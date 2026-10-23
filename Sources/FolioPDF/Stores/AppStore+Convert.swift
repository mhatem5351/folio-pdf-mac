import AppKit
import Foundation
import UniformTypeIdentifiers

// MARK: - Options

enum CompareMode: String, CaseIterable, Identifiable {
    case text, visual, both
    var id: String { rawValue }

    var title: String {
        switch self {
        case .text: "Text"
        case .visual: "Visual"
        case .both: "Text + Visual"
        }
    }

    var explanation: String {
        switch self {
        case .text: "Word-by-word differences, even when text moved to another page."
        case .visual: "Pixel differences page by page — for scans, drawings and images."
        case .both: "Text differences plus boxes around changed graphics and images."
        }
    }
}

enum ConvertPageSize: String, CaseIterable, Identifiable {
    case a4, letter, legal
    var id: String { rawValue }

    var title: String {
        switch self {
        case .a4: "A4"
        case .letter: "Letter"
        case .legal: "Legal"
        }
    }

    /// Points; matches PAGE_SIZES in Engine/ext/convert.py.
    var size: CGSize {
        switch self {
        case .a4: CGSize(width: 595.28, height: 841.89)
        case .letter: CGSize(width: 612, height: 792)
        case .legal: CGSize(width: 612, height: 1008)
        }
    }

    /// Letter/Legal for US-style locales, A4 elsewhere.
    static var localeDefault: ConvertPageSize {
        let region = Locale.current.region?.identifier ?? ""
        return ["US", "CA", "MX", "PH", "CL", "CO", "VE"].contains(region) ? .letter : .a4
    }
}

enum ConvertMargins: String, CaseIterable, Identifiable {
    case narrow, normal, wide
    var id: String { rawValue }

    var title: String {
        switch self {
        case .narrow: "Narrow"
        case .normal: "Normal"
        case .wide: "Wide"
        }
    }

    var detail: String {
        switch self {
        case .narrow: "0.5 in all round"
        case .normal: "1 in all round"
        case .wide: "1 in top & bottom, 2 in sides"
        }
    }

    /// Points; matches MARGINS in Engine/ext/convert.py.
    var insets: NSEdgeInsets {
        switch self {
        case .narrow: NSEdgeInsets(top: 36, left: 36, bottom: 36, right: 36)
        case .normal: NSEdgeInsets(top: 72, left: 72, bottom: 72, right: 72)
        case .wide: NSEdgeInsets(top: 72, left: 144, bottom: 72, right: 144)
        }
    }
}

struct ConvertOptions {
    var pageSize: ConvertPageSize = .localeDefault
    var margins: ConvertMargins = .normal
    /// One PDF with every file in order, instead of one PDF per file.
    var combine = true
    /// Folder for the results.
    var outputFolder: URL
    /// File name (without extension) of the combined PDF.
    var combinedName: String
}

/// A document picked for Compare: a file, or the open document's working copy.
struct CompareSource: Equatable {
    let url: URL
    /// Name shown in the sheet and printed in the report.
    let displayName: String
    var isOpenDocument = false
}

// MARK: - Store API

extension AppStore {
    /// Files Create PDF accepts.
    static let convertibleExtensions: Set<String> = OfficeToPDF.supportedExtensions.union([
        "txt", "text", "md", "markdown", "html", "htm", "xhtml",
        "png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "pdf"
    ])

    static var convertibleContentTypes: [UTType] {
        convertibleExtensions.compactMap { UTType(filenameExtension: $0) }
    }

    static func isConvertible(_ url: URL) -> Bool {
        convertibleExtensions.contains(url.pathExtension.lowercased())
    }

    /// The open document as a Compare source (its latest edits included).
    var openDocumentCompareSource: CompareSource? {
        guard let url = currentDocumentURL else { return nil }
        return CompareSource(url: url, displayName: documentName, isOpenDocument: true)
    }

    /// Compare two PDFs. The report is written to a temporary folder; open it
    /// with `openComparisonReport(_:)`. Errors are delivered to `completion`
    /// (shown inside the sheet) rather than through the global alert.
    func compareDocuments(
        original: CompareSource,
        revised: CompareSource,
        mode: CompareMode,
        completion: @escaping (Result<EngineCompareResult, Error>) -> Void
    ) {
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("FolioPDF-Compare", isDirectory: true)
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        let oldStem = (original.displayName as NSString).deletingPathExtension
        let newStem = (revised.displayName as NSString).deletingPathExtension
        let reportName = "Comparison – \(oldStem) vs \(newStem)".safeReportFilename + ".pdf"
        let output = folder.appendingPathComponent(reportName)
        let json = folder.appendingPathComponent("changes.json")
        var outcome: Result<EngineCompareResult, Error>?
        runEngineJob(title: "Compare Documents") { [engine] in
            do {
                try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
                let result = try engine.compare(
                    old: original.url,
                    new: revised.url,
                    output: output,
                    json: json,
                    mode: mode.rawValue,
                    oldLabel: original.displayName,
                    newLabel: revised.displayName
                )
                outcome = .success(result)
            } catch {
                outcome = .failure(error)
            }
            return output
        } onSuccess: { _ in
            completion(outcome ?? .failure(PDFEngineError.invalidOutput("Compare produced no result")))
        }
    }

    /// Open a comparison report in the canvas (asks about unsaved edits first).
    func openComparisonReport(_ result: EngineCompareResult, at reportPage: Int? = nil) {
        loadPDF(result.reportURL, checkDirty: true)
        guard let reportPage, session?.originalURL == result.reportURL else { return }
        // The canvas picks up the new document on its next update pass.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { [weak self] in
            guard let self, let view = self.pdfViewProxy.pdfView,
                  view.document === self.document,
                  let page = view.document?.page(at: max(0, reportPage - 1)) else { return }
            view.go(to: page)
        }
    }

    /// Convert office documents, text, Markdown, HTML and images to PDF, then
    /// open the (first) result. Word/RTF/ODT are laid out with AppKit; the
    /// rest goes through the engine's `convert-to-pdf`.
    func convertFilesToPDF(_ inputs: [URL], options: ConvertOptions, completion: (([URL]) -> Void)? = nil) {
        guard !inputs.isEmpty else { return }
        var produced: [URL] = []
        let layout = OfficeToPDF.Layout(pageSize: options.pageSize.size, margins: options.margins.insets)
        let title = inputs.count == 1 ? "Create PDF" : "Create PDFs"
        runEngineJob(title: title) { [engine] in
            let scratch = FileManager.default.temporaryDirectory
                .appendingPathComponent("FolioPDF-Convert", isDirectory: true)
                .appendingPathComponent(UUID().uuidString, isDirectory: true)
            try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
            try FileManager.default.createDirectory(at: options.outputFolder, withIntermediateDirectories: true)
            defer { try? FileManager.default.removeItem(at: scratch) }

            /// Office files become PDFs here; everything else the engine reads directly.
            func engineInput(for url: URL, index: Int) throws -> URL {
                guard OfficeToPDF.handles(url) else { return url }
                let pdf = scratch.appendingPathComponent("\(index)-\(url.deletingPathExtension().lastPathComponent).pdf")
                try OfficeToPDF.convert(url, to: pdf, layout: layout)
                return pdf
            }

            if options.combine || inputs.count == 1 {
                let name = inputs.count == 1 && !options.combine
                    ? inputs[0].deletingPathExtension().lastPathComponent
                    : options.combinedName
                let output = Self.uniqueURL(in: options.outputFolder, name: name)
                let sources = try inputs.enumerated().map { try engineInput(for: $1, index: $0) }
                _ = try engine.convertToPDF(
                    inputs: sources,
                    output: output,
                    pageSize: options.pageSize.rawValue,
                    margins: options.margins.rawValue,
                    title: name
                )
                produced = [output]
            } else {
                for (index, url) in inputs.enumerated() {
                    let name = url.deletingPathExtension().lastPathComponent
                    let output = Self.uniqueURL(in: options.outputFolder, name: name)
                    _ = try engine.convertToPDF(
                        inputs: [try engineInput(for: url, index: index)],
                        output: output,
                        pageSize: options.pageSize.rawValue,
                        margins: options.margins.rawValue,
                        title: name
                    )
                    produced.append(output)
                }
            }
            return produced[0]
        } onSuccess: { [weak self] first in
            guard let self else { return }
            completion?(produced)
            self.loadPDF(first, checkDirty: true)
            if produced.count > 1 {
                self.toast("Created \(produced.count) PDFs in \(options.outputFolder.lastPathComponent)")
            } else {
                self.toast("Created \(first.lastPathComponent)")
            }
        }
    }

    /// `folder/name.pdf`, or `name 2.pdf`, `name 3.pdf`… if taken.
    static func uniqueURL(in folder: URL, name: String) -> URL {
        let base = name.trimmingCharacters(in: .whitespacesAndNewlines).safeReportFilename
        let stem = base.isEmpty ? "Document" : base
        var candidate = folder.appendingPathComponent(stem + ".pdf")
        var counter = 2
        while FileManager.default.fileExists(atPath: candidate.path) {
            candidate = folder.appendingPathComponent("\(stem) \(counter).pdf")
            counter += 1
        }
        return candidate
    }
}

private extension String {
    /// Strip characters that are illegal or awkward in file names.
    var safeReportFilename: String {
        let illegal = CharacterSet(charactersIn: "/:\\?%*|\"<>").union(.newlines).union(.controlCharacters)
        return components(separatedBy: illegal).joined(separator: "-")
    }
}
