import AppKit

extension String {
    /// True when the first strongly directional character is right-to-left
    /// (Arabic, Hebrew, Syriac, Thaana, N'Ko and their presentation forms).
    var startsRightToLeft: Bool {
        for scalar in unicodeScalars {
            switch scalar.value {
            case 0x0590...0x08FF, 0xFB1D...0xFDFF, 0xFE70...0xFEFF, 0x10800...0x10FFF, 0x1E800...0x1EFFF:
                return true
            default:
                if scalar.properties.isAlphabetic { return false }
            }
        }
        return false
    }
}

extension NSTextView {
    /// Arabic/Hebrew text types right-to-left and right-aligned in the inline
    /// editors (matches the engine's `auto` alignment); everything else uses
    /// natural direction. Called on open and after every edit, so a new box
    /// flips as soon as the first Arabic letter is typed.
    func syncWritingDirection() {
        let rtl = string.startsRightToLeft
        let direction: NSWritingDirection = rtl ? .rightToLeft : .natural
        let alignment: NSTextAlignment = rtl ? .right : .natural
        let current = defaultParagraphStyle ?? NSParagraphStyle.default
        guard current.baseWritingDirection != direction || current.alignment != alignment else { return }
        let style = (current.mutableCopy() as? NSMutableParagraphStyle) ?? NSMutableParagraphStyle()
        style.baseWritingDirection = direction
        style.alignment = alignment
        defaultParagraphStyle = style
        typingAttributes[.paragraphStyle] = style
        let length = (string as NSString).length
        if length > 0 {
            textStorage?.addAttribute(.paragraphStyle, value: style, range: NSRange(location: 0, length: length))
        }
    }
}
