import PDFKit
import SwiftUI

// MARK: - Reduce file size

struct CompressSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var quality = "medium"

    private var fileSize: String {
        guard let url = store.currentDocumentURL,
              let bytes = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size]) as? NSNumber else { return "—" }
        return ByteCountFormatter.string(fromByteCount: bytes.int64Value, countStyle: .file)
    }

    var body: some View {
        ToolSheetScaffold(tool: .compress, primaryTitle: "Reduce", onSubmit: {
            store.reduceFileSize(quality: quality)
            dismiss()
        }) {
            HStack {
                Text("Current size").font(Theme.Font.body).foregroundStyle(Theme.textSecondary)
                Spacer()
                Text(fileSize).font(Theme.Font.headline.monospacedDigit()).foregroundStyle(Theme.textPrimary)
            }
            VStack(spacing: Theme.Spacing.s) {
                option("small", "Smallest file", "Screen quality (≈72–96 dpi images). Best for email.", "envelope")
                option("medium", "Balanced", "Good quality for reading and sharing.", "scalemass")
                option("high", "High quality", "Keeps print-quality images; removes waste only.", "printer")
            }
            InfoBanner(kind: .info, text: "You'll see the before/after size when it finishes. Undo restores the original.")
        }
    }

    private func option(_ value: String, _ title: String, _ detail: String, _ icon: String) -> some View {
        Button {
            quality = value
        } label: {
            HStack(spacing: Theme.Spacing.m) {
                IconTile(systemImage: icon, tint: quality == value ? Theme.accent : Theme.textTertiary, size: 32)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(Theme.Font.headline).foregroundStyle(Theme.textPrimary)
                    Text(detail).font(Theme.Font.caption).foregroundStyle(Theme.textSecondary)
                }
                Spacer()
                Image(systemName: quality == value ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(quality == value ? Theme.accent : Theme.textTertiary)
                    .font(.system(size: 16))
            }
            .padding(Theme.Spacing.m)
            .background(RoundedRectangle(cornerRadius: Theme.Radius.large).fill(quality == value ? Theme.accentSoft : Theme.surfaceRaised.opacity(0.6)))
            .overlay(RoundedRectangle(cornerRadius: Theme.Radius.large).strokeBorder(quality == value ? Theme.accent.opacity(0.4) : Theme.separator))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}
