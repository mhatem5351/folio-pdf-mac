// QA helper: real mouse events at global screen coordinates (top-left origin).
// usage: swift script/click.swift X Y [double]
//        swift script/click.swift drag X1 Y1 X2 Y2
import CoreGraphics
import Foundation
let a = CommandLine.arguments
func post(_ type: CGEventType, _ p: CGPoint, clicks: Int64 = 1) {
    let e = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: p, mouseButton: .left)
    e?.setIntegerValueField(.mouseEventClickState, value: clicks)
    e?.post(tap: .cghidEventTap)
}
if a.count >= 6, a[1] == "drag", let x1 = Double(a[2]), let y1 = Double(a[3]), let x2 = Double(a[4]), let y2 = Double(a[5]) {
    let start = CGPoint(x: x1, y: y1), end = CGPoint(x: x2, y: y2)
    post(.mouseMoved, start); usleep(80_000)
    post(.leftMouseDown, start); usleep(80_000)
    let steps = 20
    for i in 1...steps {
        let t = Double(i) / Double(steps)
        post(.leftMouseDragged, CGPoint(x: x1 + (x2 - x1) * t, y: y1 + (y2 - y1) * t))
        usleep(15_000)
    }
    usleep(60_000)
    post(.leftMouseUp, end)
    exit(0)
}
guard a.count >= 3, let x = Double(a[1]), let y = Double(a[2]) else { exit(2) }
let p = CGPoint(x: x, y: y)
let clicks = a.count > 3 && a[3] == "double" ? 2 : 1
post(.mouseMoved, p); usleep(80_000)
for n in 1...clicks {
    post(.leftMouseDown, p, clicks: Int64(n)); usleep(40_000)
    post(.leftMouseUp, p, clicks: Int64(n)); usleep(60_000)
}
