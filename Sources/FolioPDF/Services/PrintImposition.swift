import CoreGraphics
import Foundation
import PDFKit

/// Pages per sheet for "Print to HP LaserJet". The host-based printers and
/// their converters have no N-up, so Folio lays the pages out itself.
enum PrintLayout: String, CaseIterable, Identifiable {
    case one = "1"
    case two = "2"
    case four = "4"
    case booklet

    var id: String { rawValue }

    var label: String {
        switch self {
        case .one: return "1"
        case .two: return "2"
        case .four: return "4"
        case .booklet: return "Booklet"
        }
    }
}

/// Imposes a PDF for N-up and booklet printing.
///
/// Every output page is a PORTRAIT sheet of the chosen paper size. 2-up and
/// booklet sheets are laid out landscape and rotated onto the portrait page,
/// exactly as a printer driver would, so the HP driver's paper detection and
/// raster grid stay portrait. Read a 2-up/booklet sheet's back by turning it
/// over its short edge (DUPLEX_FLIP=short).
enum PrintImposition {

    /// Source pages (1-based; nil = blank) on each output page, in order.
    static func slots(layout: PrintLayout, pageCount n: Int) -> [[Int?]] {
        guard n > 0 else { return [] }
        switch layout {
        case .one:
            return (1...n).map { [$0] }
        case .two, .four:
            let per = layout == .two ? 2 : 4
            return stride(from: 1, through: n, by: per).map { first in
                (first..<(first + per)).map { $0 <= n ? $0 : nil }
            }
        case .booklet:
            return bookletSlots(pageCount: n)
        }
    }

    /// Saddle-stitch booklet: pad to a multiple of 4; sheet i (0-based) has
    /// front [P'-2i | 1+2i] and back [2+2i | P'-1-2i] (left | right).
    /// Output pages alternate front, back, front, back… so fronts sit at odd
    /// positions and backs at even ones, matching the driver's manual duplex.
    static func bookletSlots(pageCount n: Int) -> [[Int?]] {
        guard n > 0 else { return [] }
        let padded = (n + 3) / 4 * 4
        func page(_ p: Int) -> Int? { p <= n ? p : nil }
        var out: [[Int?]] = []
        for i in 0..<(padded / 4) {
            out.append([page(padded - 2 * i), page(1 + 2 * i)])
            out.append([page(2 + 2 * i), page(padded - 1 - 2 * i)])
        }
        return out
    }

    /// Portrait paper size in points; "auto" = the first page's size, upright.
    static func paperSize(_ paper: String, firstPage: CGSize) -> CGSize {
        let size: CGSize
        switch paper {
        case "a4": size = CGSize(width: 595, height: 842)
        case "letter": size = CGSize(width: 612, height: 792)
        case "legal": size = CGSize(width: 612, height: 1008)
        case "a5": size = CGSize(width: 420, height: 595)
        default: size = firstPage.width > 0 && firstPage.height > 0 ? firstPage : CGSize(width: 595, height: 842)
        }
        return CGSize(width: min(size.width, size.height), height: max(size.width, size.height))
    }

    enum ImpositionError: LocalizedError {
        case unreadable, cannotWrite
        var errorDescription: String? {
            switch self {
            case .unreadable: return "Couldn't read the document to lay out its pages."
            case .cannotWrite: return "Couldn't create the laid-out document for printing."
            }
        }
    }

    /// Write the imposed PDF. Returns the number of output pages.
    @discardableResult
    static func impose(input: URL, output: URL, layout: PrintLayout, paper: String) throws -> Int {
        guard let source = CGPDFDocument(input as CFURL), source.numberOfPages > 0 else {
            throw ImpositionError.unreadable
        }
        let n = source.numberOfPages
        let first = displaySize(source.page(at: 1))
        let sheet = paperSize(paper, firstPage: first)
        var mediaBox = CGRect(origin: .zero, size: sheet)
        guard let context = CGContext(output as CFURL, mediaBox: &mediaBox, nil) else {
            throw ImpositionError.cannotWrite
        }
        let pages = slots(layout: layout, pageCount: n)
        for slotPages in pages {
            context.beginPDFPage([kCGPDFContextMediaBox as String: NSValue(rect: mediaBox)] as CFDictionary)
            let cells = cellRects(layout: layout, sheet: sheet)
            if layout == .two || layout == .booklet {
                // Landscape canvas (sheet.height x sheet.width) rotated onto
                // the portrait page: (x, y) -> (W - y, x).
                context.saveGState()
                context.translateBy(x: sheet.width, y: 0)
                context.rotate(by: .pi / 2)
            }
            for (cell, number) in zip(cells, slotPages) {
                guard let number, let page = source.page(at: number) else { continue }
                draw(page, in: cell, context: context)
            }
            if layout == .two || layout == .booklet { context.restoreGState() }
            context.endPDFPage()
        }
        context.closePDF()
        return pages.count
    }

    /// Cells in the sheet's drawing space (landscape space for 2-up/booklet),
    /// in reading order: left→right, top→bottom.
    static func cellRects(layout: PrintLayout, sheet: CGSize) -> [CGRect] {
        switch layout {
        case .one:
            return [CGRect(origin: .zero, size: sheet)]
        case .two:
            let w = sheet.height, h = sheet.width, outer: CGFloat = 14, gutter: CGFloat = 14
            let cw = (w - 2 * outer - gutter) / 2, ch = h - 2 * outer
            return [CGRect(x: outer, y: outer, width: cw, height: ch),
                    CGRect(x: outer + cw + gutter, y: outer, width: cw, height: ch)]
        case .booklet:
            // Exact halves: each page meets the fold.
            let w = sheet.height, h = sheet.width
            return [CGRect(x: 0, y: 0, width: w / 2, height: h),
                    CGRect(x: w / 2, y: 0, width: w / 2, height: h)]
        case .four:
            let w = sheet.width, h = sheet.height, outer: CGFloat = 14, gutter: CGFloat = 12
            let cw = (w - 2 * outer - gutter) / 2, ch = (h - 2 * outer - gutter) / 2
            let top = outer + ch + gutter, bottom = outer
            return [CGRect(x: outer, y: top, width: cw, height: ch),
                    CGRect(x: outer + cw + gutter, y: top, width: cw, height: ch),
                    CGRect(x: outer, y: bottom, width: cw, height: ch),
                    CGRect(x: outer + cw + gutter, y: bottom, width: cw, height: ch)]
        }
    }

    /// Page size as displayed (crop box, with /Rotate applied).
    static func displaySize(_ page: CGPDFPage?) -> CGSize {
        guard let page else { return .zero }
        let box = page.getBoxRect(.cropBox)
        return page.rotationAngle % 180 == 0 ? box.size : CGSize(width: box.height, height: box.width)
    }

    /// Draw `page` scaled to fit `cell` (aspect kept, centred, up or down).
    static func draw(_ page: CGPDFPage, in cell: CGRect, context: CGContext) {
        let box = page.getBoxRect(.cropBox)
        let shown = displaySize(page)
        guard shown.width > 0, shown.height > 0 else { return }
        let scale = min(cell.width / shown.width, cell.height / shown.height)
        context.saveGState()
        context.translateBy(x: cell.midX, y: cell.midY)
        context.scaleBy(x: scale, y: scale)
        context.rotate(by: -CGFloat(page.rotationAngle) * .pi / 180)
        context.translateBy(x: -box.midX, y: -box.midY)
        context.clip(to: box)
        context.drawPDFPage(page)
        context.restoreGState()
    }
}
