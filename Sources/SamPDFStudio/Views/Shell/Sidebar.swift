import PDFKit
import SwiftUI

enum SidebarTab: String, CaseIterable, Identifiable {
    case pages, outline, annotations, search

    var id: String { rawValue }

    var title: String {
        switch self {
        case .pages: "Pages"
        case .outline: "Outline"
        case .annotations: "Comments"
        case .search: "Search"
        }
    }

    var systemImage: String {
        switch self {
        case .pages: "square.grid.2x2"
        case .outline: "list.bullet.indent"
        case .annotations: "text.bubble"
        case .search: "magnifyingglass"
        }
    }
}

/// Left sidebar: page thumbnails, document outline, comment list, search.
struct DocumentSidebar: View {
    @EnvironmentObject private var store: AppStore

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 2) {
                ForEach(SidebarTab.allCases) { tab in
                    Button {
                        store.sidebarTab = tab
                    } label: {
                        Image(systemName: tab.systemImage)
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(IconButtonStyle(isActive: store.sidebarTab == tab, size: 26))
                    .help(tab.title)
                }
            }
            .padding(.horizontal, Theme.Spacing.s)
            .padding(.vertical, Theme.Spacing.s - 2)

            Divider().overlay(Theme.separator)

            Group {
                switch store.sidebarTab {
                case .pages: PDFThumbnailRailView()
                case .outline: OutlineList()
                case .annotations: AnnotationList()
                case .search: SearchPanel()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Theme.sidebar)
    }
}

// MARK: - Outline

private struct OutlineNode: Identifiable {
    let id = UUID()
    let title: String
    let pageIndex: Int?
    let children: [OutlineNode]?
}

private struct OutlineList: View {
    @EnvironmentObject private var store: AppStore

    var body: some View {
        let nodes = Self.nodes(from: store.document?.outlineRoot, document: store.document)
        if nodes.isEmpty {
            SidebarEmptyState(
                systemImage: "list.bullet.indent",
                title: "No outline",
                message: "This document has no bookmarks yet.",
                actionTitle: "Add Bookmarks…"
            ) { store.launch(.bookmarks) }
        } else {
            List {
                OutlineGroup(nodes, children: \.children) { node in
                    Button {
                        go(to: node.pageIndex)
                    } label: {
                        HStack {
                            Text(node.title)
                                .font(Theme.Font.callout)
                                .lineLimit(2)
                            Spacer(minLength: 4)
                            if let index = node.pageIndex {
                                Text("\(index + 1)")
                                    .font(Theme.Font.caption.monospacedDigit())
                                    .foregroundStyle(Theme.textTertiary)
                            }
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
            .listStyle(.sidebar)
            .scrollContentBackground(.hidden)
            .id(store.documentRevision)
        }
    }

    private func go(to index: Int?) {
        guard let index, let view = store.pdfViewProxy.pdfView,
              let page = view.document?.page(at: index) else { return }
        view.go(to: page)
    }

    private static func nodes(from outline: PDFOutline?, document: PDFDocument?) -> [OutlineNode] {
        guard let outline, let document else { return [] }
        return (0..<outline.numberOfChildren).compactMap { i in
            guard let child = outline.child(at: i) else { return nil }
            let page = child.destination?.page.map { document.index(for: $0) }
            let kids = nodes(from: child, document: document)
            return OutlineNode(title: child.label ?? "Untitled", pageIndex: page, children: kids.isEmpty ? nil : kids)
        }
    }
}

// MARK: - Comments

private struct AnnotationRow: Identifiable {
    let id: Int
    let pageIndex: Int
    let kind: String
    let text: String
    let color: NSColor?
    let annotation: PDFAnnotation
}

private struct AnnotationList: View {
    @EnvironmentObject private var store: AppStore

    var body: some View {
        let rows = collectRows()
        if rows.isEmpty {
            SidebarEmptyState(
                systemImage: "text.bubble",
                title: "No comments",
                message: "Highlights, notes and markups you add appear here."
            )
        } else {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: Theme.Spacing.s) {
                    ForEach(rows) { row in
                        Button {
                            reveal(row)
                        } label: {
                            HStack(alignment: .top, spacing: Theme.Spacing.s) {
                                RoundedRectangle(cornerRadius: 2)
                                    .fill(Color(nsColor: row.color ?? .systemYellow))
                                    .frame(width: 4)
                                VStack(alignment: .leading, spacing: 3) {
                                    HStack {
                                        Text(row.kind)
                                            .font(Theme.Font.caption.weight(.semibold))
                                            .foregroundStyle(Theme.textPrimary)
                                        Spacer()
                                        Text("p. \(row.pageIndex + 1)")
                                            .font(Theme.Font.micro)
                                            .foregroundStyle(Theme.textTertiary)
                                    }
                                    if !row.text.isEmpty {
                                        Text(row.text)
                                            .font(Theme.Font.caption)
                                            .foregroundStyle(Theme.textSecondary)
                                            .lineLimit(3)
                                            .multilineTextAlignment(.leading)
                                    }
                                }
                            }
                            .padding(Theme.Spacing.s)
                            .background(RoundedRectangle(cornerRadius: Theme.Radius.medium).fill(Theme.surface))
                            .overlay(RoundedRectangle(cornerRadius: Theme.Radius.medium).strokeBorder(Theme.separator))
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(Theme.Spacing.s + 2)
            }
            .id(store.documentRevision)
        }
    }

    private func collectRows() -> [AnnotationRow] {
        guard let document = store.document else { return [] }
        var rows: [AnnotationRow] = []
        var counter = 0
        for index in 0..<document.pageCount {
            guard let page = document.page(at: index) else { continue }
            for annotation in page.annotations {
                let type = annotation.type ?? ""
                guard !["Link", "Widget", "Popup"].contains(type) else { continue }
                var text = annotation.contents ?? ""
                if text.isEmpty, ["Highlight", "Underline", "StrikeOut", "Squiggly"].contains(type) {
                    text = page.selection(for: annotation.bounds)?.string?
                        .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                }
                counter += 1
                rows.append(AnnotationRow(
                    id: counter, pageIndex: index, kind: Self.friendlyName(type),
                    text: text, color: annotation.color, annotation: annotation
                ))
            }
        }
        return rows
    }

    private func reveal(_ row: AnnotationRow) {
        guard let view = store.pdfViewProxy.pdfView, let page = row.annotation.page else { return }
        view.go(to: row.annotation.bounds.insetBy(dx: -40, dy: -40), on: page)
    }

    private static func friendlyName(_ type: String) -> String {
        switch type {
        case "Highlight": "Highlight"
        case "Underline": "Underline"
        case "StrikeOut": "Strikethrough"
        case "Squiggly": "Squiggly"
        case "Text": "Note"
        case "FreeText": "Text Box"
        case "Ink": "Drawing"
        case "Stamp": "Stamp"
        case "Square": "Rectangle"
        case "Circle": "Ellipse"
        case "Line": "Line"
        default: type.isEmpty ? "Annotation" : type
        }
    }
}

// MARK: - Search

private struct SearchHit: Identifiable {
    let id: Int
    let pageIndex: Int
    let context: AttributedString
    let selection: PDFSelection
}

private struct SearchPanel: View {
    @EnvironmentObject private var store: AppStore
    @State private var query = ""
    @State private var hits: [SearchHit] = []
    @State private var caseSensitive = false
    @State private var wholeWords = false
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 0) {
            VStack(spacing: Theme.Spacing.s) {
                HStack(spacing: 6) {
                    Image(systemName: "magnifyingglass")
                        .foregroundStyle(Theme.textTertiary)
                    TextField("Search document", text: $query)
                        .textFieldStyle(.plain)
                        .focused($focused)
                        .onSubmit(runSearch)
                    if !query.isEmpty {
                        Button {
                            query = ""
                            hits = []
                            store.pdfViewProxy.pdfView?.highlightedSelections = nil
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .foregroundStyle(Theme.textTertiary)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
                .background(RoundedRectangle(cornerRadius: Theme.Radius.medium).fill(Theme.surface))
                .overlay(RoundedRectangle(cornerRadius: Theme.Radius.medium).strokeBorder(focused ? Theme.accent.opacity(0.6) : Theme.outline))

                HStack {
                    Toggle("Match case", isOn: $caseSensitive)
                    Toggle("Whole words", isOn: $wholeWords)
                }
                .toggleStyle(.checkbox)
                .font(Theme.Font.caption)
                .foregroundStyle(Theme.textSecondary)
                .onChange(of: caseSensitive) { runSearch() }
                .onChange(of: wholeWords) { runSearch() }

                if !query.isEmpty {
                    Text(hits.isEmpty ? "No results" : "\(hits.count) result\(hits.count == 1 ? "" : "s")")
                        .font(Theme.Font.caption)
                        .foregroundStyle(Theme.textTertiary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .padding(Theme.Spacing.s + 2)

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 4) {
                    ForEach(hits) { hit in
                        Button {
                            reveal(hit)
                        } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Page \(hit.pageIndex + 1)")
                                    .font(Theme.Font.micro)
                                    .foregroundStyle(Theme.textTertiary)
                                Text(hit.context)
                                    .font(Theme.Font.caption)
                                    .foregroundStyle(Theme.textSecondary)
                                    .lineLimit(2)
                                    .multilineTextAlignment(.leading)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, Theme.Spacing.s)
                            .padding(.vertical, 6)
                            .background(RoundedRectangle(cornerRadius: Theme.Radius.small).fill(Theme.surface.opacity(0.7)))
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, Theme.Spacing.s + 2)
                .padding(.bottom, Theme.Spacing.s)
            }
        }
        .onAppear { focused = true }
        .onChange(of: store.documentRevision) { runSearch() }
    }

    private func runSearch() {
        guard let document = store.document else { return }
        let term = query.trimmingCharacters(in: .whitespaces)
        guard !term.isEmpty else {
            hits = []
            store.pdfViewProxy.pdfView?.highlightedSelections = nil
            return
        }
        var options: NSString.CompareOptions = caseSensitive ? [] : [.caseInsensitive, .diacriticInsensitive]
        options.insert(.widthInsensitive)
        var selections = document.findString(term, withOptions: options)
        if wholeWords {
            selections = selections.filter { Self.isWholeWord($0, in: document) }
        }
        let capped = Array(selections.prefix(500))
        hits = capped.enumerated().map { index, selection in
            let page = selection.pages.first
            let pageIndex = page.map { document.index(for: $0) } ?? 0
            return SearchHit(id: index, pageIndex: pageIndex, context: Self.context(for: selection, term: term), selection: selection)
        }
        for selection in capped {
            selection.color = NSColor(Theme.accent).withAlphaComponent(0.35)
        }
        store.pdfViewProxy.pdfView?.highlightedSelections = capped
        if let first = hits.first { reveal(first) }
    }

    private func reveal(_ hit: SearchHit) {
        guard let view = store.pdfViewProxy.pdfView else { return }
        view.setCurrentSelection(hit.selection, animate: true)
        view.scrollSelectionToVisible(nil)
    }

    private static func isWholeWord(_ selection: PDFSelection, in document: PDFDocument) -> Bool {
        guard let extended = selection.copy() as? PDFSelection else { return true }
        extended.extend(atStart: 1)
        extended.extend(atEnd: 1)
        let text = extended.string ?? ""
        let core = selection.string ?? ""
        guard let range = text.range(of: core) else { return true }
        let before = text[..<range.lowerBound].last
        let after = text[range.upperBound...].first
        return !(before?.isLetter ?? false) && !(after?.isLetter ?? false)
    }

    private static func context(for selection: PDFSelection, term: String) -> AttributedString {
        // Take the context from the hit's own page: extending the selection
        // runs across page breaks, so a "Page 1" hit showed page 2's text.
        var snippet = selection.string ?? term
        if let page = selection.pages.first, let text = page.string as NSString? {
            let range = selection.range(at: 0, on: page)
            if range.location != NSNotFound, NSMaxRange(range) <= text.length {
                let start = max(0, range.location - 30)
                let end = min(text.length, NSMaxRange(range) + 40)
                snippet = text.substring(with: NSRange(location: start, length: end - start))
            }
        }
        let raw = snippet.replacingOccurrences(of: "\n", with: " ")
        var result = AttributedString(raw)
        if let range = result.range(of: selection.string ?? term, options: .caseInsensitive) {
            result[range].font = .system(size: 11, weight: .bold)
            result[range].foregroundColor = Theme.accent
        }
        return result
    }
}

// MARK: - Shared

private struct SidebarEmptyState: View {
    let systemImage: String
    let title: String
    let message: String
    var actionTitle: String?
    var action: (() -> Void)?

    var body: some View {
        VStack(spacing: Theme.Spacing.s) {
            Image(systemName: systemImage)
                .font(.system(size: 26, weight: .light))
                .foregroundStyle(Theme.textTertiary)
            Text(title)
                .font(Theme.Font.headline)
                .foregroundStyle(Theme.textPrimary)
            Text(message)
                .font(Theme.Font.caption)
                .foregroundStyle(Theme.textSecondary)
                .multilineTextAlignment(.center)
            if let actionTitle, let action {
                Button(actionTitle, action: action)
                    .buttonStyle(SecondaryButtonStyle())
                    .padding(.top, 4)
            }
        }
        .padding(Theme.Spacing.l)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
