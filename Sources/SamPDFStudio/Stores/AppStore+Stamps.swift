import AppKit
import PDFKit

/// Watermark, header & footer, Bates numbering, and advanced split.
extension AppStore {
    // MARK: Document facts used by the stamp sheets

    /// File name the user knows (not the versioned working copy's name).
    var stampsDocumentFilename: String {
        session?.originalURL.lastPathComponent ?? documentName
    }

    /// Value for the {title} token: PDF title, else the file stem.
    var stampsDocumentTitle: String {
        if let title = document?.documentAttributes?[PDFDocumentAttribute.titleAttribute] as? String,
           !title.trimmingCharacters(in: .whitespaces).isEmpty {
            return title
        }
        return (stampsDocumentFilename as NSString).deletingPathExtension
    }

    /// Top-level outline entries that point at a page, in page order, with
    /// duplicates on the same page collapsed (mirrors the engine's
    /// bookmarks split).
    var topLevelBookmarks: [(title: String, page: Int)] {
        guard let document, let root = document.outlineRoot else { return [] }
        var entries: [(title: String, page: Int)] = []
        for index in 0..<root.numberOfChildren {
            guard let child = root.child(at: index),
                  let page = child.destination?.page else { continue }
            entries.append((child.label ?? "Untitled", document.index(for: page) + 1))
        }
        entries.sort { $0.page < $1.page }
        var unique: [(title: String, page: Int)] = []
        for entry in entries where unique.last?.page != entry.page {
            unique.append(entry)
        }
        return unique
    }

    /// Folder next to the original document, the default home for split parts.
    var defaultSplitParentDirectory: URL {
        session?.originalURL.deletingLastPathComponent()
            ?? FileManager.default.urls(for: .desktopDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser
    }

    // MARK: Operations on the open document (undoable)

    func applyWatermark(_ options: WatermarkOptions) {
        runEngineOp(title: "Add Watermark", success: "Watermark added") { [engine] input, output in
            _ = try engine.watermark(input: input, output: output, options: options)
        }
    }

    func applyHeaderFooter(_ options: HeaderFooterOptions) {
        var options = options
        if options.filename == nil { options.filename = stampsDocumentFilename }
        if options.title == nil { options.title = stampsDocumentTitle }
        let finalOptions = options
        runEngineOp(title: "Add Header & Footer", success: "Header & footer added") { [engine] input, output in
            _ = try engine.headerFooter(input: input, output: output, options: finalOptions)
        }
    }

    func applyBates(_ options: BatesOptions) {
        let box = BatesResultBox()
        runEngineOp(title: "Add Bates Numbers", success: "Bates numbers added", onDone: { [weak self] in
            guard let result = box.value else { return }
            self?.toast(result.first == result.last
                        ? "Bates number \(result.first) added"
                        : "Bates \(result.first) – \(result.last)")
        }) { [engine] input, output in
            box.value = try engine.bates(input: input, output: output, options: options)
        }
    }

    // MARK: Split (writes new files; the open document is unchanged)

    /// Split the current working copy (including unsaved edits) into
    /// `directory`, then reveal the parts in Finder.
    func splitDocument(_ options: SplitOptions, into directory: URL) {
        guard let input = currentDocumentURL else {
            toast("Open a PDF first")
            return
        }
        guard !busy else {
            toast("Wait for \(busyTitle) to finish")
            return
        }
        var options = options
        if options.basename == nil {
            options.basename = (stampsDocumentFilename as NSString).deletingPathExtension
        }
        let finalOptions = options
        let box = SplitResultBox()
        runEngineJob(title: "Split Document", work: { [engine] in
            let result = try engine.splitAdvanced(input: input, outputDirectory: directory, options: finalOptions)
            box.value = result
            return directory
        }, onSuccess: { [weak self] directory in
            let parts = box.value?.outputs ?? []
            let oversize = parts.filter { $0.oversize == true }.count
            if oversize > 0 {
                self?.toast("Split into \(parts.count) files — \(oversize) single page\(oversize == 1 ? " is" : "s are") over the size limit")
            } else {
                self?.toast("Split into \(parts.count) file\(parts.count == 1 ? "" : "s")")
            }
            let urls = parts.map(\.url)
            NSWorkspace.shared.activateFileViewerSelecting(urls.isEmpty ? [directory] : urls)
        })
    }
}

/// Carries an engine reply from the background work closure to `onDone`.
private final class BatesResultBox: @unchecked Sendable {
    var value: BatesResult?
}

private final class SplitResultBox: @unchecked Sendable {
    var value: SplitResult?
}
