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

    func zoom(by factor: CGFloat) {
        guard let view = pdfViewProxy.pdfView else { return }
        setZoom(view.scaleFactor * factor)
    }

    func setZoom(_ scale: CGFloat) {
        guard let view = pdfViewProxy.pdfView else { return }
        view.autoScales = false
        view.scaleFactor = min(max(scale, 0.1), 8)
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

    /// Spool the open document (or a page range of it) to the HP LaserJet
    /// driver and report the outcome from the driver log.
    func printToLaserJet(options: LaserJetPrinter.Options, pageSpec: String) {
        guard let document, let data = document.dataRepresentation() else { return }
        guard !busy else {
            toast("Wait for \(busyTitle) to finish")
            return
        }
        let engine = self.engine
        let name = documentName
        busy = true
        busyTitle = "Sending to printer"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let temp = FileManager.default.temporaryDirectory
                .appendingPathComponent("pdfstudio-print-\(UUID().uuidString)", isDirectory: true)
            defer { try? FileManager.default.removeItem(at: temp) }
            let retrySource = FileManager.default.temporaryDirectory
                .appendingPathComponent("pdfstudio-print-retry-\(UUID().uuidString).pdf")
            let job: URL
            let offset: UInt64
            do {
                try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
                var source = temp.appendingPathComponent("full.pdf")
                try data.write(to: source)
                if pageSpec != "all" {
                    let subset = temp.appendingPathComponent("pages.pdf")
                    _ = try engine.extractPages(input: source, output: subset, pages: pageSpec)
                    source = subset
                }
                try? FileManager.default.removeItem(at: retrySource)
                try FileManager.default.copyItem(at: source, to: retrySource)
                offset = LaserJetPrinter.logOffset()
                job = try LaserJetPrinter.submit(pdf: source, options: options)
            } catch {
                DispatchQueue.main.async {
                    self?.busy = false
                    self?.report(error, while: "Print to HP LaserJet")
                }
                return
            }
            DispatchQueue.main.async {
                self?.busy = false
                self?.toast(options.copies > 1 ? "Printing \(options.copies) copies of \(name)…" : "Printing \(name)…")
            }
            var outcome = LaserJetPrinter.waitForOutcome(job: job, startOffset: offset)
            // A sleeping printer can reject the first USB write before any byte
            // lands; nothing was printed, so resend once after it wakes.
            if case .failed(_, retryable: true) = outcome, let retryData = try? Data(contentsOf: retrySource) {
                Thread.sleep(forTimeInterval: 2)
                let retryFile = FileManager.default.temporaryDirectory.appendingPathComponent("pdfstudio-retry-\(UUID().uuidString).pdf")
                if (try? retryData.write(to: retryFile)) != nil {
                    let retryOffset = LaserJetPrinter.logOffset()
                    if let retryJob = try? LaserJetPrinter.submit(pdf: retryFile, options: options) {
                        outcome = LaserJetPrinter.waitForOutcome(job: retryJob, startOffset: retryOffset)
                    }
                    try? FileManager.default.removeItem(at: retryFile)
                }
            }
            try? FileManager.default.removeItem(at: retrySource)
            DispatchQueue.main.async {
                switch outcome {
                case .printed:
                    self?.toast("Sent to HP LaserJet ✓")
                case .queuedNoPrinter:
                    self?.toast("Printer not connected — the job will print when it's plugged in")
                case .failed(let message, _):
                    self?.errorMessage = message
                case .timedOut:
                    self?.toast("Still printing… check the printer")
                }
            }
        }
    }
}
