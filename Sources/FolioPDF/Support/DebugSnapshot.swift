import AppKit

/// Local QA hook: renders the key window to a PNG when another process posts
/// the distributed notification `DebugSnapshot.name` with the target path as
/// its `object`. Lets UI checks run without Screen Recording permission:
///
///     swift -e 'import Foundation; DistributedNotificationCenter.default()
///       .postNotificationName(.init("io.github.mhatem5351.FolioPDF.snapshot"),
///       object: "/tmp/shot.png", userInfo: nil, deliverImmediately: true)'
///
/// Only active when the environment variable FOLIO_QA=1 is set or the
/// `FolioQA` user default is true, so release use never listens.
enum DebugSnapshot {
    static let name = Notification.Name("io.github.mhatem5351.FolioPDF.snapshot")

    static var isEnabled: Bool {
        ProcessInfo.processInfo.environment["FOLIO_QA"] == "1"
            || UserDefaults.standard.bool(forKey: "FolioQA")
    }

    static func install() {
        guard isEnabled else { return }
        DistributedNotificationCenter.default().addObserver(
            forName: name, object: nil, queue: .main
        ) { note in
            guard let path = note.object as? String else { return }
            capture(to: URL(fileURLWithPath: path))
        }
    }

    static func capture(to url: URL) {
        // Main document window → url; an attached sheet → "<name>-sheet.png".
        guard let main = NSApp.windows.first(where: { $0.isVisible && $0.sheetParent == nil && $0.canBecomeMain })
                ?? NSApp.keyWindow else { return }
        if let sheet = main.attachedSheet {
            let sheetURL = url.deletingPathExtension().appendingPathExtension("sheet.png")
            capture(window: sheet, to: sheetURL)
        }
        capture(window: main, to: url)
    }

    private static func capture(window: NSWindow, to url: URL) {
        guard let frameView = window.contentView?.superview else { return }
        // Window-server capture of our own window includes PDFKit's tiled page
        // layers; cacheDisplay (fallback) leaves the PDF page area blank.
        if let cg = CGWindowListCreateImage(.null, .optionIncludingWindow,
                                            CGWindowID(window.windowNumber),
                                            [.boundsIgnoreFraming, .bestResolution]),
           cg.width > 1 {
            let rep = NSBitmapImageRep(cgImage: cg)
            try? rep.representation(using: .png, properties: [:])?.write(to: url)
            return
        }
        let bounds = frameView.bounds
        guard let rep = frameView.bitmapImageRepForCachingDisplay(in: bounds) else { return }
        frameView.cacheDisplay(in: bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: url)
    }
}
