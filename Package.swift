// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "FolioPDF",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "FolioPDF", targets: ["FolioPDF"])
    ],
    targets: [
        .executableTarget(
            name: "FolioPDF",
            path: "Sources/FolioPDF"
        )
    ]
)
