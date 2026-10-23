import SwiftUI

/// Presents the sheet for a catalog tool. Each feature owns its sheet view in
/// Features/<Area>/; this switch is the only place that maps IDs to views.
struct ToolSheetHost: View {
    let tool: ToolID

    var body: some View {
        switch tool {
        case .split: SplitSheet()
        case .compress: CompressSheet()
        case .bookmarks: BookmarksSheet()
        case .images: ImagesSheet()
        case .forms: FormFieldsSheet()
        case .watermark: WatermarkSheet()
        case .headerFooter: HeaderFooterSheet()
        case .bates: BatesSheet()
        case .protect: ProtectSheet()
        case .unlock: UnlockSheet()
        case .flatten: FlattenSheet()
        case .metadata: MetadataSheet()
        case .ocr: OCRSheet()
        case .convertToPDF: ConvertToPDFSheet()
        case .compare: CompareSheet()
        case .redact, .exportOffice, .printHP, .print:
            ToolPlaceholder(tool: tool)
        }
    }
}

/// Temporary body for a tool whose sheet is not built yet.
struct ToolPlaceholder: View {
    let tool: ToolID
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ToolSheetScaffold(tool: tool, primaryTitle: "Close", onSubmit: { dismiss() }) {
            InfoBanner(kind: .info, text: "\(tool.title) is coming soon.")
        }
    }
}
