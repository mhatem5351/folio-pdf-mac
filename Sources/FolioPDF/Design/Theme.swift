import AppKit
import SwiftUI

/// Folio PDF design tokens. Every view takes colors, radii, spacing and type
/// from here so light and dark mode stay consistent across the app.
enum Theme {
    // MARK: Brand

    /// Primary brand accent (indigo). Buttons, selection, active tools.
    static let accent = Color(light: 0x5B4FE9, dark: 0x8C83FF)
    /// Second stop of the brand gradient (violet).
    static let accentAlt = Color(light: 0x9B4DEB, dark: 0xB487FF)
    /// Tinted fill behind selected or active controls.
    static let accentSoft = Color(light: 0x5B4FE9, dark: 0x8C83FF, alpha: 0.13)
    /// Text drawn on top of the accent color.
    static let onAccent = Color.white

    static let brandGradient = LinearGradient(
        colors: [accent, accentAlt],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    // MARK: Semantic

    static let success = Color(light: 0x0F9F72, dark: 0x34D399)
    static let warning = Color(light: 0xD98A06, dark: 0xFBBF24)
    static let danger = Color(light: 0xE5484D, dark: 0xFF6B6B)
    static let info = Color(light: 0x0A84FF, dark: 0x64B5FF)

    // MARK: Surfaces

    /// Area behind the pages (the "desk").
    static let canvas = Color(light: 0xE9EBF0, dark: 0x141519)
    /// Window chrome: top bar and tool strip.
    static let chrome = Color(light: 0xF8F8FB, dark: 0x1C1D22)
    /// Sidebars and inspectors.
    static let sidebar = Color(light: 0xF3F4F7, dark: 0x18191D)
    /// Cards, sheets and popovers sitting on a sidebar or canvas.
    static let surface = Color(light: 0xFFFFFF, dark: 0x23242A)
    /// Raised element inside a surface (inputs, hovered rows).
    static let surfaceRaised = Color(light: 0xF1F2F6, dark: 0x2C2D34)
    /// Hairlines and dividers.
    static let separator = Color(light: 0x000000, dark: 0xFFFFFF, alpha: 0.09)
    /// Stronger outline for focused or interactive borders.
    static let outline = Color(light: 0x000000, dark: 0xFFFFFF, alpha: 0.16)

    // MARK: Text

    static let textPrimary = Color(light: 0x16171D, dark: 0xF2F2F7)
    static let textSecondary = Color(light: 0x5D6070, dark: 0xA3A5B3)
    static let textTertiary = Color(light: 0x8E91A0, dark: 0x6F7180)

    // MARK: Metrics

    enum Radius {
        static let small: CGFloat = 6
        static let medium: CGFloat = 9
        static let large: CGFloat = 13
        static let xl: CGFloat = 18
    }

    enum Spacing {
        static let xxs: CGFloat = 2
        static let xs: CGFloat = 4
        static let s: CGFloat = 8
        static let m: CGFloat = 12
        static let l: CGFloat = 16
        static let xl: CGFloat = 24
        static let xxl: CGFloat = 32
    }

    // MARK: Type

    enum Font {
        static let display = SwiftUI.Font.system(size: 28, weight: .bold, design: .rounded)
        static let title = SwiftUI.Font.system(size: 17, weight: .semibold)
        static let headline = SwiftUI.Font.system(size: 13, weight: .semibold)
        static let body = SwiftUI.Font.system(size: 13)
        static let callout = SwiftUI.Font.system(size: 12)
        static let caption = SwiftUI.Font.system(size: 11)
        static let micro = SwiftUI.Font.system(size: 10, weight: .medium)
        static let sectionLabel = SwiftUI.Font.system(size: 10.5, weight: .semibold)
    }

    // MARK: Shadows

    static let pageShadow = Color.black.opacity(0.18)
    static let floatingShadow = Color.black.opacity(0.22)

    /// Categorical accents for tool tiles (Tools hub, command palette).
    static let categoryColors: [ToolCategory: Color] = [
        .organize: Color(light: 0x2F80ED, dark: 0x5AA2FF),
        .edit: Color(light: 0x5B4FE9, dark: 0x8C83FF),
        .protect: Color(light: 0xE5484D, dark: 0xFF6B6B),
        .convert: Color(light: 0x0F9F72, dark: 0x34D399),
        .review: Color(light: 0xD98A06, dark: 0xFBBF24),
        .output: Color(light: 0x7C3AED, dark: 0xB487FF)
    ]
}

extension Color {
    /// Appearance-aware color from 0xRRGGBB values for light and dark mode.
    init(light: UInt32, dark: UInt32, alpha: Double = 1) {
        self.init(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.darkAqua, .vibrantDark, .accessibilityHighContrastDarkAqua]) != nil
            return NSColor(hex: isDark ? dark : light, alpha: alpha)
        })
    }

    init(hex: UInt32, alpha: Double = 1) {
        self.init(nsColor: NSColor(hex: hex, alpha: alpha))
    }
}

extension NSColor {
    convenience init(hex: UInt32, alpha: Double = 1) {
        self.init(
            srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
            green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255,
            alpha: CGFloat(alpha)
        )
    }
}
