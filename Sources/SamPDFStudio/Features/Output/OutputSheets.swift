import PDFKit
import SwiftUI

// MARK: - Print to HP LaserJet

struct LaserJetPrintSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @AppStorage("laserjet.copies") private var copies = 1
    @AppStorage("laserjet.paper") private var paper = "a4"
    @AppStorage("laserjet.density") private var density = 3
    @AppStorage("laserjet.customDensity") private var customDensity = false
    @AppStorage("laserjet.economode") private var economode = false
    @State private var pages = PageRangeSelection()
    @State private var device: LaserJetPrinter.Device?
    @State private var checked = false

    private let driverInstalled = LaserJetPrinter.isDriverInstalled

    var body: some View {
        ToolSheetScaffold(
            tool: .printHP,
            primaryTitle: device == nil ? "Queue Print" : "Print",
            canSubmit: driverInstalled && copies >= 1,
            onSubmit: submit
        ) {
            statusCard

            FormSection("Job") {
                FormRow("Copies") {
                    Stepper(value: $copies, in: 1...99) {
                        Text("\(copies)")
                            .font(Theme.Font.body.monospacedDigit().weight(.semibold))
                            .frame(minWidth: 24, alignment: .leading)
                    }
                    .fixedSize()
                }
                FormRow("Pages") {
                    PageRangePicker(selection: $pages, pageCount: store.pageCount)
                }
                FormRow("Paper") {
                    Picker("", selection: $paper) {
                        Text("A4").tag("a4")
                        Text("Letter").tag("letter")
                        Text("Legal").tag("legal")
                        Text("A5").tag("a5")
                        Text("Auto (from PDF)").tag("auto")
                    }
                    .labelsHidden()
                    .fixedSize()
                }
            }

            FormSection("Quality", footnote: "Toner saver prints lighter drafts and makes cartridges last longer.") {
                Toggle("Toner saver (draft)", isOn: $economode)
                Toggle("Custom darkness", isOn: $customDensity)
                if customDensity {
                    FormRow("Darkness") {
                        HStack {
                            Text("Light").font(Theme.Font.caption).foregroundStyle(Theme.textTertiary)
                            Slider(value: Binding(get: { Double(density) }, set: { density = Int($0.rounded()) }), in: 1...5, step: 1)
                            Text("Dark").font(Theme.Font.caption).foregroundStyle(Theme.textTertiary)
                        }
                    }
                }
            }
        }
        .task {
            device = await Task.detached { LaserJetPrinter.connectedDevice() }.value
            checked = true
        }
    }

    @ViewBuilder
    private var statusCard: some View {
        if !driverInstalled {
            InfoBanner(kind: .danger, text: "The HP LaserJet driver isn't installed. Install hp-laserjet-mac-driver, then try again.")
        } else if !checked {
            HStack(spacing: Theme.Spacing.s) {
                ProgressView().controlSize(.small)
                Text("Looking for the printer…").font(Theme.Font.callout).foregroundStyle(Theme.textSecondary)
            }
        } else if let device {
            HStack(spacing: Theme.Spacing.m) {
                ZStack(alignment: .bottomTrailing) {
                    IconTile(systemImage: "printer.fill", tint: ToolCategory.output.color, size: 40)
                    Circle().fill(Theme.success).frame(width: 11, height: 11)
                        .overlay(Circle().strokeBorder(Theme.surface, lineWidth: 2))
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(device.name).font(Theme.Font.headline).foregroundStyle(Theme.textPrimary)
                    Text("Connected over USB · Ready").font(Theme.Font.caption).foregroundStyle(Theme.success)
                }
                Spacer()
            }
            .padding(Theme.Spacing.m)
            .background(RoundedRectangle(cornerRadius: Theme.Radius.large).fill(Theme.success.opacity(0.08)))
            .overlay(RoundedRectangle(cornerRadius: Theme.Radius.large).strokeBorder(Theme.success.opacity(0.25)))
        } else {
            InfoBanner(kind: .warning, text: "The printer isn't connected. The job will wait in the queue and print as soon as you plug it in and turn it on.")
        }
    }

    private func submit() {
        var options = LaserJetPrinter.Options()
        options.copies = copies
        options.paper = paper
        options.economode = economode
        options.density = customDensity ? density : nil
        store.printToLaserJet(options: options, pageSpec: pages.spec(currentPage: store.currentPageNumber))
        dismiss()
    }
}

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
