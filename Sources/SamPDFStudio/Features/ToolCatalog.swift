import SwiftUI

/// Groups tools in the Tools hub and command palette.
enum ToolCategory: String, CaseIterable, Identifiable {
    case organize, edit, protect, convert, review, output

    var id: String { rawValue }

    var title: String {
        switch self {
        case .organize: "Organize"
        case .edit: "Edit & Stamp"
        case .protect: "Protect"
        case .convert: "Convert & OCR"
        case .review: "Review"
        case .output: "Print & Share"
        }
    }

    var color: Color { Theme.categoryColors[self] ?? Theme.accent }
}

/// Every document tool the app offers. Tools with `opensSheet == true` are
/// presented through `ToolSheetHost` when `AppStore.activeTool` is set; the
/// others run an action directly (see `AppStore.launch(_:)`).
enum ToolID: String, CaseIterable, Identifiable {
    // Organize
    case split
    case compress
    case bookmarks
    // Edit & Stamp
    case images
    case forms
    case watermark
    case headerFooter
    case bates
    // Protect
    case protect
    case unlock
    case flatten
    case metadata
    case redact
    // Convert & OCR
    case ocr
    case convertToPDF
    case exportOffice
    // Review
    case compare
    // Print & Share
    case printHP
    case print

    var id: String { rawValue }

    var title: String {
        switch self {
        case .split: "Split Document"
        case .compress: "Reduce File Size"
        case .bookmarks: "Bookmarks"
        case .images: "Images"
        case .forms: "Form Fields"
        case .watermark: "Watermark"
        case .headerFooter: "Header & Footer"
        case .bates: "Bates Numbering"
        case .protect: "Protect with Password"
        case .unlock: "Remove Password"
        case .flatten: "Flatten"
        case .metadata: "Properties & Privacy"
        case .redact: "Redact"
        case .ocr: "Recognize Text (OCR)"
        case .convertToPDF: "Create PDF from File"
        case .exportOffice: "Export to Office"
        case .compare: "Compare Documents"
        case .printHP: "Print to HP LaserJet"
        case .print: "Print…"
        }
    }

    var subtitle: String {
        switch self {
        case .split: "Break into several PDFs by ranges, page count, or bookmarks"
        case .compress: "Shrink images and clean up the file"
        case .bookmarks: "Add, rename, reorder, and remove the outline"
        case .images: "Replace, move, resize, extract, or delete images"
        case .forms: "Create and edit fillable fields"
        case .watermark: "Stamp text or an image behind or over pages"
        case .headerFooter: "Add running headers, footers, and page numbers"
        case .bates: "Legal-style sequential page identifiers"
        case .protect: "Encrypt with AES-256 and set permissions"
        case .unlock: "Decrypt a protected PDF you can open"
        case .flatten: "Burn annotations and form fields into the page"
        case .metadata: "Edit title and author, or strip hidden data"
        case .redact: "Permanently remove sensitive text"
        case .ocr: "Make scans searchable, including Arabic"
        case .convertToPDF: "Word, RTF, HTML, text, or images to PDF"
        case .exportOffice: "Word, Excel, PowerPoint, Markdown, images"
        case .compare: "Highlight text differences between two PDFs"
        case .printHP: "Send straight to the USB LaserJet"
        case .print: "System print dialog with all printers"
        }
    }

    var systemImage: String {
        switch self {
        case .split: "scissors"
        case .compress: "arrow.down.right.and.arrow.up.left"
        case .bookmarks: "bookmark"
        case .images: "photo.on.rectangle.angled"
        case .forms: "rectangle.and.pencil.and.ellipsis"
        case .watermark: "drop"
        case .headerFooter: "rectangle.topthird.inset.filled"
        case .bates: "number.square"
        case .protect: "lock"
        case .unlock: "lock.open"
        case .flatten: "square.3.layers.3d.down.right"
        case .metadata: "info.circle"
        case .redact: "eye.slash"
        case .ocr: "text.viewfinder"
        case .convertToPDF: "doc.badge.plus"
        case .exportOffice: "square.and.arrow.up.on.square"
        case .compare: "rectangle.split.2x1"
        case .printHP: "printer.fill"
        case .print: "printer"
        }
    }

    var category: ToolCategory {
        switch self {
        case .split, .compress, .bookmarks: .organize
        case .images, .forms, .watermark, .headerFooter, .bates: .edit
        case .protect, .unlock, .flatten, .metadata, .redact: .protect
        case .ocr, .convertToPDF, .exportOffice: .convert
        case .compare: .review
        case .printHP, .print: .output
        }
    }

    /// Tools that work without an open document.
    var worksWithoutDocument: Bool {
        switch self {
        case .convertToPDF, .compare: true
        default: false
        }
    }

    /// Presented as a sheet via ToolSheetHost (vs. a direct action).
    var opensSheet: Bool {
        switch self {
        case .redact, .print, .exportOffice: false
        default: true
        }
    }

    /// Extra words the command palette matches on.
    var keywords: String {
        switch self {
        case .split: "divide separate extract ranges"
        case .compress: "optimize smaller size shrink"
        case .bookmarks: "outline toc table of contents"
        case .images: "picture photo replace move resize"
        case .forms: "fields fillable checkbox textfield acroform"
        case .watermark: "stamp draft confidential"
        case .headerFooter: "page numbers running"
        case .bates: "legal numbering"
        case .protect: "encrypt password security permissions"
        case .unlock: "decrypt password remove"
        case .flatten: "burn merge annotations"
        case .metadata: "title author sanitize privacy hidden"
        case .redact: "black out remove sensitive"
        case .ocr: "scan searchable recognize arabic"
        case .convertToPDF: "word docx rtf html create new"
        case .exportOffice: "word excel powerpoint convert"
        case .compare: "diff difference versions"
        case .printHP: "printer laserjet hp"
        case .print: "printer"
        }
    }
}
