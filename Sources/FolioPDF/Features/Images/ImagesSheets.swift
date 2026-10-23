import AppKit
import SwiftUI

/// Images tool: browse the image placements on the current page (or the
/// whole document), then replace, extract, delete, rotate, or reposition ONE
/// placement. Every edit is a new undoable version (AppStore+Images).
struct ImagesSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss

    private enum Scope: String, CaseIterable, Identifiable {
        case page, all
        var id: String { rawValue }
    }

    @State private var scope: Scope = .page
    @State private var page = 1
    @State private var placements: [EngineImagePlacement] = []
    @State private var thumbnails: [String: NSImage] = [:]
    @State private var inlineCount = 0
    @State private var loading = false
    @State private var loadError: String?
    @State private var selectedID: String?
    @State private var fitMode: ImageFitMode = .fit
    @State private var draft = GeometryDraft()
    /// After an edit, reselect the placement on this page closest to this rect.
    @State private var reselect: (page: Int, rect: CGRect)?

    private var selected: EngineImagePlacement? {
        placements.first { $0.id == selectedID }
    }

    private var geometryDirty: Bool {
        guard let selected else { return false }
        return !draft.rect.isApproximatelyEqual(to: selected.engineRect)
    }

    var body: some View {
        ToolSheetScaffold(
            tool: .images,
            primaryTitle: geometryDirty ? "Apply" : "Done",
            canSubmit: !store.busy && (!geometryDirty || draft.isValid),
            width: 700,
            onSubmit: submit
        ) {
            toolbar
            content
            if let selected {
                ImageDetailSection(
                    placement: selected,
                    thumbnail: thumbnails[selected.id],
                    fitMode: $fitMode,
                    busy: store.busy,
                    onReplace: { replace(selected) },
                    onExtract: { store.extractImageWithPanel(selected) },
                    onRotate: { rotate(selected, by: $0) },
                    onDelete: { delete(selected) }
                )
                ImageGeometrySection(
                    draft: $draft,
                    original: selected.engineRect,
                    pageBounds: store.imagePageBounds(page: selected.page)
                )
            }
        }
        .onAppear {
            page = store.currentPageNumber
            reload()
        }
        .onChange(of: scope) { _, _ in reload() }
        .onChange(of: selectedID) { _, _ in
            if let selected { draft = GeometryDraft(rect: selected.engineRect) }
        }
    }

    // MARK: Sections

    private var toolbar: some View {
        HStack(spacing: Theme.Spacing.m) {
            Picker("", selection: $scope) {
                Text("Page \(page)").tag(Scope.page)
                Text("All pages").tag(Scope.all)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 220)

            if !loading {
                Text(countLabel)
                    .font(Theme.Font.callout)
                    .foregroundStyle(Theme.textSecondary)
            }
            Spacer()
            Button {
                reload()
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(IconButtonStyle())
            .help("Refresh")
            .disabled(loading)
        }
    }

    @ViewBuilder
    private var content: some View {
        if loading && placements.isEmpty {
            HStack(spacing: Theme.Spacing.s) {
                ProgressView().controlSize(.small)
                Text("Finding images…")
                    .font(Theme.Font.callout)
                    .foregroundStyle(Theme.textSecondary)
            }
            .frame(maxWidth: .infinity, minHeight: 140)
        } else if let loadError {
            InfoBanner(kind: .danger, text: loadError)
        } else if placements.isEmpty {
            emptyState
        } else {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 132, maximum: 170), spacing: Theme.Spacing.m)],
                      spacing: Theme.Spacing.m) {
                ForEach(placements) { placement in
                    ImageTile(
                        placement: placement,
                        thumbnail: thumbnails[placement.id],
                        isSelected: placement.id == selectedID,
                        showsPage: scope == .all
                    )
                    .onTapGesture { selectedID = placement.id }
                }
            }
            .opacity(loading ? 0.5 : 1)
        }
    }

    private var emptyState: some View {
        VStack(spacing: Theme.Spacing.s) {
            IconTile(systemImage: "photo", tint: Theme.textTertiary, size: 44)
            Text(scope == .page ? "No images on page \(page)" : "This document has no images")
                .font(Theme.Font.headline)
                .foregroundStyle(Theme.textPrimary)
            if inlineCount > 0 {
                Text("\(inlineCount) small inline image\(inlineCount == 1 ? "" : "s") can't be edited here.")
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.textTertiary)
            }
            if scope == .page {
                Button("Show all pages") { scope = .all }
                    .buttonStyle(SecondaryButtonStyle())
                    .padding(.top, Theme.Spacing.xs)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 160)
    }

    private var countLabel: String {
        let n = placements.count
        let base = n == 1 ? "1 image" : "\(n) images"
        return inlineCount > 0 ? "\(base) · \(inlineCount) inline not editable" : base
    }

    // MARK: Actions

    private func reload() {
        loading = true
        loadError = nil
        store.loadImagePlacements(page: scope == .page ? page : nil) { result in
            loading = false
            switch result {
            case .success(let list):
                placements = list.images
                inlineCount = list.inlineImages ?? 0
                var images: [String: NSImage] = [:]
                for item in list.images {
                    if let path = item.thumbnail, let image = NSImage(contentsOfFile: path) {
                        images[item.id] = image
                    }
                }
                thumbnails = images
                restoreSelection()
            case .failure(let error):
                placements = []
                thumbnails = [:]
                loadError = "Couldn't read the images in this document.\n\(error.localizedDescription)"
            }
        }
    }

    private func restoreSelection() {
        if let target = reselect {
            reselect = nil
            let candidates = placements.filter { $0.page == target.page }
            selectedID = candidates.min {
                $0.engineRect.centerDistance(to: target.rect) < $1.engineRect.centerDistance(to: target.rect)
            }?.id
        } else if selectedID == nil || selected == nil {
            selectedID = placements.first?.id
        }
        if let selected { draft = GeometryDraft(rect: selected.engineRect) }
    }

    private func submit() {
        guard let selected, geometryDirty else {
            dismiss()
            return
        }
        store.transformImage(page: selected.page, xref: selected.xref,
                             placementIndex: selected.placementIndex, engineRect: draft.rect)
        dismiss()
    }

    private func replace(_ placement: EngineImagePlacement) {
        guard let url = store.chooseReplacementImage() else { return }
        reselect = (placement.page, placement.engineRect)
        store.replaceImage(page: placement.page, xref: placement.xref,
                           placementIndex: placement.placementIndex, imageURL: url, fit: fitMode) {
            reload()
        }
    }

    private func rotate(_ placement: EngineImagePlacement, by degrees: Double) {
        reselect = (placement.page, placement.engineRect)
        store.transformImage(page: placement.page, xref: placement.xref,
                             placementIndex: placement.placementIndex,
                             engineRect: placement.engineRect, rotate: degrees) {
            reload()
        }
    }

    private func delete(_ placement: EngineImagePlacement) {
        selectedID = nil
        store.deleteImage(page: placement.page, xref: placement.xref,
                          placementIndex: placement.placementIndex) {
            reload()
        }
    }
}

// MARK: - Geometry draft

private struct GeometryDraft: Equatable {
    var x: Double = 0
    var y: Double = 0
    var width: Double = 0
    var height: Double = 0
    var lockAspect = true
    /// width / height captured when the draft was loaded or the lock engaged.
    var aspect: Double = 1

    init() {}

    init(rect: CGRect) {
        x = Double(rect.minX).rounded(toPlaces: 2)
        y = Double(rect.minY).rounded(toPlaces: 2)
        width = Double(rect.width).rounded(toPlaces: 2)
        height = Double(rect.height).rounded(toPlaces: 2)
        aspect = rect.height > 0 ? Double(rect.width / rect.height) : 1
    }

    var rect: CGRect { CGRect(x: x, y: y, width: width, height: height) }
    var isValid: Bool { width >= 1 && height >= 1 && [x, y, width, height].allSatisfy(\.isFinite) }
}

// MARK: - Tile

private struct ImageTile: View {
    let placement: EngineImagePlacement
    let thumbnail: NSImage?
    let isSelected: Bool
    let showsPage: Bool
    @State private var hovering = false

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs + 2) {
            ZStack {
                RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                    .fill(Theme.surfaceRaised)
                if let thumbnail {
                    Image(nsImage: thumbnail)
                        .resizable()
                        .interpolation(.medium)
                        .aspectRatio(contentMode: .fit)
                        .padding(Theme.Spacing.s)
                        .shadow(color: Theme.pageShadow.opacity(0.5), radius: 2, y: 1)
                } else {
                    Image(systemName: "photo")
                        .font(.system(size: 22))
                        .foregroundStyle(Theme.textTertiary)
                }
            }
            .frame(height: 96)
            .overlay(alignment: .topTrailing) {
                if placement.occurrences > 1 {
                    Badge(text: "Used \(placement.occurrences)×", tint: Theme.info)
                        .padding(6)
                }
            }
            .overlay(alignment: .topLeading) {
                if showsPage {
                    Badge(text: "p. \(placement.page)", tint: Theme.textSecondary)
                        .padding(6)
                }
            }

            VStack(alignment: .leading, spacing: 1) {
                Text("\(placement.width) × \(placement.height) px")
                    .font(Theme.Font.callout.weight(.medium))
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(1)
                Text(subtitle)
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.textTertiary)
                    .lineLimit(1)
            }
            .padding(.horizontal, 2)
        }
        .padding(Theme.Spacing.s)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                .fill(isSelected ? Theme.accentSoft : (hovering ? Theme.surfaceRaised.opacity(0.6) : Color.clear))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                .strokeBorder(isSelected ? Theme.accent : Theme.separator, lineWidth: isSelected ? 2 : 1)
        )
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
        .help("\(placement.ext.uppercased()) · \(placement.colorspace) · \(Int(placement.displayWidth)) × \(Int(placement.displayHeight)) pt on the page")
    }

    private var subtitle: String {
        let dpi = Int(min(placement.dpiX, placement.dpiY).rounded())
        return "\(placement.ext.uppercased()) · \(dpi) dpi · \(ByteCountFormatter.string(fromByteCount: Int64(placement.bytes), countStyle: .file))"
    }
}

// MARK: - Detail & actions

private struct ImageDetailSection: View {
    let placement: EngineImagePlacement
    let thumbnail: NSImage?
    @Binding var fitMode: ImageFitMode
    let busy: Bool
    let onReplace: () -> Void
    let onExtract: () -> Void
    let onRotate: (Double) -> Void
    let onDelete: () -> Void

    var body: some View {
        FormSection("Selected image") {
            HStack(alignment: .top, spacing: Theme.Spacing.m) {
                preview
                VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                    Text("Image on page \(placement.page)")
                        .font(Theme.Font.headline)
                        .foregroundStyle(Theme.textPrimary)
                    Text(facts)
                        .font(Theme.Font.callout)
                        .foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                    badges
                }
                Spacer(minLength: 0)
            }

            if placement.occurrences > 1 {
                InfoBanner(kind: .info, text: "This image appears \(placement.occurrences) times (\(pagesText)). Changes here affect only this one placement.")
            }

            Divider().overlay(Theme.separator)

            HStack(spacing: Theme.Spacing.s) {
                Button(action: onReplace) {
                    Label("Replace…", systemImage: "arrow.triangle.2.circlepath")
                }
                .buttonStyle(SecondaryButtonStyle())
                .help("Choose a new image for this placement — \(fitMode.help.lowercased())")

                Picker("", selection: $fitMode) {
                    ForEach(ImageFitMode.allCases) { mode in
                        Text(mode.title).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 170)
                .help(fitMode.help)

                Spacer(minLength: Theme.Spacing.s)

                Button { onRotate(-90) } label: { Image(systemName: "rotate.left") }
                    .buttonStyle(IconButtonStyle())
                    .help("Rotate 90° counterclockwise")
                Button { onRotate(90) } label: { Image(systemName: "rotate.right") }
                    .buttonStyle(IconButtonStyle())
                    .help("Rotate 90° clockwise")

                Button(action: onExtract) {
                    Label("Extract…", systemImage: "square.and.arrow.down")
                }
                .buttonStyle(SecondaryButtonStyle())
                .help("Save the original image file")

                Button(action: onDelete) {
                    Label("Delete", systemImage: "trash")
                        .foregroundStyle(Theme.danger)
                }
                .buttonStyle(SecondaryButtonStyle())
                .help("Remove this placement (undo with ⌘Z)")
            }
            .disabled(busy)

            Text(fitMode.help)
                .font(Theme.Font.caption)
                .foregroundStyle(Theme.textTertiary)
        }
    }

    private var preview: some View {
        ZStack {
            RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                .fill(Theme.surface)
            if let thumbnail {
                Image(nsImage: thumbnail)
                    .resizable()
                    .interpolation(.medium)
                    .aspectRatio(contentMode: .fit)
                    .padding(6)
            } else {
                Image(systemName: "photo").foregroundStyle(Theme.textTertiary)
            }
        }
        .frame(width: 84, height: 84)
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                .strokeBorder(Theme.separator, lineWidth: 1)
        )
    }

    private var facts: String {
        let size = ByteCountFormatter.string(fromByteCount: Int64(placement.bytes), countStyle: .file)
        let shown = "\(Int(placement.displayWidth.rounded())) × \(Int(placement.displayHeight.rounded())) pt"
        return "\(placement.width) × \(placement.height) px · \(placement.colorspace) · \(placement.ext.uppercased()) · \(size)\nShown at \(shown) · \(Int(min(placement.dpiX, placement.dpiY).rounded())) dpi"
    }

    @ViewBuilder
    private var badges: some View {
        let lowRes = min(placement.dpiX, placement.dpiY) < 150
        let rotated = abs(placement.rotation) > 0.5
        if lowRes || placement.hasMask || placement.nested || rotated {
            HStack(spacing: Theme.Spacing.xs) {
                if lowRes { Badge(text: "Low resolution for print", tint: Theme.warning) }
                if placement.hasMask { Badge(text: "Transparency", tint: Theme.accent) }
                if rotated { Badge(text: "Rotated \(Int(placement.rotation.rounded()))°", tint: Theme.textSecondary) }
                if placement.nested { Badge(text: "Inside a form", tint: Theme.textSecondary) }
            }
            .padding(.top, 2)
        }
    }

    private var pagesText: String {
        let pages = placement.pagesUsed
        if pages.count == 1 { return "page \(pages[0])" }
        let shown = pages.prefix(6).map(String.init).joined(separator: ", ")
        return "pages \(shown)\(pages.count > 6 ? "…" : "")"
    }
}

// MARK: - Position & size

private struct ImageGeometrySection: View {
    @Binding var draft: GeometryDraft
    let original: CGRect
    let pageBounds: CGRect?

    var body: some View {
        FormSection("Position & size",
                    footnote: "Points from the top-left corner of the page (72 pt = 1 inch). Apply saves a new version you can undo.") {
            HStack(spacing: Theme.Spacing.l) {
                NumberField(label: "X", value: $draft.x)
                NumberField(label: "Y", value: $draft.y)
                Spacer(minLength: 0)
            }
            HStack(spacing: Theme.Spacing.s) {
                NumberField(label: "W", value: widthBinding)
                Button {
                    draft.lockAspect.toggle()
                    if draft.lockAspect, draft.height > 0 { draft.aspect = draft.width / draft.height }
                } label: {
                    Image(systemName: draft.lockAspect ? "lock.fill" : "lock.open")
                }
                .buttonStyle(IconButtonStyle(isActive: draft.lockAspect))
                .help(draft.lockAspect ? "Proportions locked" : "Proportions unlocked")
                NumberField(label: "H", value: heightBinding)
                Spacer(minLength: 0)
            }
            HStack(spacing: Theme.Spacing.s) {
                Button("Fit page width", action: fitPageWidth)
                    .buttonStyle(SecondaryButtonStyle())
                Button("Center on page", action: centerOnPage)
                    .buttonStyle(SecondaryButtonStyle())
                Spacer()
                Button("Reset") { draft = GeometryDraft(rect: original) }
                    .buttonStyle(SecondaryButtonStyle())
                    .disabled(draft.rect.isApproximatelyEqual(to: original))
            }
            .disabled(pageBounds == nil)

            if let message = validation {
                InfoBanner(kind: message.kind, text: message.text)
            }
        }
    }

    private var widthBinding: Binding<Double> {
        Binding(
            get: { draft.width },
            set: { value in
                draft.width = value
                if draft.lockAspect, draft.aspect > 0 { draft.height = (value / draft.aspect).rounded(toPlaces: 2) }
            }
        )
    }

    private var heightBinding: Binding<Double> {
        Binding(
            get: { draft.height },
            set: { value in
                draft.height = value
                if draft.lockAspect, draft.aspect > 0 { draft.width = (value * draft.aspect).rounded(toPlaces: 2) }
            }
        )
    }

    private var validation: (kind: InfoBanner.Kind, text: String)? {
        if !draft.isValid { return (.danger, "Width and height must be at least 1 pt.") }
        guard let pageBounds else { return nil }
        let rect = draft.rect
        if !rect.intersects(pageBounds) { return (.warning, "The image would be entirely off the page.") }
        if !pageBounds.insetBy(dx: -0.5, dy: -0.5).contains(rect) {
            return (.info, "Part of the image will extend past the page edge and be cropped.")
        }
        return nil
    }

    private func fitPageWidth() {
        guard let pageBounds, draft.width > 0, draft.height > 0 else { return }
        let aspect = draft.width / draft.height
        let centerY = draft.y + draft.height / 2
        draft.width = Double(pageBounds.width).rounded(toPlaces: 2)
        draft.height = (draft.width / aspect).rounded(toPlaces: 2)
        draft.x = 0
        draft.y = max(0, centerY - draft.height / 2).rounded(toPlaces: 2)
    }

    private func centerOnPage() {
        guard let pageBounds else { return }
        draft.x = ((Double(pageBounds.width) - draft.width) / 2).rounded(toPlaces: 2)
        draft.y = ((Double(pageBounds.height) - draft.height) / 2).rounded(toPlaces: 2)
    }
}

private struct NumberField: View {
    let label: String
    @Binding var value: Double

    var body: some View {
        HStack(spacing: Theme.Spacing.xs + 2) {
            Text(label)
                .font(Theme.Font.callout.weight(.semibold))
                .foregroundStyle(Theme.textTertiary)
                .frame(width: 14, alignment: .leading)
            TextField("", value: $value, format: .number.precision(.fractionLength(0...2)))
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.trailing)
                .frame(width: 84)
            Text("pt")
                .font(Theme.Font.caption)
                .foregroundStyle(Theme.textTertiary)
        }
    }
}

// MARK: - Small helpers

private extension Double {
    func rounded(toPlaces places: Int) -> Double {
        let scale = pow(10, Double(places))
        return (self * scale).rounded() / scale
    }
}

private extension CGRect {
    func isApproximatelyEqual(to other: CGRect, tolerance: CGFloat = 0.05) -> Bool {
        abs(minX - other.minX) <= tolerance && abs(minY - other.minY) <= tolerance
            && abs(width - other.width) <= tolerance && abs(height - other.height) <= tolerance
    }

    func centerDistance(to other: CGRect) -> CGFloat {
        hypot(midX - other.midX, midY - other.midY)
    }
}
