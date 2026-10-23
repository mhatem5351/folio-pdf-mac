import PDFKit
import SwiftUI

/// Start screen: brand header, primary open action, quick actions, and a
/// grid of recent documents with thumbnails.
struct WelcomeView: View {
    @EnvironmentObject private var store: AppStore

    private let columns = [GridItem(.adaptive(minimum: 132, maximum: 160), spacing: Theme.Spacing.l)]

    var body: some View {
        ScrollView {
            VStack(spacing: Theme.Spacing.xxl) {
                header
                quickActions
                if !store.recentFiles.isEmpty {
                    recents
                }
            }
            .padding(.horizontal, Theme.Spacing.xxl)
            .padding(.vertical, 48)
            .frame(maxWidth: 820)
            .frame(maxWidth: .infinity)
        }
        .background(
            ZStack {
                Theme.canvas
                RadialGradient(
                    colors: [Theme.accent.opacity(0.16), .clear],
                    center: .top, startRadius: 10, endRadius: 520
                )
            }
            .ignoresSafeArea()
        )
    }

    private var header: some View {
        VStack(spacing: Theme.Spacing.m) {
            AppMark(size: 76)
            Text("Folio PDF")
                .font(Theme.Font.display)
                .foregroundStyle(Theme.textPrimary)
            Text("Edit text and images, sign, organize, protect and print — privately, on your Mac.")
                .font(Theme.Font.body)
                .foregroundStyle(Theme.textSecondary)
                .multilineTextAlignment(.center)
            HStack(spacing: Theme.Spacing.m) {
                Button {
                    store.openPDF()
                } label: {
                    Label("Open PDF…", systemImage: "folder")
                        .frame(minWidth: 130)
                }
                .buttonStyle(PrimaryButtonStyle())
                .keyboardShortcut("o")

                Button {
                    store.showCommandPalette = true
                } label: {
                    Label("Search Tools  ⌘K", systemImage: "command")
                }
                .buttonStyle(SecondaryButtonStyle())
            }
            .padding(.top, Theme.Spacing.s)
            Text("or drop a PDF anywhere in this window")
                .font(Theme.Font.caption)
                .foregroundStyle(Theme.textTertiary)
        }
    }

    private var quickActions: some View {
        HStack(spacing: Theme.Spacing.m) {
            QuickActionTile(title: "Merge PDFs", subtitle: "Combine files", systemImage: "square.stack.3d.down.right", tint: ToolCategory.organize.color) {
                store.mergePDFFiles()
            }
            QuickActionTile(title: "Images to PDF", subtitle: "Photos & scans", systemImage: "photo.on.rectangle.angled", tint: ToolCategory.convert.color) {
                store.newFromImages()
            }
            QuickActionTile(title: "Create from File", subtitle: "Word, RTF, HTML", systemImage: "doc.badge.plus", tint: ToolCategory.edit.color) {
                store.launch(.convertToPDF)
            }
            QuickActionTile(title: "Compare", subtitle: "Spot differences", systemImage: "rectangle.split.2x1", tint: ToolCategory.review.color) {
                store.launch(.compare)
            }
        }
    }

    private var recents: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.m) {
            Text("RECENT")
                .font(Theme.Font.sectionLabel)
                .tracking(0.6)
                .foregroundStyle(Theme.textTertiary)
            LazyVGrid(columns: columns, alignment: .leading, spacing: Theme.Spacing.l) {
                ForEach(store.recentFiles.prefix(8), id: \.self) { url in
                    RecentDocumentCard(url: url) {
                        store.loadPDF(url, checkDirty: true)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// The app's brand mark: gradient squircle with a stylized page.
struct AppMark: View {
    var size: CGFloat = 64

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.27, style: .continuous)
            .fill(Theme.brandGradient)
            .overlay(
                ZStack {
                    RoundedRectangle(cornerRadius: size * 0.06, style: .continuous)
                        .fill(.white)
                        .frame(width: size * 0.46, height: size * 0.58)
                        .rotationEffect(.degrees(-6))
                        .offset(x: -size * 0.04)
                        .opacity(0.55)
                    RoundedRectangle(cornerRadius: size * 0.06, style: .continuous)
                        .fill(.white)
                        .frame(width: size * 0.46, height: size * 0.58)
                        .overlay(
                            VStack(alignment: .leading, spacing: size * 0.05) {
                                ForEach(0..<4, id: \.self) { line in
                                    Capsule()
                                        .fill(Theme.accent.opacity(line == 0 ? 0.9 : 0.35))
                                        .frame(width: size * (line == 3 ? 0.18 : 0.3), height: size * 0.035)
                                }
                            }
                        )
                        .offset(x: size * 0.05, y: size * 0.02)
                }
            )
            .frame(width: size, height: size)
            .shadow(color: Theme.accent.opacity(0.35), radius: size * 0.18, y: size * 0.08)
    }
}

private struct QuickActionTile: View {
    let title: String
    let subtitle: String
    let systemImage: String
    let tint: Color
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                IconTile(systemImage: systemImage, tint: tint, size: 34)
                Spacer(minLength: 0)
                Text(title)
                    .font(Theme.Font.headline)
                    .foregroundStyle(Theme.textPrimary)
                Text(subtitle)
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.textSecondary)
            }
            .padding(Theme.Spacing.m + 2)
            .frame(maxWidth: .infinity, minHeight: 112, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.xl, style: .continuous)
                    .fill(Theme.surface)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.Radius.xl, style: .continuous)
                    .strokeBorder(hovering ? tint.opacity(0.5) : Theme.separator, lineWidth: 1)
            )
            .shadow(color: .black.opacity(hovering ? 0.12 : 0.05), radius: hovering ? 12 : 4, y: hovering ? 5 : 2)
            .offset(y: hovering ? -2 : 0)
            .animation(.easeOut(duration: 0.15), value: hovering)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}

private struct RecentDocumentCard: View {
    let url: URL
    let action: () -> Void
    @State private var thumbnail: NSImage?
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: Theme.Spacing.s) {
                ZStack {
                    RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                        .fill(Theme.surfaceRaised)
                    if let thumbnail {
                        Image(nsImage: thumbnail)
                            .resizable()
                            .aspectRatio(contentMode: .fit)
                            .padding(10)
                            .shadow(color: Theme.pageShadow, radius: 3, y: 1)
                    } else {
                        Image(systemName: "doc.richtext")
                            .font(.system(size: 28, weight: .light))
                            .foregroundStyle(Theme.textTertiary)
                    }
                }
                .frame(height: 168)
                .overlay(
                    RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                        .strokeBorder(hovering ? Theme.accent.opacity(0.6) : Theme.separator, lineWidth: hovering ? 2 : 1)
                )
                Text(url.deletingPathExtension().lastPathComponent)
                    .font(Theme.Font.callout.weight(.medium))
                    .foregroundStyle(Theme.textPrimary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(url.deletingLastPathComponent().lastPathComponent)
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.textTertiary)
                    .lineLimit(1)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(url.path)
        .task(id: url) {
            thumbnail = await Self.loadThumbnail(url)
        }
    }

    private static func loadThumbnail(_ url: URL) async -> NSImage? {
        await Task.detached(priority: .utility) {
            guard let document = PDFDocument(url: url), let page = document.page(at: 0) else { return nil }
            return page.thumbnail(of: NSSize(width: 240, height: 300), for: .cropBox)
        }.value
    }
}
