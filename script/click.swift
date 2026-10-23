// QA helper: real mouse click at global screen coordinates (top-left origin).
// usage: swift script/click.swift X Y [double]
import CoreGraphics
import Foundation
let a = CommandLine.arguments
guard a.count >= 3, let x = Double(a[1]), let y = Double(a[2]) else { exit(2) }
let p = CGPoint(x: x, y: y)
let clicks = a.count > 3 && a[3] == "double" ? 2 : 1
CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
usleep(80_000)
for n in 1...clicks {
    let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left)
    down?.setIntegerValueField(.mouseEventClickState, value: Int64(n))
    down?.post(tap: .cghidEventTap)
    usleep(40_000)
    let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left)
    up?.setIntegerValueField(.mouseEventClickState, value: Int64(n))
    up?.post(tap: .cghidEventTap)
    usleep(60_000)
}
