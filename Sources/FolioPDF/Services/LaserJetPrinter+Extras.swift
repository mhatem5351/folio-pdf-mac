import Foundation
import PDFKit

/// Driver config, printer status, the spool queue and port-disabled detection
/// for the HP LaserJet bridge. Everything here only reads/writes the driver's
/// documented files: ~/.config/hp-laserjet/config, the spool directory and
/// watcher.log (see hp-laserjet-macos-driver docs/CONFIG.md).
extension LaserJetPrinter {

    // MARK: Driver config (~/.config/hp-laserjet/config)

    static let configFile = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".config/hp-laserjet/config")

    /// Value of `KEY=value` in the driver config (last one wins, quotes and
    /// spaces trimmed), or nil.
    static func configValue(_ key: String) -> String? {
        guard let text = try? String(contentsOf: configFile, encoding: .utf8) else { return nil }
        var value: String?
        for raw in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard !line.hasPrefix("#"), let eq = line.firstIndex(of: "=") else { continue }
            guard line[..<eq].trimmingCharacters(in: .whitespaces) == key else { continue }
            value = line[line.index(after: eq)...]
                .trimmingCharacters(in: .whitespaces)
                .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
        }
        return value
    }

    /// Set `KEY=value`: replaces existing lines for the key, else appends.
    static func setConfigValue(_ key: String, _ value: String) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: configFile.deletingLastPathComponent(), withIntermediateDirectories: true)
        let text = (try? String(contentsOf: configFile, encoding: .utf8)) ?? ""
        var lines = text.isEmpty ? [] : text.components(separatedBy: "\n")
        if lines.last == "" { lines.removeLast() }
        var replaced = false
        lines = lines.compactMap { line in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard !trimmed.hasPrefix("#"), let eq = trimmed.firstIndex(of: "="),
                  trimmed[..<eq].trimmingCharacters(in: .whitespaces) == key else { return line }
            if replaced { return nil }
            replaced = true
            return "\(key)=\(value)"
        }
        if !replaced { lines.append("\(key)=\(value)") }
        try (lines.joined(separator: "\n") + "\n").write(to: configFile, atomically: true, encoding: .utf8)
    }

    /// "Back sides come out upside down": the driver rotates every back side.
    static var rotatesBacks: Bool {
        get { configValue("DUPLEX_ROTATE")?.lowercased() == "on" }
        set { try? setConfigValue("DUPLEX_ROTATE", newValue ? "on" : "off") }
    }

    /// Two-page calibration PDF the driver installs (FRONT / BACK with arrows).
    static let duplexTestSheet = URL(fileURLWithPath: "/usr/local/share/foo2zjs/duplex-test.pdf")
    static var hasDuplexTestSheet: Bool { FileManager.default.fileExists(atPath: duplexTestSheet.path) }

    // MARK: Printer status

    struct PrinterStatus: Equatable {
        enum Level { case ok, warning, problem }
        let text: String
        let level: Level
        /// When the driver logged it ("Fri Nov 6 14:35:51"); nil = just now.
        var when: String?
    }

    /// Human text for a PJL status reply (CODE / ONLINE / DISPLAY tokens).
    static func status(code: Int?, online: Bool?, display: String?) -> PrinterStatus? {
        if online == false { return PrinterStatus(text: "Offline", level: .problem) }
        guard let code else {
            if let display, !display.isEmpty { return PrinterStatus(text: display, level: .ok) }
            return online == true ? PrinterStatus(text: "Ready", level: .ok) : nil
        }
        switch code {
        case 10001, 10002: return PrinterStatus(text: "Ready", level: .ok)
        case 10003: return PrinterStatus(text: "Warming up", level: .ok)
        case 10006: return PrinterStatus(text: "Toner low", level: .warning)
        case 10023: return PrinterStatus(text: "Printing", level: .ok)
        case 40000: return PrinterStatus(text: "Sleeping (ready)", level: .ok)
        case 40021, 40022: return PrinterStatus(text: "Door open", level: .problem)
        case 41000..<42000: return PrinterStatus(text: "Out of paper — load paper", level: .problem)
        case 42000..<43000, 44000..<45000: return PrinterStatus(text: "Paper jam", level: .problem)
        default: return PrinterStatus(text: "Status code \(code)", level: .warning)
        }
    }

    /// Parse "status-kv: KEY=value" lines into a status.
    static func parseStatus(lines: [Substring]) -> PrinterStatus? {
        var code: Int?, online: Bool?, display: String?
        for line in lines {
            guard let r = line.range(of: "status-kv: ") else { continue }
            let kv = line[r.upperBound...]
            guard let eq = kv.firstIndex(of: "=") else { continue }
            let key = kv[..<eq].trimmingCharacters(in: .whitespaces).uppercased()
            let val = kv[kv.index(after: eq)...].trimmingCharacters(in: .whitespaces)
                .trimmingCharacters(in: CharacterSet(charactersIn: "\""))
            switch key {
            case "CODE": code = Int(val)
            case "ONLINE": online = val.uppercased() == "TRUE"
            case "DISPLAY": display = val
            default: break
            }
        }
        return status(code: code, online: online, display: display)
    }

    /// Last status the printer reported after a job, from the tail of
    /// watcher.log, with the time of that watcher run.
    static func lastLoggedStatus() -> PrinterStatus? {
        guard let text = logTail() else { return nil }
        let lines = text.split(separator: "\n")
        guard let last = lines.lastIndex(where: { $0.contains("status-kv: ") }) else { return nil }
        var start = last
        while start > lines.startIndex, lines[start - 1].hasPrefix("status-") { start -= 1 }
        var status = parseStatus(lines: Array(lines[start...last]))
        if let header = lines[..<start].last(where: { $0.hasPrefix("=== ") && $0.contains("watcher woke") }) {
            status?.when = header
                .replacingOccurrences(of: "=== ", with: "")
                .replacingOccurrences(of: " watcher woke ===", with: "")
                .split(separator: " ").joined(separator: " ")
        }
        return status
    }

    /// True while jobs wait in the spool (the printer is or will be busy).
    static var hasWaitingJobs: Bool {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: spoolDirectory.path)) ?? []
        return names.contains { $0.hasPrefix("job-") && $0.hasSuffix(".pdf") }
    }

    /// Ask the printer for its status now (PJL query via fwload). Blocks for
    /// up to a few seconds; call off the main thread. Only when the spool is
    /// idle, so it never interleaves with a job's USB transfer.
    static func queryStatus(productID: Int) -> PrinterStatus? {
        guard !hasWaitingJobs else { return nil }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/local/share/foo2zjs/fwload")
        process.arguments = ["--status", "0x03f0", String(format: "0x%04x", productID)]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do { try process.run() } catch { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let text = String(data: data, encoding: .utf8) ?? ""
        return parseStatus(lines: text.split(separator: "\n"))
            ?? PrinterStatus(text: "No status reply", level: .warning)
    }

    // MARK: Port disabled by macOS

    /// The driver's last "no printer" report followed a "usb port disabled"
    /// note: macOS switched the port off and only replugging the cable helps.
    static func lastDeferWasPortDisabled() -> Bool {
        guard let text = logTail() else { return false }
        let lines = text.split(separator: "\n")
        guard let deferred = lines.lastIndex(where: { $0.contains("no supported printer detected") }) else { return false }
        let block = lines[..<deferred].suffix(4)
        return block.contains { $0.contains("usb port disabled") }
    }

    // MARK: Queue

    struct QueuedJob: Identifiable, Equatable {
        enum Kind: Equatable { case waiting, saved, backsOnly }
        let pdf: URL
        let kind: Kind
        let date: Date?
        let pages: Int?
        var id: String { pdf.path }
        var opts: URL { pdf.deletingPathExtension().appendingPathExtension("opts") }
    }

    static var failedDirectory: URL { spoolDirectory.appendingPathComponent("failed", isDirectory: true) }

    /// Jobs waiting in the spool, then saved jobs (failed / back sides only).
    static func queue() -> [QueuedJob] {
        func jobs(in dir: URL, saved: Bool) -> [QueuedJob] {
            let names = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
            return names.filter { $0.hasPrefix("job-") && $0.hasSuffix(".pdf") }.sorted().map { name in
                let url = dir.appendingPathComponent(name)
                let stamp = name.dropFirst(4).prefix { $0.isNumber }
                let date = Double(stamp).map { Date(timeIntervalSince1970: $0) }
                var kind: QueuedJob.Kind = saved ? .saved : .waiting
                if saved,
                   let opts = try? String(contentsOf: url.deletingPathExtension().appendingPathExtension("opts"), encoding: .utf8),
                   opts.contains("DUPLEX_PART=backs") {
                    kind = .backsOnly
                }
                return QueuedJob(pdf: url, kind: kind, date: date, pages: PDFDocument(url: url)?.pageCount)
            }
        }
        return jobs(in: spoolDirectory, saved: false) + jobs(in: failedDirectory, saved: true)
    }

    /// Remove a waiting job before the driver picks it up.
    static func cancel(_ job: QueuedJob) {
        try? FileManager.default.removeItem(at: job.pdf)
        try? FileManager.default.removeItem(at: job.opts)
    }

    /// Delete a saved job.
    static func discard(_ job: QueuedJob) { cancel(job) }

    /// Queue a saved job again: sidecar first, then the PDF that wakes the
    /// driver. A "back sides only" job prints just its backs (after the
    /// driver's reload prompt).
    static func reprint(_ job: QueuedJob) throws {
        let fm = FileManager.default
        if fm.fileExists(atPath: job.opts.path) {
            let dest = spoolDirectory.appendingPathComponent(job.opts.lastPathComponent)
            try? fm.removeItem(at: dest)
            try fm.moveItem(at: job.opts, to: dest)
        }
        let dest = spoolDirectory.appendingPathComponent(job.pdf.lastPathComponent)
        try? fm.removeItem(at: dest)
        try fm.moveItem(at: job.pdf, to: dest)
    }

    // MARK: Log tail

    /// Last ~64 KB of watcher.log.
    static func logTail(bytes: UInt64 = 65_536) -> String? {
        guard let handle = try? FileHandle(forReadingFrom: watcherLog) else { return nil }
        defer { try? handle.close() }
        let size = (try? handle.seekToEnd()) ?? 0
        try? handle.seek(toOffset: size > bytes ? size - bytes : 0)
        guard let data = try? handle.readToEnd() else { return nil }
        return String(decoding: data, as: UTF8.self)
    }
}
