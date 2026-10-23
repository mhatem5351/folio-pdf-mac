import Foundation

extension PDFEngineClient {
    /// Reply of `add-text-layer`.
    struct TextLayerReply: Decodable {
        let output: String
        let pages: Int
        let lines: Int
        let skipped_lines: Int
    }

    /// Embed recognized lines (VisionOCR JSON, engine coordinates) as an
    /// invisible, searchable text layer.
    @discardableResult
    func addTextLayer(input: URL, output: URL, linesJSON: URL) throws -> TextLayerReply {
        try runJSON([
            "add-text-layer",
            "--input", input.path,
            "--output", output.path,
            "--json", linesJSON.path
        ], as: TextLayerReply.self)
    }
}
