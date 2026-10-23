import AppKit
import Foundation

/// Canvas-driven editing of existing images (Edit ▸ Image) and drag-to-create
/// form fields (Edit ▸ Field).
extension AppStore {
    var selectedCanvasImage: EngineImagePlacement? {
        guard let selectedImageID else { return nil }
        return canvasImages.first { $0.id == selectedImageID }
    }

    /// Reload every image placement of the working document for hit-testing.
    func refreshCanvasImages() {
        guard let input = currentDocumentURL else {
            canvasImages = []
            return
        }
        let engine = self.engine
        let revision = documentRevision
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let list = try? engine.listImages(input: input)
            DispatchQueue.main.async {
                guard let self, self.documentRevision == revision else { return }
                self.canvasImages = list?.images ?? []
            }
        }
    }

    func selectCanvasImage(_ placement: EngineImagePlacement?) {
        selectedImageID = placement?.id
    }

    /// Commit a canvas move/resize. Selection follows the placement into the
    /// new version (same page, xref and placement index).
    func transformCanvasImage(_ placement: EngineImagePlacement, to engineRect: CGRect) {
        let keepID = placement.id
        transformImage(page: placement.page, xref: placement.xref, placementIndex: placement.placementIndex,
                       engineRect: engineRect) { [weak self] in
            self?.selectedImageID = keepID
        }
    }

    func rotateSelectedImage(by degrees: Double) {
        guard let image = selectedCanvasImage else { return }
        let keepID = image.id
        transformImage(page: image.page, xref: image.xref, placementIndex: image.placementIndex,
                       engineRect: image.engineRect, rotate: degrees) { [weak self] in
            self?.selectedImageID = keepID
        }
    }

    func replaceSelectedImage() {
        guard let image = selectedCanvasImage, let url = chooseReplacementImage() else { return }
        let keepID = image.id
        replaceImage(page: image.page, xref: image.xref, placementIndex: image.placementIndex, imageURL: url) { [weak self] in
            self?.selectedImageID = keepID
        }
    }

    func extractSelectedImage() {
        guard let image = selectedCanvasImage else { return }
        extractImageWithPanel(image)
    }

    func deleteCanvasImage(_ placement: EngineImagePlacement) {
        selectedImageID = nil
        deleteImage(page: placement.page, xref: placement.xref, placementIndex: placement.placementIndex)
    }

    /// Edit ▸ Field: create a field of `newFieldType` in the dragged rect.
    func createFieldFromCanvas(pageIndex: Int, engineRect: CGRect) {
        guard engineRect.width >= 6, engineRect.height >= 6 else {
            toast("Drag a larger box to create a field")
            return
        }
        var rect = engineRect
        // Checkboxes and radios read best square.
        if ["checkbox", "radio"].contains(newFieldType) {
            let side = min(rect.width, rect.height)
            rect.size = CGSize(width: side, height: side)
        }
        let options = ["combo", "list", "radio"].contains(newFieldType) ? ["Option 1", "Option 2", "Option 3"] : []
        addFormField(type: newFieldType, name: nil, page: pageIndex + 1, engineRect: rect, options: options)
    }

    static let canvasFieldTypes: [(id: String, title: String, symbol: String)] = [
        ("text", "Text", "character.textbox"),
        ("multiline", "Paragraph", "text.alignleft"),
        ("checkbox", "Checkbox", "checkmark.square"),
        ("radio", "Radio", "circle.inset.filled"),
        ("combo", "Dropdown", "chevron.down.square"),
        ("list", "List", "list.bullet.rectangle"),
        ("signature", "Signature", "signature")
    ]
}
