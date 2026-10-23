import SwiftUI

/// Window toolbar: sidebar toggle, mode switcher (centered), and document
/// actions. Native toolbar so it picks up the system's glass styling.
struct MainToolbar: ToolbarContent {
    @ObservedObject var store: AppStore

    var body: some ToolbarContent {
        ToolbarItem(placement: .navigation) {
            Button {
                withAnimation(.easeInOut(duration: 0.2)) { store.showSidebar.toggle() }
            } label: {
                Label("Sidebar", systemImage: "sidebar.left")
            }
            .help("Show or hide the sidebar")
            .disabled(store.document == nil)
        }

        ToolbarItem(placement: .principal) {
            if store.document != nil {
                ModeSwitcher(selection: Binding(
                    get: { store.selectedOperation },
                    set: { store.selectedOperation = $0 }
                ))
            }
        }

        ToolbarItemGroup(placement: .primaryAction) {
            if store.busy {
                BusyIndicator(title: store.busyTitle)
            }

            ControlGroup {
                Button {
                    store.undo()
                } label: {
                    Label("Undo", systemImage: "arrow.uturn.backward")
                }
                .help("Undo (⌘Z)")
                .disabled(!store.canUndo || store.busy)

                Button {
                    store.redo()
                } label: {
                    Label("Redo", systemImage: "arrow.uturn.forward")
                }
                .help("Redo (⇧⌘Z)")
                .disabled(!store.canRedo || store.busy)
            }

            Button {
                store.showToolsHub = true
            } label: {
                Label("Tools", systemImage: "square.grid.2x2")
            }
            .help("All tools (⌘K to search)")

            PrintMenu(store: store)
            ShareMenu(store: store)

            Button {
                store.save()
            } label: {
                Label("Save", systemImage: store.isDirty ? "square.and.arrow.down.badge.clock" : "square.and.arrow.down")
            }
            .help(store.isDirty ? "Save changes (⌘S)" : "No unsaved changes")
            .disabled(store.document == nil || !store.isDirty || store.busy)
        }
    }
}

/// Animated pill segmented control for the editing modes.
struct ModeSwitcher: View {
    @Binding var selection: PDFOperation
    @Namespace private var pill

    var body: some View {
        HStack(spacing: 2) {
            ForEach(PDFOperation.allCases) { mode in
                let isSelected = selection == mode
                Button {
                    withAnimation(.spring(response: 0.28, dampingFraction: 0.85)) {
                        selection = mode
                    }
                } label: {
                    HStack(spacing: 5) {
                        Image(systemName: mode.systemImage)
                            .font(.system(size: 12, weight: .semibold))
                            .frame(width: 16)
                        // Only the active mode spells out its name: keeps the
                        // switcher compact enough for narrow windows.
                        if isSelected {
                            Text(mode.title)
                                .font(.system(size: 12, weight: .semibold))
                                .transition(.opacity.combined(with: .scale(scale: 0.9, anchor: .leading)))
                        }
                    }
                    .foregroundStyle(isSelected ? Theme.accent : Theme.textSecondary)
                    .padding(.horizontal, isSelected ? 11 : 8)
                    .padding(.vertical, 5)
                    .background {
                        if isSelected {
                            Capsule()
                                .fill(Theme.accentSoft)
                                .overlay(Capsule().strokeBorder(Theme.accent.opacity(0.25), lineWidth: 1))
                                .matchedGeometryEffect(id: "pill", in: pill)
                        }
                    }
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .help("\(mode.title) (⌘\((PDFOperation.allCases.firstIndex(of: mode) ?? 0) + 1))")
            }
        }
        .padding(3)
        .fixedSize()
    }
}

private struct BusyIndicator: View {
    let title: String

    var body: some View {
        HStack(spacing: 6) {
            ProgressView()
                .controlSize(.small)
            Text(title)
                .font(Theme.Font.caption)
                .foregroundStyle(Theme.textSecondary)
                .lineLimit(1)
        }
        .padding(.horizontal, 6)
    }
}

/// Print button: click prints to the HP LaserJet, menu offers the system
/// dialog with every printer.
private struct PrintMenu: View {
    @ObservedObject var store: AppStore

    var body: some View {
        Menu {
            Button {
                store.launch(.printHP)
            } label: {
                Label("Print to HP LaserJet…", systemImage: "printer.fill")
            }
            Button {
                store.launch(.print)
            } label: {
                Label("Print with System Dialog…", systemImage: "printer")
            }
        } label: {
            Label("Print", systemImage: "printer")
        } primaryAction: {
            store.launch(.printHP)
        }
        .help("Print (⌘P) · hold for more options")
        .disabled(store.document == nil || store.busy)
    }
}

private struct ShareMenu: View {
    @ObservedObject var store: AppStore

    var body: some View {
        Menu {
            Button("PDF Copy…") { store.exportPDFCopy() }
            Divider()
            Button("Word Document…") { store.exportOffice(.word) }
            Button("Excel Workbook…") { store.exportOffice(.excel) }
            Button("PowerPoint…") { store.exportOffice(.powerpoint) }
            Divider()
            Button("Plain Text…") { store.exportText(.text) }
            Button("Markdown…") { store.exportText(.markdown) }
            Button("HTML…") { store.exportText(.html) }
            Divider()
            Button("PNG Images…") { store.exportImages(.png) }
            Button("JPG Images…") { store.exportImages(.jpg) }
        } label: {
            Label("Export", systemImage: "square.and.arrow.up")
        }
        .help("Export or convert")
        .disabled(store.document == nil || store.busy)
    }
}
