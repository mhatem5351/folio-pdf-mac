import Combine
import Foundation

/// Live state of an Apple Vision OCR run, observed by OCRSheet.
final class VisionOCRRun: ObservableObject {
    enum Phase: Equatable {
        case idle
        case recognizing
        case writing
        case finished(lines: Int, pages: Int)
        case cancelled
        case nothingFound(String)
        case failed(String)
    }

    @Published fileprivate(set) var phase: Phase = .idle
    @Published fileprivate(set) var completedPages = 0
    @Published fileprivate(set) var totalPages = 0
    fileprivate var cancellation: VisionOCRCancellation?

    var isRunning: Bool { phase == .recognizing || phase == .writing }

    /// Stop recognition (only possible while pages are being recognized; the
    /// final write is quick and not interruptible).
    func cancel() {
        cancellation?.cancel()
    }

    /// Forget a finished run. A run whose engine step ended without reporting
    /// back (the engine failed and AppStore showed the error) also settles here.
    func settle(storeBusy: Bool) {
        if phase == .writing && !storeBusy {
            phase = .failed("Adding the text layer failed.")
        }
    }

    func reset() {
        guard !isRunning else { return }
        phase = .idle
        completedPages = 0
        totalPages = 0
    }
}

/// A recognition language as shown in the OCR sheet.
struct OCRLanguage: Identifiable, Hashable {
    /// Vision code ("ar-SA").
    let code: String
    /// "Arabic"
    let name: String
    /// "العربية" (empty when the same as `name`).
    let nativeName: String
    /// Tesseract traineddata code ("ara"), nil when Tesseract has none.
    let tesseractCode: String?

    var id: String { code }
}

extension AppStore {
    static let visionOCRRun = VisionOCRRun()

    var visionOCR: VisionOCRRun { Self.visionOCRRun }

    /// Default languages: Arabic + English, as available on this Mac.
    static var defaultOCRLanguageCodes: [String] {
        let available = Set(ocrLanguages.map(\.code))
        return ["ar-SA", "en-US"].filter { available.contains($0) }
    }

    /// Languages Vision recognizes on this Mac (accurate level, newest
    /// revision), Arabic and English first, then the rest by name.
    static let ocrLanguages: [OCRLanguage] = {
        let english = Locale(identifier: "en")
        let tesseract: [String: String] = [
            "ar": "ara", "ars": "ara", "en": "eng", "fr": "fra", "de": "deu", "es": "spa", "it": "ita",
            "pt": "por", "ru": "rus", "uk": "ukr", "tr": "tur", "zh-Hans": "chi_sim", "zh-Hant": "chi_tra",
            "ja": "jpn", "ko": "kor", "th": "tha", "vi": "vie", "id": "ind", "cs": "ces", "da": "dan",
            "nl": "nld", "no": "nor", "nb": "nor", "nn": "nno", "ms": "msa", "pl": "pol", "ro": "ron",
            "sv": "swe", "fi": "fin", "hi": "hin", "mr": "mar"
        ]
        let languages = VisionOCR.supportedLanguages().map { code -> OCRLanguage in
            let locale = Locale(identifier: code)
            let name = code == "ars-SA"
                ? "Arabic (Najdi)"
                : (english.localizedString(forIdentifier: code) ?? code)
            let native = locale.localizedString(forIdentifier: code) ?? ""
            let base = code.split(separator: "-").first.map(String.init) ?? code
            let tesseractCode = tesseract[code] ?? tesseract[base]
            return OCRLanguage(code: code, name: name, nativeName: native == name ? "" : native, tesseractCode: tesseractCode)
        }
        let priority = ["ar-SA": 0, "en-US": 1]
        return languages.sorted {
            let a = priority[$0.code] ?? 2
            let b = priority[$1.code] ?? 2
            return a != b ? a < b : $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
        }
    }()

    /// 0-based page indices for an engine-style page spec ("all", "3",
    /// "1-4,7"); nil when the spec is malformed or out of range.
    static func ocrPageIndices(from spec: String, pageCount: Int) -> [Int]? {
        let trimmed = spec.trimmingCharacters(in: .whitespaces).lowercased()
        if trimmed.isEmpty || trimmed == "all" {
            return Array(0..<pageCount)
        }
        var pages = Set<Int>()
        for chunk in trimmed.split(separator: ",") {
            let part = chunk.trimmingCharacters(in: .whitespaces)
            if part.isEmpty { continue }
            let bounds = part.split(separator: "-", omittingEmptySubsequences: false)
                .map { Int($0.trimmingCharacters(in: .whitespaces)) }
            switch bounds.count {
            case 1:
                guard let page = bounds[0] else { return nil }
                pages.insert(page)
            case 2:
                guard let start = bounds[0], let end = bounds[1] else { return nil }
                for page in min(start, end)...max(start, end) { pages.insert(page) }
            default:
                return nil
            }
        }
        guard !pages.isEmpty, pages.allSatisfy({ $0 >= 1 && $0 <= pageCount }) else { return nil }
        return pages.sorted().map { $0 - 1 }
    }

    /// Recognize text with Apple Vision and add it as an invisible text layer
    /// (one undoable version). Recognition runs off the main thread with
    /// progress in `visionOCR`; cancelling leaves the document untouched.
    func recognizeTextWithVision(languages: [String], pageSpec: String, skipPagesWithText: Bool) {
        guard let session, document != nil else {
            toast("Open a PDF first")
            return
        }
        guard !busy else {
            toast("Wait for \(busyTitle) to finish")
            return
        }
        guard let indices = Self.ocrPageIndices(from: pageSpec, pageCount: pageCount), !indices.isEmpty else {
            toast("Check the page range")
            return
        }

        let run = visionOCR
        let cancellation = VisionOCRCancellation()
        run.cancellation = cancellation
        run.completedPages = 0
        run.totalPages = indices.count
        run.phase = .recognizing

        let input = session.currentURL
        let revision = documentRevision
        var options = VisionOCR.Options()
        options.languages = languages
        options.pageIndices = indices
        options.skipPagesWithText = skipPagesWithText
        busy = true
        busyTitle = "Recognizing Text"

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let outcome = Result {
                try VisionOCR.recognize(url: input, options: options, cancellation: cancellation) { done, total in
                    DispatchQueue.main.async {
                        run.completedPages = done
                        run.totalPages = total
                    }
                }
            }
            DispatchQueue.main.async {
                guard let self else { return }
                self.busy = false
                run.cancellation = nil
                self.finishVisionRecognition(outcome, input: input, revision: revision, run: run)
            }
        }
    }

    private func finishVisionRecognition(_ outcome: Result<VisionOCR.Result, Error>, input: URL, revision: Int, run: VisionOCRRun) {
        let result: VisionOCR.Result
        switch outcome {
        case .failure(let error):
            if case .cancelled? = error as? VisionOCR.OCRError {
                run.phase = .cancelled
                toast("Text recognition stopped — nothing was changed")
            } else {
                run.phase = .failed(error.localizedDescription)
                report(error, while: "Recognize Text")
            }
            return
        case .success(let value):
            result = value
        }

        guard revision == documentRevision, session?.currentURL == input else {
            run.phase = .failed("The document changed while text was being recognized. Run it again.")
            return
        }
        guard result.lineCount > 0 else {
            let message = result.pages.allSatisfy(\.skipped)
                ? "Every selected page already contains text."
                : "No text was found on the selected pages."
            run.phase = .nothingFound(message)
            toast(message)
            return
        }

        let json = FileManager.default.temporaryDirectory
            .appendingPathComponent("pdfstudio-ocr-\(UUID().uuidString).json")
        do {
            try VisionOCR.writeJSON(result, to: json)
        } catch {
            run.phase = .failed(error.localizedDescription)
            report(error, while: "Recognize Text")
            return
        }

        run.phase = .writing
        let lines = result.lineCount
        let pages = result.recognizedPageCount
        runEngineOp(title: "Recognize Text", onDone: { [weak self] in
            try? FileManager.default.removeItem(at: json)
            run.phase = .finished(lines: lines, pages: pages)
            self?.toast("Recognized \(lines) line\(lines == 1 ? "" : "s") on \(pages) page\(pages == 1 ? "" : "s")")
        }) { [engine] input, output in
            defer { if !FileManager.default.fileExists(atPath: output.path) { try? FileManager.default.removeItem(at: json) } }
            try engine.addTextLayer(input: input, output: output, linesJSON: json)
        }
    }

    /// Tesseract (OCRmyPDF) through the existing engine `ocr` command. It runs
    /// on the whole document or the current page only.
    func recognizeTextWithTesseract(languages: [OCRLanguage], currentPageOnly: Bool, skipPagesWithText: Bool) {
        let codes = languages.compactMap(\.tesseractCode).reduce(into: [String]()) { result, code in
            if !result.contains(code) { result.append(code) }
        }
        ocrLanguage = codes.isEmpty ? "eng" : codes.joined(separator: "+")
        forceOCR = !skipPagesWithText
        runOCR(currentPageOnly: currentPageOnly)
    }
}

extension AppStore {
    /// A document whose first pages carry no text is probably a scan headed
    /// for OCR: load the recognition models now so it starts instantly.
    func warmUpOCRIfScanned() {
        guard let document else { return }
        let sample = (0..<min(3, document.pageCount)).compactMap { document.page(at: $0) }
        if sample.contains(where: { !VisionOCR.pageHasText($0) }) {
            VisionOCR.warmUp(languages: Self.defaultOCRLanguageCodes)
        }
    }
}
