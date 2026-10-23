import AppKit
import SwiftUI
import UniformTypeIdentifiers

// MARK: - Compare

struct CompareSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss

    @State private var original: CompareSource?
    @State private var revised: CompareSource?
    @State private var mode: CompareMode = .text
    @State private var running = false
    @State private var result: EngineCompareResult?
    @State private var errorText: String?
    @State private var didSeed = false

    private var sameFile: Bool {
        guard let original, let revised else { return false }
        return original.url.standardizedFileURL == revised.url.standardizedFileURL
    }

    private var canCompare: Bool {
        original != nil && revised != nil && !sameFile && !running && !store.busy
    }

    var body: some View {
        ToolSheetScaffold(
            tool: .compare,
            primaryTitle: result == nil ? "Compare" : "Open Report",
            canSubmit: result == nil ? canCompare : true,
            width: 600,
            onSubmit: submit
        ) {
            if let result {
                CompareResultView(result: result, onOpen: { page in
                    store.openComparisonReport(result, at: page)
                    dismiss()
                }, onRestart: {
                    self.result = nil
                })
            } else {
                setup
            }
        }
        .onAppear {
            guard !didSeed else { return }
            didSeed = true
            original = store.openDocumentCompareSource
        }
    }

    @ViewBuilder private var setup: some View {
        FormSection("Documents") {
            HStack(alignment: .center, spacing: Theme.Spacing.s) {
                CompareSlot(role: "Original", source: $original, tint: Theme.danger,
                            openDocument: store.openDocumentCompareSource)
                Button {
                    swap(&original, &revised)
                } label: {
                    Image(systemName: "arrow.left.arrow.right")
                }
                .buttonStyle(IconButtonStyle())
                .help("Swap original and revised")
                .disabled(original == nil && revised == nil)
                CompareSlot(role: "Revised", source: $revised, tint: Theme.success,
                            openDocument: store.openDocumentCompareSource)
            }
        }

        if sameFile {
            InfoBanner(kind: .warning, text: "Original and Revised are the same file. Choose two different versions.")
        }

        FormSection("Compare by", footnote: mode.explanation) {
            Picker("", selection: $mode) {
                ForEach(CompareMode.allCases) { mode in
                    Text(mode.title).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
        }

        if running {
            HStack(spacing: Theme.Spacing.s) {
                ProgressView().controlSize(.small)
                Text(mode == .text ? "Comparing text…" : "Comparing pages…")
                    .font(Theme.Font.callout)
                    .foregroundStyle(Theme.textSecondary)
            }
        } else if let errorText {
            InfoBanner(kind: .danger, text: errorText)
        } else {
            InfoBanner(kind: .info, text: "The report opens as a new document: original on the left with deletions in red, revision on the right with insertions in green.")
        }
    }

    private func submit() {
        if let result {
            store.openComparisonReport(result)
            dismiss()
            return
        }
        guard let original, let revised, canCompare else { return }
        running = true
        errorText = nil
        store.compareDocuments(original: original, revised: revised, mode: mode) { outcome in
            running = false
            switch outcome {
            case .success(let value):
                result = value
            case .failure(let error):
                errorText = error.localizedDescription
            }
        }
    }
}

/// One drop-target card for a Compare input.
private struct CompareSlot: View {
    let role: String
    @Binding var source: CompareSource?
    let tint: Color
    let openDocument: CompareSource?
    @State private var targeted = false

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            HStack {
                Text(role.uppercased())
                    .font(Theme.Font.sectionLabel)
                    .tracking(0.6)
                    .foregroundStyle(tint)
                Spacer()
                if source != nil {
                    Button {
                        source = nil
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(Theme.textTertiary)
                    .help("Clear")
                }
            }
            HStack(spacing: Theme.Spacing.s) {
                Image(systemName: source == nil ? "doc.badge.plus" : "doc.richtext")
                    .font(.system(size: 20, weight: .regular))
                    .foregroundStyle(source == nil ? Theme.textTertiary : tint)
                    .frame(width: 26)
                VStack(alignment: .leading, spacing: 2) {
                    Text(source?.displayName ?? "Drop a PDF here")
                        .font(Theme.Font.headline)
                        .foregroundStyle(source == nil ? Theme.textSecondary : Theme.textPrimary)
                        .lineLimit(2)
                        .truncationMode(.middle)
                    if source?.isOpenDocument == true {
                        Badge(text: "Open document", tint: Theme.accent)
                    } else if let source {
                        Text(source.url.deletingLastPathComponent().path)
                            .font(Theme.Font.caption)
                            .foregroundStyle(Theme.textTertiary)
                            .lineLimit(1)
                            .truncationMode(.head)
                    } else {
                        Text("or choose a file")
                            .font(Theme.Font.caption)
                            .foregroundStyle(Theme.textTertiary)
                    }
                }
                Spacer(minLength: 0)
            }
            .frame(minHeight: 44, alignment: .leading)
            HStack(spacing: Theme.Spacing.s) {
                Button("Choose…", action: choose)
                    .buttonStyle(SecondaryButtonStyle())
                if let openDocument, source?.isOpenDocument != true {
                    Button("Use Open") { source = openDocument }
                        .buttonStyle(SecondaryButtonStyle())
                        .help("Use \(openDocument.displayName)")
                }
            }
        }
        .padding(Theme.Spacing.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                .fill(targeted ? tint.opacity(0.08) : Theme.surface)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                .strokeBorder(targeted ? tint : Theme.outline,
                              style: StrokeStyle(lineWidth: targeted ? 1.5 : 1, dash: source == nil ? [5, 4] : []))
        )
        .dropDestination(for: URL.self) { urls, _ in
            guard let pdf = urls.first(where: { $0.pathExtension.lowercased() == "pdf" }) else { return false }
            source = CompareSource(url: pdf, displayName: pdf.lastPathComponent)
            return true
        } isTargeted: { targeted = $0 }
        .animation(.easeOut(duration: 0.12), value: targeted)
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.pdf]
        panel.allowsMultipleSelection = false
        panel.message = "Choose the \(role.lowercased()) PDF"
        panel.prompt = "Choose"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        source = CompareSource(url: url, displayName: url.lastPathComponent)
    }
}

/// Summary after a comparison: counts and the list of changes.
private struct CompareResultView: View {
    let result: EngineCompareResult
    let onOpen: (Int?) -> Void
    let onRestart: () -> Void

    var body: some View {
        if result.identical {
            InfoBanner(kind: .success, text: result.mode == "text"
                       ? "No differences found. Both documents contain the same text."
                       : "No differences found in text or appearance.")
        } else if !result.textFound && result.mode == "text" {
            InfoBanner(kind: .warning, text: "Neither document has selectable text (scanned pages?). Try Visual mode, or recognize text first.")
        }

        HStack(spacing: Theme.Spacing.s) {
            StatTile(value: result.insertions, label: "Insertions", tint: Theme.success)
            StatTile(value: result.deletions, label: "Deletions", tint: Theme.danger)
            StatTile(value: result.changedPages.count, label: "Changed pages", tint: Theme.accent)
            if result.mode != "text" {
                StatTile(value: result.visualRegions, label: "Visual areas", tint: Theme.warning)
            }
        }

        if !result.changes.isEmpty {
            FormSection("Changes", footnote: footnote) {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(result.changes) { change in
                            ChangeRow(change: change) { onOpen(change.reportPage) }
                            if change.index < result.changes.count - 1 {
                                Divider().overlay(Theme.separator)
                            }
                        }
                    }
                }
                .frame(height: min(260, CGFloat(result.changes.count) * 46 + 4))
            }
        } else if !result.changedPages.isEmpty {
            FormSection("Changed pages") {
                ForEach(result.changedPages, id: \.reportPage) { page in
                    Button {
                        onOpen(page.reportPage)
                    } label: {
                        HStack {
                            Text(pageLabel(old: page.pageOld, new: page.pageNew))
                                .font(Theme.Font.body)
                                .foregroundStyle(Theme.textPrimary)
                            Spacer()
                            if page.visualRegions > 0 {
                                Badge(text: "\(page.visualRegions) visual", tint: Theme.warning)
                            }
                            Image(systemName: "chevron.right")
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundStyle(Theme.textTertiary)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }

        HStack {
            Button {
                onRestart()
            } label: {
                Label("New Comparison", systemImage: "arrow.uturn.backward")
            }
            .buttonStyle(SecondaryButtonStyle())
            Spacer()
            Text("Report: \(result.reportPages) pages")
                .font(Theme.Font.caption)
                .foregroundStyle(Theme.textTertiary)
        }
    }

    private var footnote: String {
        if result.truncated {
            return "Showing the first \(result.changes.count) of \(result.totalChanges) changes; the report marks every one. Click a change to open the report at that page."
        }
        return "Click a change to open the report at that page."
    }

    private func pageLabel(old: Int?, new: Int?) -> String {
        switch (old, new) {
        case let (old?, new?): return "Original p. \(old)  →  Revised p. \(new)"
        case let (old?, nil): return "Original p. \(old) (removed)"
        case let (nil, new?): return "Revised p. \(new) (added)"
        default: return "Page"
        }
    }
}

private struct StatTile: View {
    let value: Int
    let label: String
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("\(value)")
                .font(.system(size: 22, weight: .bold, design: .rounded))
                .foregroundStyle(tint)
                .monospacedDigit()
            Text(label)
                .font(Theme.Font.caption)
                .foregroundStyle(Theme.textSecondary)
        }
        .padding(.horizontal, Theme.Spacing.m)
        .padding(.vertical, Theme.Spacing.s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                .fill(tint.opacity(0.08))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                .strokeBorder(tint.opacity(0.2), lineWidth: 1)
        )
    }
}

private struct ChangeRow: View {
    let change: EngineCompareResult.Change
    let onOpen: () -> Void
    @State private var hovering = false

    private var kind: (String, Color) {
        switch change.type {
        case "insert": ("Inserted", Theme.success)
        case "delete": ("Deleted", Theme.danger)
        default: ("Changed", Theme.warning)
        }
    }

    var body: some View {
        Button(action: onOpen) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                Badge(text: kind.0, tint: kind.1)
                    .frame(width: 64, alignment: .leading)
                changeText
                    .font(Theme.Font.callout)
                    .lineLimit(2)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text(pages)
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.textTertiary)
                    .monospacedDigit()
            }
            .padding(.vertical, 7)
            .padding(.horizontal, Theme.Spacing.xs)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.small, style: .continuous)
                    .fill(hovering ? Theme.surfaceRaised : Color.clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help("Show in report (page \(change.reportPage))")
    }

    private var changeText: Text {
        switch change.type {
        case "delete":
            return Text(change.text).strikethrough(color: Theme.danger).foregroundColor(Theme.textSecondary)
        case "insert":
            return Text(change.text).foregroundColor(Theme.textPrimary)
        default:
            return Text(change.oldText ?? "").strikethrough(color: Theme.danger).foregroundColor(Theme.textSecondary)
                + Text("  →  ").foregroundColor(Theme.textTertiary)
                + Text(change.text).foregroundColor(Theme.textPrimary)
        }
    }

    private var pages: String {
        switch change.type {
        case "insert": return "p. \(change.pageNew ?? 0)"
        case "delete": return "p. \(change.pageOld ?? 0)"
        default:
            let old = change.pageOld ?? 0
            let new = change.pageNew ?? 0
            return old == new ? "p. \(old)" : "p. \(old) → \(new)"
        }
    }
}

// MARK: - Convert to PDF

struct ConvertToPDFSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss

    @State private var files: [URL] = []
    @State private var skipped: [String] = []
    @State private var pageSize: ConvertPageSize = .localeDefault
    @State private var margins: ConvertMargins = .normal
    @State private var combine = true
    @State private var combinedName = ""
    @State private var nameEdited = false
    @State private var customFolder: URL?
    @State private var targeted = false

    private var outputFolder: URL? {
        customFolder ?? files.first?.deletingLastPathComponent()
    }

    private var folderWritable: Bool {
        guard let outputFolder else { return true }
        return FileManager.default.isWritableFile(atPath: outputFolder.path)
    }

    private var willCombine: Bool { combine && files.count > 1 }

    private var trimmedName: String {
        combinedName.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var canSubmit: Bool {
        !files.isEmpty && folderWritable && (!willCombine || !trimmedName.isEmpty) && !store.busy
    }

    private var primaryTitle: String {
        if files.count > 1 && !combine { return "Create \(files.count) PDFs" }
        return "Create PDF"
    }

    var body: some View {
        ToolSheetScaffold(
            tool: .convertToPDF,
            primaryTitle: primaryTitle,
            canSubmit: canSubmit,
            width: 560,
            onSubmit: submit
        ) {
            dropZone

            if !skipped.isEmpty {
                InfoBanner(kind: .warning, text: "Skipped \(skipped.joined(separator: ", ")) — not a supported file type.")
            }

            if !files.isEmpty {
                fileList
                layoutSection
                outputSection
            }
        }
    }

    // MARK: Sections

    private var dropZone: some View {
        let compact = !files.isEmpty
        return VStack(spacing: compact ? Theme.Spacing.xs : Theme.Spacing.s) {
            if !compact {
                IconTile(systemImage: "arrow.down.doc", tint: ToolID.convertToPDF.category.color, size: 44)
            }
            Text(compact ? "Drop more files here" : "Drop files to turn into a PDF")
                .font(compact ? Theme.Font.callout.weight(.medium) : Theme.Font.headline)
                .foregroundStyle(Theme.textPrimary)
            if !compact {
                Text("Word, RTF, OpenDocument, text, Markdown, HTML, images or PDFs")
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.textSecondary)
                    .multilineTextAlignment(.center)
            }
            Button(compact ? "Add Files…" : "Choose Files…", action: chooseFiles)
                .buttonStyle(SecondaryButtonStyle())
                .padding(.top, compact ? 0 : Theme.Spacing.xs)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, compact ? Theme.Spacing.m : Theme.Spacing.xl)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                .fill(targeted ? Theme.accentSoft : Theme.surfaceRaised.opacity(0.5))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                .strokeBorder(targeted ? Theme.accent : Theme.outline,
                              style: StrokeStyle(lineWidth: targeted ? 1.5 : 1, dash: [6, 4]))
        )
        .dropDestination(for: URL.self) { urls, _ in
            add(urls)
            return true
        } isTargeted: { targeted = $0 }
        .animation(.easeOut(duration: 0.12), value: targeted)
    }

    private var fileList: some View {
        FormSection(files.count == 1 ? "1 file" : "\(files.count) files",
                    footnote: willCombine ? "Files are combined top to bottom. Use the arrows to reorder." : nil) {
            VStack(spacing: 0) {
                ForEach(Array(files.enumerated()), id: \.element) { index, url in
                    FileRow(
                        url: url,
                        canMoveUp: index > 0,
                        canMoveDown: index < files.count - 1,
                        onMoveUp: { move(index, by: -1) },
                        onMoveDown: { move(index, by: 1) },
                        onRemove: { remove(at: index) }
                    )
                    if index < files.count - 1 {
                        Divider().overlay(Theme.separator)
                    }
                }
            }
        }
    }

    private var layoutSection: some View {
        FormSection("Page layout",
                    footnote: "Applies to documents and text. Images are fitted to the page; PDFs keep their own pages.") {
            FormRow("Page size", labelWidth: 90) {
                Picker("", selection: $pageSize) {
                    ForEach(ConvertPageSize.allCases) { size in
                        Text(size.title).tag(size)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(maxWidth: 260)
            }
            FormRow("Margins", labelWidth: 90) {
                VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                    Picker("", selection: $margins) {
                        ForEach(ConvertMargins.allCases) { margin in
                            Text(margin.title).tag(margin)
                        }
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .frame(maxWidth: 260)
                    Text(margins.detail)
                        .font(Theme.Font.caption)
                        .foregroundStyle(Theme.textTertiary)
                }
            }
        }
    }

    private var outputSection: some View {
        FormSection("Output") {
            if files.count > 1 {
                Toggle(isOn: $combine) {
                    VStack(alignment: .leading, spacing: 1) {
                        Text("Combine into one PDF")
                            .font(Theme.Font.body)
                            .foregroundStyle(Theme.textPrimary)
                        Text(combine ? "One document, in the order above" : "One PDF for each file")
                            .font(Theme.Font.caption)
                            .foregroundStyle(Theme.textTertiary)
                    }
                }
                .toggleStyle(.switch)
                .controlSize(.small)
            }
            if willCombine {
                FormRow("File name", labelWidth: 90) {
                    HStack(spacing: Theme.Spacing.xs) {
                        TextField("Combined", text: Binding(
                            get: { combinedName },
                            set: { combinedName = $0; nameEdited = true }
                        ))
                        .textFieldStyle(.roundedBorder)
                        Text(".pdf")
                            .font(Theme.Font.body)
                            .foregroundStyle(Theme.textTertiary)
                    }
                }
            }
            FormRow("Save to", labelWidth: 90) {
                HStack(spacing: Theme.Spacing.s) {
                    Image(systemName: "folder")
                        .foregroundStyle(Theme.textSecondary)
                    Text(outputFolder.map { displayPath($0) } ?? "Next to the first file")
                        .font(Theme.Font.callout)
                        .foregroundStyle(Theme.textPrimary)
                        .lineLimit(1)
                        .truncationMode(.head)
                        .help(outputFolder?.path ?? "")
                    Spacer(minLength: Theme.Spacing.s)
                    Button("Change…", action: chooseFolder)
                        .buttonStyle(SecondaryButtonStyle())
                }
            }
            if !folderWritable {
                InfoBanner(kind: .danger, text: "Folio PDF can't write to this folder. Choose another location.")
            }
        }
    }

    // MARK: Actions

    private func add(_ urls: [URL]) {
        var rejected: [String] = []
        for url in urls {
            guard AppStore.isConvertible(url) else {
                rejected.append(url.lastPathComponent)
                continue
            }
            if !files.contains(url) { files.append(url) }
        }
        skipped = rejected
        if !nameEdited, let first = files.first {
            combinedName = first.deletingPathExtension().lastPathComponent
        }
    }

    private func move(_ index: Int, by offset: Int) {
        let target = index + offset
        guard files.indices.contains(index), files.indices.contains(target) else { return }
        withAnimation(.easeOut(duration: 0.15)) {
            files.swapAt(index, target)
        }
        if !nameEdited, let first = files.first {
            combinedName = first.deletingPathExtension().lastPathComponent
        }
    }

    private func remove(at index: Int) {
        guard files.indices.contains(index) else { return }
        _ = withAnimation(.easeOut(duration: 0.15)) {
            files.remove(at: index)
        }
        if !nameEdited {
            combinedName = files.first?.deletingPathExtension().lastPathComponent ?? ""
        }
    }

    private func chooseFiles() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = AppStore.convertibleContentTypes
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.message = "Choose documents, text, web pages or images to turn into a PDF"
        panel.prompt = "Add"
        guard panel.runModal() == .OK else { return }
        add(panel.urls)
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = outputFolder
        panel.prompt = "Save Here"
        panel.message = "Choose where to save the PDF"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        customFolder = url
    }

    private func submit() {
        guard canSubmit, let folder = outputFolder else { return }
        let options = ConvertOptions(
            pageSize: pageSize,
            margins: margins,
            combine: combine,
            outputFolder: folder,
            combinedName: trimmedName.isEmpty ? "Combined" : trimmedName
        )
        store.convertFilesToPDF(files, options: options)
        dismiss()
    }

    private func displayPath(_ url: URL) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let path = url.path
        return path.hasPrefix(home) ? "~" + path.dropFirst(home.count) : path
    }
}

private struct FileRow: View {
    let url: URL
    let canMoveUp: Bool
    let canMoveDown: Bool
    let onMoveUp: () -> Void
    let onMoveDown: () -> Void
    let onRemove: () -> Void
    @State private var hovering = false

    private var kind: String {
        switch url.pathExtension.lowercased() {
        case "docx", "doc", "wordml": "Word document"
        case "rtf", "rtfd": "Rich text"
        case "odt": "OpenDocument text"
        case "txt", "text": "Plain text"
        case "md", "markdown": "Markdown"
        case "html", "htm", "xhtml": "Web page"
        case "pdf": "PDF — pages kept as is"
        default: "Image"
        }
    }

    var body: some View {
        HStack(spacing: Theme.Spacing.s) {
            Image(nsImage: NSWorkspace.shared.icon(forFile: url.path))
                .resizable()
                .frame(width: 26, height: 26)
            VStack(alignment: .leading, spacing: 1) {
                Text(url.lastPathComponent)
                    .font(Theme.Font.body)
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(kind)
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.textTertiary)
            }
            Spacer(minLength: Theme.Spacing.s)
            HStack(spacing: 0) {
                Button(action: onMoveUp) { Image(systemName: "chevron.up") }
                    .buttonStyle(IconButtonStyle(size: 24))
                    .disabled(!canMoveUp)
                    .help("Move up")
                Button(action: onMoveDown) { Image(systemName: "chevron.down") }
                    .buttonStyle(IconButtonStyle(size: 24))
                    .disabled(!canMoveDown)
                    .help("Move down")
                Button(action: onRemove) { Image(systemName: "minus.circle") }
                    .buttonStyle(IconButtonStyle(size: 24))
                    .help("Remove")
            }
            .opacity(hovering ? 1 : 0.55)
        }
        .padding(.vertical, 6)
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
    }
}
