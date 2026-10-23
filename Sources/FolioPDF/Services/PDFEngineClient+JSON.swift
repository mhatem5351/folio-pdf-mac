import Foundation

extension PDFEngineClient {
    /// Run an engine command and decode its JSON reply. Engine replies are
    /// `{"ok": true, ...}`; a reply with ok=false surfaces as an error.
    func runJSON<T: Decodable>(_ args: [String], as type: T.Type = T.self) throws -> T {
        let result = try run(args)
        let data = Data(result.stdout.utf8)
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw PDFEngineError.invalidOutput("Could not decode \(args.first ?? "engine") reply: \(error.localizedDescription)\n\(result.stdout)")
        }
    }
}
