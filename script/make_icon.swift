// Renders the Folio PDF app icon (1024px master) → Resources/AppIcon.icns.
// usage: swift script/make_icon.swift <out-dir>
import AppKit
import CoreGraphics

let out = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ".")
let S: CGFloat = 1024
let cs = CGColorSpace(name: CGColorSpace.sRGB)!
func rgb(_ hex: UInt32, _ a: CGFloat = 1) -> CGColor {
    CGColor(colorSpace: cs, components: [CGFloat((hex >> 16) & 255) / 255, CGFloat((hex >> 8) & 255) / 255, CGFloat(hex & 255) / 255, a])!
}

func render(size px: Int) -> CGImage {
    let ctx = CGContext(data: nil, width: px, height: px, bitsPerComponent: 8, bytesPerRow: 0, space: cs,
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    let k = CGFloat(px) / S
    ctx.scaleBy(x: k, y: k)

    // macOS icon grid: 824pt squircle centred on the 1024 canvas.
    let tile = CGRect(x: 100, y: 100, width: 824, height: 824)
    let squircle = CGPath(roundedRect: tile, cornerWidth: 185, cornerHeight: 185, transform: nil)

    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -14), blur: 36, color: rgb(0x1E1660, 0.45))
    ctx.addPath(squircle); ctx.setFillColor(rgb(0x5B4FE9)); ctx.fillPath()
    ctx.restoreGState()

    ctx.saveGState()
    ctx.addPath(squircle); ctx.clip()
    let gradient = CGGradient(colorsSpace: cs, colors: [rgb(0x6D5DF6), rgb(0x9B4DEB), rgb(0xC04FD8)] as CFArray, locations: [0, 0.6, 1])!
    ctx.drawLinearGradient(gradient, start: CGPoint(x: 100, y: 924), end: CGPoint(x: 924, y: 100), options: [])
    // soft top highlight
    let gloss = CGGradient(colorsSpace: cs, colors: [rgb(0xFFFFFF, 0.22), rgb(0xFFFFFF, 0)] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(gloss, start: CGPoint(x: 512, y: 924), end: CGPoint(x: 512, y: 560), options: [])
    ctx.restoreGState()

    // back page (tilted, translucent)
    ctx.saveGState()
    ctx.translateBy(x: 470, y: 520); ctx.rotate(by: 0.12)
    let back = CGPath(roundedRect: CGRect(x: -190, y: -245, width: 380, height: 490), cornerWidth: 34, cornerHeight: 34, transform: nil)
    ctx.addPath(back); ctx.setFillColor(rgb(0xFFFFFF, 0.42)); ctx.fillPath()
    ctx.restoreGState()

    // front page with folded corner
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -10), blur: 30, color: rgb(0x2A1B6E, 0.35))
    let front = CGMutablePath()
    let f = CGRect(x: 360, y: 250, width: 380, height: 500)
    let fold: CGFloat = 96
    front.move(to: CGPoint(x: f.minX + 34, y: f.minY))
    front.addLine(to: CGPoint(x: f.maxX - 34, y: f.minY))
    front.addQuadCurve(to: CGPoint(x: f.maxX, y: f.minY + 34), control: CGPoint(x: f.maxX, y: f.minY))
    front.addLine(to: CGPoint(x: f.maxX, y: f.maxY - fold))
    front.addLine(to: CGPoint(x: f.maxX - fold, y: f.maxY))
    front.addLine(to: CGPoint(x: f.minX + 34, y: f.maxY))
    front.addQuadCurve(to: CGPoint(x: f.minX, y: f.maxY - 34), control: CGPoint(x: f.minX, y: f.maxY))
    front.addLine(to: CGPoint(x: f.minX, y: f.minY + 34))
    front.addQuadCurve(to: CGPoint(x: f.minX + 34, y: f.minY), control: CGPoint(x: f.minX, y: f.minY))
    front.closeSubpath()
    ctx.addPath(front); ctx.setFillColor(rgb(0xFFFFFF)); ctx.fillPath()
    ctx.restoreGState()
    // fold triangle
    let corner = CGMutablePath()
    corner.move(to: CGPoint(x: f.maxX, y: f.maxY - fold))
    corner.addLine(to: CGPoint(x: f.maxX - fold, y: f.maxY - fold))
    corner.addLine(to: CGPoint(x: f.maxX - fold, y: f.maxY))
    corner.closeSubpath()
    ctx.addPath(corner); ctx.setFillColor(rgb(0xD9D3FF)); ctx.fillPath()

    // text lines
    let lines: [(CGFloat, CGFloat, UInt32, CGFloat)] = [
        (650, 200, 0x5B4FE9, 1), (585, 270, 0x5B4FE9, 0.35), (535, 270, 0x5B4FE9, 0.35),
        (485, 230, 0x5B4FE9, 0.35), (435, 270, 0x5B4FE9, 0.35)
    ]
    for (y, w, c, a) in lines {
        let r = CGRect(x: 415, y: y, width: w, height: 22)
        ctx.addPath(CGPath(roundedRect: r, cornerWidth: 11, cornerHeight: 11, transform: nil))
        ctx.setFillColor(rgb(c, a)); ctx.fillPath()
    }
    // pen nib accent (edit)
    ctx.saveGState()
    ctx.translateBy(x: 690, y: 330); ctx.rotate(by: -0.78)
    let pen = CGPath(roundedRect: CGRect(x: -26, y: -150, width: 52, height: 230), cornerWidth: 18, cornerHeight: 18, transform: nil)
    ctx.setShadow(offset: CGSize(width: 0, height: -6), blur: 16, color: rgb(0x2A1B6E, 0.4))
    ctx.addPath(pen); ctx.setFillColor(rgb(0xFFB547)); ctx.fillPath()
    ctx.restoreGState()
    ctx.saveGState()
    ctx.translateBy(x: 690, y: 330); ctx.rotate(by: -0.78)
    let tip = CGMutablePath()
    tip.move(to: CGPoint(x: -26, y: -150)); tip.addLine(to: CGPoint(x: 26, y: -150)); tip.addLine(to: CGPoint(x: 0, y: -205)); tip.closeSubpath()
    ctx.addPath(tip); ctx.setFillColor(rgb(0x3B2F8F)); ctx.fillPath()
    ctx.addPath(CGPath(rect: CGRect(x: -26, y: 40, width: 52, height: 22), transform: nil)); ctx.setFillColor(rgb(0xF08A24)); ctx.fillPath()
    ctx.restoreGState()
    return ctx.makeImage()!
}

let iconset = out.appendingPathComponent("AppIcon.iconset")
try? FileManager.default.removeItem(at: iconset)
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
for base in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let px = base * scale
        let name = scale == 1 ? "icon_\(base)x\(base).png" : "icon_\(base)x\(base)@2x.png"
        let rep = NSBitmapImageRep(cgImage: render(size: px))
        try rep.representation(using: .png, properties: [:])!.write(to: iconset.appendingPathComponent(name))
    }
}
let master = NSBitmapImageRep(cgImage: render(size: 1024))
try master.representation(using: .png, properties: [:])!.write(to: out.appendingPathComponent("AppIcon.png"))
print(iconset.path)
