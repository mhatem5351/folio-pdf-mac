import Foundation

// MARK: - Compare

/// Reply of the engine `compare` command (Engine/ext/compare.py).
struct EngineCompareResult: Decodable {
    struct Change: Decodable, Identifiable, Hashable {
        /// "insert", "delete" or "replace".
        let type: String
        /// Inserted / replacement text, or the deleted text for deletions.
        let text: String
        /// Original wording of a replacement.
        let oldText: String?
        let pageOld: Int?
        let pageNew: Int?
        /// 1-based page of the report PDF that shows this change.
        let reportPage: Int
        // Position in the list, filled in after decoding (stable identity).
        var index: Int = 0
        var id: Int { index }

        private enum CodingKeys: String, CodingKey {
            case type, text
            case oldText = "old_text"
            case pageOld = "page_old"
            case pageNew = "page_new"
            case reportPage = "report_page"
        }
    }

    struct ChangedPage: Decodable, Hashable {
        let reportPage: Int
        let pageOld: Int?
        let pageNew: Int?
        let insertions: Int
        let deletions: Int
        let visualRegions: Int

        private enum CodingKeys: String, CodingKey {
            case insertions, deletions
            case reportPage = "report_page"
            case pageOld = "page_old"
            case pageNew = "page_new"
            case visualRegions = "visual_regions"
        }
    }

    let ok: Bool
    let output: String
    let json: String?
    let mode: String
    let pagesOld: Int
    let pagesNew: Int
    let insertions: Int
    let deletions: Int
    let wordsInserted: Int
    let wordsDeleted: Int
    let identical: Bool
    let textFound: Bool
    let changedPages: [ChangedPage]
    let summaryPages: Int
    let reportPages: Int
    let totalChanges: Int
    let truncated: Bool
    var changes: [Change]

    var reportURL: URL { URL(fileURLWithPath: output) }
    var visualRegions: Int { changedPages.reduce(0) { $0 + $1.visualRegions } }

    private enum CodingKeys: String, CodingKey {
        case ok, output, json, mode, insertions, deletions, identical, truncated, changes
        case pagesOld = "pages_old"
        case pagesNew = "pages_new"
        case wordsInserted = "words_inserted"
        case wordsDeleted = "words_deleted"
        case textFound = "text_found"
        case changedPages = "changed_pages"
        case summaryPages = "summary_pages"
        case reportPages = "report_pages"
        case totalChanges = "total_changes"
    }
}

// MARK: - Convert

/// Reply of the engine `convert-to-pdf` command (Engine/ext/convert.py).
struct EngineConvertResult: Decodable {
    struct Input: Decodable {
        let input: String
        let kind: String
        let pages: Int
    }

    let ok: Bool
    let output: String
    let pages: Int
    let bytes: Int
    let inputs: [Input]
}

extension PDFEngineClient {
    /// Side-by-side comparison report of two PDFs.
    /// - Parameters:
    ///   - mode: "text", "visual" or "both".
    ///   - oldLabel/newLabel: names printed in the report (the working copy
    ///     of an open document has an internal file name).
    func compare(
        old: URL,
        new: URL,
        output: URL,
        json: URL? = nil,
        mode: String = "text",
        oldLabel: String? = nil,
        newLabel: String? = nil,
        maxChanges: Int = 300
    ) throws -> EngineCompareResult {
        var args = [
            "compare",
            "--old", old.path,
            "--new", new.path,
            "--output", output.path,
            "--mode", mode,
            "--max-changes", String(maxChanges)
        ]
        if let json { args += ["--json", json.path] }
        if let oldLabel { args += ["--old-label", oldLabel] }
        if let newLabel { args += ["--new-label", newLabel] }
        var result = try runJSON(args, as: EngineCompareResult.self)
        for index in result.changes.indices {
            result.changes[index].index = index
        }
        return result
    }

    /// Lay out text / Markdown / HTML, place images, pass PDFs through, and
    /// write everything in order into one PDF.
    /// - Parameters:
    ///   - pageSize: "a4", "letter" or "legal".
    ///   - margins: "narrow", "normal" or "wide".
    func convertToPDF(
        inputs: [URL],
        output: URL,
        pageSize: String,
        margins: String,
        title: String? = nil
    ) throws -> EngineConvertResult {
        var args = [
            "convert-to-pdf",
            "--output", output.path,
            "--page-size", pageSize,
            "--margins", margins
        ]
        for input in inputs {
            args += ["--input", input.path]
        }
        if let title, !title.isEmpty { args += ["--title", title] }
        return try runJSON(args, as: EngineConvertResult.self)
    }

    /// Rewrite ToUnicode maps from presentation forms to base letters so
    /// Arabic copied or searched in the PDF reads as typed.
    @discardableResult
    func fixTextMap(input: URL, output: URL) throws -> EngineCommandResult {
        try run(["fix-text-map", "--input", input.path, "--output", output.path])
    }
}
