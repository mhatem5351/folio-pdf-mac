import PDFKit
import SwiftUI
import UniformTypeIdentifiers

/// Page management: a grid of page thumbnails that supports
/// click-to-select, drag-to-reorder, and double-click to jump into reading.
struct PagesGridView: View {
    @EnvironmentObject private var store: AppStore
    @State private var draggedIndex: Int?
    @State private var dropTarget: Int?
    @AppStorage("pagesGrid.thumbnailWidth") private var thumbnailWidth: Double = 170

    private var columns: [GridItem] {
        [GridItem(.adaptive(minimum: thumbnailWidth, maximum: thumbnailWidth + 40), spacing: Theme.Spacing.xl)]
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider().overlay(Theme.separator)
            ScrollView {
                LazyVGrid(columns: columns, spacing: Theme.Spacing.xl) {
                    ForEach(0..<store.pageCount, id: \.self) { index in
                        PageThumbnailCell(
                            index: index,
                            document: store.document,
                            isSelected: store.selectedPageIndices.contains(index),
                            isDropTarget: dropTarget == index && draggedIndex != index
                        )
                        .onTapGesture(count: 2) {
                            openPage(index)
                        }
                        .onTapGesture {
                            toggleSelection(index)
                        }
                        .onDrag {
                            draggedIndex = index
                            return NSItemProvider(object: String(index) as NSString)
                        }
                        .onDrop(
                            of: [UTType.text.identifier],
                            delegate: PageDropDelegate(
                                targetIndex: index,
                                draggedIndex: $draggedIndex,
                                dropTarget: $dropTarget,
                                store: store
                            )
                        )
                    }
                }
                .padding(Theme.Spacing.xxl)
                .id(store.documentRevision)
            }
        }
        .background(Theme.canvas)
    }

    private var header: some View {
        HStack(spacing: Theme.Spacing.m) {
            Text(selectionSummary)
                .font(Theme.Font.callout.weight(.medium))
                .foregroundStyle(Theme.textSecondary)
            Button("Select All") {
                store.selectedPageIndices = Set(0..<store.pageCount)
            }
            .buttonStyle(ToolChipStyle())
            .disabled(store.pageCount == 0)
            if !store.selectedPageIndices.isEmpty {
                Button("Clear") { store.selectedPageIndices = [] }
                    .buttonStyle(ToolChipStyle())
            }
            Spacer()
            Image(systemName: "square.grid.3x3")
                .font(.system(size: 11))
                .foregroundStyle(Theme.textTertiary)
            Slider(value: $thumbnailWidth, in: 110...300)
                .frame(width: 120)
                .controlSize(.mini)
            Image(systemName: "square.grid.2x2")
                .font(.system(size: 13))
                .foregroundStyle(Theme.textTertiary)
        }
        .padding(.horizontal, Theme.Spacing.l)
        .padding(.vertical, Theme.Spacing.s)
        .background(Theme.chrome)
    }

    private var selectionSummary: String {
        let count = store.selectedPageIndices.count
        if count == 0 { return "\(store.pageCount) pages · click to select, drag to reorder" }
        return "\(count) of \(store.pageCount) selected"
    }

    private func toggleSelection(_ index: Int) {
        if store.selectedPageIndices.contains(index) {
            store.selectedPageIndices.remove(index)
        } else {
            store.selectedPageIndices.insert(index)
        }
    }

    private func openPage(_ index: Int) {
        store.selectedOperation = .read
        DispatchQueue.main.async {
            if let pdfView = store.pdfViewProxy.pdfView,
               let page = pdfView.document?.page(at: index) {
                pdfView.go(to: page)
            }
        }
    }
}

private struct PageThumbnailCell: View {
    let index: Int
    let document: PDFDocument?
    let isSelected: Bool
    let isDropTarget: Bool
    @State private var hovering = false

    var body: some View {
        VStack(spacing: Theme.Spacing.s) {
            thumbnail
                .clipShape(RoundedRectangle(cornerRadius: 3))
                .shadow(color: Theme.pageShadow, radius: hovering || isSelected ? 8 : 3, y: hovering ? 3 : 1)
                .overlay(
                    RoundedRectangle(cornerRadius: 6)
                        .strokeBorder(isSelected ? Theme.accent : (hovering ? Theme.outline : Color.clear), lineWidth: isSelected ? 3 : 1)
                        .padding(-5)
                )
                .overlay(alignment: .topTrailing) {
                    if isSelected {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 20))
                            .symbolRenderingMode(.palette)
                            .foregroundStyle(Theme.onAccent, Theme.accent)
                            .background(Circle().fill(Theme.surface).padding(2))
                            .offset(x: 10, y: -10)
                            .transition(.scale.combined(with: .opacity))
                    }
                }
                .overlay(alignment: .leading) {
                    if isDropTarget {
                        Capsule()
                            .fill(Theme.accent)
                            .frame(width: 4)
                            .padding(.vertical, -6)
                            .offset(x: -14)
                    }
                }
                .scaleEffect(hovering ? 1.015 : 1)

            HStack(spacing: 4) {
                Text("\(index + 1)")
                    .font(Theme.Font.caption.monospacedDigit().weight(.semibold))
                    .foregroundStyle(isSelected ? Theme.onAccent : Theme.textSecondary)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 2)
                    .background(Capsule().fill(isSelected ? Theme.accent : Theme.surfaceRaised))
                if let size = pageSizeLabel {
                    Text(size)
                        .font(Theme.Font.micro)
                        .foregroundStyle(Theme.textTertiary)
                }
            }
        }
        .padding(6)
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
        .animation(.easeOut(duration: 0.15), value: hovering)
        .animation(.spring(response: 0.25, dampingFraction: 0.8), value: isSelected)
    }

    private var thumbnail: some View {
        Group {
            if let image = renderThumbnail() {
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fit)
            } else {
                Rectangle()
                    .fill(.white)
                    .aspectRatio(0.77, contentMode: .fit)
            }
        }
    }

    private var pageSizeLabel: String? {
        guard let page = document?.page(at: index) else { return nil }
        let box = page.bounds(for: .cropBox)
        let w = box.width, h = box.height
        func near(_ a: CGFloat, _ b: CGFloat) -> Bool { abs(a - b) < 3 }
        let (short, long) = (min(w, h), max(w, h))
        if near(short, 595), near(long, 842) { return "A4" }
        if near(short, 612), near(long, 792) { return "Letter" }
        if near(short, 612), near(long, 1008) { return "Legal" }
        if near(short, 420), near(long, 595) { return "A5" }
        return "\(Int(w))×\(Int(h))"
    }

    private func renderThumbnail() -> NSImage? {
        guard let page = document?.page(at: index) else { return nil }
        return page.thumbnail(of: NSSize(width: 360, height: 480), for: .cropBox)
    }
}

private struct PageDropDelegate: DropDelegate {
    let targetIndex: Int
    @Binding var draggedIndex: Int?
    @Binding var dropTarget: Int?
    let store: AppStore

    func dropEntered(info: DropInfo) {
        dropTarget = targetIndex
    }

    func dropExited(info: DropInfo) {
        if dropTarget == targetIndex { dropTarget = nil }
    }

    func dropUpdated(info: DropInfo) -> DropProposal? {
        DropProposal(operation: .move)
    }

    func performDrop(info: DropInfo) -> Bool {
        dropTarget = nil
        guard let source = draggedIndex else { return false }
        draggedIndex = nil
        guard source != targetIndex else { return false }
        store.movePage(from: source, to: targetIndex > source ? targetIndex + 1 : targetIndex)
        return true
    }
}
