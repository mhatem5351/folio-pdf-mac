import Foundation

struct EngineCommandResult {
    let stdout: String
    let stderr: String
}

enum PDFEngineError: LocalizedError {
    case missingEngine(String)
    case commandFailed(command: String, status: Int32, stdout: String, stderr: String)
    case invalidOutput(String)

    var errorDescription: String? {
        switch self {
        case .missingEngine(let message):
            return message
        case .commandFailed(let command, let status, let stdout, let stderr):
            let details = [stdout, stderr].filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }.joined(separator: "\n")
            return "\(command) failed with status \(status)\n\(details)"
        case .invalidOutput(let message):
            return message
        }
    }
}

final class PDFEngineClient {
    let projectRoot: URL
    private let pythonURL: URL
    private let scriptURL: URL

    init() {
        let bundle = Bundle.main
        let infoRoot = bundle.object(forInfoDictionaryKey: "FolioProjectRoot") as? String
        let root = infoRoot.flatMap { URL(fileURLWithPath: $0) } ?? Self.findProjectRoot()
        projectRoot = root

        let infoScript = bundle.object(forInfoDictionaryKey: "FolioEngineScriptPath") as? String

        pythonURL = Self.enginePythonURL
        scriptURL = infoScript.flatMap { URL(fileURLWithPath: $0) }
            ?? bundle.url(forResource: "pdf_engine", withExtension: "py")
            ?? root.appendingPathComponent("Engine/pdf_engine.py")
    }

    func health() throws -> EngineHealth {
        let output = try run(["health"]).stdout
        let data = Data(output.utf8)
        do {
            return try JSONDecoder().decode(EngineHealth.self, from: data)
        } catch {
            throw PDFEngineError.invalidOutput("Could not decode engine health: \(error.localizedDescription)\n\(output)")
        }
    }

    func metadata(input: URL) throws -> String {
        try run(["metadata", "--input", input.path]).stdout
    }

    func merge(inputs: [URL], output: URL) throws -> EngineCommandResult {
        var args = ["merge", "--output", output.path]
        for input in inputs {
            args.append(contentsOf: ["--input", input.path])
        }
        return try run(args)
    }

    func mergePages(items: [MergePageItem], output: URL) throws -> EngineCommandResult {
        var args = ["merge-pages", "--output", output.path]
        for item in items {
            args.append(contentsOf: [
                "--page-item",
                "\(item.sourceURL.path)::\(item.pageNumber)"
            ])
        }
        return try run(args)
    }

    func split(input: URL, outputDirectory: URL, pages: String?) throws -> EngineCommandResult {
        var args = [
            "split",
            "--input", input.path,
            "--output-dir", outputDirectory.path
        ]
        if let pages, !pages.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            args.append(contentsOf: ["--pages", pages])
        }
        return try run(args)
    }

    func extractPages(input: URL, output: URL, pages: String) throws -> EngineCommandResult {
        try run([
            "extract-pages",
            "--input", input.path,
            "--output", output.path,
            "--pages", pages
        ])
    }

    func deletePages(input: URL, output: URL, pages: String) throws -> EngineCommandResult {
        try run([
            "delete-pages",
            "--input", input.path,
            "--output", output.path,
            "--pages", pages
        ])
    }

    func rotatePages(input: URL, output: URL, pages: String?, degrees: Int) throws -> EngineCommandResult {
        var args = [
            "rotate-pages",
            "--input", input.path,
            "--output", output.path,
            "--degrees", String(degrees)
        ]
        if let pages, !pages.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            args.append(contentsOf: ["--pages", pages])
        }
        return try run(args)
    }

    func cropPages(input: URL, output: URL, pages: String?, left: Double, top: Double, right: Double, bottom: Double) throws -> EngineCommandResult {
        var args = [
            "crop-pages",
            "--input", input.path,
            "--output", output.path,
            "--left", String(left),
            "--top", String(top),
            "--right", String(right),
            "--bottom", String(bottom)
        ]
        if let pages, !pages.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            args.append(contentsOf: ["--pages", pages])
        }
        return try run(args)
    }

    struct EngineTextBlock: Decodable {
        let ok: Bool
        let found: Bool
        let rect: [Double]?
        let text: String?
        let fontname: String?
        let fontsize: Double?
        let color: String?
        let bold: Bool?
        let italic: Bool?
        let line_count: Int?
        let line_height: Double?
    }

    struct EnginePageBlocks: Decodable {
        let ok: Bool
        let blocks: [EngineBlockPayload]
    }

    struct EngineBlockPayload: Decodable {
        let rect: [Double]
        let text: String
        let fontname: String
        let fontsize: Double
        let color: String
        let bold: Bool
        let italic: Bool
        let line_count: Int
        let line_height: Double
    }

    func pageBlocks(input: URL, page: Int) throws -> [EngineBlockPayload] {
        let output = try run([
            "page-blocks",
            "--input", input.path,
            "--page", String(page)
        ]).stdout
        do {
            return try JSONDecoder().decode(EnginePageBlocks.self, from: Data(output.utf8)).blocks
        } catch {
            throw PDFEngineError.invalidOutput("Could not decode page-blocks result: \(error.localizedDescription)\n\(output)")
        }
    }

    func blockAt(input: URL, page: Int, x: Double, y: Double) throws -> EngineTextBlock {
        let output = try run([
            "block-at",
            "--input", input.path,
            "--page", String(page),
            "--x", String(x),
            "--y", String(y)
        ]).stdout
        do {
            return try JSONDecoder().decode(EngineTextBlock.self, from: Data(output.utf8))
        } catch {
            throw PDFEngineError.invalidOutput("Could not decode block-at result: \(error.localizedDescription)\n\(output)")
        }
    }

    /// The engine's "reflow text below" outcome for one replace-block.
    /// `applied == false` carries a `reason` ("overflow", "noop",
    /// "spans_columns", "rotated_page", "blocked", …).
    struct ReplaceBlockReflow: Decodable, Equatable {
        let applied: Bool
        let reason: String?
        /// How far the content below moved (points; positive = down).
        let delta: Double?
        let moved: Int?

        /// Skipped because the content below would have run off the page.
        var skippedForOverflow: Bool { !applied && reason == "overflow" }
    }

    struct ReplaceBlockResult {
        let command: EngineCommandResult
        /// nil when `--reflow` was not passed (or the reply had no `reflow`).
        let reflow: ReplaceBlockReflow?
    }

    private struct ReplaceBlockReply: Decodable {
        let reflow: ReplaceBlockReflow?
    }

    @discardableResult
    func replaceBlock(
        input: URL,
        output: URL,
        page: Int,
        rect: String,
        text: String,
        fontSize: Double = 0,
        fontFamily: String? = nil,
        colorHex: String? = nil,
        bold: Bool = false,
        italic: Bool = false,
        underline: Bool = false,
        alignment: String = "auto",
        background: String? = nil,
        lineHeight: Double = 0,
        trackOriginal: String? = nil,
        originalText: String? = nil,
        colorRunsJSON: String? = nil,
        /// Box the replacement text is laid into when the user resized the
        /// editor frame (engine coords, "x0,y0,x1,y1"). Erasing still follows
        /// `rect`. Absent = render into `rect`, exactly as before.
        renderRect: String? = nil,
        /// Move the content below the edited paragraph (same column) up or
        /// down so it fits the paragraph's new height.
        reflow: Bool = false
    ) throws -> ReplaceBlockResult {
        var args = [
            "replace-block",
            "--input", input.path,
            "--output", output.path,
            "--page", String(page),
            "--rect", rect,
            "--text", text
        ]
        if fontSize > 0 {
            args.append(contentsOf: ["--font-size", String(fontSize)])
        }
        if let fontFamily, !fontFamily.isEmpty {
            args.append(contentsOf: ["--font", fontFamily])
        }
        if let colorHex, !colorHex.isEmpty {
            args.append(contentsOf: ["--color", colorHex])
        }
        if bold {
            args.append("--bold")
        }
        if italic {
            args.append("--italic")
        }
        if underline {
            args.append("--underline")
        }
        if alignment != "auto" {
            args.append(contentsOf: ["--align", alignment])
        }
        if let background, !background.isEmpty {
            args.append(contentsOf: ["--background", background])
        }
        if lineHeight > 0 {
            args.append(contentsOf: ["--line-height", String(lineHeight)])
        }
        if let trackOriginal, !trackOriginal.isEmpty {
            args.append(contentsOf: ["--track-original", trackOriginal])
        }
        if let originalText, !originalText.isEmpty {
            args.append(contentsOf: ["--original-text", originalText])
        }
        if let colorRunsJSON, !colorRunsJSON.isEmpty {
            args.append(contentsOf: ["--color-runs", colorRunsJSON])
        }
        if let renderRect, !renderRect.isEmpty {
            args.append(contentsOf: ["--render-rect", renderRect])
        }
        if reflow {
            args.append("--reflow")
        }
        let result = try run(args)
        // The edit itself already succeeded; an unreadable reflow report must
        // not turn it into a failure, so decoding is best-effort.
        let reply = try? JSONDecoder().decode(ReplaceBlockReply.self, from: Data(result.stdout.utf8))
        return ReplaceBlockResult(command: result, reflow: reply?.reflow)
    }

    func blockBackground(input: URL, output: URL, page: Int, rect: String, colorHex: String) throws -> EngineCommandResult {
        try run([
            "block-background",
            "--input", input.path,
            "--output", output.path,
            "--page", String(page),
            "--rect", rect,
            "--color", colorHex
        ])
    }

    func setPassword(input: URL, output: URL, password: String) throws -> EngineCommandResult {
        try run([
            "set-password",
            "--input", input.path,
            "--output", output.path,
            "--password", password
        ])
    }

    func addSymbol(input: URL, output: URL, page: Int, kind: String, x: Double, y: Double, size: Double = 16) throws -> EngineCommandResult {
        try run([
            "add-symbol",
            "--input", input.path,
            "--output", output.path,
            "--page", String(page),
            "--kind", kind,
            "--x", String(x),
            "--y", String(y),
            "--size", String(size)
        ])
    }

    struct CompressResult: Decodable {
        let ok: Bool
        let before_bytes: Int
        let after_bytes: Int
    }

    func compress(input: URL, output: URL, quality: String) throws -> CompressResult {
        let stdout = try run([
            "compress",
            "--input", input.path,
            "--output", output.path,
            "--quality", quality
        ]).stdout
        do {
            return try JSONDecoder().decode(CompressResult.self, from: Data(stdout.utf8))
        } catch {
            throw PDFEngineError.invalidOutput("Could not decode compress result: \(error.localizedDescription)\n\(stdout)")
        }
    }

    func addPageNumbers(input: URL, output: URL, position: String, format: String, start: Int, fontSize: Double = 11) throws -> EngineCommandResult {
        try run([
            "add-page-numbers",
            "--input", input.path,
            "--output", output.path,
            "--position", position,
            "--number-format", format,
            "--start", String(start),
            "--font-size", String(fontSize)
        ])
    }

    func resizePages(input: URL, output: URL, width: Double, height: Double) throws -> EngineCommandResult {
        try run([
            "resize-pages",
            "--input", input.path,
            "--output", output.path,
            "--width", String(width),
            "--height", String(height)
        ])
    }

    func redline(input: URL, output: URL, page: Int, kind: String, rects: String? = nil, x: Double = 0, y: Double = 0, note: String? = nil) throws -> EngineCommandResult {
        var args = [
            "redline",
            "--input", input.path,
            "--output", output.path,
            "--page", String(page),
            "--kind", kind
        ]
        if let rects, !rects.isEmpty {
            args.append(contentsOf: ["--rects", rects])
        }
        if x != 0 || y != 0 {
            args.append(contentsOf: ["--x", String(x), "--y", String(y)])
        }
        if let note, !note.isEmpty {
            args.append(contentsOf: ["--note", note])
        }
        return try run(args)
    }

    func moveBlock(
        input: URL,
        output: URL,
        page: Int,
        rect: String,
        text: String,
        destX: Double,
        destY: Double,
        fontSize: Double = 0,
        fontFamily: String? = nil,
        colorHex: String? = nil,
        bold: Bool = false,
        italic: Bool = false,
        underline: Bool = false,
        alignment: String = "auto",
        lineHeight: Double = 0,
        originalText: String? = nil
    ) throws -> EngineCommandResult {
        var args = [
            "move-block",
            "--input", input.path,
            "--output", output.path,
            "--page", String(page),
            "--rect", rect,
            "--text", text,
            "--dest-x", String(destX),
            "--dest-y", String(destY)
        ]
        if fontSize > 0 {
            args.append(contentsOf: ["--font-size", String(fontSize)])
        }
        if let fontFamily, !fontFamily.isEmpty {
            args.append(contentsOf: ["--font", fontFamily])
        }
        if let colorHex, !colorHex.isEmpty {
            args.append(contentsOf: ["--color", colorHex])
        }
        if bold {
            args.append("--bold")
        }
        if italic {
            args.append("--italic")
        }
        if underline {
            args.append("--underline")
        }
        if alignment != "auto" {
            args.append(contentsOf: ["--align", alignment])
        }
        if lineHeight > 0 {
            args.append(contentsOf: ["--line-height", String(lineHeight)])
        }
        if let originalText, !originalText.isEmpty {
            args.append(contentsOf: ["--original-text", originalText])
        }
        return try run(args)
    }

    func replaceText(input: URL, output: URL, find: String, replacement: String, page: Int? = nil, rect: String? = nil) throws -> EngineCommandResult {
        var args = [
            "replace-text",
            "--input", input.path,
            "--output", output.path,
            "--find", find,
            "--replace", replacement,
            "--auto-size",
            "--match-style"
        ]
        if let page {
            args.append(contentsOf: ["--page", String(page)])
        }
        if let rect, !rect.isEmpty {
            args.append(contentsOf: ["--rect", rect])
        }
        return try run(args)
    }

    func redactText(input: URL, output: URL, find: String, label: String, pages: String?, page: Int? = nil, rect: String? = nil) throws -> EngineCommandResult {
        var args = [
            "redact-text",
            "--input", input.path,
            "--output", output.path,
            "--find", find,
            "--label", label
        ]
        if let pages, !pages.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            args.append(contentsOf: ["--pages", pages])
        }
        if let page {
            args.append(contentsOf: ["--page", String(page)])
        }
        if let rect, !rect.isEmpty {
            args.append(contentsOf: ["--rect", rect])
        }
        return try run(args)
    }

    func addText(
        input: URL,
        output: URL,
        page: Int,
        x: Double,
        y: Double,
        text: String,
        fontSize: Double,
        fontName: String = "Helvetica",
        colorHex: String = "#000000",
        bold: Bool = false,
        italic: Bool = false,
        underline: Bool = false,
        alignment: String = "auto"
    ) throws -> EngineCommandResult {
        var args = [
            "add-text",
            "--input", input.path,
            "--output", output.path,
            "--page", String(page),
            "--x", String(x),
            "--y", String(y),
            "--font-size", String(fontSize),
            "--text", text,
            "--font", fontName,
            "--color", colorHex
        ]
        if bold {
            args.append("--bold")
        }
        if italic {
            args.append("--italic")
        }
        if underline {
            args.append("--underline")
        }
        if alignment != "auto" {
            args.append(contentsOf: ["--align", alignment])
        }
        return try run(args)
    }

    func annotateText(input: URL, output: URL, kind: AnnotationKind, find: String, note: String, pages: String?) throws -> EngineCommandResult {
        var args = [
            "annotate-text",
            "--input", input.path,
            "--output", output.path,
            "--kind", kind.rawValue,
            "--find", find
        ]
        if !note.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            args.append(contentsOf: ["--note", note])
        }
        if let pages, !pages.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            args.append(contentsOf: ["--pages", pages])
        }
        return try run(args)
    }

    func addNote(input: URL, output: URL, page: Int, x: Double, y: Double, text: String) throws -> EngineCommandResult {
        try run([
            "add-note",
            "--input", input.path,
            "--output", output.path,
            "--page", String(page),
            "--x", String(x),
            "--y", String(y),
            "--text", text
        ])
    }

    func addSignature(input: URL, output: URL, page: Int, x: Double, y: Double, width: Double, height: Double, text: String, fontSize: Double) throws -> EngineCommandResult {
        try run([
            "add-signature",
            "--input", input.path,
            "--output", output.path,
            "--page", String(page),
            "--x", String(x),
            "--y", String(y),
            "--width", String(width),
            "--height", String(height),
            "--text", text,
            "--font-size", String(fontSize)
        ])
    }

    func addImage(input: URL, output: URL, image: URL, page: Int, x: Double, y: Double, width: Double, height: Double) throws -> EngineCommandResult {
        try run([
            "add-image",
            "--input", input.path,
            "--output", output.path,
            "--image", image.path,
            "--page", String(page),
            "--x", String(x),
            "--y", String(y),
            "--width", String(width),
            "--height", String(height)
        ])
    }

    func pasteRegion(
        input: URL,
        source: URL,
        output: URL,
        sourcePage: Int,
        sourceRect: String,
        destinationPage: Int,
        destinationX: Double,
        destinationY: Double,
        eraseSource: Bool
    ) throws -> EngineCommandResult {
        var args = [
            "paste-region",
            "--input", input.path,
            "--source", source.path,
            "--output", output.path,
            "--source-page", String(sourcePage),
            "--source-rect", sourceRect,
            "--destination-page", String(destinationPage),
            "--destination-x", String(destinationX),
            "--destination-y", String(destinationY)
        ]
        if eraseSource {
            args.append("--erase-source")
        }
        return try run(args)
    }

    func addLink(input: URL, output: URL, page: Int, x: Double, y: Double, width: Double, height: Double, url: String) throws -> EngineCommandResult {
        try run([
            "add-link",
            "--input", input.path,
            "--output", output.path,
            "--page", String(page),
            "--x", String(x),
            "--y", String(y),
            "--width", String(width),
            "--height", String(height),
            "--url", url
        ])
    }

    func ocr(input: URL, output: URL, language: String, force: Bool, page: Int?) throws -> EngineCommandResult {
        var args = [
            "ocr",
            "--input", input.path,
            "--output", output.path,
            "--language", language
        ]
        if force {
            args.append("--force")
        }
        if let page {
            args.append(contentsOf: ["--page", String(page)])
        }
        return try run(args)
    }

    func enhanceScan(input: URL, output: URL, grayscale: Bool, denoise: Bool, contrast: Double, sharpness: Double) throws -> EngineCommandResult {
        var args = [
            "enhance-scan",
            "--input", input.path,
            "--output", output.path,
            "--contrast", String(contrast),
            "--sharpness", String(sharpness)
        ]
        if grayscale {
            args.append("--grayscale")
        }
        if denoise {
            args.append("--denoise")
        }
        return try run(args)
    }

    func exportImages(input: URL, outputDirectory: URL, format: ImageExportFormat, dpi: Int, name: String? = nil) throws -> EngineCommandResult {
        var args = [
            "export-images",
            "--input", input.path,
            "--output-dir", outputDirectory.path,
            "--format", format.rawValue,
            "--dpi", String(dpi)
        ]
        if let name { args += ["--name", name] }
        return try run(args)
    }

    func exportText(input: URL, output: URL, format: TextExportFormat) throws -> EngineCommandResult {
        try run([
            "export-text",
            "--input", input.path,
            "--output", output.path,
            "--format", format.rawValue
        ])
    }

    func exportDOCX(input: URL, output: URL) throws -> EngineCommandResult {
        try run([
            "export-docx",
            "--input", input.path,
            "--output", output.path
        ])
    }

    func exportXLSX(input: URL, output: URL) throws -> EngineCommandResult {
        try run([
            "export-xlsx",
            "--input", input.path,
            "--output", output.path
        ])
    }

    func exportPPTX(input: URL, output: URL) throws -> EngineCommandResult {
        try run([
            "export-pptx",
            "--input", input.path,
            "--output", output.path
        ])
    }

    func imagesToPDF(inputs: [URL], output: URL) throws -> EngineCommandResult {
        var args = ["images-to-pdf", "--output", output.path]
        for input in inputs {
            args.append(contentsOf: ["--input", input.path])
        }
        return try run(args)
    }

    func run(_ args: [String]) throws -> EngineCommandResult {
        guard FileManager.default.fileExists(atPath: pythonURL.path) else {
            throw PDFEngineError.missingEngine("Missing engine Python at \(pythonURL.path). Run script/bootstrap_engine.sh.")
        }
        guard FileManager.default.fileExists(atPath: scriptURL.path) else {
            throw PDFEngineError.missingEngine("Missing PDF engine script at \(scriptURL.path).")
        }

        let process = Process()
        process.executableURL = pythonURL
        // Text extracted from PDFs can contain embedded NULs (obfuscated
        // fonts); Process throws an uncatchable ObjC exception on such
        // arguments, crashing the app. Strip them at the boundary.
        let sanitized = args.map { $0.replacingOccurrences(of: "\0", with: "") }
        process.arguments = [scriptURL.path] + sanitized
        Self.logForQA(sanitized)
        // Run from a neutral, never-TCC-protected directory. Pointing cwd at
        // a Documents/Desktop path makes the subprocess hang on macOS
        // folder-permission checks whenever the app is re-signed.
        let neutralCWD = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/FolioPDF", isDirectory: true)
        try? FileManager.default.createDirectory(at: neutralCWD, withIntermediateDirectories: true)
        process.currentDirectoryURL = neutralCWD

        var environment = ProcessInfo.processInfo.environment
        let existingPath = environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        environment["PATH"] = [
            pythonURL.deletingLastPathComponent().path,
            "/opt/homebrew/bin",
            "/usr/local/bin",
            existingPath
        ].joined(separator: ":")
        if Self.bundledPythonURL != nil {
            // The bundled Python is signed and read-only: no stray bytecode
            // writes into the app, and no user site-packages leaking in.
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PYTHONNOUSERSITE"] = "1"
        }
        let fontConfig = "/opt/homebrew/etc/fonts/fonts.conf"
        if FileManager.default.fileExists(atPath: fontConfig) {
            environment["FONTCONFIG_FILE"] = fontConfig
        }
        process.environment = environment

        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr

        try process.run()
        // Drain both pipes while the engine runs: waiting first deadlocks as
        // soon as a reply outgrows the pipe buffer (64 KB, e.g. page-blocks
        // on a long document) — the engine blocks writing, we block waiting.
        var errData = Data()
        let errDone = DispatchSemaphore(value: 0)
        DispatchQueue.global(qos: .utility).async {
            errData = stderr.fileHandleForReading.readDataToEndOfFile()
            errDone.signal()
        }
        let outData = stdout.fileHandleForReading.readDataToEndOfFile()
        errDone.wait()
        process.waitUntilExit()

        let out = String(data: outData, encoding: .utf8) ?? ""
        let err = String(data: errData, encoding: .utf8) ?? ""
        Self.logForQA(["=> status \(process.terminationStatus)", String(out.suffix(400)), String(err.suffix(600))])

        guard process.terminationStatus == 0 else {
            throw PDFEngineError.commandFailed(
                command: ([scriptURL.lastPathComponent] + args).joined(separator: " "),
                status: process.terminationStatus,
                stdout: out,
                stderr: err
            )
        }

        return EngineCommandResult(stdout: out, stderr: err)
    }

    /// QA builds (FOLIO_QA=1 / FolioQA default) append every engine
    /// invocation as one JSON line to ~/Library/Logs/FolioPDF/engine.log so a
    /// UI run can be replayed exactly from the command line.
    private static func logForQA(_ args: [String]) {
        guard DebugSnapshot.isEnabled,
              let data = try? JSONSerialization.data(withJSONObject: args),
              var line = String(data: data, encoding: .utf8) else { return }
        line += "\n"
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/FolioPDF", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("engine.log")
        if let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile()
            handle.write(Data(line.utf8))
            try? handle.close()
        } else {
            try? Data(line.utf8).write(to: url)
        }
    }

    /// The Python that runs the engine: a dev build's configured venv, else
    /// the self-contained Python a release bundles in Resources/python, else
    /// the default venv (source builds that skipped the Info.plist key).
    static var enginePythonURL: URL {
        let bundle = Bundle.main
        if let configured = bundle.object(forInfoDictionaryKey: "FolioEnginePythonPath") as? String {
            return URL(fileURLWithPath: configured)
        }
        if let bundled = bundledPythonURL {
            return bundled
        }
        return defaultVenvURL.appendingPathComponent("bin/python3")
    }

    /// Release builds ship a relocatable CPython with every engine package
    /// preinstalled, so nothing is downloaded on first launch.
    static var bundledPythonURL: URL? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let python = resources.appendingPathComponent("python/bin/python3")
        return FileManager.default.isExecutableFile(atPath: python.path) ? python : nil
    }

    // Venv lives outside iCloud-synced folders; iCloud eviction makes imports hang.
    static var defaultVenvURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/FolioPDF/engine-venv")
    }

    private static func findProjectRoot() -> URL {
        let bundleURL = Bundle.main.bundleURL
        let candidates = [
            bundleURL.deletingLastPathComponent().deletingLastPathComponent(),
            URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        ]

        for candidate in candidates {
            let script = candidate.appendingPathComponent("Engine/pdf_engine.py")
            if FileManager.default.fileExists(atPath: script.path) {
                return candidate
            }
        }

        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    }
}
