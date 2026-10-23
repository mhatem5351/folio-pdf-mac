import Foundation
import IOKit

/// Bridge to the local hp-laserjet-mac-driver: jobs are PDFs dropped into
/// ~/HP-LaserJet-spool (optionally with a `job-<ts>.opts` sidecar); the
/// driver's launchd watcher rasterises and sends them over USB, logging to
/// watcher.log, which we tail to report the outcome.
enum LaserJetPrinter {
    struct Options {
        var copies = 1
        /// 1 (light) … 5 (dark); nil = driver default.
        var density: Int?
        var economode = false
        /// a4, letter, legal, a5, auto.
        var paper = "a4"
    }

    struct Device {
        let name: String
        let productID: Int
    }

    enum Outcome {
        case printed
        case queuedNoPrinter
        /// `retryable`: the transfer died before any byte reached the printer
        /// (typically the printer waking from sleep), so resending is safe.
        case failed(String, retryable: Bool)
        case timedOut
    }

    static let spoolDirectory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("HP-LaserJet-spool", isDirectory: true)
    static let watcherLog = spoolDirectory.appendingPathComponent("watcher.log")
    private static let watcherAgent = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/LaunchAgents/com.hp-laserjet.watcher.plist")
    private static let driverBinary = URL(fileURLWithPath: "/usr/local/share/foo2zjs/fwload")

    /// Host-based HP LaserJets the driver supports (USB vendor 0x03f0).
    private static let supportedProductIDs: Set<Int> = [
        0x0517, 0x1317, 0x4117, 0x2B17, 0x002A, 0x092A,
        0x3D17, 0x3E17, 0x4817, 0x4917, 0x3F17, 0x4017, 0x3B17
    ]

    static var isDriverInstalled: Bool {
        FileManager.default.fileExists(atPath: driverBinary.path)
            && FileManager.default.fileExists(atPath: watcherAgent.path)
    }

    /// The attached supported LaserJet, if any (IOKit USB registry scan).
    static func connectedDevice() -> Device? {
        guard let matching = IOServiceMatching("IOUSBHostDevice") else { return nil }
        var iterator: io_iterator_t = 0
        guard IOServiceGetMatchingServices(kIOMainPortDefault, matching, &iterator) == KERN_SUCCESS else { return nil }
        defer { IOObjectRelease(iterator) }
        var service = IOIteratorNext(iterator)
        while service != 0 {
            defer {
                IOObjectRelease(service)
                service = IOIteratorNext(iterator)
            }
            func intProperty(_ key: String) -> Int? {
                (IORegistryEntryCreateCFProperty(service, key as CFString, kCFAllocatorDefault, 0)?
                    .takeRetainedValue() as? NSNumber)?.intValue
            }
            guard intProperty("idVendor") == 0x03F0,
                  let product = intProperty("idProduct"),
                  supportedProductIDs.contains(product) else { continue }
            let name = IORegistryEntryCreateCFProperty(service, "USB Product Name" as CFString, kCFAllocatorDefault, 0)?
                .takeRetainedValue() as? String
            return Device(name: name ?? "HP LaserJet", productID: product)
        }
        return nil
    }

    /// Queue `pdf` for printing. Returns the spooled job URL.
    @discardableResult
    static func submit(pdf: URL, options: Options) throws -> URL {
        let fm = FileManager.default
        try fm.createDirectory(at: spoolDirectory, withIntermediateDirectories: true)
        let stamp = "\(Int(Date().timeIntervalSince1970))-\(UUID().uuidString.prefix(6))"
        let job = spoolDirectory.appendingPathComponent("job-\(stamp).pdf")

        var lines = ["COPIES=\(max(1, options.copies))", "PAPER=\(options.paper)"]
        if let density = options.density { lines.append("DENSITY=\(min(max(density, 1), 5))") }
        lines.append("ECONOMODE=\(options.economode ? "on" : "off")")
        // Sidecar first so it's in place when the PDF's arrival wakes the watcher.
        try (lines.joined(separator: "\n") + "\n")
            .write(to: spoolDirectory.appendingPathComponent("job-\(stamp).opts"), atomically: true, encoding: .utf8)

        let incoming = spoolDirectory.appendingPathComponent(".incoming-\(stamp).pdf")
        try? fm.removeItem(at: incoming)
        try fm.copyItem(at: pdf, to: incoming)
        try fm.moveItem(at: incoming, to: job)
        return job
    }

    /// Follow watcher.log until the job is sent (or deferred/failed). Blocks;
    /// call off the main thread.
    static func waitForOutcome(job: URL, startOffset: UInt64, timeout: TimeInterval = 120) -> Outcome {
        let deadline = Date().addingTimeInterval(timeout)
        var seenJob = false
        var nothingSent = false
        while Date() < deadline {
            if let text = logText(from: startOffset) {
                for line in text.split(separator: "\n") {
                    if line.contains(job.lastPathComponent) { seenJob = true }
                    if line.contains("no supported printer detected") { return .queuedNoPrinter }
                    guard seenJob else { continue }
                    if line.contains("(sent 0)") || line.hasPrefix("sent 0/") { nothingSent = true }
                    if let range = line.range(of: "fwload exit ") {
                        let code = line[range.upperBound...].prefix { $0.isNumber }
                        return code == "0" ? .printed : .failed(
                            "The printer didn't accept the job (code \(code)). Check that it's on and has paper. Details: \(watcherLog.path)",
                            retryable: nothingSent)
                    }
                    if line.contains("ERROR") { return .failed(String(line).trimmingCharacters(in: .whitespaces), retryable: false) }
                }
            }
            Thread.sleep(forTimeInterval: 0.5)
        }
        return .timedOut
    }

    /// Current size of watcher.log, to read only lines written after submit.
    static func logOffset() -> UInt64 {
        ((try? FileManager.default.attributesOfItem(atPath: watcherLog.path)[.size]) as? NSNumber)?.uint64Value ?? 0
    }

    private static func logText(from offset: UInt64) -> String? {
        guard let handle = try? FileHandle(forReadingFrom: watcherLog) else { return nil }
        defer { try? handle.close() }
        // The watcher truncates the log when it grows large; restart from 0.
        let size = (try? handle.seekToEnd()) ?? 0
        try? handle.seek(toOffset: size < offset ? 0 : offset)
        guard let data = try? handle.readToEnd() else { return nil }
        return String(data: data, encoding: .utf8)
    }
}
