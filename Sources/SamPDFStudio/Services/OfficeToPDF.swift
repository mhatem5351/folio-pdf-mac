import AppKit
import CoreGraphics

/// Converts rich-text office documents (.docx .doc .rtf .rtfd .odt) to PDF
/// with AppKit's importers and TextKit pagination — no LibreOffice needed.
///
/// AppKit keeps fonts, sizes, colors, alignment, lists, tables (NSTextTable),
/// page breaks and embedded images. Arabic/Hebrew are shaped and laid out
/// right-to-left by CoreText. Safe to call off the main thread (no views; the
/// HTML importer, which needs the main thread, is not used here).
enum OfficeToPDF {
    struct Layout {
        /// Page size in points.
        var pageSize: CGSize
        /// Margins in points.
        var margins: NSEdgeInsets
    }

    enum ConversionError: LocalizedError {
        case unsupported(String)
        case unreadable(String, Error)
        case cannotCreatePDF(String)

        var errorDescription: String? {
            switch self {
            case .unsupported(let name):
                return "\(name) is not a Word, RTF or OpenDocument file."
            case .unreadable(let name, let error):
                return "Could not read \(name): \(error.localizedDescription)"
            case .cannotCreatePDF(let path):
                return "Could not create \(path)."
            }
        }
    }

    /// File extensions handled here (lowercase).
    static let supportedExtensions: Set<String> = ["docx", "doc", "rtf", "rtfd", "odt", "wordml"]

    static func handles(_ url: URL) -> Bool {
        supportedExtensions.contains(url.pathExtension.lowercased())
    }

    /// Write `input` as a PDF to `output`; returns the number of pages.
    @discardableResult
    static func convert(_ input: URL, to output: URL, layout: Layout) throws -> Int {
        let text = try load(input)
        prepare(text, textWidth: layout.pageSize.width - layout.margins.left - layout.margins.right,
                textHeight: layout.pageSize.height - layout.margins.top - layout.margins.bottom)
        return try render(text, to: output, layout: layout, title: input.deletingPathExtension().lastPathComponent)
    }

    // MARK: - Loading

    private static func documentType(for url: URL) -> NSAttributedString.DocumentType? {
        switch url.pathExtension.lowercased() {
        case "docx": return .officeOpenXML
        case "doc": return .docFormat
        case "rtf": return .rtf
        case "rtfd": return .rtfd
        case "odt": return .openDocument
        case "wordml": return .wordML
        default: return nil
        }
    }

    private static func load(_ url: URL) throws -> NSMutableAttributedString {
        guard let type = documentType(for: url) else {
            throw ConversionError.unsupported(url.lastPathComponent)
        }
        do {
            let string = try NSAttributedString(
                url: url,
                options: [.documentType: type],
                documentAttributes: nil
            )
            return NSMutableAttributedString(attributedString: string)
        } catch {
            // A mislabelled file (e.g. an RTF saved as .doc): let AppKit sniff.
            if let sniffed = try? NSAttributedString(url: url, options: [:], documentAttributes: nil) {
                return NSMutableAttributedString(attributedString: sniffed)
            }
            throw ConversionError.unreadable(url.lastPathComponent, error)
        }
    }

    // MARK: - Preparation

    /// Arabic in fonts like Arial or SF Arabic ends up in the PDF mapped to
    /// presentation forms or decomposed dot glyphs, which breaks copy and
    /// search. Geeza Pro (the macOS Arabic system font) maps glyphs back to
    /// the letters as typed, so Arabic runs are set in it at the same size
    /// and weight.
    static let normalizesArabicFonts = true

    private static let arabicPattern = try? NSRegularExpression(
        pattern: "[\\u0600-\\u06FF\\u0750-\\u077F\\u08A0-\\u08FF\\uFB50-\\uFDFF\\uFE70-\\uFEFF]+"
    )

    private static func prepare(_ text: NSMutableAttributedString, textWidth: CGFloat, textHeight: CGFloat) {
        let whole = NSRange(location: 0, length: text.length)
        text.beginEditing()
        defer { text.endEditing() }

        if normalizesArabicFonts, let arabicPattern {
            for match in arabicPattern.matches(in: text.string, range: whole) {
                text.enumerateAttribute(.font, in: match.range) { value, range, _ in
                    let current = (value as? NSFont) ?? NSFont.systemFont(ofSize: 12)
                    let bold = current.fontDescriptor.symbolicTraits.contains(.bold)
                    let name = bold ? "GeezaPro-Bold" : "GeezaPro"
                    if !current.fontName.hasPrefix("GeezaPro"),
                       let geeza = NSFont(name: name, size: current.pointSize) {
                        text.addAttribute(.font, value: geeza, range: range)
                    }
                }
            }
        }

        // Images wider or taller than the text area would never fit a page
        // (TextKit then leaves them out); scale them down to fit.
        text.enumerateAttribute(.attachment, in: whole) { value, _, _ in
            guard let attachment = value as? NSTextAttachment else { return }
            let image = attachment.image
                ?? attachment.fileWrapper?.regularFileContents.flatMap { NSImage(data: $0) }
            guard let image else { return }
            let size = attachment.bounds.size == .zero ? image.size : attachment.bounds.size
            guard size.width > 0, size.height > 0 else { return }
            let scale = min(1, textWidth / size.width, (textHeight - 4) / size.height)
            guard scale < 1 else { return }
            let fitted = NSSize(width: floor(size.width * scale), height: floor(size.height * scale))
            let resized = image.copy() as? NSImage ?? image
            resized.size = fitted
            attachment.attachmentCell = NSTextAttachmentCell(imageCell: resized)
            attachment.bounds = CGRect(origin: .zero, size: fitted)
        }
    }

    // MARK: - Pagination and drawing

    private static func render(_ text: NSAttributedString, to output: URL, layout: Layout, title: String) throws -> Int {
        let storage = NSTextStorage(attributedString: text)
        let layoutManager = NSLayoutManager()
        layoutManager.allowsNonContiguousLayout = false
        layoutManager.usesFontLeading = true
        storage.addLayoutManager(layoutManager)

        let margins = layout.margins
        let textSize = CGSize(
            width: max(72, layout.pageSize.width - margins.left - margins.right),
            height: max(72, layout.pageSize.height - margins.top - margins.bottom)
        )

        // One container per page until every glyph is placed. A run of empty
        // containers means something can never fit; stop instead of looping.
        var containers: [NSTextContainer] = []
        var emptyStreak = 0
        let totalGlyphs = layoutManager.numberOfGlyphs
        repeat {
            let container = NSTextContainer(size: textSize)
            container.lineFragmentPadding = 0
            layoutManager.addTextContainer(container)
            containers.append(container)
            let range = layoutManager.glyphRange(for: container)
            emptyStreak = range.length == 0 ? emptyStreak + 1 : 0
            if NSMaxRange(range) >= totalGlyphs || emptyStreak >= 3 || containers.count >= 10_000 {
                break
            }
        } while true
        // Drop trailing empty pages (keep at least one page).
        while containers.count > 1, layoutManager.glyphRange(for: containers[containers.count - 1]).length == 0 {
            containers.removeLast()
        }

        var mediaBox = CGRect(origin: .zero, size: layout.pageSize)
        let info: [CFString: Any] = [
            kCGPDFContextTitle: title,
            kCGPDFContextCreator: "PDF Studio"
        ]
        try? FileManager.default.removeItem(at: output)
        guard let context = CGContext(output as CFURL, mediaBox: &mediaBox, info as CFDictionary) else {
            throw ConversionError.cannotCreatePDF(output.path)
        }
        let origin = CGPoint(x: margins.left, y: margins.top)
        for container in containers {
            context.beginPDFPage(nil)
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(cgContext: context, flipped: true)
            // TextKit draws in a top-left, y-down space.
            context.translateBy(x: 0, y: layout.pageSize.height)
            context.scaleBy(x: 1, y: -1)
            let range = layoutManager.glyphRange(for: container)
            if range.length > 0 {
                layoutManager.drawBackground(forGlyphRange: range, at: origin)
                layoutManager.drawGlyphs(forGlyphRange: range, at: origin)
            }
            NSGraphicsContext.restoreGraphicsState()
            context.endPDFPage()
        }
        context.closePDF()
        return containers.count
    }
}
