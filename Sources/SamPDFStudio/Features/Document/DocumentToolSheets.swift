import SwiftUI

// MARK: - Protect

struct ProtectSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss

    @State private var requireOpenPassword = false
    @State private var openPassword = ""
    @State private var openConfirm = ""
    @State private var ownerPassword = ""
    @State private var ownerConfirm = ""
    @State private var showPasswords = false
    @State private var allowed: Set<PDFPermission> = [.print, .printHQ, .copy, .forms, .accessibility]
    @State private var encryption: PDFEncryptionMethod = .aes256

    /// Toggles shown to the user; screen-reader access is always granted.
    private let editablePermissions: [PDFPermission] = [.print, .printHQ, .copy, .modify, .annotate, .forms, .assemble]

    private var openMismatch: Bool { requireOpenPassword && !openConfirm.isEmpty && openPassword != openConfirm }
    private var ownerMismatch: Bool { !ownerConfirm.isEmpty && ownerPassword != ownerConfirm }
    private var samePasswords: Bool { requireOpenPassword && !openPassword.isEmpty && openPassword == ownerPassword }

    private var validationMessage: String? {
        if requireOpenPassword {
            if openPassword.isEmpty { return "Enter a password to open the document." }
            if openPassword != openConfirm { return "The open passwords don't match." }
        }
        if ownerPassword.isEmpty { return "Enter a permissions password." }
        if ownerPassword != ownerConfirm { return "The permissions passwords don't match." }
        if samePasswords { return "Use a different permissions password — otherwise anyone who can open the file can change its security." }
        return nil
    }

    var body: some View {
        ToolSheetScaffold(
            tool: .protect,
            primaryTitle: "Save Protected Copy…",
            canSubmit: validationMessage == nil,
            width: 560,
            onSubmit: submit
        ) {
            FormSection("Open password", footnote: "Without one, anyone can open and read the file, but the permissions below still apply.") {
                Toggle("Require a password to open the document", isOn: $requireOpenPassword.animation(.easeOut(duration: 0.15)))
                    .toggleStyle(.switch)
                    .controlSize(.small)
                if requireOpenPassword {
                    passwordRows(password: $openPassword, confirm: $openConfirm, mismatch: openMismatch, placeholder: "Open password")
                }
            }

            FormSection("Permissions password", footnote: "Required to change security settings or remove the restrictions later.") {
                passwordRows(password: $ownerPassword, confirm: $ownerConfirm, mismatch: ownerMismatch, placeholder: "Permissions password")
                if samePasswords {
                    InlineMessage(kind: .warning, text: "Must differ from the open password.")
                }
                Toggle("Show passwords", isOn: $showPasswords)
                    .toggleStyle(.checkbox)
                    .font(Theme.Font.callout)
            }

            FormSection("Allowed actions") {
                HStack {
                    Text("\(editablePermissions.filter(allowed.contains).count) of \(editablePermissions.count) allowed")
                        .font(Theme.Font.callout)
                        .foregroundStyle(Theme.textSecondary)
                    Spacer()
                    Button("Allow All") { allowed.formUnion(editablePermissions) }
                        .buttonStyle(.link)
                    Text("·").foregroundStyle(Theme.textTertiary)
                    Button("Restrict All") { allowed.subtract(editablePermissions) }
                        .buttonStyle(.link)
                }
                .font(Theme.Font.callout)
                LazyVGrid(columns: [GridItem(.flexible(), alignment: .leading), GridItem(.flexible(), alignment: .leading)],
                          alignment: .leading, spacing: Theme.Spacing.s) {
                    ForEach(editablePermissions) { permission in
                        Toggle(isOn: binding(for: permission)) {
                            Label(permission.title, systemImage: permission.systemImage)
                                .font(Theme.Font.body)
                        }
                        .toggleStyle(.checkbox)
                    }
                }
                if allowed.contains(.printHQ) && !allowed.contains(.print) {
                    InlineMessage(kind: .info, text: "High-quality printing needs Printing to be allowed too.")
                }
            }

            FormSection("Encryption") {
                Picker("", selection: $encryption) {
                    ForEach(PDFEncryptionMethod.allCases) { method in
                        Text(method.title).tag(method)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
            }

            InfoBanner(kind: .info, text: "The protected copy is saved as a new file. The document you're editing stays unlocked so you can keep working.")
        }
    }

    @ViewBuilder
    private func passwordRows(password: Binding<String>, confirm: Binding<String>, mismatch: Bool, placeholder: String) -> some View {
        FormRow("Password") {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs + 2) {
                PasswordInput(placeholder: placeholder, text: password, reveal: showPasswords)
                PasswordStrengthMeter(password: password.wrappedValue)
            }
        }
        FormRow("Confirm") {
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                PasswordInput(placeholder: "Re-enter password", text: confirm, reveal: showPasswords)
                if mismatch {
                    InlineMessage(kind: .danger, text: "Passwords don't match.")
                } else if !confirm.wrappedValue.isEmpty {
                    InlineMessage(kind: .success, text: "Passwords match.")
                }
            }
        }
    }

    private func binding(for permission: PDFPermission) -> Binding<Bool> {
        Binding(
            get: { allowed.contains(permission) },
            set: { isOn in
                if isOn { allowed.insert(permission) } else { allowed.remove(permission) }
            }
        )
    }

    private func submit() {
        guard validationMessage == nil else { return }
        var permissions = allowed
        permissions.insert(.accessibility)
        store.protectDocument(
            ownerPassword: ownerPassword,
            userPassword: requireOpenPassword ? openPassword : nil,
            allow: permissions,
            encryption: encryption
        )
        dismiss()
    }
}

// MARK: - Unlock

struct UnlockSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss

    @State private var info: PDFSecurityInfo?
    @State private var loadError: String?
    @State private var password = ""
    @State private var reveal = false
    @State private var passwordError: String?
    @State private var verifying = false

    private var isProtected: Bool { info?.encrypted ?? false }

    var body: some View {
        ToolSheetScaffold(
            tool: .unlock,
            primaryTitle: verifying ? "Checking…" : "Remove Password",
            canSubmit: isProtected && !password.isEmpty && !verifying,
            onSubmit: submit
        ) {
            FormSection("Current security") {
                if let loadError {
                    InlineMessage(kind: .danger, text: loadError)
                } else if let info {
                    SecuritySummary(info: info)
                } else {
                    HStack(spacing: Theme.Spacing.s) {
                        ProgressView().controlSize(.small)
                        Text("Reading security settings…")
                            .font(Theme.Font.callout)
                            .foregroundStyle(Theme.textSecondary)
                    }
                }
            }

            if let info, info.encrypted {
                FormSection(
                    info.needsPass ? "Password" : "Permissions password",
                    footnote: info.needsPass
                        ? "Enter the password you use to open this PDF."
                        : "This file opens without a password; lifting its restrictions takes the permissions (owner) password."
                ) {
                    PasswordInput(placeholder: "Password", text: $password, reveal: reveal)
                        .onChange(of: password) { _, _ in passwordError = nil }
                        .onSubmit(submit)
                    HStack {
                        if let passwordError {
                            InlineMessage(kind: .danger, text: passwordError)
                        }
                        Spacer()
                        Toggle("Show", isOn: $reveal)
                            .toggleStyle(.checkbox)
                            .font(Theme.Font.callout)
                    }
                }
                InfoBanner(kind: .info, text: "The password is removed from the working copy as a new version (Undo restores it). Save to update the file on disk.")
            } else if info != nil {
                InfoBanner(kind: .success, text: "This PDF isn't password protected — there's nothing to remove.")
            }
        }
        .onAppear(perform: load)
    }

    private func load() {
        store.fetchSecurityInfo { result in
            switch result {
            case .success(let value): info = value
            case .failure(let error): loadError = error.localizedDescription
            }
        }
    }

    private func submit() {
        guard let info, info.encrypted, !password.isEmpty, !verifying else { return }
        verifying = true
        // Check the password first so a typo is reported inline instead of
        // after the sheet has closed.
        store.fetchSecurityInfo(password: password) { result in
            verifying = false
            switch result {
            case .success(let checked):
                let role = checked.authenticatedAs ?? "none"
                if role == "none" {
                    passwordError = "Incorrect password."
                } else if !info.needsPass && role != "owner" {
                    passwordError = "That isn't the permissions password."
                } else {
                    store.unlockDocument(password: password)
                    dismiss()
                }
            case .failure(let error):
                passwordError = error.localizedDescription
            }
        }
    }
}

private struct SecuritySummary: View {
    let info: PDFSecurityInfo

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.m) {
            IconTile(
                systemImage: info.encrypted ? "lock.fill" : "lock.open.fill",
                tint: info.encrypted ? Theme.danger : Theme.success,
                size: 34
            )
            VStack(alignment: .leading, spacing: Theme.Spacing.xs) {
                HStack(spacing: Theme.Spacing.s) {
                    Text(headline)
                        .font(Theme.Font.headline)
                        .foregroundStyle(Theme.textPrimary)
                    if let method = info.method {
                        Badge(text: method, tint: Theme.accent)
                    }
                }
                Text(detail)
                    .font(Theme.Font.callout)
                    .foregroundStyle(Theme.textSecondary)
                    .fixedSize(horizontal: false, vertical: true)
                if !info.denied.isEmpty {
                    FlowBadges(items: info.denied.map(\.title), tint: Theme.warning, prefix: "No ")
                }
            }
        }
    }

    private var headline: String {
        if !info.encrypted { return "Not protected" }
        return info.needsPass ? "Password required to open" : "Opens freely, with restrictions"
    }

    private var detail: String {
        if !info.encrypted { return "Anyone can open, print, copy and edit this file." }
        if info.locked { return "Permissions become visible once the password is entered." }
        if info.denied.isEmpty { return "All actions are allowed." }
        return "Restricted actions:"
    }
}

// MARK: - Flatten

struct FlattenSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss

    @State private var target: FlattenTarget = .all
    @State private var properties: PDFDocumentProperties?

    private var nothingToFlatten: Bool {
        guard let properties else { return false }
        switch target {
        case .annotations: return properties.annotations == 0
        case .forms: return !properties.hasForms
        case .all: return properties.annotations == 0 && !properties.hasForms
        }
    }

    var body: some View {
        ToolSheetScaffold(tool: .flatten, primaryTitle: "Flatten", canSubmit: !nothingToFlatten, onSubmit: submit) {
            FormSection("What to flatten") {
                VStack(spacing: Theme.Spacing.s) {
                    ForEach(FlattenTarget.allCases) { option in
                        OptionCard(
                            title: option.title,
                            detail: detail(for: option),
                            systemImage: icon(for: option),
                            trailing: count(for: option),
                            isSelected: target == option
                        ) { target = option }
                    }
                }
            }

            if nothingToFlatten {
                InfoBanner(kind: .warning, text: "This document has no \(target == .annotations ? "annotations" : target == .forms ? "form fields" : "annotations or form fields") to flatten.")
            } else {
                InfoBanner(kind: .info, text: "Flattening draws items exactly as they look into the page itself. They can no longer be edited, moved or deleted — but form values and comments stay visible and print reliably everywhere. Undo is available.")
            }
        }
        .onAppear {
            store.fetchDocumentProperties { result in
                if case .success(let value) = result { properties = value }
            }
        }
    }

    private func detail(for option: FlattenTarget) -> String {
        switch option {
        case .annotations: "Highlights, notes, stamps, drawings and signatures. Links stay clickable."
        case .forms: "Filled-in fields become plain text; the form stops being fillable."
        case .all: "Both annotations and form fields — ideal before sending a final copy."
        }
    }

    private func icon(for option: FlattenTarget) -> String {
        switch option {
        case .annotations: "highlighter"
        case .forms: "rectangle.and.pencil.and.ellipsis"
        case .all: "square.3.layers.3d.down.right"
        }
    }

    private func count(for option: FlattenTarget) -> String? {
        guard let properties else { return nil }
        switch option {
        case .annotations: return "\(properties.annotations)"
        case .forms: return properties.hasForms ? "Yes" : "None"
        case .all: return nil
        }
    }

    private func submit() {
        store.flattenDocument(target)
        dismiss()
    }
}

// MARK: - Metadata & privacy

struct MetadataSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss

    @State private var properties: PDFDocumentProperties?
    @State private var loadError: String?
    @State private var fields = PDFMetadataFields()
    @State private var original = PDFMetadataFields()
    @State private var remove: Set<PDFHiddenData> = []

    private var fieldsChanged: Bool { fields != original }
    private var clearsProperties: Bool { remove.contains(.metadata) }
    private var canSubmit: Bool { properties != nil && (fieldsChanged || !remove.isEmpty) }

    var body: some View {
        ToolSheetScaffold(
            tool: .metadata,
            primaryTitle: remove.isEmpty ? "Save Properties" : "Apply",
            canSubmit: canSubmit,
            width: 580,
            onSubmit: submit
        ) {
            if let loadError {
                InfoBanner(kind: .danger, text: loadError)
            } else if let properties {
                FileFactsCard(properties: properties, fileName: store.documentName)

                FormSection("Properties", footnote: clearsProperties ? "These fields will be cleared because “Document properties” is selected below." : nil) {
                    FormRow("Title", labelWidth: 90) { TextField("Untitled", text: $fields.title).textFieldStyle(.roundedBorder) }
                    FormRow("Author", labelWidth: 90) { TextField("Not set", text: $fields.author).textFieldStyle(.roundedBorder) }
                    FormRow("Subject", labelWidth: 90) { TextField("Not set", text: $fields.subject).textFieldStyle(.roundedBorder) }
                    FormRow("Keywords", labelWidth: 90) { TextField("Comma-separated", text: $fields.keywords).textFieldStyle(.roundedBorder) }
                    FormRow("Creator", labelWidth: 90) { TextField("App that created it", text: $fields.creator).textFieldStyle(.roundedBorder) }
                    FormRow("Producer", labelWidth: 90) { TextField("PDF library", text: $fields.producer).textFieldStyle(.roundedBorder) }
                    if fieldsChanged && !clearsProperties {
                        HStack {
                            Spacer()
                            Button("Revert Changes") { fields = original }
                                .buttonStyle(.link)
                                .font(Theme.Font.callout)
                        }
                    }
                }
                .disabled(clearsProperties)
                .opacity(clearsProperties ? 0.55 : 1)

                FormSection("Remove hidden data", footnote: "Strip information that travels with the file but isn't visible on the page. Applied as one undoable step.") {
                    HStack {
                        Text(remove.isEmpty ? "Nothing selected" : "\(remove.count) selected")
                            .font(Theme.Font.callout)
                            .foregroundStyle(Theme.textSecondary)
                        Spacer()
                        Button("Select Found") { remove = Set(PDFHiddenData.allCases.filter { found($0, in: properties) != nil }) }
                            .buttonStyle(.link)
                        Text("·").foregroundStyle(Theme.textTertiary)
                        Button("Clear") { remove = [] }
                            .buttonStyle(.link)
                    }
                    .font(Theme.Font.callout)
                    ForEach(PDFHiddenData.allCases) { item in
                        HiddenDataRow(
                            item: item,
                            found: found(item, in: properties),
                            isOn: Binding(
                                get: { remove.contains(item) },
                                set: { isOn in if isOn { remove.insert(item) } else { remove.remove(item) } }
                            )
                        )
                    }
                    if remove.contains(.annotations) && properties.annotations > 0 {
                        InlineMessage(kind: .warning, text: "All \(properties.annotations) comments and markup items will be deleted, not flattened.")
                    }
                }
            } else {
                HStack(spacing: Theme.Spacing.s) {
                    ProgressView().controlSize(.small)
                    Text("Reading document properties…")
                        .font(Theme.Font.callout)
                        .foregroundStyle(Theme.textSecondary)
                }
                .frame(maxWidth: .infinity, minHeight: 120)
            }
        }
        .onAppear(perform: load)
    }

    /// Badge text when the item is known to be present, nil otherwise.
    private func found(_ item: PDFHiddenData, in properties: PDFDocumentProperties) -> String? {
        switch item {
        case .metadata:
            let filled = [properties.title, properties.author, properties.subject, properties.keywords,
                          properties.creator, properties.producer].filter { !$0.isEmpty }.count
            return filled > 0 ? "\(filled) field\(filled == 1 ? "" : "s")" : nil
        case .xmp: return properties.hasXMP ? "Found" : nil
        case .javascript: return properties.hasJavaScript ? "Found" : nil
        case .embeddedFiles: return properties.embeddedFiles > 0 ? "\(properties.embeddedFiles)" : nil
        case .annotations: return properties.annotations > 0 ? "\(properties.annotations)" : nil
        case .links, .hiddenText, .thumbnails: return nil
        }
    }

    private func load() {
        store.fetchDocumentProperties { result in
            switch result {
            case .success(let value):
                properties = value
                fields = PDFMetadataFields(value)
                original = fields
            case .failure(let error):
                loadError = error.localizedDescription
            }
        }
    }

    private func submit() {
        guard canSubmit else { return }
        let newFields: PDFMetadataFields? = (fieldsChanged && !clearsProperties) ? fields : nil
        store.applyDocumentProperties(fields: newFields, remove: remove)
        dismiss()
    }
}

private struct FileFactsCard: View {
    let properties: PDFDocumentProperties
    let fileName: String

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.m) {
            HStack(spacing: Theme.Spacing.m) {
                IconTile(systemImage: "doc.richtext", tint: Theme.accent, size: 36)
                VStack(alignment: .leading, spacing: 2) {
                    Text(fileName.isEmpty ? "Current document" : fileName)
                        .font(Theme.Font.headline)
                        .foregroundStyle(Theme.textPrimary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(summaryLine)
                        .font(Theme.Font.callout)
                        .foregroundStyle(Theme.textSecondary)
                }
                Spacer()
                if properties.encrypted {
                    Badge(text: "Encrypted", tint: Theme.danger)
                }
            }
            Grid(alignment: .leading, horizontalSpacing: Theme.Spacing.l, verticalSpacing: Theme.Spacing.xs) {
                GridRow {
                    fact("Page size", pageSize)
                    fact("Created", format(properties.creationDate))
                }
                GridRow {
                    fact("PDF version", properties.pdfVersion ?? "—")
                    fact("Modified", format(properties.modificationDate))
                }
            }
            if !contents.isEmpty {
                FlowBadges(items: contents, tint: Theme.textSecondary, prefix: "")
            }
        }
        .padding(Theme.Spacing.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                .fill(Theme.accentSoft.opacity(0.5))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                .strokeBorder(Theme.separator, lineWidth: 1)
        )
    }

    private func fact(_ label: String, _ value: String) -> some View {
        HStack(spacing: Theme.Spacing.s) {
            Text(label)
                .font(Theme.Font.caption)
                .foregroundStyle(Theme.textTertiary)
                .frame(width: 70, alignment: .leading)
            Text(value)
                .font(Theme.Font.callout)
                .foregroundStyle(Theme.textPrimary)
                .lineLimit(1)
        }
    }

    private var summaryLine: String {
        let size = ByteCountFormatter.string(fromByteCount: Int64(properties.fileSize), countStyle: .file)
        return "\(properties.pages) page\(properties.pages == 1 ? "" : "s") · \(size)"
    }

    private var pageSize: String {
        guard let width = properties.pageWidth, let height = properties.pageHeight else { return "—" }
        let named: [(String, Double, Double)] = [("A4", 595, 842), ("Letter", 612, 792), ("Legal", 612, 1008), ("A3", 842, 1191), ("A5", 420, 595)]
        let short = min(width, height), long = max(width, height)
        if let match = named.first(where: { abs($0.1 - short) < 2 && abs($0.2 - long) < 2 }) {
            return "\(match.0)\(width > height ? " landscape" : "")"
        }
        return String(format: "%.0f × %.0f mm", width / 72 * 25.4, height / 72 * 25.4)
    }

    private var contents: [String] {
        var items: [String] = []
        if properties.bookmarks > 0 { items.append("\(properties.bookmarks) bookmark\(properties.bookmarks == 1 ? "" : "s")") }
        if properties.annotations > 0 { items.append("\(properties.annotations) annotation\(properties.annotations == 1 ? "" : "s")") }
        if properties.hasForms { items.append("Form fields") }
        if properties.embeddedFiles > 0 { items.append("\(properties.embeddedFiles) attachment\(properties.embeddedFiles == 1 ? "" : "s")") }
        if properties.hasJavaScript { items.append("JavaScript") }
        if properties.hasXMP { items.append("XMP metadata") }
        return items
    }

    private func format(_ date: Date?) -> String {
        guard let date else { return "—" }
        return date.formatted(date: .abbreviated, time: .shortened)
    }
}

private struct HiddenDataRow: View {
    let item: PDFHiddenData
    let found: String?
    @Binding var isOn: Bool

    var body: some View {
        Toggle(isOn: $isOn) {
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.s) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(item.title)
                        .font(Theme.Font.body)
                        .foregroundStyle(Theme.textPrimary)
                    Text(item.detail)
                        .font(Theme.Font.caption)
                        .foregroundStyle(Theme.textTertiary)
                }
                Spacer(minLength: Theme.Spacing.s)
                if let found {
                    Badge(text: found, tint: Theme.warning)
                }
            }
        }
        .toggleStyle(.checkbox)
    }
}

// MARK: - Bookmarks

struct BookmarksSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss

    @State private var entries: [PDFOutlineEntry] = []
    @State private var original: [PDFOutlineEntry] = []
    @State private var loaded = false
    @State private var loadError: String?
    @State private var selection: UUID?
    @FocusState private var focusedTitle: UUID?

    private var selectedIndex: Int? { entries.firstIndex { $0.id == selection } }
    private var untitledCount: Int { entries.filter { $0.title.trimmingCharacters(in: .whitespaces).isEmpty }.count }
    private var hasChanges: Bool { entries != original }

    var body: some View {
        ToolSheetScaffold(
            tool: .bookmarks,
            primaryTitle: "Save Bookmarks",
            canSubmit: loaded && hasChanges && untitledCount == 0,
            width: 620,
            onSubmit: submit
        ) {
            if let loadError {
                InfoBanner(kind: .danger, text: loadError)
            } else if !loaded {
                HStack(spacing: Theme.Spacing.s) {
                    ProgressView().controlSize(.small)
                    Text("Reading bookmarks…")
                        .font(Theme.Font.callout)
                        .foregroundStyle(Theme.textSecondary)
                }
                .frame(maxWidth: .infinity, minHeight: 120)
            } else {
                toolbar
                outlineList
                if untitledCount > 0 {
                    InlineMessage(kind: .danger, text: untitledCount == 1 ? "One bookmark needs a title." : "\(untitledCount) bookmarks need a title.")
                }
                Text("Tip: select a bookmark, then use the arrows to reorder or nest it. Saving creates a new version you can undo.")
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.textTertiary)
            }
        }
        .onAppear(perform: load)
    }

    // MARK: Toolbar

    private var toolbar: some View {
        HStack(spacing: Theme.Spacing.xs) {
            Button {
                addBookmark()
            } label: {
                Label("Add at Page \(store.currentPageNumber)", systemImage: "plus")
            }
            .buttonStyle(SecondaryButtonStyle())

            Spacer()

            Group {
                toolButton("decrease.indent", help: "Outdent", enabled: canOutdent) { outdent() }
                toolButton("increase.indent", help: "Indent", enabled: canIndent) { indent() }
                toolButton("arrow.up", help: "Move up", enabled: canMoveUp) { move(by: -1) }
                toolButton("arrow.down", help: "Move down", enabled: canMoveDown) { move(by: 1) }
                toolButton("arrow.right.circle", help: "Go to page", enabled: selectedIndex != nil) {
                    if let index = selectedIndex { store.goToPage(entries[index].page) }
                }
                toolButton("trash", help: "Delete", enabled: selectedIndex != nil) { deleteSelected() }
            }
        }
    }

    private func toolButton(_ systemImage: String, help: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: systemImage)
        }
        .buttonStyle(IconButtonStyle())
        .help(help)
        .disabled(!enabled)
    }

    // MARK: List

    @ViewBuilder
    private var outlineList: some View {
        if entries.isEmpty {
            VStack(spacing: Theme.Spacing.s) {
                Image(systemName: "bookmark")
                    .font(.system(size: 26, weight: .light))
                    .foregroundStyle(Theme.textTertiary)
                Text("No bookmarks yet")
                    .font(Theme.Font.headline)
                    .foregroundStyle(Theme.textPrimary)
                Text("Add one for the page you're viewing to build a table of contents.")
                    .font(Theme.Font.callout)
                    .foregroundStyle(Theme.textSecondary)
                    .multilineTextAlignment(.center)
            }
            .frame(maxWidth: .infinity, minHeight: 160)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                    .strokeBorder(Theme.separator, style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
            )
        } else {
            VStack(spacing: 0) {
                ForEach($entries) { $entry in
                    BookmarkRow(
                        entry: $entry,
                        pageCount: max(store.pageCount, 1),
                        isSelected: selection == entry.id,
                        focus: $focusedTitle,
                        onSelect: { selection = entry.id },
                        onGo: {
                            selection = entry.id
                            store.goToPage(entry.page)
                        }
                    )
                    if entry.id != entries.last?.id {
                        Divider().overlay(Theme.separator)
                    }
                }
            }
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                    .fill(Theme.surfaceRaised.opacity(0.6))
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                    .strokeBorder(Theme.separator, lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous))
        }
    }

    // MARK: Editing

    private var canIndent: Bool {
        guard let index = selectedIndex, index > 0 else { return false }
        return entries[index].level <= entries[index - 1].level
    }

    private var canOutdent: Bool {
        guard let index = selectedIndex else { return false }
        return entries[index].level > 1
    }

    private var canMoveUp: Bool { (selectedIndex ?? 0) > 0 }
    private var canMoveDown: Bool {
        guard let index = selectedIndex else { return false }
        return index < entries.count - 1
    }

    /// Indices of the entry and its nested children.
    private func subtree(at index: Int) -> Range<Int> {
        let level = entries[index].level
        var end = index + 1
        while end < entries.count && entries[end].level > level { end += 1 }
        return index..<end
    }

    private func addBookmark() {
        let page = store.currentPageNumber
        // Keep the list in reading order: insert after the last bookmark that
        // starts on or before this page, at that bookmark's level.
        let insertAt = (entries.lastIndex { $0.page <= page }).map { $0 + 1 } ?? 0
        let level = insertAt > 0 ? entries[insertAt - 1].level : 1
        let entry = PDFOutlineEntry(level: level, title: store.suggestedBookmarkTitle(forPage: page), page: page)
        entries.insert(entry, at: insertAt)
        normalizeLevels()
        selection = entry.id
        DispatchQueue.main.async { focusedTitle = entry.id }
    }

    private func indent() {
        guard canIndent, let index = selectedIndex else { return }
        for i in subtree(at: index) { entries[i].level += 1 }
        normalizeLevels()
    }

    private func outdent() {
        guard canOutdent, let index = selectedIndex else { return }
        for i in subtree(at: index) { entries[i].level = max(1, entries[i].level - 1) }
        normalizeLevels()
    }

    private func move(by offset: Int) {
        guard let index = selectedIndex else { return }
        let target = index + offset
        guard entries.indices.contains(target) else { return }
        entries.swapAt(index, target)
        normalizeLevels()
    }

    private func deleteSelected() {
        guard let index = selectedIndex else { return }
        entries.remove(at: index)
        normalizeLevels()
        selection = entries.isEmpty ? nil : entries[min(index, entries.count - 1)].id
    }

    /// Enforce a valid tree: first entry at level 1, no level skipped.
    private func normalizeLevels() {
        var previous = 0
        for i in entries.indices {
            let level = min(max(entries[i].level, 1), previous + 1)
            if entries[i].level != level { entries[i].level = level }
            previous = level
        }
    }

    private func load() {
        store.fetchOutline { result in
            switch result {
            case .success(let value):
                entries = value
                normalizeLevels()
                original = entries
                selection = entries.first?.id
                loaded = true
            case .failure(let error):
                loadError = error.localizedDescription
            }
        }
    }

    private func submit() {
        guard loaded, untitledCount == 0 else { return }
        var cleaned = entries
        for i in cleaned.indices {
            cleaned[i].title = cleaned[i].title.trimmingCharacters(in: .whitespacesAndNewlines)
            cleaned[i].page = min(max(cleaned[i].page, 1), max(store.pageCount, 1))
        }
        store.saveOutline(cleaned)
        dismiss()
    }
}

private struct BookmarkRow: View {
    @Binding var entry: PDFOutlineEntry
    let pageCount: Int
    let isSelected: Bool
    var focus: FocusState<UUID?>.Binding
    let onSelect: () -> Void
    let onGo: () -> Void

    var body: some View {
        HStack(spacing: Theme.Spacing.s) {
            Image(systemName: entry.level == 1 ? "bookmark.fill" : "bookmark")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(isSelected ? Theme.accent : Theme.textTertiary)
                .padding(.leading, CGFloat(entry.level - 1) * 18)
            TextField("Bookmark title", text: $entry.title)
                .textFieldStyle(.plain)
                .font(entry.level == 1 ? Theme.Font.body.weight(.medium) : Theme.Font.body)
                .foregroundStyle(Theme.textPrimary)
                .focused(focus, equals: entry.id)
            Spacer(minLength: Theme.Spacing.s)
            Stepper(value: $entry.page, in: 1...pageCount) {
                Text("p. \(entry.page)")
                    .font(Theme.Font.callout.monospacedDigit())
                    .foregroundStyle(Theme.textSecondary)
            }
            .controlSize(.small)
            .fixedSize()
            Button(action: onGo) {
                Image(systemName: "arrow.right.circle")
            }
            .buttonStyle(IconButtonStyle(size: 22))
            .help("Go to page \(entry.page)")
        }
        .padding(.horizontal, Theme.Spacing.m)
        .padding(.vertical, Theme.Spacing.xs + 2)
        .background(isSelected ? Theme.accentSoft : Color.clear)
        .contentShape(Rectangle())
        .simultaneousGesture(TapGesture().onEnded { onSelect() })
        .onChange(of: focus.wrappedValue) { _, newValue in
            if newValue == entry.id { onSelect() }
        }
    }
}

// MARK: - Shared pieces

/// Secure field that can be revealed as plain text.
private struct PasswordInput: View {
    let placeholder: String
    @Binding var text: String
    var reveal: Bool

    var body: some View {
        Group {
            if reveal {
                TextField(placeholder, text: $text)
            } else {
                SecureField(placeholder, text: $text)
            }
        }
        .textFieldStyle(.roundedBorder)
        .autocorrectionDisabled()
    }
}

/// Four-segment strength bar with a label.
private struct PasswordStrengthMeter: View {
    let password: String

    private var score: Int {
        guard !password.isEmpty else { return 0 }
        let length = password.count
        var classes = 0
        if password.rangeOfCharacter(from: .lowercaseLetters) != nil { classes += 1 }
        if password.rangeOfCharacter(from: .uppercaseLetters) != nil { classes += 1 }
        if password.rangeOfCharacter(from: .decimalDigits) != nil { classes += 1 }
        if password.rangeOfCharacter(from: CharacterSet.alphanumerics.inverted) != nil { classes += 1 }
        var value = 1
        if length >= 8 && classes >= 2 { value = 2 }
        if length >= 10 && classes >= 3 { value = 3 }
        if (length >= 12 && classes >= 3) || length >= 16 { value = 4 }
        return value
    }

    private var label: String {
        switch score {
        case 0: ""
        case 1: "Weak"
        case 2: "Fair"
        case 3: "Good"
        default: "Strong"
        }
    }

    private var color: Color {
        switch score {
        case 1: Theme.danger
        case 2: Theme.warning
        case 3: Theme.info
        default: Theme.success
        }
    }

    var body: some View {
        HStack(spacing: Theme.Spacing.s) {
            HStack(spacing: 3) {
                ForEach(0..<4, id: \.self) { index in
                    Capsule()
                        .fill(index < score ? color : Theme.separator)
                        .frame(height: 4)
                }
            }
            .frame(width: 120)
            Text(password.isEmpty ? "Use 12+ characters with mixed types" : label)
                .font(Theme.Font.caption)
                .foregroundStyle(password.isEmpty ? Theme.textTertiary : color)
        }
        .animation(.easeOut(duration: 0.15), value: score)
    }
}

/// Compact one-line validation message.
private struct InlineMessage: View {
    let kind: InfoBanner.Kind
    let text: String

    var body: some View {
        Label {
            Text(text)
                .fixedSize(horizontal: false, vertical: true)
        } icon: {
            Image(systemName: icon)
        }
        .font(Theme.Font.caption)
        .foregroundStyle(color)
    }

    private var icon: String {
        switch kind {
        case .info: "info.circle.fill"
        case .warning: "exclamationmark.triangle.fill"
        case .danger: "xmark.circle.fill"
        case .success: "checkmark.circle.fill"
        }
    }

    private var color: Color {
        switch kind {
        case .info: Theme.info
        case .warning: Theme.warning
        case .danger: Theme.danger
        case .success: Theme.success
        }
    }
}

/// Selectable card used for exclusive choices.
private struct OptionCard: View {
    let title: String
    let detail: String
    let systemImage: String
    var trailing: String?
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(alignment: .center, spacing: Theme.Spacing.m) {
                IconTile(systemImage: systemImage, tint: isSelected ? Theme.accent : Theme.textSecondary, size: 30)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(Theme.Font.headline)
                        .foregroundStyle(Theme.textPrimary)
                    Text(detail)
                        .font(Theme.Font.caption)
                        .foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .multilineTextAlignment(.leading)
                }
                Spacer(minLength: Theme.Spacing.s)
                if let trailing {
                    Badge(text: trailing, tint: isSelected ? Theme.accent : Theme.textSecondary)
                }
                Image(systemName: isSelected ? "largecircle.fill.circle" : "circle")
                    .font(.system(size: 15))
                    .foregroundStyle(isSelected ? Theme.accent : Theme.textTertiary)
            }
            .padding(Theme.Spacing.m)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                    .fill(isSelected ? Theme.accentSoft : Theme.surface)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                    .strokeBorder(isSelected ? Theme.accent.opacity(0.45) : Theme.separator, lineWidth: 1)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

/// Wrapping row of small capsules.
private struct FlowBadges: View {
    let items: [String]
    let tint: Color
    let prefix: String

    var body: some View {
        FlowLayout(spacing: Theme.Spacing.xs) {
            ForEach(items, id: \.self) { item in
                Badge(text: prefix + item.prefix(1).lowercased() + item.dropFirst(), tint: tint)
            }
        }
    }
}

/// Minimal flow layout: places children left to right, wrapping lines.
private struct FlowLayout: Layout {
    var spacing: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, lineHeight: CGFloat = 0, maxX: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0 && x + size.width > width {
                x = 0
                y += lineHeight + spacing
                lineHeight = 0
            }
            x += size.width + spacing
            maxX = max(maxX, x - spacing)
            lineHeight = max(lineHeight, size.height)
        }
        return CGSize(width: proposal.width ?? maxX, height: y + lineHeight)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, lineHeight: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > bounds.minX && x + size.width > bounds.maxX {
                x = bounds.minX
                y += lineHeight + spacing
                lineHeight = 0
            }
            subview.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            lineHeight = max(lineHeight, size.height)
        }
    }
}
