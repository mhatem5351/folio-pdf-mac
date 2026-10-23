import PDFKit
import SwiftUI

/// Floating glass pill at the bottom of the canvas: page navigation and zoom.
struct CanvasStatusPill: View {
    @EnvironmentObject private var store: AppStore
    @StateObject private var tracker = CanvasPageTracker()
    @State private var pageField = ""
    @FocusState private var editingPage: Bool

    var body: some View {
        HStack(spacing: Theme.Spacing.s) {
            Button { step(-1) } label: { Image(systemName: "chevron.up") }
                .buttonStyle(IconButtonStyle(size: 24))
                .disabled(tracker.pageIndex <= 0)
                .help("Previous page")

            HStack(spacing: 4) {
                ZStack {
                    // Live page number; the field underneath takes typing.
                    if pageField.isEmpty && !editingPage {
                        Text("\(tracker.pageIndex + 1)")
                            .font(Theme.Font.callout.monospacedDigit().weight(.semibold))
                            .foregroundStyle(Theme.textPrimary)
                            .allowsHitTesting(false)
                    }
                    TextField("", text: $pageField)
                        .textFieldStyle(.plain)
                        .multilineTextAlignment(.center)
                        .font(Theme.Font.callout.monospacedDigit().weight(.semibold))
                        .focused($editingPage)
                        .onSubmit(jump)
                }
                .frame(width: 30)
                Text("of \(store.pageCount)")
                    .font(Theme.Font.callout.monospacedDigit())
                    .foregroundStyle(Theme.textSecondary)
            }

            Button { step(1) } label: { Image(systemName: "chevron.down") }
                .buttonStyle(IconButtonStyle(size: 24))
                .disabled(tracker.pageIndex >= store.pageCount - 1)
                .help("Next page")

            Rectangle().fill(Theme.separator).frame(width: 1, height: 18)

            Button { zoom(by: 1 / 1.2) } label: { Image(systemName: "minus") }
                .buttonStyle(IconButtonStyle(size: 24))
                .help("Zoom out (⌘−)")

            Menu {
                Button("Fit Width") { fitWidth() }
                Button("Fit Page") { fitPage() }
                Divider()
                ForEach([50, 75, 100, 125, 150, 200, 300], id: \.self) { percent in
                    Button("\(percent)%") { setScale(CGFloat(percent) / 100) }
                }
            } label: {
                Text("\(Int((tracker.scale * 100).rounded()))%")
                    .font(Theme.Font.callout.monospacedDigit().weight(.medium))
                    .foregroundStyle(Theme.textPrimary)
                    .frame(minWidth: 42)
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()

            Button { zoom(by: 1.2) } label: { Image(systemName: "plus") }
                .buttonStyle(IconButtonStyle(size: 24))
                .help("Zoom in (⌘+)")

            Button { fitWidth() } label: { Image(systemName: "arrow.left.and.right.square") }
                .buttonStyle(IconButtonStyle(size: 24))
                .help("Fit width")
        }
        .padding(.horizontal, Theme.Spacing.s + 2)
        .padding(.vertical, 5)
        .background(.regularMaterial, in: Capsule())
        .background(Theme.surface.opacity(0.75), in: Capsule())
        .overlay(Capsule().strokeBorder(Theme.separator))
        .shadow(color: Theme.floatingShadow, radius: 14, y: 4)
        .onAppear { tracker.attach(store.pdfViewProxy) }
    }

    private var view: PDFView? { store.pdfViewProxy.pdfView }

    private func step(_ delta: Int) {
        guard let view, let document = view.document else { return }
        let target = min(max(tracker.pageIndex + delta, 0), document.pageCount - 1)
        if let page = document.page(at: target) { view.go(to: page) }
    }

    private func jump() {
        editingPage = false
        guard let view, let document = view.document,
              let number = Int(pageField.trimmingCharacters(in: .whitespaces)) else {
            pageField = ""
            return
        }
        let target = min(max(number - 1, 0), document.pageCount - 1)
        if let page = document.page(at: target) { view.go(to: page) }
        // Empty field shows the live page number as its placeholder.
        pageField = ""
    }

    private func zoom(by factor: CGFloat) {
        guard let view else { return }
        setScale(view.scaleFactor * factor)
    }

    private func setScale(_ scale: CGFloat) {
        guard let view else { return }
        view.autoScales = false
        view.scaleFactor = min(max(scale, view.minScaleFactor, 0.1), min(view.maxScaleFactor, 8))
    }

    private func fitWidth() {
        guard let view else { return }
        view.autoScales = true
    }

    private func fitPage() {
        guard let view, let page = view.currentPage else { return }
        let bounds = page.bounds(for: view.displayBox)
        let available = view.bounds.insetBy(dx: 24, dy: 24)
        guard bounds.width > 0, bounds.height > 0 else { return }
        let rotated = page.rotation % 180 != 0
        let w = rotated ? bounds.height : bounds.width
        let h = rotated ? bounds.width : bounds.height
        setScale(min(available.width / w, available.height / h))
        view.go(to: page)
    }
}
