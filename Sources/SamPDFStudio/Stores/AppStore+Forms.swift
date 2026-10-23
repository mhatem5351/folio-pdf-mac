import AppKit
import PDFKit

/// Form-field authoring and filling (engine: Engine/ext/forms.py).
///
/// Mutations go through `runEngineOp`, so each one is a new undoable version
/// of the open document. Reads (`loadFormFields`, `detectFormFields`) run in
/// the background without blocking the UI or touching history.
/// All rects are engine coordinates: points from the top-left of the page's
/// unrotated CropBox, y growing downward.
extension AppStore {
    // MARK: Reading

    /// Fields of the working document, in page order.
    func loadFormFields(completion: @escaping (Result<[PDFFormField], Error>) -> Void) {
        guard let input = currentDocumentURL else {
            completion(.success([]))
            return
        }
        let engine = self.engine
        DispatchQueue.global(qos: .userInitiated).async {
            let result = Result { try engine.listFormFields(input: input) }
            DispatchQueue.main.async { completion(result) }
        }
    }

    /// Likely fields ("Prepare Form"): underscore blanks, empty drawn boxes,
    /// small squares (checkboxes), and signature lines. `pages` is an engine
    /// page spec; nil scans every page.
    func detectFormFields(pages: String? = nil, completion: @escaping (Result<[PDFFormFieldSuggestion], Error>) -> Void) {
        guard let input = currentDocumentURL else {
            completion(.success([]))
            return
        }
        let engine = self.engine
        DispatchQueue.global(qos: .userInitiated).async {
            let result = Result { try engine.detectFormFields(input: input, pages: pages) }
            DispatchQueue.main.async { completion(result) }
        }
    }

    // MARK: Creating

    /// Create a field at an engine-space rect on a 1-based page. Entry point
    /// for a canvas drag-to-create tool. `type` is a `PDFFormFieldKind` raw
    /// value; `onAdded` receives the final (possibly auto-suffixed) name.
    func addFormField(
        type: String,
        name: String?,
        page: Int,
        engineRect: CGRect,
        options: [String],
        value: String? = nil,
        group: String? = nil,
        onAdded: ((String) -> Void)? = nil
    ) {
        guard let kind = PDFFormFieldKind(rawValue: type) else {
            toast("Unknown field type \(type)")
            return
        }
        var spec = PDFFormFieldSpec(kind: kind, name: name, page: page, rect: engineRect.standardized)
        spec.options = options.isEmpty && group == nil ? kind.defaultOptions : options
        spec.value = value
        spec.group = group
        let box = FormReplyBox()
        runEngineOp(title: "Add \(kind.title)", onDone: {
            if let reply = box.reply {
                onAdded?(reply.name)
            }
        }) { [engine] input, output in
            box.reply = try engine.addFormField(input: input, output: output, spec: spec)
        }
    }

    /// Place a default-size field of `kind`, centered on `page` (defaults to
    /// the page shown in the canvas).
    func addFormFieldCentered(kind: PDFFormFieldKind, page: Int? = nil, onAdded: ((String) -> Void)? = nil) {
        let pageNumber = page ?? currentPageNumber
        addFormField(
            type: kind.rawValue,
            name: nil,
            page: pageNumber,
            engineRect: defaultFormFieldRect(kind: kind, page: pageNumber),
            options: kind.defaultOptions,
            onAdded: onAdded
        )
    }

    /// Default-size rect for `kind`, centered on a 1-based page.
    func defaultFormFieldRect(kind: PDFFormFieldKind, page: Int) -> CGRect {
        let bounds = document?.page(at: max(0, page - 1))?.bounds(for: .cropBox)
            ?? CGRect(x: 0, y: 0, width: 612, height: 792)
        let size = kind.defaultSize
        return CGRect(
            x: ((bounds.width - size.width) / 2).rounded(),
            y: ((bounds.height - size.height) / 2).rounded(),
            width: size.width,
            height: size.height
        )
    }

    /// Add one more button to an existing radio group, placed after its
    /// last button (to the right, or on the next row when out of room).
    func addRadioButton(to field: PDFFormField, label: String, onAdded: ((String) -> Void)? = nil) {
        guard let last = field.widgets.last else { return }
        let rect = last.engineRect
        let pageWidth = document?.page(at: max(0, last.page - 1))?.bounds(for: .cropBox).width ?? 612
        let gap = max(rect.width * 2, 24)
        var next = rect.offsetBy(dx: gap, dy: 0)
        if next.maxX > pageWidth - 12 {
            next = CGRect(x: field.widgets.first?.engineRect.minX ?? rect.minX, y: rect.maxY + 8,
                          width: rect.width, height: rect.height)
        }
        addFormField(type: PDFFormFieldKind.radio.rawValue, name: nil, page: last.page, engineRect: next,
                     options: [], value: label, group: field.name, onAdded: onAdded)
    }

    /// Create every suggestion from `detect-fields` in one undoable step.
    /// `onDone` receives the number of fields created.
    func applyDetectedFormFields(pages: String? = nil, onDone: ((Int) -> Void)? = nil) {
        let box = FormDetectBox()
        runEngineOp(title: "Create Detected Fields", onDone: {
            onDone?(box.reply?.created?.count ?? 0)
        }) { [engine] input, output in
            box.reply = try engine.applyDetectedFormFields(input: input, output: output, pages: pages)
        }
    }

    // MARK: Changing

    func updateFormField(name: String, changes: PDFFormFieldChanges, onDone: (() -> Void)? = nil) {
        guard !changes.isEmpty else {
            onDone?()
            return
        }
        runEngineOp(title: "Update Field", onDone: onDone) { [engine] input, output in
            try engine.updateFormField(input: input, output: output, name: name, changes: changes)
        }
    }

    func deleteFormField(name: String, onDone: (() -> Void)? = nil) {
        runEngineOp(title: "Delete Field", onDone: onDone) { [engine] input, output in
            try engine.deleteFormField(input: input, output: output, name: name)
        }
    }

    /// Fill several fields at once: text → string, checkbox → `.bool`,
    /// radio/dropdown/list → option string ("" clears).
    func fillFormFields(_ values: [String: PDFFormFieldValue], onDone: (() -> Void)? = nil) {
        guard !values.isEmpty else {
            onDone?()
            return
        }
        let box = FormFillBox()
        runEngineOp(title: "Fill Form", onDone: { [weak self] in
            if let reply = box.reply, !reply.invalid.isEmpty || !reply.missing.isEmpty {
                let skipped = reply.invalid.count + reply.missing.count
                self?.toast("Filled \(reply.filled.count) field\(reply.filled.count == 1 ? "" : "s"); \(skipped) skipped")
            }
            onDone?()
        }) { [engine] input, output in
            box.reply = try engine.fillFormFields(input: input, output: output, values: values)
        }
    }
}

/// Carries an engine reply from the background work closure to `onDone`.
private final class FormReplyBox {
    var reply: PDFAddFormFieldReply?
}

private final class FormDetectBox {
    var reply: PDFDetectFormFieldsReply?
}

private final class FormFillBox {
    var reply: PDFFillFormFieldsReply?
}
