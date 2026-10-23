import AppKit
import PDFKit

/// Edit ▸ Image on the canvas: click an existing image to select it, drag to
/// move, drag a handle to resize (corners keep the aspect ratio; hold Shift to
/// stretch). The page itself only changes on mouse-up, when the new frame is
/// handed to `onTransform` as an engine rect (points from the top-left of the
/// page's crop box). Owned by the canvas PDFView, which forwards mouse events.
final class CanvasImageEditor {
    private unowned let pdfView: PDFView

    /// Placements of every page (engine `list-images`), drawing order.
    var placements: [EngineImagePlacement] = [] {
        didSet {
            if let selectedID, !placements.contains(where: { $0.id == selectedID }) {
                // Arrives during a SwiftUI update pass: tell the store later.
                select(nil, notify: false)
                DispatchQueue.main.async { [weak self] in self?.onSelect?(nil) }
            } else {
                reposition()
            }
        }
    }
    private(set) var selectedID: String?
    var onSelect: ((EngineImagePlacement?) -> Void)?
    var onTransform: ((EngineImagePlacement, CGRect) -> Void)?

    private var overlay: ImageSelectionOverlay?
    private var drag: DragState?

    private struct DragState {
        let placement: EngineImagePlacement
        let handle: ImageSelectionOverlay.Handle?
        let startPoint: NSPoint
        let startFrame: NSRect
        var moved = false
    }

    init(pdfView: PDFView) {
        self.pdfView = pdfView
    }

    var selected: EngineImagePlacement? {
        guard let selectedID else { return nil }
        return placements.first { $0.id == selectedID }
    }

    // MARK: Selection

    func select(id: String?) {
        select(placements.first { $0.id == id }, notify: false)
    }

    private func select(_ placement: EngineImagePlacement?, notify: Bool) {
        selectedID = placement?.id
        if placement == nil {
            overlay?.removeFromSuperview()
            overlay = nil
        } else {
            reposition()
        }
        if notify { onSelect?(placement) }
    }

    func clear() {
        drag = nil
        select(nil, notify: false)
    }

    // MARK: Mouse

    /// Returns true when the click belongs to image editing (hit an image or
    /// a handle). False lets the caller fall back (e.g. place a new image).
    func mouseDown(at viewPoint: NSPoint) -> Bool {
        if let overlay, let current = selected {
            let local = overlay.convert(viewPoint, from: pdfView)
            if let handle = overlay.handle(at: local) {
                drag = DragState(placement: current, handle: handle, startPoint: viewPoint, startFrame: overlay.frame)
                return true
            }
            if overlay.imageFrame.contains(local) {
                drag = DragState(placement: current, handle: nil, startPoint: viewPoint, startFrame: overlay.frame)
                return true
            }
        }
        guard let hit = hitTest(viewPoint) else {
            if selectedID != nil { select(nil, notify: true) }
            return false
        }
        select(hit, notify: true)
        if let overlay {
            drag = DragState(placement: hit, handle: nil, startPoint: viewPoint, startFrame: overlay.frame)
        }
        return true
    }

    func mouseDragged(to viewPoint: NSPoint, shift: Bool) {
        guard var state = drag, let overlay else { return }
        let dx = viewPoint.x - state.startPoint.x
        let dy = viewPoint.y - state.startPoint.y
        if !state.moved, hypot(dx, dy) < 2 { return }
        state.moved = true
        drag = state
        let inset = ImageSelectionOverlay.inset
        var image = state.startFrame.insetBy(dx: inset, dy: inset)
        if let handle = state.handle {
            image = Self.resize(image, handle: handle, dx: dx, dy: dy,
                                keepAspect: handle.isCorner && !shift, flipped: pdfView.isFlipped)
        } else {
            image = image.offsetBy(dx: dx, dy: dy)
        }
        overlay.frame = image.insetBy(dx: -inset, dy: -inset)
        overlay.sizeLabel = sizeLabel(forViewRect: image, page: state.placement.page)
        overlay.needsDisplay = true
    }

    func mouseUp() {
        guard let state = drag else { return }
        drag = nil
        guard state.moved, let overlay,
              let page = pdfView.document?.page(at: state.placement.page - 1) else {
            reposition()
            return
        }
        let inset = ImageSelectionOverlay.inset
        let viewRect = overlay.frame.insetBy(dx: inset, dy: inset)
        let pageRect = pdfView.convert(viewRect, to: page)
        let engine = Self.engineRect(fromPDFKitRect: pageRect, page: page)
        guard engine.width > 2, engine.height > 2 else {
            reposition()
            return
        }
        onTransform?(state.placement, engine)
    }

    /// Keep the overlay glued to its image through scroll and zoom.
    func reposition() {
        guard drag == nil else { return }
        guard let placement = selected, let rect = viewRect(for: placement) else {
            overlay?.removeFromSuperview()
            overlay = nil
            return
        }
        let inset = ImageSelectionOverlay.inset
        let frame = rect.insetBy(dx: -inset, dy: -inset)
        if overlay == nil {
            let view = ImageSelectionOverlay(frame: frame)
            pdfView.addSubview(view)
            overlay = view
        }
        overlay?.frame = frame
        overlay?.sizeLabel = sizeLabel(forViewRect: rect, page: placement.page)
        overlay?.needsDisplay = true
    }

    // MARK: Geometry

    private func hitTest(_ viewPoint: NSPoint) -> EngineImagePlacement? {
        guard let page = pdfView.page(for: viewPoint, nearest: false),
              let document = pdfView.document else { return nil }
        let pageNumber = document.index(for: page) + 1
        // Last drawn is on top.
        for placement in placements.reversed() where placement.page == pageNumber {
            if let rect = viewRect(for: placement), rect.insetBy(dx: -2, dy: -2).contains(viewPoint) {
                return placement
            }
        }
        return nil
    }

    private func viewRect(for placement: EngineImagePlacement) -> NSRect? {
        guard let page = pdfView.document?.page(at: placement.page - 1) else { return nil }
        let pageRect = Self.pdfKitRect(fromEngineRect: placement.engineRect, page: page)
        return pdfView.convert(pageRect, from: page)
    }

    private func sizeLabel(forViewRect rect: NSRect, page number: Int) -> String {
        guard let page = pdfView.document?.page(at: number - 1) else { return "" }
        let pageRect = pdfView.convert(rect, to: page)
        return "\(Int(pageRect.width.rounded())) × \(Int(pageRect.height.rounded())) pt"
    }

    static func engineRect(fromPDFKitRect rect: NSRect, page: PDFPage) -> CGRect {
        let bounds = page.bounds(for: .cropBox)
        return CGRect(x: rect.minX - bounds.minX, y: bounds.maxY - rect.maxY, width: rect.width, height: rect.height)
    }

    static func pdfKitRect(fromEngineRect rect: CGRect, page: PDFPage) -> NSRect {
        let bounds = page.bounds(for: .cropBox)
        return NSRect(x: bounds.minX + rect.minX, y: bounds.maxY - rect.maxY, width: rect.width, height: rect.height)
    }

    /// `flipped`: the superview's y grows downward, so the visual top edge is
    /// minY (the overlay itself is unflipped, so handle.top is always visual).
    private static func resize(_ rect: NSRect, handle: ImageSelectionOverlay.Handle, dx: CGFloat, dy: CGFloat,
                               keepAspect: Bool, flipped: Bool) -> NSRect {
        var minX = rect.minX, maxX = rect.maxX, minY = rect.minY, maxY = rect.maxY
        let movesMinY = flipped ? handle.top : handle.bottom
        let movesMaxY = flipped ? handle.bottom : handle.top
        if handle.left { minX += dx }
        if handle.right { maxX += dx }
        if movesMinY { minY += dy }
        if movesMaxY { maxY += dy }
        let minSide: CGFloat = 8
        if maxX - minX < minSide { if handle.left { minX = maxX - minSide } else { maxX = minX + minSide } }
        if maxY - minY < minSide { if movesMinY { minY = maxY - minSide } else { maxY = minY + minSide } }
        var result = NSRect(x: minX, y: minY, width: maxX - minX, height: maxY - minY)
        if keepAspect, rect.width > 0, rect.height > 0 {
            let aspect = rect.width / rect.height
            let scale = max(result.width / rect.width, result.height / rect.height)
            let width = rect.width * scale
            let height = width / aspect
            result.origin.x = handle.left ? rect.maxX - width : rect.minX
            result.origin.y = movesMinY ? rect.maxY - height : rect.minY
            result.size = NSSize(width: width, height: height)
        }
        return result
    }
}

/// Dashed accent frame with eight square handles and a size badge.
final class ImageSelectionOverlay: NSView {
    struct Handle: Equatable {
        let left: Bool, right: Bool, top: Bool, bottom: Bool
        var isCorner: Bool { (left || right) && (top || bottom) }

        static let all: [Handle] = [
            Handle(left: true, right: false, top: true, bottom: false),
            Handle(left: false, right: false, top: true, bottom: false),
            Handle(left: false, right: true, top: true, bottom: false),
            Handle(left: false, right: true, top: false, bottom: false),
            Handle(left: false, right: true, top: false, bottom: true),
            Handle(left: false, right: false, top: false, bottom: true),
            Handle(left: true, right: false, top: false, bottom: true),
            Handle(left: true, right: false, top: false, bottom: false)
        ]

        var cursor: NSCursor {
            if isCorner { return .crosshair }
            return (left || right) ? .resizeLeftRight : .resizeUpDown
        }
    }

    /// Room around the image for handles and the badge.
    static let inset: CGFloat = 8
    private static let handleSize: CGFloat = 9
    var sizeLabel = ""

    var imageFrame: NSRect { bounds.insetBy(dx: Self.inset, dy: Self.inset) }

    override var isFlipped: Bool { false }

    override func hitTest(_ point: NSPoint) -> NSView? {
        // Let the PDFView receive every click; it forwards to the editor.
        nil
    }

    func handle(at point: NSPoint) -> Handle? {
        let hit = Self.handleSize + 6
        for handle in Handle.all {
            let center = handleCenter(handle)
            if NSRect(x: center.x - hit / 2, y: center.y - hit / 2, width: hit, height: hit).contains(point) {
                return handle
            }
        }
        return nil
    }

    private func handleCenter(_ handle: Handle) -> NSPoint {
        let r = imageFrame
        let x = handle.left ? r.minX : (handle.right ? r.maxX : r.midX)
        let y = handle.bottom ? r.minY : (handle.top ? r.maxY : r.midY)
        return NSPoint(x: x, y: y)
    }

    override func draw(_ dirtyRect: NSRect) {
        let accent = NSColor(Theme.accent)
        let frame = imageFrame
        accent.withAlphaComponent(0.08).setFill()
        frame.fill()

        let border = NSBezierPath(rect: frame)
        border.lineWidth = 1.5
        border.setLineDash([5, 3], count: 2, phase: 0)
        accent.setStroke()
        border.stroke()

        for handle in Handle.all {
            let c = handleCenter(handle)
            let s = Self.handleSize
            let box = NSBezierPath(roundedRect: NSRect(x: c.x - s / 2, y: c.y - s / 2, width: s, height: s), xRadius: 2, yRadius: 2)
            NSColor.white.setFill()
            box.fill()
            box.lineWidth = 1.5
            accent.setStroke()
            box.stroke()
        }

        guard !sizeLabel.isEmpty else { return }
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 10, weight: .semibold),
            .foregroundColor: NSColor.white
        ]
        let text = NSAttributedString(string: sizeLabel, attributes: attributes)
        let size = text.size()
        let badge = NSRect(x: frame.midX - size.width / 2 - 6, y: frame.minY + 6, width: size.width + 12, height: size.height + 4)
        guard badge.width < frame.width - 4, badge.height < frame.height - 8 else { return }
        accent.setFill()
        NSBezierPath(roundedRect: badge, xRadius: badge.height / 2, yRadius: badge.height / 2).fill()
        text.draw(at: NSPoint(x: badge.minX + 6, y: badge.minY + 2))
    }
}
