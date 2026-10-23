import AppKit
import PDFKit

extension AppStore {
    /// 1-based number of the page currently shown in the canvas.
    var currentPageNumber: Int {
        guard let view = pdfViewProxy.pdfView,
              let page = view.currentPage,
              let document = view.document else { return 1 }
        return document.index(for: page) + 1
    }

    /// Working copy of the open document (what engine commands should read).
    var currentDocumentURL: URL? { session?.currentURL }

    /// Open a catalog tool: sheet tools present through ToolSheetHost, action
    /// tools run immediately.
    func launch(_ tool: ToolID) {
        guard document != nil || tool.worksWithoutDocument else {
            toast("Open a PDF first")
            return
        }
        switch tool {
        case .redact:
            selectedOperation = .edit
            selectedEditTool = .redact
        case .exportOffice:
            exportOffice(.word)
        case .print:
            printDocument()
        case .printHP:
            printToLaserJet()
        default:
            activeTool = tool
        }
    }

    /// System print dialog (all printers; the PDF ▾ menu also offers
    /// "Print on HP LaserJet").
    func printDocument() {
        guard let document, let view = pdfViewProxy.pdfView else { return }
        let info = NSPrintInfo.shared
        info.horizontalPagination = .fit
        info.verticalPagination = .fit
        info.isHorizontallyCentered = true
        info.isVerticallyCentered = true
        guard let operation = document.printOperation(for: info, scalingMode: .pageScaleToFit, autoRotate: true) else { return }
        operation.showsPrintPanel = true
        operation.showsProgressPanel = true
        operation.runModal(for: view.window ?? NSApp.mainWindow ?? NSWindow(), delegate: nil, didRun: nil, contextInfo: nil)
    }

    /// Placeholder until the LaserJet integration lands (Output feature).
    @objc func printToLaserJet() {
        toast("Print to HP LaserJet is coming soon")
    }
}
