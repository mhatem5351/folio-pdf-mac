import CoreGraphics
import Foundation

/// One placement of an image XObject on a page (engine `list-images`,
/// `image-at`, `image-thumbnails`). Geometry is in engine coordinates:
/// top-left origin, relative to the page's unrotated crop box, y down.
struct EngineImagePlacement: Decodable, Identifiable, Hashable {
    let page: Int
    let xref: Int
    let name: String
    let placementIndex: Int
    let bbox: [Double]
    /// Unit square -> engine coords, image top-left at (0,0) (MuPDF style).
    let matrix: [Double]
    /// Clockwise angle of the image's x-axis on the page, degrees.
    let rotation: Double
    let flipped: Bool
    let displayWidth: Double
    let displayHeight: Double
    let dpiX: Double
    let dpiY: Double
    /// Placements of this image XObject across the whole document.
    let occurrences: Int
    let pagesUsed: [Int]
    /// Drawn through a Form XObject (still editable; the form is cloned when shared).
    let nested: Bool
    let width: Int
    let height: Int
    let bpc: Int
    let colorspace: String
    let components: Int
    let filter: String
    let ext: String
    let bytes: Int
    let hasMask: Bool
    let thumbnail: String?
    let thumbWidth: Int?
    let thumbHeight: Int?

    var id: String { "\(page)-\(xref)-\(placementIndex)" }

    var engineRect: CGRect {
        guard bbox.count == 4 else { return .zero }
        return CGRect(x: bbox[0], y: bbox[1], width: bbox[2] - bbox[0], height: bbox[3] - bbox[1])
    }

    /// Engine `--rect` string for a rect in engine coordinates.
    static func rectArgument(_ rect: CGRect) -> String {
        "\(rect.minX),\(rect.minY),\(rect.maxX),\(rect.maxY)"
    }

    enum CodingKeys: String, CodingKey {
        case page, xref, name, bbox, matrix, rotation, flipped, occurrences, nested
        case width, height, bpc, colorspace, components, filter, ext, bytes, thumbnail
        case placementIndex = "placement_index"
        case displayWidth = "display_width"
        case displayHeight = "display_height"
        case dpiX = "dpi_x"
        case dpiY = "dpi_y"
        case pagesUsed = "pages_used"
        case hasMask = "has_mask"
        case thumbWidth = "thumb_width"
        case thumbHeight = "thumb_height"
    }
}

/// How a replacement image sits inside the old placement's frame.
enum ImageFitMode: String, CaseIterable, Identifiable {
    case fit, fill, stretch
    var id: String { rawValue }
    var title: String {
        switch self {
        case .fit: "Fit"
        case .fill: "Fill"
        case .stretch: "Stretch"
        }
    }
    var help: String {
        switch self {
        case .fit: "Keep proportions, show the whole image inside the old frame"
        case .fill: "Keep proportions, cover the old frame and crop the overflow"
        case .stretch: "Distort the image to exactly match the old frame"
        }
    }
}

struct EngineImageList: Decodable {
    let images: [EngineImagePlacement]
    let count: Int
    let inlineImages: Int?

    enum CodingKeys: String, CodingKey {
        case images, count
        case inlineImages = "inline_images"
    }
}

struct EngineImageHit: Decodable {
    let found: Bool
    let image: EngineImagePlacement?
}

/// Reply of transform-image / replace-image / delete-image.
struct EngineImageEditResult: Decodable {
    let output: String
    let page: Int
    let xref: Int
    let placementIndex: Int
    let nested: Bool
    let clonedForms: [Int]
    let bbox: [Double]?

    enum CodingKeys: String, CodingKey {
        case output, page, xref, nested, bbox
        case placementIndex = "placement_index"
        case clonedForms = "cloned_forms"
    }
}

struct EngineExtractedImage: Decodable {
    let path: String
    let format: String
    let width: Int
    let height: Int
    let colorspace: String
    let bytes: Int
}

extension PDFEngineClient {
    func listImages(input: URL, page: Int? = nil) throws -> EngineImageList {
        var args = ["list-images", "--input", input.path]
        if let page { args += ["--page", String(page)] }
        return try runJSON(args)
    }

    /// Placements plus a PNG thumbnail each (`thumbnail` path). `page == nil`
    /// covers every page.
    func imageThumbnails(input: URL, page: Int?, outputDirectory: URL, maxSide: Int = 256) throws -> EngineImageList {
        var args = ["image-thumbnails", "--input", input.path, "--output-dir", outputDirectory.path, "--max", String(maxSide)]
        if let page { args += ["--page", String(page)] }
        return try runJSON(args)
    }

    func imageAt(input: URL, page: Int, x: Double, y: Double, tolerance: Double = 0) throws -> EngineImagePlacement? {
        let hit: EngineImageHit = try runJSON([
            "image-at", "--input", input.path, "--page", String(page),
            "--x", String(x), "--y", String(y), "--tolerance", String(tolerance)
        ])
        return hit.found ? hit.image : nil
    }

    @discardableResult
    func transformImage(input: URL, output: URL, page: Int, xref: Int, placementIndex: Int,
                        rect: CGRect, rotate: Double = 0) throws -> EngineImageEditResult {
        var args = imageTargetArgs("transform-image", input, output, page, xref, placementIndex)
        args += ["--rect", EngineImagePlacement.rectArgument(rect)]
        if rotate != 0 { args += ["--rotate", String(rotate)] }
        return try runJSON(args)
    }

    @discardableResult
    func replaceImage(input: URL, output: URL, page: Int, xref: Int, placementIndex: Int,
                      image: URL, fit: ImageFitMode) throws -> EngineImageEditResult {
        var args = imageTargetArgs("replace-image", input, output, page, xref, placementIndex)
        args += ["--image", image.path, "--fit", fit.rawValue]
        return try runJSON(args)
    }

    @discardableResult
    func deleteImage(input: URL, output: URL, page: Int, xref: Int, placementIndex: Int) throws -> EngineImageEditResult {
        try runJSON(imageTargetArgs("delete-image", input, output, page, xref, placementIndex))
    }

    /// Writes the image in its original encoding when possible (JPEG/JPEG
    /// 2000), PNG otherwise; the returned `path` may differ in extension.
    func extractImage(input: URL, xref: Int, output: URL, forcePNG: Bool = false) throws -> EngineExtractedImage {
        try runJSON([
            "extract-image", "--input", input.path, "--xref", String(xref),
            "--output", output.path, "--format", forcePNG ? "png" : "auto"
        ])
    }

    private func imageTargetArgs(_ command: String, _ input: URL, _ output: URL, _ page: Int,
                                 _ xref: Int, _ placementIndex: Int) -> [String] {
        [command, "--input", input.path, "--output", output.path, "--page", String(page),
         "--xref", String(xref), "--placement-index", String(placementIndex)]
    }
}
