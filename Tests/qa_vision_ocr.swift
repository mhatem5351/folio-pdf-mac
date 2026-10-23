// Standalone harness for Services/VisionOCR.swift (XCTest needs full Xcode).
// Built and run by Tests/run_qa_vision_ocr.sh, and by Tests/qa_ocr_layer.py:
//   qa_vision_ocr <input.pdf> <output.json> [--languages ar-SA,en-US] [--pages 1,3] [--skip-text]
// Prints one JSON summary line on stdout; writes the add-text-layer JSON.
import Foundation

@main
struct QAVisionOCR {
    static func main() async {
        var args = Array(CommandLine.arguments.dropFirst())
        if args.first == "--languages-only" {
            print(VisionOCR.supportedLanguages().joined(separator: ","))
            return
        }
        guard args.count >= 2 else {
            FileHandle.standardError.write(Data("usage: qa_vision_ocr in.pdf out.json [--languages a,b] [--pages 1,2] [--skip-text]\n".utf8))
            exit(2)
        }
        let input = URL(fileURLWithPath: args.removeFirst())
        let output = URL(fileURLWithPath: args.removeFirst())
        var options = VisionOCR.Options()
        while !args.isEmpty {
            let flag = args.removeFirst()
            switch flag {
            case "--languages":
                options.languages = args.isEmpty ? [] : args.removeFirst().split(separator: ",").map(String.init)
            case "--pages":
                let spec = args.isEmpty ? "" : args.removeFirst()
                options.pageIndices = spec.split(separator: ",").compactMap { Int($0.trimmingCharacters(in: .whitespaces)) }.map { $0 - 1 }
            case "--skip-text":
                options.skipPagesWithText = true
            default:
                FileHandle.standardError.write(Data("unknown flag \(flag)\n".utf8))
                exit(2)
            }
        }

        let started = Date()
        let progressLog = ProgressLog()
        do {
            let result = try await VisionOCR.recognize(url: input, options: options) { done, total in
                progressLog.record(done, total)
            }
            try VisionOCR.writeJSON(result, to: output)
            let summary: [String: Any] = [
                "ok": true,
                "revision": result.revision,
                "languages": result.languages,
                "pages": result.pages.count,
                "skipped": result.pages.filter(\.skipped).map(\.page),
                "lines": result.lineCount,
                "progress_events": progressLog.count,
                "seconds": Date().timeIntervalSince(started),
                "supported_arabic": VisionOCR.supportedLanguages().contains { $0.hasPrefix("ar") }
            ]
            let data = try JSONSerialization.data(withJSONObject: summary, options: [.sortedKeys])
            print(String(decoding: data, as: UTF8.self))
        } catch {
            print("{\"ok\": false, \"message\": \"\(error.localizedDescription)\"}")
            exit(1)
        }
    }
}

final class ProgressLog: @unchecked Sendable {
    private let lock = NSLock()
    private var events = 0

    func record(_ done: Int, _ total: Int) {
        lock.lock()
        events += 1
        lock.unlock()
    }

    var count: Int {
        lock.lock()
        defer { lock.unlock() }
        return events
    }
}
