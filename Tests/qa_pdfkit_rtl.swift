// PDFKit (the app's text layer, and Preview's) on the mixed Arabic / Latin /
// digit fixture written by Tests/qa_rtl_extraction.py:
//   page.string, selection(for: rect) and a drag selection(from:to:) -- what
//   copy uses -- must give the logical string; findString must find it.
// Lines PDFKit cannot read logically by itself ("pdfkit": false in the
// manifest, see qa_rtl_extraction.py) are reported, not failed.
// Usage: qa_pdfkit_rtl tmp/qa/rtl/manifest.json   Ends with "pdfkit rtl ok".
import Foundation
import PDFKit

func flat(_ s: String) -> String {
    s.replacingOccurrences(of: "\u{00A0}", with: " ")
        .components(separatedBy: .whitespacesAndNewlines).filter { !$0.isEmpty }.joined(separator: " ")
}

func startsRightToLeft(_ s: String) -> Bool {
    for scalar in s.unicodeScalars {
        switch scalar.value {
        case 0x0590...0x08FF, 0xFB1D...0xFDFF, 0xFE70...0xFEFC: return true
        case 0x41...0x5A, 0x61...0x7A: return false
        default: continue
        }
    }
    return false
}

let arguments = CommandLine.arguments
guard arguments.count > 1,
      let data = FileManager.default.contents(atPath: arguments[1]),
      let manifest = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
      let path = manifest["pdf"] as? String,
      let items = manifest["items"] as? [[String: Any]],
      let document = PDFDocument(url: URL(fileURLWithPath: path)) else {
    print("FAIL cannot read manifest / fixture (run Tests/qa_rtl_extraction.py first)")
    exit(1)
}

var failures: [String] = []
func check(_ condition: Bool, _ label: String) {
    print((condition ? "ok   " : "FAIL ") + label)
    if !condition { failures.append(label) }
}

var pageTexts: [Int: String] = [:]
for item in items {
    guard let pageNumber = item["page"] as? Int, let page = document.page(at: pageNumber - 1),
          let rect = item["rect"] as? [Double], let expected = item["expected"] as? String else { continue }
    let lines = max(1, item["lines"] as? Int ?? 1)
    let pdfkitReadsIt = item["pdfkit"] as? Bool ?? true
    let kind = item["kind"] as? String ?? "?"
    let label = "[\(kind)] \(expected.debugDescription)"
    let height = page.bounds(for: .cropBox).height
    let pageText = pageTexts[pageNumber] ?? flat(page.string ?? "")
    pageTexts[pageNumber] = pageText

    // Rectangle selection, kept inside the text rows (selection hit-testing
    // uses generous glyph boxes, a neighbouring row must not leak in).
    let lineHeight = (rect[3] - rect[1]) / Double(lines)
    let inset = lineHeight * 0.25
    let selectionRect = CGRect(x: rect[0], y: height - rect[3] + inset,
                               width: rect[2] - rect[0], height: rect[3] - rect[1] - 2 * inset)
    let selected = flat(page.selection(for: selectionRect)?.string ?? "")

    // Drag selection from just before the start of the first row to just past
    // the end of the last, as a user drags across whole lines. (Partial drags
    // inside an RTL row follow PDFKit's own glyph-to-character mapping.)
    let firstRow = height - (rect[1] + lineHeight / 2)
    let lastRow = height - (rect[3] - lineHeight / 2)
    let rtl = startsRightToLeft(expected)
    let from = CGPoint(x: rtl ? rect[2] + 4 : rect[0] - 4, y: firstRow)
    let to = CGPoint(x: rtl ? rect[0] - 4 : rect[2] + 4, y: lastRow)
    let dragged = flat(page.selection(from: from, to: to)?.string ?? "")

    let firstLine = expected.components(separatedBy: "\n")[0]
    let found = document.findString(firstLine, withOptions: []).contains { $0.pages.contains(page) }

    if pdfkitReadsIt {
        check(pageText.contains(flat(expected)), "page.string \(label)")
        check(selected == flat(expected), "selection(for:) \(label) -> \(selected.debugDescription)")
        check(dragged == flat(expected), "drag selection \(label) -> \(dragged.debugDescription)")
        check(found, "findString \(firstLine.debugDescription) on page \(pageNumber)")
    } else {
        print("note PDFKit's own bidi reads \(label) as \(selected.debugDescription) (known PDFKit limitation)")
    }
}

if !failures.isEmpty {
    print("\(failures.count) PDFKit check(s) failed")
    exit(1)
}
print("pdfkit rtl ok")
