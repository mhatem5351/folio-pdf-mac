import SwiftUI

/// Form Fields tool: browse fields by page, edit their properties, place new
/// ones, auto-detect blanks ("Prepare Form"), and fill values in bulk.
struct FormFieldsSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss

    private enum Mode: String, CaseIterable, Identifiable {
        case edit, fill
        var id: String { rawValue }
        var title: String { self == .edit ? "Edit Fields" : "Fill Values" }
    }

    private enum Detection: Equatable {
        case idle
        case running
        case found([PDFFormFieldSuggestion])
    }

    @State private var mode: Mode = .edit
    @State private var fields: [PDFFormField] = []
    @State private var loading = true
    @State private var loadError: String?
    @State private var search = ""
    @State private var selectedName: String?
    @State private var pendingSelection: String?
    @State private var draft = FormFieldDraft()
    @State private var detection: Detection = .idle
    @State private var fillValues: [String: PDFFormFieldValue] = [:]

    private let paneHeight: CGFloat = 384

    var body: some View {
        ToolSheetScaffold(
            tool: .forms,
            primaryTitle: primaryTitle,
            canSubmit: canSubmit,
            width: 720,
            onSubmit: submit
        ) {
            header
            detectionBanner
            content
        }
        .onAppear(perform: reload)
        .onChange(of: store.documentRevision) { reload() }
        .onChange(of: selectedName) { syncDraft() }
    }

    // MARK: Header

    private var header: some View {
        HStack(spacing: Theme.Spacing.s) {
            Picker("", selection: $mode) {
                ForEach(Mode.allCases) { mode in
                    Text(mode.title).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 210)

            if !fields.isEmpty {
                Badge(text: "\(fields.count) field\(fields.count == 1 ? "" : "s")", tint: Theme.textSecondary)
            }
            Spacer()
            addFieldMenu
            Button {
                runDetection()
            } label: {
                Label("Auto-Detect", systemImage: "wand.and.stars")
            }
            .buttonStyle(SecondaryButtonStyle())
            .disabled(store.busy || detection == .running)
            .help("Find blanks like “Name: ____”, empty boxes and checkboxes")
        }
    }

    private var addFieldMenu: some View {
        Menu {
            ForEach(PDFFormFieldKind.allCases) { kind in
                Button {
                    addField(kind)
                } label: {
                    Label(kind.title, systemImage: kind.systemImage)
                }
            }
        } label: {
            Label("Add Field", systemImage: "plus")
                .font(Theme.Font.body.weight(.medium))
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.visible)
        .fixedSize()
        .padding(.horizontal, Theme.Spacing.m)
        .padding(.vertical, 5)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                .fill(Theme.accentSoft)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                .strokeBorder(Theme.accent.opacity(0.35), lineWidth: 1)
        )
        .disabled(store.busy)
        .help("Place a field in the middle of page \(store.currentPageNumber); fine-tune it afterwards")
    }

    // MARK: Detection

    @ViewBuilder
    private var detectionBanner: some View {
        switch detection {
        case .idle:
            EmptyView()
        case .running:
            HStack(spacing: Theme.Spacing.s) {
                ProgressView().controlSize(.small)
                Text("Looking for blanks, boxes and checkboxes…")
                    .font(Theme.Font.callout)
                    .foregroundStyle(Theme.textSecondary)
                Spacer()
            }
            .padding(Theme.Spacing.m)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                    .fill(Theme.surfaceRaised)
            )
        case .found(let suggestions) where suggestions.isEmpty:
            HStack(alignment: .top) {
                InfoBanner(kind: .info, text: "No likely fields found. Use Add Field to place fields by hand.")
                Button {
                    detection = .idle
                } label: {
                    Image(systemName: "xmark")
                }
                .buttonStyle(IconButtonStyle())
            }
        case .found(let suggestions):
            DetectionCard(
                suggestions: suggestions,
                busy: store.busy,
                onDismiss: { detection = .idle },
                onApply: applyDetection
            )
        }
    }

    // MARK: Content

    @ViewBuilder
    private var content: some View {
        if loading && fields.isEmpty {
            HStack {
                Spacer()
                ProgressView("Reading fields…")
                    .controlSize(.small)
                Spacer()
            }
            .frame(height: paneHeight)
        } else if let loadError {
            InfoBanner(kind: .danger, text: loadError)
        } else if fields.isEmpty {
            emptyState
        } else if mode == .edit {
            HStack(alignment: .top, spacing: Theme.Spacing.l) {
                fieldList
                Divider().overlay(Theme.separator)
                inspector
            }
            .frame(height: paneHeight)
        } else {
            FillValuesPane(fields: fields, values: $fillValues)
                .frame(height: paneHeight)
        }
    }

    private var emptyState: some View {
        VStack(spacing: Theme.Spacing.m) {
            IconTile(systemImage: "rectangle.and.pencil.and.ellipsis", tint: Theme.accent, size: 52)
            Text("No form fields yet")
                .font(Theme.Font.title)
                .foregroundStyle(Theme.textPrimary)
            Text("Auto-Detect scans the pages for blanks such as “Name: ______”, empty boxes, small squares and signature lines, and turns them into fillable fields. You can also place fields one by one with Add Field.")
                .font(Theme.Font.callout)
                .foregroundStyle(Theme.textSecondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 440)
                .fixedSize(horizontal: false, vertical: true)
            Button {
                runDetection()
            } label: {
                Label("Auto-Detect Fields", systemImage: "wand.and.stars")
            }
            .buttonStyle(PrimaryButtonStyle())
            .disabled(store.busy || detection == .running)
        }
        .frame(maxWidth: .infinity)
        .frame(height: paneHeight)
    }

    // MARK: Field list

    private var filteredFields: [PDFFormField] {
        let query = search.trimmingCharacters(in: .whitespaces).lowercased()
        guard !query.isEmpty else { return fields }
        return fields.filter { field in
            field.name.lowercased().contains(query)
                || (field.kind?.title.lowercased().contains(query) ?? false)
                || field.value.stringValue.lowercased().contains(query)
        }
    }

    private var fieldsByPage: [(page: Int, fields: [PDFFormField])] {
        let grouped = Dictionary(grouping: filteredFields, by: \.page)
        return grouped.keys.sorted().map { page in
            (page, grouped[page]?.sorted { lhs, rhs in
                if abs(lhs.rect[1] - rhs.rect[1]) > 4 { return lhs.rect[1] < rhs.rect[1] }
                return lhs.rect[0] < rhs.rect[0]
            } ?? [])
        }
    }

    private var fieldList: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            SearchField(text: $search)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(fieldsByPage, id: \.page) { group in
                        HStack {
                            Text("PAGE \(group.page)")
                                .font(Theme.Font.sectionLabel)
                                .tracking(0.6)
                                .foregroundStyle(Theme.textTertiary)
                            Spacer()
                            Text("\(group.fields.count)")
                                .font(Theme.Font.micro)
                                .foregroundStyle(Theme.textTertiary)
                        }
                        .padding(.horizontal, Theme.Spacing.xs)
                        .padding(.top, Theme.Spacing.s)
                        .padding(.bottom, Theme.Spacing.xxs)
                        ForEach(group.fields) { field in
                            Button {
                                selectedName = field.name
                            } label: {
                                FieldRow(field: field, isSelected: field.name == selectedName)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    if filteredFields.isEmpty {
                        Text("No fields match “\(search)”")
                            .font(Theme.Font.callout)
                            .foregroundStyle(Theme.textTertiary)
                            .padding(.top, Theme.Spacing.l)
                            .frame(maxWidth: .infinity)
                    }
                }
            }
        }
        .frame(width: 236)
    }

    // MARK: Inspector

    private var selectedField: PDFFormField? {
        guard let selectedName else { return nil }
        return fields.first { $0.name == selectedName }
    }

    @ViewBuilder
    private var inspector: some View {
        if let field = selectedField {
            FormFieldInspector(
                field: field,
                draft: $draft,
                otherNames: Set(fields.map(\.name)).subtracting([field.name]),
                pageSize: pageSize(for: field.page),
                busy: store.busy,
                onSave: { save(field) },
                onRevert: { draft = FormFieldDraft(field: field) },
                onDelete: { delete(field) },
                onAddRadioButton: { addRadioButton(to: field) }
            )
        } else {
            VStack(spacing: Theme.Spacing.s) {
                Image(systemName: "cursorarrow.click")
                    .font(.system(size: 26, weight: .light))
                    .foregroundStyle(Theme.textTertiary)
                Text("Select a field to edit its properties")
                    .font(Theme.Font.callout)
                    .foregroundStyle(Theme.textSecondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func pageSize(for page: Int) -> CGSize {
        store.document?.page(at: max(0, page - 1))?.bounds(for: .cropBox).size ?? CGSize(width: 612, height: 792)
    }

    // MARK: Footer action

    private var draftChanges: PDFFormFieldChanges? {
        guard let field = selectedField else { return nil }
        let changes = draft.changes(from: field)
        return changes.isEmpty ? nil : changes
    }

    private var changedFillValues: [String: PDFFormFieldValue] {
        var changed: [String: PDFFormFieldValue] = [:]
        for field in fields {
            if let value = fillValues[field.name], value != field.value {
                changed[field.name] = value
            }
        }
        return changed
    }

    private var primaryTitle: String {
        switch mode {
        case .edit:
            return draftChanges == nil ? "Done" : "Save & Close"
        case .fill:
            let count = changedFillValues.count
            return count == 0 ? "Apply Values" : "Apply \(count) Value\(count == 1 ? "" : "s")"
        }
    }

    private var canSubmit: Bool {
        switch mode {
        case .edit:
            guard let field = selectedField, draftChanges != nil else { return true }
            return !store.busy && draft.validationError(for: field, otherNames: Set(fields.map(\.name)).subtracting([field.name])) == nil
        case .fill:
            return !store.busy && !changedFillValues.isEmpty
        }
    }

    private func submit() {
        switch mode {
        case .edit:
            guard let field = selectedField, let changes = draftChanges else {
                dismiss()
                return
            }
            store.updateFormField(name: field.name, changes: changes) { dismiss() }
        case .fill:
            store.fillFormFields(changedFillValues) { dismiss() }
        }
    }

    // MARK: Actions

    private func reload() {
        loading = true
        store.loadFormFields { result in
            loading = false
            switch result {
            case .success(let list):
                loadError = nil
                fields = list
                fillValues = fillValues.filter { name, _ in list.contains { $0.name == name } }
                if let pending = pendingSelection, list.contains(where: { $0.name == pending }) {
                    selectedName = pending
                    pendingSelection = nil
                } else if selectedName == nil || !list.contains(where: { $0.name == selectedName }) {
                    selectedName = list.first?.name
                }
                syncDraft()
            case .failure(let error):
                loadError = "Couldn’t read the form fields. \(error.localizedDescription)"
            }
        }
    }

    private func syncDraft() {
        if let field = selectedField {
            draft = FormFieldDraft(field: field)
        } else {
            draft = FormFieldDraft()
        }
    }

    private func addField(_ kind: PDFFormFieldKind) {
        mode = .edit
        store.addFormFieldCentered(kind: kind) { name in
            pendingSelection = name
            reload()
        }
    }

    private func addRadioButton(to field: PDFFormField) {
        let label = "Option \(field.options.count + 1)"
        store.addRadioButton(to: field, label: label) { name in
            pendingSelection = name
            reload()
        }
    }

    private func save(_ field: PDFFormField) {
        let changes = draft.changes(from: field)
        let newName = changes.newName
        store.updateFormField(name: field.name, changes: changes) {
            if let newName { pendingSelection = newName }
            reload()
        }
    }

    private func delete(_ field: PDFFormField) {
        store.deleteFormField(name: field.name) {
            selectedName = nil
            reload()
        }
    }

    private func runDetection() {
        detection = .running
        store.detectFormFields { result in
            switch result {
            case .success(let suggestions):
                detection = .found(suggestions)
            case .failure(let error):
                detection = .idle
                store.report(error, while: "Auto-Detect Fields")
            }
        }
    }

    private func applyDetection() {
        store.applyDetectedFormFields { count in
            detection = .idle
            mode = .edit
            store.toast("Created \(count) field\(count == 1 ? "" : "s")")
            reload()
        }
    }
}

// MARK: - Draft

/// Editable copy of a field's properties, diffed against the original to
/// build an `update-field` call.
private struct FormFieldDraft: Equatable {
    var name = ""
    var text = ""
    var checked = false
    var selection = ""
    var options: [String] = []
    var fontSize: Double = 0
    var required = false
    var readonly = false
    var tooltip = ""
    var maxLen = 0
    var x: Double = 0
    var y: Double = 0
    var width: Double = 0
    var height: Double = 0

    init() {}

    init(field: PDFFormField) {
        name = field.name
        switch field.kind {
        case .checkbox:
            checked = field.value.boolValue
        case .radio, .combo, .list:
            selection = field.value.stringValue
        default:
            text = field.value.stringValue
        }
        options = field.options
        fontSize = field.fontSize
        required = field.required
        readonly = field.readonly
        tooltip = field.tooltip
        maxLen = field.maxLen
        let rect = field.engineRect
        x = Self.rounded(rect.minX)
        y = Self.rounded(rect.minY)
        width = Self.rounded(rect.width)
        height = Self.rounded(rect.height)
    }

    private static func rounded(_ value: CGFloat) -> Double { (Double(value) * 100).rounded() / 100 }

    var trimmedOptions: [String] {
        options.map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    }

    var rect: CGRect { CGRect(x: x, y: y, width: width, height: height) }

    func changes(from field: PDFFormField) -> PDFFormFieldChanges {
        var changes = PDFFormFieldChanges()
        let kind = field.kind
        let cleanName = name.trimmingCharacters(in: .whitespaces)
        if cleanName != field.name { changes.newName = cleanName }

        if kind?.hasOptions == true, trimmedOptions != field.options {
            changes.options = trimmedOptions
        }
        switch kind {
        case .checkbox:
            if checked != field.value.boolValue { changes.value = checked ? "true" : "false" }
        case .radio, .combo, .list:
            if selection != field.value.stringValue || changes.options != nil { changes.value = selection }
        case .text, .multiline:
            if text != field.value.stringValue { changes.value = text }
        default:
            break
        }
        if kind?.hasTextStyle == true, abs(fontSize - field.fontSize) > 0.01 { changes.fontSize = fontSize }
        if required != field.required { changes.required = required }
        if readonly != field.readonly { changes.readonly = readonly }
        if tooltip != field.tooltip { changes.tooltip = tooltip }
        if kind == .text || kind == .multiline, maxLen != field.maxLen { changes.maxLen = maxLen }
        let original = FormFieldDraft(field: field)
        if abs(x - original.x) > 0.01 || abs(y - original.y) > 0.01
            || abs(width - original.width) > 0.01 || abs(height - original.height) > 0.01 {
            changes.rect = rect
        }
        return changes
    }

    /// First blocking problem, or nil when the draft can be saved.
    func validationError(for field: PDFFormField, otherNames: Set<String>) -> String? {
        let cleanName = name.trimmingCharacters(in: .whitespaces)
        if cleanName.isEmpty { return "Give the field a name." }
        if cleanName.contains(".") { return "Field names can’t contain a period." }
        if otherNames.contains(cleanName) { return "Another field already uses this name." }
        if field.kind?.hasOptions == true {
            let opts = trimmedOptions
            if opts.isEmpty { return "Add at least one option." }
            if Set(opts).count != opts.count { return "Options must be unique." }
            if field.kind == .radio, opts.count != field.options.count {
                return "A radio group needs one option per button."
            }
        }
        if width < 4 || height < 4 { return "Width and height must be at least 4 pt." }
        return nil
    }
}

// MARK: - Inspector

private struct FormFieldInspector: View {
    let field: PDFFormField
    @Binding var draft: FormFieldDraft
    let otherNames: Set<String>
    let pageSize: CGSize
    let busy: Bool
    let onSave: () -> Void
    let onRevert: () -> Void
    let onDelete: () -> Void
    let onAddRadioButton: () -> Void

    @State private var confirmingDelete = false
    private let labelWidth: CGFloat = 86

    private var kind: PDFFormFieldKind { field.kind ?? .text }
    private var isDirty: Bool { !draft.changes(from: field).isEmpty }
    private var error: String? { draft.validationError(for: field, otherNames: otherNames) }
    private var offPage: Bool {
        draft.x < 0 || draft.y < 0
            || draft.x + draft.width > Double(pageSize.width) + 0.5
            || draft.y + draft.height > Double(pageSize.height) + 0.5
    }

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: Theme.Spacing.l) {
                    titleRow
                    identitySection
                    if kind.isFillable { valueSection }
                    if kind.hasOptions { optionsSection }
                    behaviorSection
                    positionSection
                }
                .padding(.trailing, Theme.Spacing.s)
            }
            Divider().overlay(Theme.separator)
            actionBar
                .padding(.top, Theme.Spacing.m)
        }
        .confirmationDialog(
            "Delete “\(field.name)”?",
            isPresented: $confirmingDelete
        ) {
            Button("Delete Field", role: .destructive, action: onDelete)
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(field.widgets.count > 1
                 ? "All \(field.widgets.count) buttons of this group are removed. You can undo this."
                 : "The field and its value are removed from the page. You can undo this.")
        }
    }

    private var titleRow: some View {
        HStack(spacing: Theme.Spacing.m) {
            IconTile(systemImage: kind.systemImage, tint: Theme.accent, size: 34)
            VStack(alignment: .leading, spacing: 2) {
                Text(field.name)
                    .font(Theme.Font.headline)
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text("\(kind.title) · Page \(field.page)")
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.textSecondary)
            }
            Spacer()
            if isDirty {
                Badge(text: "Edited", tint: Theme.warning)
            }
        }
    }

    private var identitySection: some View {
        FormSection("Field") {
            FormRow("Name", labelWidth: labelWidth) {
                TextField("Field name", text: $draft.name)
                    .textFieldStyle(.roundedBorder)
            }
            FormRow("Tooltip", labelWidth: labelWidth) {
                TextField("Shown when hovering (optional)", text: $draft.tooltip)
                    .textFieldStyle(.roundedBorder)
            }
        }
    }

    @ViewBuilder
    private var valueSection: some View {
        FormSection("Value") {
            switch kind {
            case .checkbox:
                Toggle("Checked", isOn: $draft.checked)
                    .toggleStyle(.checkbox)
            case .radio, .combo, .list:
                FormRow("Selected", labelWidth: labelWidth) {
                    Picker("", selection: $draft.selection) {
                        Text("None").tag("")
                        ForEach(draft.trimmedOptions, id: \.self) { option in
                            Text(option).tag(option)
                        }
                    }
                    .labelsHidden()
                    .frame(maxWidth: 220, alignment: .leading)
                }
            case .multiline:
                TextField("Empty", text: $draft.text, axis: .vertical)
                    .lineLimit(3...6)
                    .textFieldStyle(.roundedBorder)
            default:
                TextField("Empty", text: $draft.text)
                    .textFieldStyle(.roundedBorder)
            }
        }
    }

    private var optionsSection: some View {
        FormSection(
            "Options",
            footnote: kind == .radio
                ? "Each option is one button on the page. Rename them here; Add Button places another one next to the group."
                : "Shown in this order in the \(kind == .combo ? "dropdown" : "list")."
        ) {
            ForEach(draft.options.indices, id: \.self) { index in
                HStack(spacing: Theme.Spacing.s) {
                    Text("\(index + 1)")
                        .font(Theme.Font.micro)
                        .foregroundStyle(Theme.textTertiary)
                        .frame(width: 16, alignment: .trailing)
                    TextField("Option", text: optionBinding(index))
                        .textFieldStyle(.roundedBorder)
                    if kind != .radio {
                        Button {
                            removeOption(at: index)
                        } label: {
                            Image(systemName: "minus.circle")
                        }
                        .buttonStyle(IconButtonStyle(size: 22))
                        .disabled(draft.options.count <= 1)
                        .help("Remove option")
                    }
                }
            }
            HStack {
                if kind == .radio {
                    Button {
                        onAddRadioButton()
                    } label: {
                        Label("Add Button", systemImage: "plus.circle")
                    }
                    .buttonStyle(SecondaryButtonStyle())
                    .disabled(busy || isDirty)
                    .help(isDirty ? "Save or revert your edits first" : "Place another button in this group")
                } else {
                    Button {
                        draft.options.append("Option \(draft.options.count + 1)")
                    } label: {
                        Label("Add Option", systemImage: "plus.circle")
                    }
                    .buttonStyle(SecondaryButtonStyle())
                }
                Spacer()
            }
        }
    }

    private var behaviorSection: some View {
        FormSection("Behavior") {
            HStack(spacing: Theme.Spacing.l) {
                Toggle("Required", isOn: $draft.required)
                Toggle("Read-only", isOn: $draft.readonly)
            }
            .toggleStyle(.checkbox)
            if kind.hasTextStyle {
                FormRow("Font size", labelWidth: labelWidth) {
                    HStack(spacing: Theme.Spacing.m) {
                        Toggle("Auto", isOn: autoFontBinding)
                            .toggleStyle(.checkbox)
                        if draft.fontSize > 0 {
                            Stepper(value: $draft.fontSize, in: 4...72, step: 1) {
                                Text("\(Int(draft.fontSize)) pt")
                                    .font(Theme.Font.body.monospacedDigit())
                                    .frame(width: 44, alignment: .leading)
                            }
                        }
                    }
                }
            }
            if kind == .text || kind == .multiline {
                FormRow("Max length", labelWidth: labelWidth) {
                    HStack(spacing: Theme.Spacing.s) {
                        TextField("0", value: $draft.maxLen, format: .number)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 64)
                        Text(draft.maxLen > 0 ? "characters" : "No limit")
                            .font(Theme.Font.caption)
                            .foregroundStyle(Theme.textTertiary)
                    }
                }
            }
        }
    }

    private var positionSection: some View {
        FormSection("Position", footnote: "Points from the top-left corner of page \(field.page) (\(Int(pageSize.width)) × \(Int(pageSize.height)) pt).") {
            HStack(spacing: Theme.Spacing.m) {
                numberField("X", value: $draft.x)
                numberField("Y", value: $draft.y)
            }
            HStack(spacing: Theme.Spacing.m) {
                numberField("W", value: $draft.width)
                numberField("H", value: $draft.height)
            }
            if offPage {
                Label("Part of the field is outside the page.", systemImage: "exclamationmark.triangle.fill")
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.warning)
            }
        }
    }

    private var actionBar: some View {
        HStack(spacing: Theme.Spacing.s) {
            Button(role: .destructive) {
                confirmingDelete = true
            } label: {
                Label("Delete", systemImage: "trash")
                    .foregroundStyle(Theme.danger)
            }
            .buttonStyle(SecondaryButtonStyle())
            .disabled(busy)

            if isDirty, let error {
                Label(error, systemImage: "exclamationmark.circle.fill")
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.danger)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: Theme.Spacing.s)
            Button("Revert", action: onRevert)
                .buttonStyle(SecondaryButtonStyle())
                .disabled(!isDirty || busy)
            Button("Save", action: onSave)
                .buttonStyle(PrimaryButtonStyle())
                .disabled(!isDirty || error != nil || busy)
        }
    }

    private func numberField(_ label: String, value: Binding<Double>) -> some View {
        HStack(spacing: Theme.Spacing.xs) {
            Text(label)
                .font(Theme.Font.caption.weight(.semibold))
                .foregroundStyle(Theme.textTertiary)
                .frame(width: 14, alignment: .leading)
            TextField(label, value: value, format: .number.precision(.fractionLength(0...2)))
                .textFieldStyle(.roundedBorder)
                .frame(maxWidth: .infinity)
        }
    }

    private var autoFontBinding: Binding<Bool> {
        Binding(
            get: { draft.fontSize <= 0 },
            set: { draft.fontSize = $0 ? 0 : max(field.fontSize, 11) }
        )
    }

    /// Editing an option keeps the current selection pointing at it.
    private func optionBinding(_ index: Int) -> Binding<String> {
        Binding(
            get: { index < draft.options.count ? draft.options[index] : "" },
            set: { newValue in
                guard index < draft.options.count else { return }
                let old = draft.options[index].trimmingCharacters(in: .whitespaces)
                draft.options[index] = newValue
                if !old.isEmpty, draft.selection == old {
                    draft.selection = newValue.trimmingCharacters(in: .whitespaces)
                }
            }
        )
    }

    private func removeOption(at index: Int) {
        guard draft.options.indices.contains(index) else { return }
        let removed = draft.options.remove(at: index).trimmingCharacters(in: .whitespaces)
        if draft.selection == removed { draft.selection = "" }
    }
}

// MARK: - Fill values

private struct FillValuesPane: View {
    let fields: [PDFFormField]
    @Binding var values: [String: PDFFormFieldValue]

    private var pages: [(page: Int, fields: [PDFFormField])] {
        let grouped = Dictionary(grouping: fields, by: \.page)
        return grouped.keys.sorted().map { page in
            (page, grouped[page]?.sorted { lhs, rhs in
                if abs(lhs.rect[1] - rhs.rect[1]) > 4 { return lhs.rect[1] < rhs.rect[1] }
                return lhs.rect[0] < rhs.rect[0]
            } ?? [])
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Text("Type or pick values, then apply them all at once. Read-only and signature fields are skipped.")
                .font(Theme.Font.callout)
                .foregroundStyle(Theme.textSecondary)
            ScrollView {
                VStack(alignment: .leading, spacing: Theme.Spacing.l) {
                    ForEach(pages, id: \.page) { group in
                        FormSection("Page \(group.page)") {
                            ForEach(group.fields) { field in
                                row(field)
                            }
                        }
                    }
                }
                .padding(.trailing, Theme.Spacing.s)
            }
        }
    }

    private func row(_ field: PDFFormField) -> some View {
        let kind = field.kind ?? .text
        let editable = kind.isFillable && !field.readonly
        let edited = values[field.name].map { $0 != field.value } ?? false
        return HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.m) {
            HStack(spacing: Theme.Spacing.s) {
                Image(systemName: kind.systemImage)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(edited ? Theme.accent : Theme.textTertiary)
                    .frame(width: 16)
                Text(field.name)
                    .font(Theme.Font.body)
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                if field.required {
                    Text("*")
                        .font(Theme.Font.body.weight(.bold))
                        .foregroundStyle(Theme.danger)
                }
            }
            .frame(width: 190, alignment: .leading)

            control(for: field, kind: kind)
                .disabled(!editable)
                .frame(maxWidth: .infinity, alignment: .leading)

            if field.readonly {
                Image(systemName: "lock.fill")
                    .font(.system(size: 10))
                    .foregroundStyle(Theme.textTertiary)
                    .help("Read-only field")
            }
        }
    }

    @ViewBuilder
    private func control(for field: PDFFormField, kind: PDFFormFieldKind) -> some View {
        switch kind {
        case .checkbox:
            Toggle("Checked", isOn: boolBinding(field))
                .toggleStyle(.checkbox)
        case .radio:
            Picker("", selection: textBinding(field)) {
                ForEach(field.options, id: \.self) { option in
                    Text(option).tag(option)
                }
                Text("None").tag("")
            }
            .pickerStyle(.radioGroup)
            .horizontalRadioGroupLayout()
            .labelsHidden()
        case .combo, .list:
            Picker("", selection: textBinding(field)) {
                Text("None").tag("")
                ForEach(field.options, id: \.self) { option in
                    Text(option).tag(option)
                }
            }
            .labelsHidden()
            .frame(maxWidth: 240, alignment: .leading)
        case .multiline:
            TextField("Empty", text: textBinding(field), axis: .vertical)
                .lineLimit(2...4)
                .textFieldStyle(.roundedBorder)
        case .text:
            TextField("Empty", text: textBinding(field))
                .textFieldStyle(.roundedBorder)
        case .signature, .button:
            Text(kind == .signature ? "Signed in a PDF viewer" : "Not fillable")
                .font(Theme.Font.caption)
                .foregroundStyle(Theme.textTertiary)
        }
    }

    private func textBinding(_ field: PDFFormField) -> Binding<String> {
        Binding(
            get: { values[field.name]?.stringValue ?? field.value.stringValue },
            set: { values[field.name] = .text($0) }
        )
    }

    private func boolBinding(_ field: PDFFormField) -> Binding<Bool> {
        Binding(
            get: { values[field.name]?.boolValue ?? field.value.boolValue },
            set: { values[field.name] = .bool($0) }
        )
    }
}

// MARK: - Small pieces

private struct FieldRow: View {
    let field: PDFFormField
    let isSelected: Bool

    private var subtitle: String {
        let kind = field.kind?.title ?? field.type.capitalized
        let value: String
        switch field.value {
        case .bool(let checked): value = checked ? "Checked" : ""
        case .text(let text): value = text
        }
        return value.isEmpty ? kind : "\(kind) · \(value)"
    }

    var body: some View {
        HStack(spacing: Theme.Spacing.s) {
            IconTile(systemImage: field.kind?.systemImage ?? "questionmark.square.dashed",
                     tint: isSelected ? Theme.accent : Theme.textSecondary, size: 26)
            VStack(alignment: .leading, spacing: 1) {
                Text(field.name)
                    .font(Theme.Font.body.weight(.medium))
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(subtitle)
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.textTertiary)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
            if field.required {
                Circle()
                    .fill(Theme.warning)
                    .frame(width: 6, height: 6)
                    .help("Required")
            }
            if field.readonly {
                Image(systemName: "lock.fill")
                    .font(.system(size: 9))
                    .foregroundStyle(Theme.textTertiary)
                    .help("Read-only")
            }
        }
        .padding(.horizontal, Theme.Spacing.s)
        .padding(.vertical, 5)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                .fill(isSelected ? Theme.accentSoft : Color.clear)
        )
        .contentShape(Rectangle())
    }
}

private struct SearchField: View {
    @Binding var text: String

    var body: some View {
        HStack(spacing: Theme.Spacing.xs + 2) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Theme.textTertiary)
            TextField("Search fields", text: $text)
                .textFieldStyle(.plain)
                .font(Theme.Font.body)
            if !text.isEmpty {
                Button {
                    text = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(Theme.textTertiary)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, Theme.Spacing.s)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                .fill(Theme.surfaceRaised)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                .strokeBorder(Theme.separator, lineWidth: 1)
        )
    }
}

private struct DetectionCard: View {
    let suggestions: [PDFFormFieldSuggestion]
    let busy: Bool
    let onDismiss: () -> Void
    let onApply: () -> Void

    private var breakdown: String {
        let counts = Dictionary(grouping: suggestions, by: \.type).mapValues(\.count)
        let order = PDFFormFieldKind.allCases.map(\.rawValue)
        let parts = counts.keys.sorted { (order.firstIndex(of: $0) ?? 99) < (order.firstIndex(of: $1) ?? 99) }
            .map { type -> String in
                let kind = PDFFormFieldKind(rawValue: type)
                let title = kind == .multiline ? "multiline text field" : (kind?.title ?? type).lowercased()
                let count = counts[type] ?? 0
                // "checkboxes" / "list boxes", not "checkboxs".
                let plural = title.hasSuffix("x") ? title + "es" : title + "s"
                return "\(count) \(count == 1 ? title : plural)"
            }
        let pages = Set(suggestions.map(\.page)).count
        return parts.joined(separator: " · ") + " on \(pages) page\(pages == 1 ? "" : "s")"
    }

    private var hints: String {
        let named = suggestions.map(\.nameHint).filter { !$0.isEmpty }
        guard !named.isEmpty else { return "" }
        let shown = named.prefix(5).joined(separator: ", ")
        return named.count > 5 ? "\(shown), +\(named.count - 5) more" : shown
    }

    var body: some View {
        HStack(alignment: .center, spacing: Theme.Spacing.m) {
            IconTile(systemImage: "wand.and.stars", tint: Theme.success, size: 34)
            VStack(alignment: .leading, spacing: 2) {
                Text("\(suggestions.count) likely field\(suggestions.count == 1 ? "" : "s") found")
                    .font(Theme.Font.headline)
                    .foregroundStyle(Theme.textPrimary)
                Text(breakdown)
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.textSecondary)
                if !hints.isEmpty {
                    Text(hints)
                        .font(Theme.Font.caption)
                        .foregroundStyle(Theme.textTertiary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
            }
            Spacer(minLength: Theme.Spacing.s)
            Button("Dismiss", action: onDismiss)
                .buttonStyle(SecondaryButtonStyle())
            Button("Create \(suggestions.count) Field\(suggestions.count == 1 ? "" : "s")", action: onApply)
                .buttonStyle(PrimaryButtonStyle())
                .disabled(busy)
        }
        .padding(Theme.Spacing.m)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                .fill(Theme.success.opacity(0.08))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                .strokeBorder(Theme.success.opacity(0.25), lineWidth: 1)
        )
    }
}
