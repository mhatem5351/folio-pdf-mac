import SwiftUI

/// "All Tools" sheet: every catalog tool as a tile, grouped by category,
/// with instant filtering.
struct ToolsHubView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""

    private let columns = [GridItem(.adaptive(minimum: 200), spacing: Theme.Spacing.m)]

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: Theme.Spacing.m) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("All Tools")
                        .font(Theme.Font.title)
                        .foregroundStyle(Theme.textPrimary)
                    Text(store.document == nil ? "Open a PDF to use document tools" : store.documentName)
                        .font(Theme.Font.callout)
                        .foregroundStyle(Theme.textSecondary)
                        .lineLimit(1)
                }
                Spacer()
                HStack(spacing: 6) {
                    Image(systemName: "magnifyingglass")
                        .foregroundStyle(Theme.textTertiary)
                    TextField("Filter tools", text: $query)
                        .textFieldStyle(.plain)
                        .frame(width: 180)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(RoundedRectangle(cornerRadius: Theme.Radius.medium).fill(Theme.surfaceRaised))
                Button { dismiss() } label: { Image(systemName: "xmark") }
                    .buttonStyle(IconButtonStyle())
                    .keyboardShortcut(.cancelAction)
            }
            .padding(Theme.Spacing.xl)

            Divider().overlay(Theme.separator)

            ScrollView {
                VStack(alignment: .leading, spacing: Theme.Spacing.xl) {
                    ForEach(ToolCategory.allCases) { category in
                        let tools = filtered(in: category)
                        if !tools.isEmpty {
                            VStack(alignment: .leading, spacing: Theme.Spacing.s + 2) {
                                HStack(spacing: 6) {
                                    Circle().fill(category.color).frame(width: 7, height: 7)
                                    Text(category.title.uppercased())
                                        .font(Theme.Font.sectionLabel)
                                        .tracking(0.6)
                                        .foregroundStyle(Theme.textTertiary)
                                }
                                LazyVGrid(columns: columns, alignment: .leading, spacing: Theme.Spacing.m) {
                                    ForEach(tools) { tool in
                                        ToolTile(tool: tool, enabled: store.document != nil || tool.worksWithoutDocument) {
                                            dismiss()
                                            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
                                                store.launch(tool)
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                .padding(Theme.Spacing.xl)
            }
        }
        .frame(width: 760, height: 600)
        .background(Theme.surface)
    }

    private func filtered(in category: ToolCategory) -> [ToolID] {
        let all = ToolID.allCases.filter { $0.category == category }
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return all }
        return all.filter { "\($0.title) \($0.subtitle) \($0.keywords)".lowercased().contains(q) }
    }
}

private struct ToolTile: View {
    let tool: ToolID
    let enabled: Bool
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(alignment: .top, spacing: Theme.Spacing.m) {
                IconTile(systemImage: tool.systemImage, tint: tool.category.color, size: 34)
                VStack(alignment: .leading, spacing: 3) {
                    Text(tool.title)
                        .font(Theme.Font.headline)
                        .foregroundStyle(Theme.textPrimary)
                    Text(tool.subtitle)
                        .font(Theme.Font.caption)
                        .foregroundStyle(Theme.textSecondary)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                }
                Spacer(minLength: 0)
            }
            .padding(Theme.Spacing.m)
            .frame(maxWidth: .infinity, minHeight: 74, alignment: .topLeading)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                    .fill(hovering ? Theme.surfaceRaised : Theme.surface)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                    .strokeBorder(hovering ? tool.category.color.opacity(0.45) : Theme.separator)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.45)
        .onHover { hovering = $0 }
    }
}

// MARK: - Command palette

/// ⌘K palette: fuzzy-ish search over tools, modes and common commands.
struct CommandPaletteView: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var highlighted = 0
    @FocusState private var focused: Bool

    private struct Command: Identifiable {
        let id: String
        let title: String
        let subtitle: String
        let systemImage: String
        let tint: Color
        let keywords: String
        let enabled: Bool
        let run: (AppStore) -> Void
    }

    var body: some View {
        let results = matches()
        VStack(spacing: 0) {
            HStack(spacing: Theme.Spacing.s) {
                Image(systemName: "command")
                    .foregroundStyle(Theme.accent)
                    .font(.system(size: 15, weight: .semibold))
                TextField("Search tools and commands…", text: $query)
                    .textFieldStyle(.plain)
                    .font(.system(size: 17))
                    .focused($focused)
                    .onSubmit { execute(results) }
                    .onChange(of: query) { highlighted = 0 }
            }
            .padding(Theme.Spacing.l)

            Divider().overlay(Theme.separator)

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 2) {
                        ForEach(Array(results.enumerated()), id: \.element.id) { index, command in
                            Button {
                                highlighted = index
                                execute(results)
                            } label: {
                                HStack(spacing: Theme.Spacing.m) {
                                    IconTile(systemImage: command.systemImage, tint: command.tint, size: 28)
                                    VStack(alignment: .leading, spacing: 1) {
                                        Text(command.title)
                                            .font(Theme.Font.body.weight(.medium))
                                            .foregroundStyle(Theme.textPrimary)
                                        Text(command.subtitle)
                                            .font(Theme.Font.caption)
                                            .foregroundStyle(Theme.textSecondary)
                                            .lineLimit(1)
                                    }
                                    Spacer()
                                    if index == highlighted {
                                        Image(systemName: "return")
                                            .font(Theme.Font.caption)
                                            .foregroundStyle(Theme.textTertiary)
                                    }
                                }
                                .padding(.horizontal, Theme.Spacing.m)
                                .padding(.vertical, 7)
                                .background(
                                    RoundedRectangle(cornerRadius: Theme.Radius.medium)
                                        .fill(index == highlighted ? Theme.accentSoft : Color.clear)
                                )
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .disabled(!command.enabled)
                            .opacity(command.enabled ? 1 : 0.4)
                            .id(index)
                        }
                    }
                    .padding(Theme.Spacing.s)
                }
                .onChange(of: highlighted) { _, index in proxy.scrollTo(index) }
            }
            .frame(height: 360)
        }
        .frame(width: 560)
        .background(Theme.surface)
        .onAppear { focused = true }
        .onKeyPress(.downArrow) {
            highlighted = min(highlighted + 1, max(results.count - 1, 0))
            return .handled
        }
        .onKeyPress(.upArrow) {
            highlighted = max(highlighted - 1, 0)
            return .handled
        }
        .onKeyPress(.escape) {
            dismiss()
            return .handled
        }
    }

    private func execute(_ results: [Command]) {
        guard results.indices.contains(highlighted) else { return }
        let command = results[highlighted]
        guard command.enabled else { return }
        dismiss()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
            command.run(store)
        }
    }

    private func matches() -> [Command] {
        let hasDoc = store.document != nil
        var all: [Command] = [
            Command(id: "open", title: "Open PDF…", subtitle: "Browse for a document", systemImage: "folder", tint: Theme.accent, keywords: "file browse", enabled: true) { $0.openPDF() },
            Command(id: "merge", title: "Merge PDFs…", subtitle: "Combine several files into one", systemImage: "square.stack.3d.down.right", tint: ToolCategory.organize.color, keywords: "combine join", enabled: true) { $0.mergePDFFiles() },
            Command(id: "images", title: "New PDF from Images…", subtitle: "Photos and scans to PDF", systemImage: "photo.on.rectangle.angled", tint: ToolCategory.convert.color, keywords: "jpg png scan", enabled: true) { $0.newFromImages() },
            Command(id: "save", title: "Save", subtitle: "Write changes to the original file", systemImage: "square.and.arrow.down", tint: Theme.accent, keywords: "write", enabled: hasDoc && store.isDirty) { $0.save() },
            Command(id: "saveas", title: "Save As…", subtitle: "Save a copy under a new name", systemImage: "square.and.arrow.down.on.square", tint: Theme.accent, keywords: "copy duplicate", enabled: hasDoc) { $0.saveAs() },
            Command(id: "signatures", title: "Manage Signatures…", subtitle: "Draw or type your signature", systemImage: "signature", tint: ToolCategory.edit.color, keywords: "sign", enabled: true) { $0.showSignatureManager = true }
        ]
        for mode in PDFOperation.allCases {
            all.append(Command(id: "mode-\(mode.rawValue)", title: "\(mode.title) Mode", subtitle: "Switch the canvas to \(mode.title.lowercased())", systemImage: mode.systemImage, tint: Theme.accent, keywords: "mode switch", enabled: hasDoc) { $0.selectedOperation = mode })
        }
        for tool in ToolID.allCases {
            all.append(Command(id: "tool-\(tool.rawValue)", title: tool.title, subtitle: tool.subtitle, systemImage: tool.systemImage, tint: tool.category.color, keywords: tool.keywords, enabled: hasDoc || tool.worksWithoutDocument) { $0.launch(tool) })
        }
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return all }
        let terms = q.split(separator: " ")
        return all
            .filter { command in
                let hay = "\(command.title) \(command.subtitle) \(command.keywords)".lowercased()
                return terms.allSatisfy { hay.contains($0) }
            }
            .sorted { lhs, rhs in
                let l = lhs.title.lowercased().hasPrefix(q), r = rhs.title.lowercased().hasPrefix(q)
                if l != r { return l }
                return lhs.enabled && !rhs.enabled
            }
    }
}
