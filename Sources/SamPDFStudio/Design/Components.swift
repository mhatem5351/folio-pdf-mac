import SwiftUI

// MARK: - Buttons

/// Filled brand button for the main action of a sheet or panel.
struct PrimaryButtonStyle: ButtonStyle {
    var destructive = false
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.Font.headline)
            .foregroundStyle(Theme.onAccent)
            .padding(.horizontal, Theme.Spacing.l)
            .padding(.vertical, 7)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                    .fill(destructive ? AnyShapeStyle(Theme.danger) : AnyShapeStyle(Theme.brandGradient))
            )
            .opacity(isEnabled ? (configuration.isPressed ? 0.85 : 1) : 0.45)
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
    }
}

/// Quiet bordered button for secondary actions (Cancel, Browse…).
struct SecondaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    @State private var hovering = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.Font.body.weight(.medium))
            .foregroundStyle(Theme.textPrimary)
            .padding(.horizontal, Theme.Spacing.m + 2)
            .padding(.vertical, 6)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                    .fill(hovering || configuration.isPressed ? Theme.surfaceRaised : Theme.surface)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                    .strokeBorder(Theme.outline, lineWidth: 1)
            )
            .opacity(isEnabled ? 1 : 0.45)
            .onHover { hovering = $0 }
    }
}

/// Borderless icon button used in toolbars and strips. `isActive` tints it.
struct IconButtonStyle: ButtonStyle {
    var isActive = false
    var size: CGFloat = 28
    @Environment(\.isEnabled) private var isEnabled
    @State private var hovering = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13.5, weight: .medium))
            .foregroundStyle(isActive ? Theme.accent : Theme.textSecondary)
            .frame(minWidth: size, minHeight: size)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.small + 1, style: .continuous)
                    .fill(isActive ? Theme.accentSoft
                          : (hovering || configuration.isPressed ? Theme.surfaceRaised : Color.clear))
            )
            .contentShape(Rectangle())
            .opacity(isEnabled ? 1 : 0.35)
            .onHover { hovering = $0 }
    }
}

/// Icon + label tool chip for the contextual tool strip.
struct ToolChipStyle: ButtonStyle {
    var isActive = false
    @Environment(\.isEnabled) private var isEnabled
    @State private var hovering = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.Font.callout.weight(.medium))
            .foregroundStyle(isActive ? Theme.accent : Theme.textPrimary)
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                    .fill(isActive ? Theme.accentSoft
                          : (hovering || configuration.isPressed ? Theme.surfaceRaised : Color.clear))
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                    .strokeBorder(isActive ? Theme.accent.opacity(0.35) : Color.clear, lineWidth: 1)
            )
            .contentShape(Rectangle())
            .opacity(isEnabled ? 1 : 0.4)
            .onHover { hovering = $0 }
    }
}

// MARK: - Sheet scaffold

/// Standard layout for every tool sheet: icon header, scrolling body, and a
/// footer with Cancel + primary action. Keeps all tools visually identical.
///
///     ToolSheetScaffold(tool: .watermark, primaryTitle: "Apply",
///                       canSubmit: !text.isEmpty, onSubmit: apply) { … }
struct ToolSheetScaffold<Content: View>: View {
    let title: String
    let subtitle: String
    let systemImage: String
    var tint: Color = Theme.accent
    var primaryTitle: String = "Apply"
    var primaryDestructive = false
    var canSubmit = true
    var width: CGFloat = 520
    var onSubmit: () -> Void
    @ViewBuilder var content: () -> Content
    @Environment(\.dismiss) private var dismiss

    init(
        title: String,
        subtitle: String,
        systemImage: String,
        tint: Color = Theme.accent,
        primaryTitle: String = "Apply",
        primaryDestructive: Bool = false,
        canSubmit: Bool = true,
        width: CGFloat = 520,
        onSubmit: @escaping () -> Void,
        @ViewBuilder content: @escaping () -> Content
    ) {
        self.title = title
        self.subtitle = subtitle
        self.systemImage = systemImage
        self.tint = tint
        self.primaryTitle = primaryTitle
        self.primaryDestructive = primaryDestructive
        self.canSubmit = canSubmit
        self.width = width
        self.onSubmit = onSubmit
        self.content = content
    }

    /// Convenience: take title, subtitle, icon and tint from a catalog tool.
    init(
        tool: ToolID,
        primaryTitle: String = "Apply",
        primaryDestructive: Bool = false,
        canSubmit: Bool = true,
        width: CGFloat = 520,
        onSubmit: @escaping () -> Void,
        @ViewBuilder content: @escaping () -> Content
    ) {
        self.init(
            title: tool.title,
            subtitle: tool.subtitle,
            systemImage: tool.systemImage,
            tint: tool.category.color,
            primaryTitle: primaryTitle,
            primaryDestructive: primaryDestructive,
            canSubmit: canSubmit,
            width: width,
            onSubmit: onSubmit,
            content: content
        )
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .center, spacing: Theme.Spacing.m) {
                IconTile(systemImage: systemImage, tint: tint, size: 38)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(Theme.Font.title)
                        .foregroundStyle(Theme.textPrimary)
                    Text(subtitle)
                        .font(Theme.Font.callout)
                        .foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer()
            }
            .padding(Theme.Spacing.xl)

            Divider().overlay(Theme.separator)

            ScrollView {
                VStack(alignment: .leading, spacing: Theme.Spacing.l) {
                    content()
                }
                .padding(Theme.Spacing.xl)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 520)

            Divider().overlay(Theme.separator)

            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .buttonStyle(SecondaryButtonStyle())
                    .keyboardShortcut(.cancelAction)
                Button(primaryTitle) {
                    onSubmit()
                }
                .buttonStyle(PrimaryButtonStyle(destructive: primaryDestructive))
                .keyboardShortcut(.defaultAction)
                .disabled(!canSubmit)
            }
            .padding(.horizontal, Theme.Spacing.xl)
            .padding(.vertical, Theme.Spacing.l)
        }
        .frame(width: width)
        .background(Theme.surface)
    }
}

// MARK: - Form building blocks

/// Titled group of controls inside a sheet or inspector.
struct FormSection<Content: View>: View {
    let title: String
    var footnote: String?
    @ViewBuilder var content: () -> Content

    init(_ title: String, footnote: String? = nil, @ViewBuilder content: @escaping () -> Content) {
        self.title = title
        self.footnote = footnote
        self.content = content
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Text(title.uppercased())
                .font(Theme.Font.sectionLabel)
                .tracking(0.6)
                .foregroundStyle(Theme.textTertiary)
            VStack(alignment: .leading, spacing: Theme.Spacing.s + 2) {
                content()
            }
            .padding(Theme.Spacing.m)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                    .fill(Theme.surfaceRaised.opacity(0.6))
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                    .strokeBorder(Theme.separator, lineWidth: 1)
            )
            if let footnote {
                Text(footnote)
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.textTertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// Label on the left, control on the right.
struct FormRow<Control: View>: View {
    let label: String
    var labelWidth: CGFloat = 120
    @ViewBuilder var control: () -> Control

    init(_ label: String, labelWidth: CGFloat = 120, @ViewBuilder control: @escaping () -> Control) {
        self.label = label
        self.labelWidth = labelWidth
        self.control = control
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.m) {
            Text(label)
                .font(Theme.Font.body)
                .foregroundStyle(Theme.textSecondary)
                .frame(width: labelWidth, alignment: .leading)
            control()
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// Which pages an operation applies to. `spec` is the engine page spec
/// ("all", "3", "1-4,7"), ready to pass as `--pages`.
struct PageRangeSelection: Equatable {
    enum Mode: String, CaseIterable, Identifiable {
        case all, current, custom
        var id: String { rawValue }
        var title: String {
            switch self {
            case .all: "All pages"
            case .current: "Current page"
            case .custom: "Pages…"
            }
        }
    }

    var mode: Mode = .all
    var custom: String = ""

    func spec(currentPage: Int) -> String {
        switch mode {
        case .all: "all"
        case .current: String(currentPage)
        case .custom: custom.trimmingCharacters(in: .whitespaces).isEmpty ? "all" : custom
        }
    }
}

struct PageRangePicker: View {
    @Binding var selection: PageRangeSelection
    var pageCount: Int

    var body: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.s) {
            Picker("", selection: $selection.mode) {
                ForEach(PageRangeSelection.Mode.allCases) { mode in
                    Text(mode.title).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            if selection.mode == .custom {
                TextField("e.g. 1-3, 5, 8-\(max(pageCount, 1))", text: $selection.custom)
                    .textFieldStyle(.roundedBorder)
            }
        }
    }
}

/// Row of color swatches with an optional system color well.
struct ColorSwatchPicker: View {
    @Binding var color: Color
    var swatches: [Color] = [
        Color(hex: 0x16171D), Color(hex: 0x6B7280), Color(hex: 0xE5484D), Color(hex: 0xF5A524),
        Color(hex: 0x12B886), Color(hex: 0x2F80ED), Color(hex: 0x5B4FE9), Color(hex: 0xD946EF)
    ]

    var body: some View {
        HStack(spacing: 6) {
            ForEach(Array(swatches.enumerated()), id: \.offset) { _, swatch in
                Button {
                    color = swatch
                } label: {
                    Circle()
                        .fill(swatch)
                        .frame(width: 18, height: 18)
                        .overlay(Circle().strokeBorder(Theme.outline, lineWidth: 1))
                        .overlay(
                            Circle()
                                .strokeBorder(Theme.accent, lineWidth: 2)
                                .padding(-3)
                                .opacity(swatch.engineHex == color.engineHex ? 1 : 0)
                        )
                }
                .buttonStyle(.plain)
            }
            ColorPicker("", selection: $color, supportsOpacity: false)
                .labelsHidden()
                .frame(width: 26)
        }
    }
}

/// Inline message box inside sheets (tips, warnings, errors).
struct InfoBanner: View {
    enum Kind { case info, warning, danger, success }
    var kind: Kind = .info
    let text: String

    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.s) {
            Image(systemName: icon)
                .foregroundStyle(color)
                .font(.system(size: 13, weight: .semibold))
            Text(text)
                .font(Theme.Font.callout)
                .foregroundStyle(Theme.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(Theme.Spacing.m)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                .fill(color.opacity(0.1))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                .strokeBorder(color.opacity(0.25), lineWidth: 1)
        )
    }

    private var icon: String {
        switch kind {
        case .info: "info.circle.fill"
        case .warning: "exclamationmark.triangle.fill"
        case .danger: "xmark.octagon.fill"
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

/// Rounded square with a tinted SF Symbol — tool identity everywhere.
struct IconTile: View {
    let systemImage: String
    var tint: Color = Theme.accent
    var size: CGFloat = 32

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.28, style: .continuous)
            .fill(tint.opacity(0.14))
            .overlay(
                Image(systemName: systemImage)
                    .font(.system(size: size * 0.45, weight: .semibold))
                    .foregroundStyle(tint)
            )
            .frame(width: size, height: size)
    }
}

/// Small capsule label ("New", "Beta", page counts).
struct Badge: View {
    let text: String
    var tint: Color = Theme.accent

    var body: some View {
        Text(text)
            .font(Theme.Font.micro)
            .foregroundStyle(tint)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(tint.opacity(0.14)))
    }
}
