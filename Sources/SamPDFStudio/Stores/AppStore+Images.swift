import AppKit
import PDFKit
import UniformTypeIdentifiers

/// Editing existing images, one placement at a time. Every edit goes through
/// `runEngineOp`, so it lands as a new undoable version of the document.
/// Geometry is in engine coordinates (top-left origin of the page's crop box).
extension AppStore {
    // MARK: Queries (no new version, no busy overlay)

    /// Placements on `page` (1-based) or on every page when nil, with PNG
    /// thumbnails rendered as they appear on the page.
    func loadImagePlacements(page: Int?, maxThumbnail: Int = 256,
                             completion: @escaping (Result<EngineImageList, Error>) -> Void) {
        guard let input = currentDocumentURL else {
            completion(.success(EngineImageList(images: [], count: 0, inlineImages: 0)))
            return
        }
        let directory = Self.imageThumbnailDirectory()
        let engine = self.engine
        DispatchQueue.global(qos: .userInitiated).async {
            let result = Result { try engine.imageThumbnails(input: input, page: page, outputDirectory: directory, maxSide: maxThumbnail) }
            DispatchQueue.main.async { completion(result) }
        }
    }

    /// Topmost image placement under an engine-space point (canvas hit test).
    /// `completion(nil)` when nothing is there or the lookup failed.
    func imageAt(page: Int, engineX: Double, engineY: Double, tolerance: Double = 2,
                 completion: @escaping (EngineImagePlacement?) -> Void) {
        guard let input = currentDocumentURL else {
            completion(nil)
            return
        }
        let engine = self.engine
        DispatchQueue.global(qos: .userInitiated).async {
            let hit = try? engine.imageAt(input: input, page: page, x: engineX, y: engineY, tolerance: tolerance)
            DispatchQueue.main.async { completion(hit) }
        }
    }

    /// Page size in engine units (points) — the engine rect of the whole page.
    func imagePageBounds(page: Int) -> CGRect? {
        guard let document, page >= 1, page <= document.pageCount,
              let pdfPage = document.page(at: page - 1) else { return nil }
        let box = pdfPage.bounds(for: .cropBox)
        return CGRect(x: 0, y: 0, width: box.width, height: box.height)
    }

    // MARK: Edits (new undoable version)

    /// Move/resize exactly this placement so its bounding box becomes
    /// `engineRect`; `rotate` adds a clockwise turn (degrees) about its center.
    func transformImage(page: Int, xref: Int, placementIndex: Int, engineRect: CGRect,
                        rotate: Double = 0, onDone: (() -> Void)? = nil) {
        guard engineRect.width >= 1, engineRect.height >= 1 else {
            toast("Image size must be at least 1 pt")
            return
        }
        let engine = self.engine
        runEngineOp(title: rotate == 0 ? "Image moved" : "Image rotated", onDone: onDone) { input, output in
            try engine.transformImage(input: input, output: output, page: page, xref: xref,
                                      placementIndex: placementIndex, rect: engineRect, rotate: rotate)
        }
    }

    /// Show `imageURL` in place of this placement only (other uses of the
    /// same image keep the original).
    func replaceImage(page: Int, xref: Int, placementIndex: Int, imageURL: URL,
                      fit: ImageFitMode = .fit, onDone: (() -> Void)? = nil) {
        let source: URL
        do {
            source = try Self.engineReadableImage(imageURL)
        } catch {
            report(error, while: "Replace image")
            return
        }
        let engine = self.engine
        runEngineOp(title: "Image replaced", onDone: onDone) { input, output in
            try engine.replaceImage(input: input, output: output, page: page, xref: xref,
                                    placementIndex: placementIndex, image: source, fit: fit)
        }
    }

    func deleteImage(page: Int, xref: Int, placementIndex: Int, onDone: (() -> Void)? = nil) {
        let engine = self.engine
        runEngineOp(title: "Image deleted", onDone: onDone) { input, output in
            try engine.deleteImage(input: input, output: output, page: page, xref: xref, placementIndex: placementIndex)
        }
    }

    /// Save the image's pixels to `url` (original JPEG/JPEG 2000 bytes when
    /// possible, PNG otherwise — the extension is corrected to match).
    func extractImage(xref: Int, to url: URL, forcePNG: Bool = false) {
        guard let input = currentDocumentURL else {
            toast("Open a PDF first")
            return
        }
        let engine = self.engine
        runEngineJob(title: "Extract image", work: {
            URL(fileURLWithPath: try engine.extractImage(input: input, xref: xref, output: url, forcePNG: forcePNG).path)
        }, onSuccess: { [weak self] saved in
            self?.toast("Saved \(saved.lastPathComponent)")
            self?.reveal(saved)
        })
    }

    // MARK: Panels

    /// Image picker for "Replace…"; nil when cancelled.
    func chooseReplacementImage() -> URL? {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.image]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.message = "Choose the new image — only the selected placement changes"
        panel.prompt = "Replace"
        return panel.runModal() == .OK ? panel.url : nil
    }

    /// Save panel for "Extract…", then runs the extraction.
    func extractImageWithPanel(_ placement: EngineImagePlacement) {
        let ext = Self.extractExtension(for: placement)
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "\(documentBaseName)-p\(placement.page)-image\(placement.xref).\(ext)"
        panel.allowedContentTypes = [UTType(filenameExtension: ext) ?? .png]
        panel.message = ext == "png" ? "Saved as lossless PNG" : "Saved in its original \(ext.uppercased()) encoding"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        extractImage(xref: placement.xref, to: url)
    }

    // MARK: Helpers

    /// What extract-image will produce for this placement.
    static func extractExtension(for placement: EngineImagePlacement) -> String {
        let passthrough = (placement.ext == "jpg" || placement.ext == "jp2")
            && (placement.components == 1 || placement.components == 3)
            && !placement.hasMask
        return passthrough ? placement.ext : "png"
    }

    private var documentBaseName: String {
        (currentDocumentURL?.deletingPathExtension().lastPathComponent ?? "image")
    }

    /// Fresh folder per load (thumbnail names repeat across versions, and
    /// NSImage caches by path); the previous load's folder is removed.
    private static func imageThumbnailDirectory() -> URL {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("PDFStudio-image-thumbs", isDirectory: true)
        if let previous = try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil) {
            for folder in previous { try? FileManager.default.removeItem(at: folder) }
        }
        let url = root.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    /// MuPDF reads PNG/JPEG/TIFF/GIF/BMP/JPEG 2000 directly; anything else
    /// macOS can open (HEIC, WebP, PDF…) is converted to PNG first.
    private static func engineReadableImage(_ url: URL) throws -> URL {
        let direct: Set<String> = ["png", "jpg", "jpeg", "tif", "tiff", "gif", "bmp", "jp2", "jpx", "j2k", "pnm", "pbm", "pgm", "ppm"]
        if direct.contains(url.pathExtension.lowercased()) { return url }
        guard let image = NSImage(contentsOf: url),
              let tiff = image.tiffRepresentation,
              let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else {
            throw PDFEngineError.invalidOutput("Could not read \(url.lastPathComponent) as an image.")
        }
        let out = FileManager.default.temporaryDirectory
            .appendingPathComponent("PDFStudio-replace-\(UUID().uuidString).png")
        try png.write(to: out)
        return out
    }
}
