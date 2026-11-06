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
    @AppStorage("laserjet.twoSided") private var twoSided = false
    @AppStorage("laserjet.flip") private var flip = "long"
    @AppStorage("laserjet.layout") private var layoutRaw = PrintLayout.one.rawValue
    @AppStorage("laserjet.customScale") private var customScale = false
    @AppStorage("laserjet.scale") private var scale = 100
    @AppStorage("laserjet.margin") private var margin = 0
    @State private var pages = PageRangeSelection()
    @State private var device: LaserJetPrinter.Device?
    @State private var checked = false
    @State private var portDisabled = false
    @State private var status: LaserJetPrinter.PrinterStatus?
    @State private var checkingStatus = false
    @State private var statusNote: String?
    @State private var queue: [LaserJetPrinter.QueuedJob] = []
    @State private var rotateBacks = false

    private let driverInstalled = LaserJetPrinter.isDriverInstalled
    private let queueTimer = Timer.publish(every: 3, on: .main, in: .common).autoconnect()

    private var layout: PrintLayout { PrintLayout(rawValue: layoutRaw) ?? .one }
    /// Booklets are always two-sided.
    private var effectiveTwoSided: Bool { layout == .booklet || twoSided }
    /// 2-up and booklet sheets are landscape: their backs turn over the short edge.
    private var effectiveFlip: String { layout == .two || layout == .booklet ? "short" : flip }

    var body: some View {
        ToolSheetScaffold(
            tool: .printHP,
            primaryTitle: device == nil ? "Queue Print" : "Print",
            canSubmit: driverInstalled && copies >= 1,
            onSubmit: submit
        ) {
            statusCard

            if !queue.isEmpty { queueSection }

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

            layoutSection

            FormSection("Size") {
                FormRow("Scale") {
                    Picker("", selection: $customScale) {
                        Text("Fit to page").tag(false)
                        Text("Custom").tag(true)
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .fixedSize()
                }
                if customScale {
                    FormRow("Percent") {
                        Stepper(value: $scale, in: 25...400, step: 5) {
                            Text("\(scale)%")
                                .font(Theme.Font.body.monospacedDigit().weight(.semibold))
                                .frame(minWidth: 44, alignment: .leading)
                        }
                        .fixedSize()
                    }
                }
                FormRow("Margin") {
                    Picker("", selection: $margin) {
                        Text("None").tag(0)
                        Text("Small").tag(18)
                        Text("Medium").tag(36)
                        Text("Large").tag(54)
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
            rotateBacks = LaserJetPrinter.rotatesBacks
            refreshQueue()
            let found = await Task.detached { LaserJetPrinter.connectedDevice() }.value
            device = found
            portDisabled = found == nil && LaserJetPrinter.lastDeferWasPortDisabled()
            if found != nil { status = LaserJetPrinter.lastLoggedStatus() }
            checked = true
        }
        .onReceive(queueTimer) { _ in refreshQueue() }
    }

    // MARK: Layout & sides

    @ViewBuilder
    private var layoutSection: some View {
        FormSection("Layout") {
            FormRow("Pages per sheet") {
                Picker("", selection: $layoutRaw) {
                    ForEach(PrintLayout.allCases) { Text($0.label).tag($0.rawValue) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
            }
            FormRow("Sides") {
                Picker("", selection: Binding(get: { effectiveTwoSided }, set: { twoSided = $0 })) {
                    Text("One side").tag(false)
                    Text("Both sides").tag(true)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                .disabled(layout == .booklet)
            }
            if effectiveTwoSided && (layout == .one || layout == .four) {
                FormRow("Flip on") {
                    Picker("", selection: $flip) {
                        Text("Long edge (book)").tag("long")
                        Text("Short edge (notepad)").tag("short")
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .fixedSize()
                }
            }
            if let caption = layoutCaption {
                caption_(caption)
            }
            if effectiveTwoSided {
                Toggle("Back sides come out upside down", isOn: Binding(
                    get: { rotateBacks },
                    set: { rotateBacks = $0; LaserJetPrinter.rotatesBacks = $0 }
                ))
                .help("Turns every back side around. Applies to all two-sided printing, including ⌘P.")
                if LaserJetPrinter.hasDuplexTestSheet {
                    Button("Print a two-sided test sheet") { printTestSheet() }
                        .buttonStyle(.link)
                        .font(Theme.Font.callout)
                        .help("One sheet with arrows on both sides, to check how to put the stack back in.")
                }
            }
        }
    }

    private var layoutCaption: String? {
        switch layout {
        case .booklet:
            return "Prints both sides with the pages in booklet order. Fold the printed stack in half to make \(bookletSize) booklet."
        case .two where effectiveTwoSided, .four where effectiveTwoSided, .one where effectiveTwoSided:
            return "Your printer prints one side at a time. It prints the front sides, then asks you to put the stack back in the tray, printed side down, and prints the back sides in reverse order."
        case .two, .four:
            return "Pages are shrunk to fit \(layout.rawValue) on each sheet."
        case .one:
            return nil
        }
    }

    private var bookletSize: String {
        switch paper {
        case "a4": return "an A5"
        case "a5": return "an A6"
        case "letter": return "a half-letter"
        case "legal": return "a half-legal"
        default: return "a half-size"
        }
    }

    private func caption_(_ text: String) -> some View {
        Text(text)
            .font(Theme.Font.caption)
            .foregroundStyle(Theme.textTertiary)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: Status

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
                    Circle().fill(statusColor).frame(width: 11, height: 11)
                        .overlay(Circle().strokeBorder(Theme.surface, lineWidth: 2))
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(device.name).font(Theme.Font.headline).foregroundStyle(Theme.textPrimary)
                    Text(statusLine).font(Theme.Font.caption).foregroundStyle(statusColor)
                    if let note = statusNote ?? status?.when.map({ "Last report: \($0)" }) {
                        Text(note).font(Theme.Font.micro).foregroundStyle(Theme.textTertiary)
                    }
                }
                Spacer()
                Button(checkingStatus ? "Checking…" : "Check now") { checkStatus(device) }
                    .buttonStyle(.link)
                    .font(Theme.Font.callout)
                    .disabled(checkingStatus)
            }
            .padding(Theme.Spacing.m)
            .background(RoundedRectangle(cornerRadius: Theme.Radius.large).fill(statusColor.opacity(0.08)))
            .overlay(RoundedRectangle(cornerRadius: Theme.Radius.large).strokeBorder(statusColor.opacity(0.25)))
        } else if portDisabled {
            InfoBanner(kind: .warning, text: "macOS switched off the printer's USB port. Unplug the printer's USB cable and plug it back in. Waiting jobs print as soon as it's back.")
        } else {
            InfoBanner(kind: .warning, text: "The printer isn't connected. The job will wait in the queue and print as soon as you plug it in and turn it on.")
        }
    }

    private var statusLine: String {
        "Connected over USB · \(status?.text ?? "Ready")"
    }

    private var statusColor: Color {
        switch status?.level {
        case .problem: return Theme.danger
        case .warning: return Theme.warning
        default: return Theme.success
        }
    }

    private func checkStatus(_ device: LaserJetPrinter.Device) {
        guard !LaserJetPrinter.hasWaitingJobs else {
            statusNote = "Printer busy with a job — check again when it's done"
            return
        }
        checkingStatus = true
        statusNote = nil
        let productID = device.productID
        Task {
            let result = await Task.detached { LaserJetPrinter.queryStatus(productID: productID) }.value
            checkingStatus = false
            if let result {
                status = result
                statusNote = "Checked just now"
            } else {
                statusNote = "Printer busy with a job — check again when it's done"
            }
        }
    }

    // MARK: Queue

    private var queueSection: some View {
        FormSection("Queue") {
            ForEach(queue) { job in
                HStack(spacing: Theme.Spacing.s) {
                    Image(systemName: job.kind == .waiting ? "clock" : "exclamationmark.arrow.circlepath")
                        .foregroundStyle(job.kind == .waiting ? Theme.textSecondary : Theme.warning)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(jobTitle(job)).font(Theme.Font.body).foregroundStyle(Theme.textPrimary)
                        Text(jobDetail(job)).font(Theme.Font.caption).foregroundStyle(Theme.textTertiary)
                    }
                    Spacer()
                    switch job.kind {
                    case .waiting:
                        Button("Cancel") { LaserJetPrinter.cancel(job); refreshQueue() }
                            .buttonStyle(SecondaryButtonStyle())
                    case .saved, .backsOnly:
                        Button("Discard") { LaserJetPrinter.discard(job); refreshQueue() }
                            .buttonStyle(SecondaryButtonStyle())
                        Button("Print") { reprint(job) }
                            .buttonStyle(SecondaryButtonStyle())
                    }
                }
            }
        }
    }

    private func jobTitle(_ job: LaserJetPrinter.QueuedJob) -> String {
        switch job.kind {
        case .waiting: return "Waiting to print"
        case .saved: return "Saved after a failed print"
        case .backsOnly: return "Back sides only"
        }
    }

    private func jobDetail(_ job: LaserJetPrinter.QueuedJob) -> String {
        var parts: [String] = []
        if let date = job.date { parts.append(date.formatted(date: .abbreviated, time: .shortened)) }
        if let pages = job.pages { parts.append(pages == 1 ? "1 page" : "\(pages) pages") }
        return parts.joined(separator: " · ")
    }

    private func reprint(_ job: LaserJetPrinter.QueuedJob) {
        do {
            try LaserJetPrinter.reprint(job)
            store.toast(job.kind == .backsOnly
                ? "Back sides queued — a prompt shows how to put the stack back in"
                : "Queued to print again")
        } catch {
            store.toast("Couldn't queue the job again")
        }
        refreshQueue()
    }

    private func refreshQueue() {
        let jobs = LaserJetPrinter.queue()
        if jobs != queue { queue = jobs }
    }

    // MARK: Submit

    private func baseOptions() -> LaserJetPrinter.Options {
        var options = LaserJetPrinter.Options()
        options.copies = copies
        options.paper = paper
        options.economode = economode
        options.density = customDensity ? density : nil
        options.scale = customScale ? scale : nil
        options.margin = margin > 0 ? margin : nil
        return options
    }

    private func submit() {
        var options = baseOptions()
        options.layout = layout
        options.twoSided = effectiveTwoSided
        options.flip = effectiveFlip
        store.printToLaserJet(options: options, pageSpec: pages.spec(currentPage: store.currentPageNumber))
        dismiss()
    }

    private func printTestSheet() {
        var options = LaserJetPrinter.Options()
        options.copies = 1
        options.paper = paper
        options.twoSided = true
        options.flip = "long"
        store.printToLaserJet(options: options, pageSpec: "all", sourceFile: LaserJetPrinter.duplexTestSheet)
        dismiss()
    }
}
