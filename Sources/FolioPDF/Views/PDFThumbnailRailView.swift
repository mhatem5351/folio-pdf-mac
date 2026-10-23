import PDFKit
import SwiftUI

/// Sidebar thumbnails. Hand-rolled (not PDFKit's PDFThumbnailView, whose
/// internal collection view performs AutoLayout off the main thread when the
/// document is swapped mid-generation — a hard crash).
struct PDFThumbnailRailView: View {
    @EnvironmentObject private var store: AppStore
    @StateObject private var tracker = CanvasPageTracker()

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: Theme.Spacing.l) {
                    ForEach(0..<store.pageCount, id: \.self) { index in
                        let isCurrent = tracker.pageIndex == index
                        VStack(spacing: 6) {
                            RailThumbnail(document: store.document, index: index)
                                .clipShape(RoundedRectangle(cornerRadius: 3))
                                .shadow(color: Theme.pageShadow, radius: isCurrent ? 6 : 3, y: 1)
                                .overlay(
                                    RoundedRectangle(cornerRadius: 5)
                                        .strokeBorder(isCurrent ? Theme.accent : Color.clear, lineWidth: 2.5)
                                        .padding(-4)
                                )
                            Text("\(index + 1)")
                                .font(Theme.Font.micro.monospacedDigit())
                                .foregroundStyle(isCurrent ? Theme.onAccent : Theme.textSecondary)
                                .padding(.horizontal, 7)
                                .padding(.vertical, 1.5)
                                .background(Capsule().fill(isCurrent ? Theme.accent : Color.clear))
                        }
                        .id(index)
                        .contentShape(Rectangle())
                        .onTapGesture {
                            if let pdfView = store.pdfViewProxy.pdfView,
                               let page = pdfView.document?.page(at: index) {
                                pdfView.go(to: page)
                            }
                        }
                    }
                }
                .padding(.vertical, Theme.Spacing.l)
                .padding(.horizontal, Theme.Spacing.l)
                .id(store.documentRevision)
            }
            .onChange(of: tracker.pageIndex) { _, index in
                withAnimation(.easeInOut(duration: 0.2)) { proxy.scrollTo(index, anchor: .center) }
            }
        }
        .onAppear { tracker.attach(store.pdfViewProxy) }
    }
}

/// Publishes the canvas's current page index and zoom so SwiftUI chrome
/// (thumbnail highlight, page/zoom pill) follows scrolling and zooming.
final class CanvasPageTracker: ObservableObject {
    @Published var pageIndex: Int = 0
    @Published var scale: CGFloat = 1
    private var observers: [NSObjectProtocol] = []
    private weak var proxy: PDFViewProxy?

    func attach(_ proxy: PDFViewProxy) {
        self.proxy = proxy
        guard observers.isEmpty else { return }
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: .PDFViewPageChanged, object: nil, queue: .main) { [weak self] note in
            self?.refresh(from: note.object as? PDFView)
        })
        observers.append(center.addObserver(forName: .PDFViewScaleChanged, object: nil, queue: .main) { [weak self] note in
            self?.refresh(from: note.object as? PDFView)
        })
        observers.append(center.addObserver(forName: .PDFViewDocumentChanged, object: nil, queue: .main) { [weak self] note in
            self?.refresh(from: note.object as? PDFView)
        })
        refresh(from: proxy.pdfView)
    }

    private func refresh(from view: PDFView?) {
        guard let view, view === proxy?.pdfView else { return }
        if let page = view.currentPage, let document = view.document {
            let index = document.index(for: page)
            if index != pageIndex { pageIndex = index }
        }
        if abs(view.scaleFactor - scale) > 0.001 { scale = view.scaleFactor }
    }

    deinit {
        observers.forEach(NotificationCenter.default.removeObserver)
    }
}

private struct RailThumbnail: View {
    let document: PDFDocument?
    let index: Int

    var body: some View {
        if let page = document?.page(at: index) {
            Image(nsImage: page.thumbnail(of: NSSize(width: 150, height: 200), for: .cropBox))
                .resizable()
                .aspectRatio(contentMode: .fit)
                .frame(maxWidth: 140)
        } else {
            Rectangle()
                .fill(.white)
                .frame(width: 140, height: 180)
        }
    }
}
