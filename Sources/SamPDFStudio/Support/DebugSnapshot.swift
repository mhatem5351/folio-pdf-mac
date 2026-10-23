import AppKit

/// Local QA hook: renders the key window to a PNG when another process posts
/// the distributed notification `DebugSnapshot.name` with the target path as
/// its `object`. Lets UI checks run without Screen Recording permission:
///
///     swift -e 'import Foundation; DistributedNotificationCenter.default()
///       .postNotificationName(.init("local.pdfstudio.snapshot"),
///       object: "/tmp/shot.png", userInfo: nil, deliverImmediately: true)'
///
/// Only active when the environment variable PDFSTUDIO_QA=1 is set or the
/// `PDFStudioQA` user default is true, so release use never listens.
enum DebugSnapshot {
    static let name = Notification.Name("local.pdfstudio.snapshot")

    static var isEnabled: Bool {
        ProcessInfo.processInfo.environment["PDFSTUDIO_QA"] == "1"
            || UserDefaults.standard.bool(forKey: "PDFStudioQA")
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
        guard let window = NSApp.keyWindow ?? NSApp.windows.first(where: { $0.isVisible }),
              let frameView = window.contentView?.superview else { return }
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
