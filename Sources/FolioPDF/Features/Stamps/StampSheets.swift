import AppKit
import PDFKit
import SwiftUI
import UniformTypeIdentifiers

// MARK: - Watermark

struct WatermarkSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss

    private enum Kind: String, CaseIterable, Identifiable {
        case text = "Text", image = "Image"
        var id: String { rawValue }
    }

    @State private var kind: Kind = .text
    @State private var text = "CONFIDENTIAL"
    @State private var fontKey = "helv"
    @State private var fontSize: Double = 60
    @State private var color = Color(hex: 0xE5484D)
    @State private var imageURL: URL?
    @State private var imagePreview: NSImage?
    @State private var imageScale: Double = 0.5
    @State private var opacity: Double = 0.3
    @State private var rotation: Double = 45
    @State private var position: StampPosition = .center
    @State private var layer: WatermarkLayer = .over
    @State private var pages = PageRangeSelection()
    @State private var preview: StampPreviewPage?

    private static let presets = ["CONFIDENTIAL", "DRAFT", "COPY", "APPROVED", "سري"]

    private var trimmedText: String { text.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var pagesError: String? { pageSpecError(pages, pageCount: store.pageCount) }

    private var contentError: String? {
        switch kind {
        case .text: trimmedText.isEmpty ? "Enter the watermark text." : nil
        case .image: imageURL == nil ? "Choose an image to stamp." : nil
        }
    }

    var body: some View {
        ToolSheetScaffold(
            tool: .watermark,
            primaryTitle: "Add Watermark",
            canSubmit: contentError == nil && pagesError == nil,
            width: 760,
            onSubmit: submit
        ) {
            HStack(alignment: .top, spacing: Theme.Spacing.xl) {
                VStack(alignment: .leading, spacing: Theme.Spacing.l) {
                    contentSection
                    appearanceSection
                    placementSection
                    FormSection("Pages") {
                        PageRangePicker(selection: $pages, pageCount: store.pageCount)
                        if let pagesError { ValidationText(pagesError) }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                    Text("PREVIEW")
                        .font(Theme.Font.sectionLabel)
                        .tracking(0.6)
                        .foregroundStyle(Theme.textTertiary)
                    StampPreviewCanvas(page: preview, width: 230) { scale, size in
                        watermarkOverlay(scale: scale, pageSize: size)
                    }
                    Text("Page \(store.currentPageNumber) · approximate")
                        .font(Theme.Font.caption)
                        .foregroundStyle(Theme.textTertiary)
                }
                .frame(width: 230)
            }
        }
        .onAppear { preview = StampPreviewPage(store: store) }
    }

    // MARK: Sections

    private var contentSection: some View {
        FormSection("Content") {
            Picker("", selection: $kind) {
                ForEach(Kind.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            if kind == .text {
                TextField("Watermark text", text: $text, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...3)
                HStack(spacing: 6) {
                    ForEach(Self.presets, id: \.self) { preset in
                        Button(preset) { text = preset }
                            .buttonStyle(ToolChipStyle(isActive: trimmedText == preset))
                    }
                }
                FormRow("Font") {
                    Picker("", selection: $fontKey) {
                        Text("Helvetica").tag("helv")
                        Text("Times").tag("tiro")
                        Text("Courier").tag("cour")
                    }
                    .labelsHidden()
                    .frame(width: 140)
                }
                FormRow("Size") {
                    SliderWithValue(value: $fontSize, range: 12...160, step: 1) { "\(Int($0)) pt" }
                }
                FormRow("Color") {
                    ColorSwatchPicker(color: $color)
                }
                if trimmedText.unicodeScalars.contains(where: { $0.value > 0xFF }) {
                    Text("Non-Latin text is drawn with a matching system font (e.g. SF Arabic) with full shaping.")
                        .font(Theme.Font.caption)
                        .foregroundStyle(Theme.textTertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                HStack(spacing: Theme.Spacing.m) {
                    ZStack {
                        RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                            .fill(Theme.surface)
                        if let imagePreview {
                            Image(nsImage: imagePreview)
                                .resizable()
                                .scaledToFit()
                                .padding(4)
                        } else {
                            Image(systemName: "photo")
                                .foregroundStyle(Theme.textTertiary)
                        }
                    }
                    .frame(width: 56, height: 56)
                    .overlay(
                        RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                            .strokeBorder(Theme.separator, lineWidth: 1)
                    )
                    VStack(alignment: .leading, spacing: 4) {
                        Text(imageURL?.lastPathComponent ?? "No image selected")
                            .font(Theme.Font.body)
                            .foregroundStyle(imageURL == nil ? Theme.textTertiary : Theme.textPrimary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Button(imageURL == nil ? "Choose Image…" : "Replace…", action: chooseImage)
                            .buttonStyle(SecondaryButtonStyle())
                    }
                }
                FormRow("Width") {
                    SliderWithValue(value: $imageScale, range: 0.1...1, step: 0.05) { "\(Int(($0 * 100).rounded()))% of page" }
                }
            }
            if let contentError { ValidationText(contentError) }
        }
    }

    private var appearanceSection: some View {
        FormSection("Appearance") {
            FormRow("Opacity") {
                SliderWithValue(value: $opacity, range: 0.05...1, step: 0.05) { "\(Int(($0 * 100).rounded()))%" }
            }
            FormRow("Rotation") {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        ForEach([0.0, 45.0, 90.0, -45.0], id: \.self) { preset in
                            Button("\(Int(preset))°") { rotation = preset }
                                .buttonStyle(ToolChipStyle(isActive: rotation == preset))
                        }
                    }
                    SliderWithValue(value: $rotation, range: -180...180, step: 1) { "\(Int($0))°" }
                }
            }
            FormRow("Layer") {
                Picker("", selection: $layer) {
                    ForEach(WatermarkLayer.allCases) { Text($0.title).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 240)
            }
        }
    }

    private var placementSection: some View {
        FormSection("Position", footnote: position == .tile ? "Repeats the watermark across the whole page." : nil) {
            HStack(alignment: .center, spacing: Theme.Spacing.l) {
                AnchorPicker(selection: $position, allowed: StampPosition.grid)
                    .opacity(position == .tile ? 0.45 : 1)
                VStack(alignment: .leading, spacing: 6) {
                    Text(position.title)
                        .font(Theme.Font.headline)
                        .foregroundStyle(Theme.textPrimary)
                    Button {
                        position = position == .tile ? .center : .tile
                    } label: {
                        Label("Tile across page", systemImage: "square.grid.3x3")
                    }
                    .buttonStyle(ToolChipStyle(isActive: position == .tile))
                }
            }
        }
    }

    // MARK: Preview

    private var previewFont: NSFont {
        let size = CGFloat(fontSize)
        switch fontKey {
        case "tiro": return NSFont(name: "Times New Roman", size: size) ?? .systemFont(ofSize: size)
        case "cour": return NSFont(name: "Courier", size: size) ?? .monospacedSystemFont(ofSize: size, weight: .regular)
        default: return NSFont(name: "Helvetica", size: size) ?? .systemFont(ofSize: size)
        }
    }

    @ViewBuilder
    private func watermarkOverlay(scale: CGFloat, pageSize: CGSize) -> some View {
        let stampSize = watermarkSize(pageSize: pageSize)
        let centers = stampCenters(pageSize: pageSize, stampSize: stampSize, rotation: rotation, position: position)
        ZStack(alignment: .topLeading) {
            ForEach(Array(centers.enumerated()), id: \.offset) { _, center in
                watermarkMark(scale: scale, stampSize: stampSize)
                    .rotationEffect(.degrees(-rotation))
                    .position(x: center.x * scale, y: center.y * scale)
            }
        }
        .frame(width: pageSize.width * scale, height: pageSize.height * scale, alignment: .topLeading)
        .opacity(opacity)
        .allowsHitTesting(false)
    }

    @ViewBuilder
    private func watermarkMark(scale: CGFloat, stampSize: CGSize) -> some View {
        switch kind {
        case .text:
            Text(trimmedText.isEmpty ? " " : trimmedText)
                .font(Font(previewFont.withSize(max(1, previewFont.pointSize * scale)) as CTFont))
                .multilineTextAlignment(.center)
                .foregroundStyle(color)
                .fixedSize()
        case .image:
            if let imagePreview {
                Image(nsImage: imagePreview)
                    .resizable()
                    .frame(width: stampSize.width * scale, height: stampSize.height * scale)
            }
        }
    }

    /// Unrotated stamp size in page points.
    private func watermarkSize(pageSize: CGSize) -> CGSize {
        switch kind {
        case .text:
            return measuredTextSize(trimmedText.isEmpty ? " " : trimmedText, font: previewFont)
        case .image:
            let width = pageSize.width * imageScale
            let aspect = imagePreview.map { $0.size.height / max($0.size.width, 1) } ?? 0.6
            return CGSize(width: width, height: width * aspect)
        }
    }

    // MARK: Actions

    private func chooseImage() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.png, .jpeg, .tiff, .gif, .bmp, .heic]
        panel.allowsMultipleSelection = false
        panel.prompt = "Use Image"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        imageURL = url
        imagePreview = NSImage(contentsOf: url)
    }

    private func submit() {
        var options = WatermarkOptions()
        switch kind {
        case .text: options.content = .text(trimmedText)
        case .image:
            guard let imageURL else { return }
            options.content = .image(imageURL)
        }
        options.font = fontKey
        options.fontSize = fontSize
        options.colorHex = color.engineHex
        options.opacity = opacity
        options.rotation = rotation
        options.position = position
        options.layer = layer
        options.scale = imageScale
        options.pages = pages.spec(currentPage: store.currentPageNumber)
        store.applyWatermark(options)
        dismiss()
    }
}

// MARK: - Header & Footer

struct HeaderFooterSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss

    @State private var slots: [HeaderFooterSlot: String] = [.footerCenter: "Page {page} of {pages}"]
    @State private var focusedSlot: HeaderFooterSlot = .footerCenter
    @FocusState private var focus: HeaderFooterSlot?
    @State private var fontSize: Double = 10
    @State private var color = Color(hex: 0x6B7280)
    @State private var margin: Double = 28
    @State private var startNumber = 1
    @State private var dateFormat = DateStyle.iso
    @State private var pages = PageRangeSelection()
    @State private var preview: StampPreviewPage?

    private enum DateStyle: String, CaseIterable, Identifiable {
        case iso = "%Y-%m-%d"
        case dayFirst = "%d/%m/%Y"
        case monthFirst = "%m/%d/%Y"
        case long = "%B %d, %Y"
        var id: String { rawValue }

        var swiftFormat: String {
            switch self {
            case .iso: "yyyy-MM-dd"
            case .dayFirst: "dd/MM/yyyy"
            case .monthFirst: "MM/dd/yyyy"
            case .long: "MMMM dd, yyyy"
            }
        }

        var sample: String {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.dateFormat = swiftFormat
            return formatter.string(from: Date())
        }
    }

    private static let tokens = ["{page}", "{pages}", "{date}", "{title}", "{filename}"]

    private var hasContent: Bool {
        slots.values.contains { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    }
    private var pagesError: String? { pageSpecError(pages, pageCount: store.pageCount) }

    var body: some View {
        ToolSheetScaffold(
            tool: .headerFooter,
            primaryTitle: "Add Header & Footer",
            canSubmit: hasContent && pagesError == nil,
            width: 640,
            onSubmit: submit
        ) {
            FormSection("Text", footnote: "Click a slot, then insert a token. Tokens are filled in per page.") {
                slotRow(header: true)
                pageBody
                slotRow(header: false)
                HStack(spacing: 6) {
                    Text("Insert")
                        .font(Theme.Font.caption)
                        .foregroundStyle(Theme.textTertiary)
                    ForEach(Self.tokens, id: \.self) { token in
                        Button(token) { insert(token) }
                            .buttonStyle(ToolChipStyle())
                    }
                }
                if !hasContent { ValidationText("Fill in at least one slot.") }
            }

            HStack(alignment: .top, spacing: Theme.Spacing.l) {
                FormSection("Format") {
                    FormRow("Size", labelWidth: 70) {
                        SliderWithValue(value: $fontSize, range: 6...24, step: 0.5) { String(format: "%g pt", $0) }
                    }
                    FormRow("Color", labelWidth: 70) {
                        ColorSwatchPicker(color: $color)
                    }
                    FormRow("Margin", labelWidth: 70) {
                        SliderWithValue(value: $margin, range: 8...72, step: 1) { "\(Int($0)) pt" }
                    }
                    FormRow("Start at", labelWidth: 70) {
                        HStack(spacing: 6) {
                            TextField("", value: $startNumber, format: .number)
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 64)
                            Stepper("", value: $startNumber, in: 0...99_999)
                                .labelsHidden()
                        }
                    }
                    FormRow("Date", labelWidth: 70) {
                        Picker("", selection: $dateFormat) {
                            ForEach(DateStyle.allCases) { Text($0.sample).tag($0) }
                        }
                        .labelsHidden()
                        .frame(width: 170)
                    }
                }
                VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                    Text("PREVIEW")
                        .font(Theme.Font.sectionLabel)
                        .tracking(0.6)
                        .foregroundStyle(Theme.textTertiary)
                    StampPreviewCanvas(page: preview, width: 170) { scale, size in
                        slotOverlay(scale: scale, pageSize: size)
                    }
                }
                .frame(width: 170)
            }

            FormSection("Pages", footnote: "Numbering starts at the first selected page.") {
                PageRangePicker(selection: $pages, pageCount: store.pageCount)
                if let pagesError { ValidationText(pagesError) }
            }
        }
        .onAppear { preview = StampPreviewPage(store: store) }
        .onChange(of: focus) { _, newValue in
            if let newValue { focusedSlot = newValue }
        }
    }

    // MARK: Slot editor

    private func slotRow(header: Bool) -> some View {
        HStack(spacing: Theme.Spacing.s) {
            ForEach(HeaderFooterSlot.allCases.filter { $0.isHeader == header }) { slot in
                VStack(alignment: .leading, spacing: 3) {
                    Text("\(header ? "Header" : "Footer") · \(slot.title)")
                        .font(Theme.Font.micro)
                        .foregroundStyle(focusedSlot == slot ? Theme.accent : Theme.textTertiary)
                    TextField(slot.title, text: binding(for: slot))
                        .textFieldStyle(.roundedBorder)
                        .multilineTextAlignment(alignment(for: slot))
                        .focused($focus, equals: slot)
                }
            }
        }
    }

    /// Faux page body between the header and footer rows.
    private var pageBody: some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(0..<3, id: \.self) { row in
                RoundedRectangle(cornerRadius: 2)
                    .fill(Theme.outline.opacity(0.5))
                    .frame(height: 4)
                    .frame(maxWidth: row == 2 ? 220 : .infinity, alignment: .leading)
            }
        }
        .padding(.vertical, 6)
        .padding(.horizontal, Theme.Spacing.m)
    }

    private func binding(for slot: HeaderFooterSlot) -> Binding<String> {
        Binding(
            get: { slots[slot] ?? "" },
            set: { slots[slot] = $0 }
        )
    }

    private func alignment(for slot: HeaderFooterSlot) -> TextAlignment {
        switch slot {
        case .headerLeft, .footerLeft: .leading
        case .headerCenter, .footerCenter: .center
        case .headerRight, .footerRight: .trailing
        }
    }

    private func insert(_ token: String) {
        let current = slots[focusedSlot] ?? ""
        let needsSpace = !(current.isEmpty || current.hasSuffix(" "))
        slots[focusedSlot] = current + (needsSpace ? " " : "") + token
        focus = focusedSlot
    }

    // MARK: Preview

    private func resolved(_ template: String) -> String {
        let selected = pageIndices(pages, pageCount: store.pageCount, currentPage: store.currentPageNumber)
        let currentIndex = store.currentPageNumber - 1
        let order = selected.firstIndex(of: currentIndex) ?? 0
        let values: [String: String] = [
            "{pages}": String(startNumber + max(selected.count, 1) - 1),
            "{page}": String(startNumber + order),
            "{date}": dateFormat.sample,
            "{title}": store.stampsDocumentTitle,
            "{filename}": store.stampsDocumentFilename
        ]
        var text = template
        for (token, value) in values {
            text = text.replacingOccurrences(of: token, with: value)
        }
        return text
    }

    private func slotOverlay(scale: CGFloat, pageSize: CGSize) -> some View {
        let font = NSFont(name: "Helvetica", size: CGFloat(fontSize)) ?? .systemFont(ofSize: CGFloat(fontSize))
        let included = pageIndices(pages, pageCount: store.pageCount, currentPage: store.currentPageNumber)
            .contains(store.currentPageNumber - 1)
        return ZStack(alignment: .topLeading) {
            if included {
                ForEach(HeaderFooterSlot.allCases) { slot in
                    let text = resolved(slots[slot] ?? "").trimmingCharacters(in: .whitespaces)
                    if !text.isEmpty {
                        let size = measuredTextSize(text, font: font)
                        let center = anchoredCenter(
                            pageSize: pageSize, boxSize: size,
                            unitX: slotUnitX(slot), unitY: slot.isHeader ? 0 : 1, margin: margin
                        )
                        Text(text)
                            .font(Font(font.withSize(max(1, font.pointSize * scale)) as CTFont))
                            .foregroundStyle(color)
                            .fixedSize()
                            .position(x: center.x * scale, y: center.y * scale)
                    }
                }
            }
        }
        .frame(width: pageSize.width * scale, height: pageSize.height * scale, alignment: .topLeading)
        .allowsHitTesting(false)
    }

    private func slotUnitX(_ slot: HeaderFooterSlot) -> Double {
        switch slot {
        case .headerLeft, .footerLeft: 0
        case .headerCenter, .footerCenter: 0.5
        case .headerRight, .footerRight: 1
        }
    }

    private func submit() {
        var options = HeaderFooterOptions()
        options.slots = slots
        options.fontSize = fontSize
        options.colorHex = color.engineHex
        options.margin = margin
        options.startNumber = startNumber
        options.dateFormat = dateFormat.rawValue
        options.pages = pages.spec(currentPage: store.currentPageNumber)
        store.applyHeaderFooter(options)
        dismiss()
    }
}

// MARK: - Bates numbering

struct BatesSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss

    @State private var prefix = "ABC"
    @State private var suffix = ""
    @State private var start = 1
    @State private var digits = 6
    @State private var position: StampPosition = .bottomRight
    @State private var fontSize: Double = 10
    @State private var color = Color(hex: 0x16171D)
    @State private var pages = PageRangeSelection()

    private var options: BatesOptions {
        var options = BatesOptions()
        options.prefix = prefix
        options.suffix = suffix
        options.start = max(0, start)
        options.digits = digits
        options.position = position
        options.fontSize = fontSize
        options.colorHex = color.engineHex
        options.pages = pages.spec(currentPage: store.currentPageNumber)
        return options
    }

    private var selectedCount: Int {
        pageIndices(pages, pageCount: store.pageCount, currentPage: store.currentPageNumber).count
    }
    private var pagesError: String? { pageSpecError(pages, pageCount: store.pageCount) }
    private var startError: String? { start < 0 ? "Start number can't be negative." : nil }

    var body: some View {
        ToolSheetScaffold(
            tool: .bates,
            primaryTitle: "Add Bates Numbers",
            canSubmit: pagesError == nil && startError == nil && selectedCount > 0,
            onSubmit: submit
        ) {
            sampleCard

            FormSection("Identifier") {
                FormRow("Prefix") {
                    TextField("e.g. ABC", text: $prefix)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 180)
                }
                FormRow("Start number") {
                    HStack(spacing: 6) {
                        TextField("", value: $start, format: .number.grouping(.never))
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 100)
                        Stepper("", value: $start, in: 0...999_999_999)
                            .labelsHidden()
                    }
                }
                FormRow("Digits") {
                    Stepper(value: $digits, in: 1...12) {
                        Text("\(digits)")
                            .font(Theme.Font.body.monospacedDigit())
                            .frame(width: 24, alignment: .leading)
                    }
                }
                FormRow("Suffix") {
                    TextField("optional", text: $suffix)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 180)
                }
                if let startError { ValidationText(startError) }
            }

            FormSection("Placement & style") {
                HStack(alignment: .center, spacing: Theme.Spacing.l) {
                    AnchorPicker(selection: $position, allowed: StampPosition.edgeSlots)
                    VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                        Text(position.title)
                            .font(Theme.Font.headline)
                            .foregroundStyle(Theme.textPrimary)
                        FormRow("Size", labelWidth: 44) {
                            SliderWithValue(value: $fontSize, range: 6...20, step: 0.5) { String(format: "%g pt", $0) }
                        }
                        FormRow("Color", labelWidth: 44) {
                            ColorSwatchPicker(color: $color)
                        }
                    }
                }
            }

            FormSection("Pages") {
                PageRangePicker(selection: $pages, pageCount: store.pageCount)
                if let pagesError { ValidationText(pagesError) }
            }
        }
    }

    private var sampleCard: some View {
        let first = options.identifier(max(0, start))
        let last = options.identifier(max(0, start) + max(selectedCount, 1) - 1)
        return HStack(spacing: Theme.Spacing.m) {
            VStack(alignment: .leading, spacing: 4) {
                Text("FIRST PAGE")
                    .font(Theme.Font.sectionLabel)
                    .tracking(0.6)
                    .foregroundStyle(Theme.textTertiary)
                Text(first)
                    .font(.system(size: 20, weight: .semibold, design: .monospaced))
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                    .textSelection(.enabled)
            }
            Spacer(minLength: Theme.Spacing.s)
            if selectedCount > 1 {
                Image(systemName: "arrow.right")
                    .foregroundStyle(Theme.textTertiary)
                Spacer(minLength: Theme.Spacing.s)
                VStack(alignment: .trailing, spacing: 4) {
                    Text("LAST PAGE")
                        .font(Theme.Font.sectionLabel)
                        .tracking(0.6)
                        .foregroundStyle(Theme.textTertiary)
                    Text(last)
                        .font(.system(size: 20, weight: .semibold, design: .monospaced))
                        .foregroundStyle(Theme.textSecondary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.6)
                }
            }
        }
        .padding(Theme.Spacing.l)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                .fill(Theme.accentSoft)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                .strokeBorder(Theme.accent.opacity(0.25), lineWidth: 1)
        )
    }

    private func submit() {
        store.applyBates(options)
        dismiss()
    }
}

// MARK: - Split

struct SplitSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss

    @State private var mode: SplitMode = .every
    @State private var ranges = ""
    @State private var every = 1
    @State private var maxMB: Double = 10
    @State private var outputDirectory: URL?

    private var stem: String {
        (store.stampsDocumentFilename as NSString).deletingPathExtension
    }

    private var defaultDirectory: URL {
        store.defaultSplitParentDirectory.appendingPathComponent("\(stem) parts", isDirectory: true)
    }

    private var targetDirectory: URL { outputDirectory ?? defaultDirectory }

    private var bookmarks: [(title: String, page: Int)] { store.topLevelBookmarks }

    /// Parsed parts, or an error message for inline validation.
    private var plan: (parts: [ClosedRange<Int>], error: String?) {
        let total = store.pageCount
        guard total > 0 else { return ([], "Open a PDF first.") }
        switch mode {
        case .ranges:
            return parseSplitRanges(ranges, total: total)
        case .every:
            guard every >= 1 else { return ([], "Enter a page count of 1 or more.") }
            let parts = stride(from: 1, through: total, by: every).map { $0...min($0 + every - 1, total) }
            return (parts, nil)
        case .bookmarks:
            guard !bookmarks.isEmpty else {
                return ([], "This PDF has no top-level bookmarks. Add some in Bookmarks, or pick another mode.")
            }
            var starts = bookmarks.map(\.page)
            if starts.first != 1 { starts.insert(1, at: 0) }
            let parts = starts.enumerated().map { index, start in
                start...(index + 1 < starts.count ? starts[index + 1] - 1 : total)
            }
            return (parts, nil)
        case .size:
            guard maxMB > 0 else { return ([], "Enter a size above 0 MB.") }
            return ([], nil)
        }
    }

    private var estimatedSizeParts: Int {
        guard let url = store.currentDocumentURL,
              let bytes = (try? url.resourceValues(forKeys: [.fileSizeKey]))?.fileSize,
              maxMB > 0 else { return 1 }
        let parts = Int((Double(bytes) / (maxMB * 1_048_576)).rounded(.up))
        return min(max(parts, 1), max(store.pageCount, 1))
    }

    private var partsLabel: String {
        if mode == .size {
            let count = estimatedSizeParts
            return "about \(count) file\(count == 1 ? "" : "s")"
        }
        let count = plan.parts.count
        return "\(count) file\(count == 1 ? "" : "s")"
    }

    var body: some View {
        ToolSheetScaffold(
            tool: .split,
            primaryTitle: "Split",
            canSubmit: plan.error == nil && (mode == .size || !plan.parts.isEmpty),
            width: 560,
            onSubmit: submit
        ) {
            modePicker

            FormSection(mode.title) {
                modeInputs
                if let error = plan.error {
                    ValidationText(error)
                }
            }

            FormSection("Result") {
                HStack(spacing: Theme.Spacing.s) {
                    Image(systemName: "doc.on.doc")
                        .foregroundStyle(Theme.accent)
                    Text(store.pageCount > 0 ? "\(store.pageCount) pages into" : "Nothing to split")
                        .font(Theme.Font.body)
                        .foregroundStyle(Theme.textSecondary)
                    if plan.error == nil {
                        Badge(text: partsLabel)
                    }
                    Spacer()
                }
                if plan.error == nil, let sample = sampleFilename {
                    Text(sample)
                        .font(Theme.Font.caption.monospaced())
                        .foregroundStyle(Theme.textTertiary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                Divider().overlay(Theme.separator)
                HStack(spacing: Theme.Spacing.s) {
                    Image(systemName: "folder.fill")
                        .foregroundStyle(Theme.info)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(targetDirectory.lastPathComponent)
                            .font(Theme.Font.body.weight(.medium))
                            .foregroundStyle(Theme.textPrimary)
                            .lineLimit(1)
                        Text(targetDirectory.deletingLastPathComponent().path(percentEncoded: false))
                            .font(Theme.Font.caption)
                            .foregroundStyle(Theme.textTertiary)
                            .lineLimit(1)
                            .truncationMode(.head)
                    }
                    Spacer()
                    Button("Choose…", action: chooseFolder)
                        .buttonStyle(SecondaryButtonStyle())
                }
            }
            InfoBanner(kind: .info, text: "Your open document isn't changed. The new files open in Finder when they're ready.")
        }
    }

    // MARK: Pieces

    private var modePicker: some View {
        HStack(spacing: Theme.Spacing.s) {
            ForEach(SplitMode.allCases) { option in
                Button {
                    mode = option
                } label: {
                    VStack(spacing: 6) {
                        IconTile(systemImage: option.systemImage, tint: mode == option ? Theme.accent : Theme.textSecondary, size: 30)
                        Text(option.title)
                            .font(Theme.Font.callout.weight(.medium))
                            .foregroundStyle(mode == option ? Theme.accent : Theme.textPrimary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.85)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, Theme.Spacing.m)
                    .background(
                        RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                            .fill(mode == option ? Theme.accentSoft : Theme.surfaceRaised.opacity(0.6))
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                            .strokeBorder(mode == option ? Theme.accent.opacity(0.45) : Theme.separator, lineWidth: 1)
                    )
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
    }

    @ViewBuilder
    private var modeInputs: some View {
        switch mode {
        case .ranges:
            FormRow("Ranges") {
                TextField("e.g. 1-3, 4-6, 7-", text: $ranges)
                    .textFieldStyle(.roundedBorder)
            }
            Text("Each comma-separated range becomes one file. “7-” runs to the last page.")
                .font(Theme.Font.caption)
                .foregroundStyle(Theme.textTertiary)
        case .every:
            FormRow("Pages per file") {
                HStack(spacing: 6) {
                    TextField("", value: $every, format: .number)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 64)
                    Stepper("", value: $every, in: 1...max(store.pageCount, 1))
                        .labelsHidden()
                }
            }
        case .bookmarks:
            if !bookmarks.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(Array(zip(plan.parts.indices, plan.parts)), id: \.0) { index, range in
                        HStack(spacing: Theme.Spacing.s) {
                            Text(String(format: "%02d", index + 1))
                                .font(Theme.Font.caption.monospacedDigit())
                                .foregroundStyle(Theme.textTertiary)
                            Text(bookmarkTitle(forPartStartingAt: range.lowerBound))
                                .font(Theme.Font.body)
                                .foregroundStyle(Theme.textPrimary)
                                .lineLimit(1)
                            Spacer()
                            Text(range.count == 1 ? "p. \(range.lowerBound)" : "pp. \(range.lowerBound)–\(range.upperBound)")
                                .font(Theme.Font.caption.monospacedDigit())
                                .foregroundStyle(Theme.textSecondary)
                        }
                    }
                }
            }
        case .size:
            FormRow("Max size per file") {
                HStack(spacing: 6) {
                    TextField("", value: $maxMB, format: .number.precision(.fractionLength(0...1)))
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 70)
                    Text("MB")
                        .font(Theme.Font.body)
                        .foregroundStyle(Theme.textSecondary)
                    ForEach([5.0, 10.0, 25.0], id: \.self) { preset in
                        Button("\(Int(preset)) MB") { maxMB = preset }
                            .buttonStyle(ToolChipStyle(isActive: maxMB == preset))
                    }
                }
            }
            Text("Pages are kept in order; a single page larger than the limit gets a file of its own.")
                .font(Theme.Font.caption)
                .foregroundStyle(Theme.textTertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func bookmarkTitle(forPartStartingAt page: Int) -> String {
        bookmarks.first { $0.page == page }?.title ?? "Front matter"
    }

    private var sampleFilename: String? {
        let safeStem = stem.safeFilename
        switch mode {
        case .bookmarks:
            guard let first = plan.parts.first else { return nil }
            return "\(safeStem)_01-\(bookmarkTitle(forPartStartingAt: first.lowerBound).safeFilename).pdf, …"
        default:
            return "\(safeStem)_part-01.pdf, \(safeStem)_part-02.pdf, …"
        }
    }

    // MARK: Actions

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Save Parts Here"
        panel.message = "Choose where to save the split files"
        panel.directoryURL = store.defaultSplitParentDirectory
        guard panel.runModal() == .OK, let url = panel.url else { return }
        outputDirectory = url
    }

    private func submit() {
        var options = SplitOptions()
        options.mode = mode
        options.ranges = ranges
        options.every = every
        options.maxMB = maxMB
        options.basename = stem
        store.splitDocument(options, into: targetDirectory)
        dismiss()
    }
}

// MARK: - Shared pieces

/// Small red validation line under a field.
private struct ValidationText: View {
    let message: String
    init(_ message: String) { self.message = message }

    var body: some View {
        Label(message, systemImage: "exclamationmark.circle.fill")
            .font(Theme.Font.caption)
            .foregroundStyle(Theme.danger)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// Slider with a fixed-width value label on the right.
private struct SliderWithValue: View {
    @Binding var value: Double
    let range: ClosedRange<Double>
    var step: Double = 1
    let label: (Double) -> String

    var body: some View {
        HStack(spacing: Theme.Spacing.s) {
            Slider(value: $value, in: range, step: step)
            Text(label(value))
                .font(Theme.Font.callout.monospacedDigit())
                .foregroundStyle(Theme.textSecondary)
                .frame(width: 84, alignment: .trailing)
        }
    }
}

/// Mini page with clickable anchor dots. Positions not in `allowed` are hidden.
private struct AnchorPicker: View {
    @Binding var selection: StampPosition
    let allowed: [StampPosition]

    var body: some View {
        VStack(spacing: 0) {
            ForEach(0..<3, id: \.self) { row in
                HStack(spacing: 0) {
                    ForEach(0..<3, id: \.self) { column in
                        cell(StampPosition.grid[row * 3 + column])
                    }
                }
            }
        }
        .frame(width: 76, height: 100)
        .padding(4)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.small, style: .continuous)
                .fill(Theme.surface)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.small, style: .continuous)
                .strokeBorder(Theme.outline, lineWidth: 1)
        )
    }

    @ViewBuilder
    private func cell(_ position: StampPosition) -> some View {
        let isAllowed = allowed.contains(position)
        let isSelected = selection == position
        Button {
            selection = position
        } label: {
            ZStack {
                Color.clear
                if isAllowed {
                    RoundedRectangle(cornerRadius: 3, style: .continuous)
                        .fill(isSelected ? Theme.accent : Theme.outline)
                        .frame(width: isSelected ? 16 : 10, height: isSelected ? 8 : 6)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!isAllowed)
        .help(isAllowed ? position.title : "")
    }
}

/// Current page rendered once for the live previews.
struct StampPreviewPage {
    let image: NSImage
    /// Visible page size in points (rotation applied).
    let size: CGSize

    init?(store: AppStore) {
        guard let document = store.document,
              let page = document.page(at: max(0, store.currentPageNumber - 1)) else { return nil }
        let bounds = page.bounds(for: .cropBox).size
        let quarterTurns = ((page.rotation % 360) + 360) % 360 / 90
        size = quarterTurns % 2 == 0 ? bounds : CGSize(width: bounds.height, height: bounds.width)
        image = page.thumbnail(of: NSSize(width: 520, height: 520), for: .cropBox)
    }
}

/// Page thumbnail with an overlay drawn in page points × `scale`.
private struct StampPreviewCanvas<Overlay: View>: View {
    let page: StampPreviewPage?
    let width: CGFloat
    @ViewBuilder let overlay: (_ scale: CGFloat, _ pageSize: CGSize) -> Overlay

    var body: some View {
        if let page, page.size.width > 0, page.size.height > 0 {
            let scale = width / page.size.width
            let height = page.size.height * scale
            ZStack(alignment: .topLeading) {
                Image(nsImage: page.image)
                    .resizable()
                    .interpolation(.high)
                    .frame(width: width, height: height)
                overlay(scale, page.size)
            }
            .frame(width: width, height: height)
            .clipped()
            .background(Color.white)
            .overlay(Rectangle().strokeBorder(Theme.separator, lineWidth: 1))
            .shadow(color: Theme.pageShadow, radius: 6, y: 2)
        } else {
            RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                .fill(Theme.surfaceRaised)
                .frame(width: width, height: width * 1.3)
                .overlay(
                    Text("No preview")
                        .font(Theme.Font.caption)
                        .foregroundStyle(Theme.textTertiary)
                )
        }
    }
}

// MARK: - Geometry & validation helpers (mirror Engine/ext/stamps.py)

private func measuredTextSize(_ text: String, font: NSFont) -> CGSize {
    let lines = text.components(separatedBy: "\n")
    let width = lines.map { ($0 as NSString).size(withAttributes: [.font: font]).width }.max() ?? 0
    return CGSize(width: width, height: CGFloat(lines.count) * font.pointSize * 1.2)
}

private func rotatedSize(_ size: CGSize, degrees: Double) -> CGSize {
    let radians = degrees * .pi / 180
    let c = abs(cos(radians)), s = abs(sin(radians))
    return CGSize(width: size.width * c + size.height * s, height: size.width * s + size.height * c)
}

/// Centre of a box anchored at (unitX, unitY) with the engine's margin rules.
private func anchoredCenter(pageSize: CGSize, boxSize: CGSize, unitX: Double, unitY: Double, margin: Double) -> CGPoint {
    let x = margin + (pageSize.width - 2 * margin - boxSize.width) * unitX + boxSize.width / 2
    let y = margin + (pageSize.height - 2 * margin - boxSize.height) * unitY + boxSize.height / 2
    return CGPoint(x: x, y: y)
}

/// Watermark centres in page points (one for anchors, a brick grid for tile).
private func stampCenters(pageSize: CGSize, stampSize: CGSize, rotation: Double, position: StampPosition) -> [CGPoint] {
    let box = rotatedSize(stampSize, degrees: rotation)
    guard position == .tile else {
        let margin = min(36, pageSize.width * 0.06, pageSize.height * 0.06)
        return [anchoredCenter(pageSize: pageSize, boxSize: box, unitX: position.unitX, unitY: position.unitY, margin: margin)]
    }
    var stepX = box.width + max(24, box.width * 0.3)
    var stepY = box.height + max(24, box.height * 0.5)
    guard stepX > 0, stepY > 0 else { return [] }
    var cols = Int((pageSize.width / stepX).rounded(.up)) + 2
    var rows = Int((pageSize.height / stepY).rounded(.up)) + 2
    while cols * rows > 400 {
        stepX *= 1.25
        stepY *= 1.25
        cols = Int((pageSize.width / stepX).rounded(.up)) + 2
        rows = Int((pageSize.height / stepY).rounded(.up)) + 2
    }
    let originX = pageSize.width / 2 - box.width / 2 - stepX * Double(cols / 2)
    let originY = pageSize.height / 2 - box.height / 2 - stepY * Double(rows / 2)
    let page = CGRect(origin: .zero, size: pageSize)
    var centers: [CGPoint] = []
    for row in 0..<rows {
        let shift = row % 2 == 1 ? stepX / 2 : 0
        for col in 0...cols {
            let x0 = originX + Double(col) * stepX + shift - stepX / 2
            let y0 = originY + Double(row) * stepY
            let rect = CGRect(x: x0, y: y0, width: box.width, height: box.height)
            if rect.intersects(page) {
                centers.append(CGPoint(x: rect.midX, y: rect.midY))
            }
        }
    }
    return centers
}

/// 0-based page indices for a selection, or [] when the custom spec is invalid.
private func pageIndices(_ selection: PageRangeSelection, pageCount: Int, currentPage: Int) -> [Int] {
    switch selection.mode {
    case .all: return Array(0..<max(pageCount, 0))
    case .current: return [max(currentPage - 1, 0)]
    case .custom:
        let spec = selection.custom.trimmingCharacters(in: .whitespaces)
        if spec.isEmpty || spec.lowercased() == "all" { return Array(0..<max(pageCount, 0)) }
        guard parsePageSpec(spec, pageCount: pageCount).error == nil else { return [] }
        return parsePageSpec(spec, pageCount: pageCount).pages
    }
}

private func pageSpecError(_ selection: PageRangeSelection, pageCount: Int) -> String? {
    guard selection.mode == .custom else { return nil }
    let spec = selection.custom.trimmingCharacters(in: .whitespaces)
    if spec.isEmpty || spec.lowercased() == "all" { return nil }
    return parsePageSpec(spec, pageCount: pageCount).error
}

/// Mirrors the engine's parse_page_spec: "1-3, 5, 8-10".
private func parsePageSpec(_ spec: String, pageCount: Int) -> (pages: [Int], error: String?) {
    var pages = Set<Int>()
    for chunk in spec.split(separator: ",") {
        let part = chunk.trimmingCharacters(in: .whitespaces)
        if part.isEmpty { continue }
        let bounds = part.split(separator: "-", omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: .whitespaces) }
        guard bounds.count <= 2, let first = Int(bounds[0]), let last = Int(bounds.last ?? "") else {
            return ([], "“\(part)” isn't a page or range like 2 or 4-7.")
        }
        let low = min(first, last), high = max(first, last)
        guard low >= 1, high <= pageCount else {
            return ([], "Pages must be between 1 and \(pageCount).")
        }
        for page in low...high { pages.insert(page - 1) }
    }
    return pages.isEmpty ? ([], "Enter at least one page.") : (pages.sorted(), nil)
}

/// Mirrors split-advanced --ranges: "1-3, 4-6, 7-" (open ends allowed).
private func parseSplitRanges(_ spec: String, total: Int) -> (parts: [ClosedRange<Int>], error: String?) {
    var parts: [ClosedRange<Int>] = []
    for chunk in spec.split(separator: ",") {
        let item = chunk.trimmingCharacters(in: .whitespaces)
        if item.isEmpty { continue }
        let bounds = item.split(separator: "-", omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: .whitespaces) }
        var low: Int?
        var high: Int?
        if bounds.count == 1 {
            low = Int(bounds[0])
            high = low
        } else if bounds.count == 2 {
            low = bounds[0].isEmpty ? 1 : Int(bounds[0])
            high = bounds[1].isEmpty ? total : Int(bounds[1])
        }
        guard let start = low, let end = high else {
            return ([], "“\(item)” isn't a range like 1-3 or 7-.")
        }
        let first = min(start, end), last = max(start, end)
        guard first >= 1, last <= total else {
            return ([], "“\(item)” is outside pages 1–\(total).")
        }
        parts.append(first...last)
    }
    return parts.isEmpty ? ([], "Enter one or more ranges, e.g. 1-3, 4-6.") : (parts, nil)
}
