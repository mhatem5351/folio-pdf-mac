import CoreGraphics
import CoreText
import Foundation
import PDFKit
import Vision

/// Cooperative cancellation for a Vision OCR run. `cancel()` may be called
/// from any thread; it stops between pages and aborts the page in flight.
final class VisionOCRCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    private var inFlight: VNRequest?

    var isCancelled: Bool {
        lock.lock()
        defer { lock.unlock() }
        return cancelled
    }

    func cancel() {
        lock.lock()
        cancelled = true
        let request = inFlight
        lock.unlock()
        request?.cancel()
    }

    fileprivate func track(_ request: VNRequest?) {
        lock.lock()
        inFlight = request
        lock.unlock()
    }
}

/// On-device text recognition for PDF pages with Apple Vision.
///
/// Each requested page is rendered upright (page rotation applied, crop box
/// only) at ~300 dpi, recognized with `VNRecognizeTextRequest` (accurate
/// level, newest revision — Arabic needs revision 3+), and every line comes
/// back with its quad in ENGINE coordinates: the unrotated crop box with a
/// top-left origin, in points — the same space `page.get_text()` uses and the
/// engine's `add-text-layer` command expects.
enum VisionOCR {
    struct Options {
        /// BCP-47 codes in priority order ("ar-SA", "en-US"). Empty: detect automatically.
        var languages: [String] = []
        /// 0-based page indices; nil means every page.
        var pageIndices: [Int]?
        /// Leave pages that already have a text layer untouched.
        var skipPagesWithText = false
        var dpi: CGFloat = 300
        /// Longest rendered side, in pixels (keeps huge pages in memory budget).
        var maxPixelDimension: CGFloat = 5000
        var usesLanguageCorrection = true
        /// Lines below this confidence are dropped.
        var minimumConfidence: Float = 0.2
    }

    struct Line: Codable, Equatable {
        let text: String
        let confidence: Float
        /// Corners in engine coordinates: upper-left, upper-right, lower-left, lower-right.
        let quad: [[Double]]
        /// Axis-aligned bounds of `quad` (x0, y0, x1, y1), engine coordinates.
        let rect: [Double]
    }

    struct Page: Codable, Equatable {
        /// 1-based page number.
        let page: Int
        let width: Double
        let height: Double
        let rotation: Int
        let skipped: Bool
        let lines: [Line]
    }

    struct Result: Codable, Equatable {
        let pages: [Page]
        let languages: [String]
        let revision: Int

        var lineCount: Int { pages.reduce(0) { $0 + $1.lines.count } }
        var recognizedPageCount: Int { pages.filter { !$0.skipped && !$0.lines.isEmpty }.count }
    }

    enum OCRError: LocalizedError {
        case cannotOpen(URL)
        case pageOutOfRange(Int, Int)
        case renderFailed(Int)
        case unsupportedLanguages([String])
        case cancelled

        var errorDescription: String? {
            switch self {
            case .cannotOpen(let url): "Could not open \(url.lastPathComponent) for text recognition."
            case .pageOutOfRange(let page, let count): "Page \(page) is out of range (the document has \(count) pages)."
            case .renderFailed(let page): "Page \(page) could not be rendered for text recognition."
            case .unsupportedLanguages(let codes): "Apple Vision cannot recognize \(codes.joined(separator: ", ")) on this Mac."
            case .cancelled: "Text recognition was cancelled."
            }
        }
    }

    // MARK: Capabilities

    /// Newest `VNRecognizeTextRequest` revision this OS supports.
    static var latestRevision: Int {
        VNRecognizeTextRequest.supportedRevisions.max() ?? VNRecognizeTextRequestRevision3
    }

    /// Languages accurate recognition supports at the newest revision.
    static func supportedLanguages() -> [String] {
        let request = makeRequest(languages: [], correction: true)
        return (try? request.supportedRecognitionLanguages()) ?? []
    }

    /// Requested codes Vision supports, keeping their order. Matches by exact
    /// code first, then by language ("ar" matches "ar-SA").
    static func resolveLanguages(_ requested: [String], supported: [String]? = nil) -> (usable: [String], unsupported: [String]) {
        let available = supported ?? supportedLanguages()
        var usable: [String] = []
        var unsupported: [String] = []
        for code in requested {
            if available.contains(code) {
                if !usable.contains(code) { usable.append(code) }
                continue
            }
            let base = code.split(separator: "-").first.map(String.init) ?? code
            if let match = available.first(where: { $0 == base || $0.hasPrefix(base + "-") }) {
                if !usable.contains(match) { usable.append(match) }
            } else {
                unsupported.append(code)
            }
        }
        return (usable, unsupported)
    }

    /// True when the page already carries real, extractable text.
    static func pageHasText(_ page: PDFPage) -> Bool {
        let text = page.string ?? ""
        return text.unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) }.count >= 4
    }

    // MARK: Recognition

    /// Recognize text synchronously (call off the main thread).
    /// `progress(done, total)` fires after each page from the worker thread.
    static func recognize(
        url: URL,
        options: Options,
        cancellation: VisionOCRCancellation? = nil,
        progress: ((Int, Int) -> Void)? = nil
    ) throws -> Result {
        guard let cgDocument = CGPDFDocument(url as CFURL) else { throw OCRError.cannotOpen(url) }
        let pageCount = cgDocument.numberOfPages
        let indices = options.pageIndices ?? Array(0..<pageCount)
        if let bad = indices.first(where: { $0 < 0 || $0 >= pageCount }) {
            throw OCRError.pageOutOfRange(bad + 1, pageCount)
        }

        var languages: [String] = []
        if !options.languages.isEmpty {
            let resolved = resolveLanguages(options.languages)
            guard !resolved.usable.isEmpty else { throw OCRError.unsupportedLanguages(resolved.unsupported) }
            languages = resolved.usable
        }

        // PDFKit only for the "already has text" check; a private instance is
        // safe to use on this background thread.
        let textDocument = options.skipPagesWithText ? PDFDocument(url: url) : nil

        var pages: [Page] = []
        progress?(0, indices.count)
        for (done, index) in indices.enumerated() {
            if cancellation?.isCancelled == true { throw OCRError.cancelled }
            guard let cgPage = cgDocument.page(at: index + 1) else { throw OCRError.renderFailed(index + 1) }
            let crop = cgPage.getBoxRect(.cropBox)
            let rotation = ((Int(cgPage.rotationAngle) % 360) + 360) % 360

            if let textDocument, let page = textDocument.page(at: index), pageHasText(page) {
                pages.append(Page(page: index + 1, width: crop.width, height: crop.height,
                                  rotation: rotation, skipped: true, lines: []))
                progress?(done + 1, indices.count)
                continue
            }

            let lines = try autoreleasepool { () throws -> [Line] in
                guard let rendered = render(cgPage, options: options) else { throw OCRError.renderFailed(index + 1) }
                let request = makeRequest(languages: languages, correction: options.usesLanguageCorrection)
                cancellation?.track(request)
                defer { cancellation?.track(nil) }
                let handler = VNImageRequestHandler(cgImage: rendered.image, options: [:])
                do {
                    try handler.perform([request])
                } catch {
                    if cancellation?.isCancelled == true { throw OCRError.cancelled }
                    throw error
                }
                if cancellation?.isCancelled == true { throw OCRError.cancelled }
                let observations = request.results ?? []
                return observations.compactMap { observation in
                    line(from: observation, rendered: rendered, crop: crop, minimumConfidence: options.minimumConfidence)
                }
            }
            pages.append(Page(page: index + 1, width: crop.width, height: crop.height,
                              rotation: rotation, skipped: false, lines: lines))
            progress?(done + 1, indices.count)
        }
        return Result(pages: pages, languages: languages, revision: latestRevision)
    }

    // MARK: Warm-up

    private static let warmUpLock = NSLock()
    private static var warmedLanguageSets: Set<String> = []

    /// Load Vision's recognition models in the background before the user
    /// asks for OCR. The first accurate request after a reboot (or a long
    /// idle) spends 10–30 s loading models — macOS then keeps them cached
    /// system-wide, so later requests take milliseconds. Runs once per
    /// language set per launch; cheap when the models are already loaded.
    static func warmUp(languages: [String]) {
        let key = languages.joined(separator: ",")
        warmUpLock.lock()
        let isNew = warmedLanguageSets.insert(key).inserted
        warmUpLock.unlock()
        guard isNew else { return }
        DispatchQueue.global(qos: .utility).async {
            guard let image = warmUpImage() else { return }
            let request = makeRequest(languages: resolveLanguages(languages).usable, correction: true)
            try? VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
        }
    }

    /// A small image with real Arabic and Latin text: a blank image would stop
    /// at text detection and never load the recognizer.
    private static func warmUpImage() -> CGImage? {
        let size = CGSize(width: 720, height: 120)
        guard let context = CGContext(
            data: nil, width: Int(size.width), height: Int(size.height),
            bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
        ) else { return nil }
        context.setFillColor(CGColor(gray: 1, alpha: 1))
        context.fill(CGRect(origin: .zero, size: size))
        let text = NSAttributedString(string: "مرحبا بالعالم Hello 2026", attributes: [
            .font: CTFontCreateWithName("Helvetica" as CFString, 44, nil),
            .foregroundColor: CGColor(gray: 0, alpha: 1)
        ])
        context.textPosition = CGPoint(x: 24, y: 40)
        CTLineDraw(CTLineCreateWithAttributedString(text), context)
        return context.makeImage()
    }

    /// Async wrapper: runs on a background queue; cancelling the Task cancels
    /// the recognition.
    static func recognize(
        url: URL,
        options: Options,
        progress: (@Sendable (Int, Int) -> Void)? = nil
    ) async throws -> Result {
        let cancellation = VisionOCRCancellation()
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Result, Error>) in
                DispatchQueue.global(qos: .userInitiated).async {
                    do {
                        let result = try recognize(url: url, options: options, cancellation: cancellation, progress: progress)
                        continuation.resume(returning: result)
                    } catch {
                        continuation.resume(throwing: error)
                    }
                }
            }
        } onCancel: {
            cancellation.cancel()
        }
    }

    /// Write a result as the JSON `add-text-layer` reads.
    static func writeJSON(_ result: Result, to url: URL) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        try encoder.encode(result).write(to: url, options: .atomic)
    }

    // MARK: Internals

    private struct RenderedPage {
        let image: CGImage
        /// Page space -> upright display space (points, bottom-left origin).
        let pageToDisplay: CGAffineTransform
        let displaySize: CGSize
    }

    private static func makeRequest(languages: [String], correction: Bool) -> VNRecognizeTextRequest {
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.revision = latestRevision
        request.usesLanguageCorrection = correction
        if languages.isEmpty {
            request.automaticallyDetectsLanguage = true
        } else {
            request.recognitionLanguages = languages
        }
        return request
    }

    private static func render(_ page: CGPDFPage, options: Options) -> RenderedPage? {
        let crop = page.getBoxRect(.cropBox)
        let rotation = ((Int(page.rotationAngle) % 360) + 360) % 360
        let displaySize = rotation % 180 == 0 ? crop.size : CGSize(width: crop.height, height: crop.width)
        guard displaySize.width > 1, displaySize.height > 1 else { return nil }

        var scale = options.dpi / 72
        let longest = max(displaySize.width, displaySize.height) * scale
        if longest > options.maxPixelDimension {
            scale *= options.maxPixelDimension / longest
        }
        let pixelWidth = max(1, Int((displaySize.width * scale).rounded()))
        let pixelHeight = max(1, Int((displaySize.height * scale).rounded()))

        guard let context = CGContext(
            data: nil,
            width: pixelWidth,
            height: pixelHeight,
            bitsPerComponent: 8,
            bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
        ) else { return nil }
        context.setFillColor(CGColor(gray: 1, alpha: 1))
        context.fill(CGRect(x: 0, y: 0, width: pixelWidth, height: pixelHeight))
        context.interpolationQuality = .high

        // getDrawingTransform maps the crop box upright into the target rect
        // (rotation applied) but never scales up, so scale separately.
        let pageToDisplay = page.getDrawingTransform(
            .cropBox,
            rect: CGRect(origin: .zero, size: displaySize),
            rotate: 0,
            preserveAspectRatio: true
        )
        context.scaleBy(x: CGFloat(pixelWidth) / displaySize.width, y: CGFloat(pixelHeight) / displaySize.height)
        context.concatenate(pageToDisplay)
        context.clip(to: crop)
        context.drawPDFPage(page)

        guard let image = context.makeImage() else { return nil }
        return RenderedPage(image: image, pageToDisplay: pageToDisplay, displaySize: displaySize)
    }

    private static func line(
        from observation: VNRecognizedTextObservation,
        rendered: RenderedPage,
        crop: CGRect,
        minimumConfidence: Float
    ) -> Line? {
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let text = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, candidate.confidence >= minimumConfidence else { return nil }

        let displayToPage = rendered.pageToDisplay.inverted()
        func engine(_ normalized: CGPoint) -> [Double] {
            // Vision: normalized, lower-left origin, upright image.
            let display = CGPoint(x: normalized.x * rendered.displaySize.width,
                                  y: normalized.y * rendered.displaySize.height)
            let page = display.applying(displayToPage)
            return [Double(page.x - crop.minX), Double(crop.maxY - page.y)]
        }
        let quad = [
            engine(observation.topLeft),
            engine(observation.topRight),
            engine(observation.bottomLeft),
            engine(observation.bottomRight)
        ]
        let xs = quad.map { $0[0] }
        let ys = quad.map { $0[1] }
        return Line(
            text: text,
            confidence: candidate.confidence,
            quad: quad,
            rect: [xs.min() ?? 0, ys.min() ?? 0, xs.max() ?? 0, ys.max() ?? 0]
        )
    }
}
