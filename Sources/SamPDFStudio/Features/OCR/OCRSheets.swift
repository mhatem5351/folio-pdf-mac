import SwiftUI

/// Recognize Text (OCR): Apple Vision (on-device, Arabic-ready) or Tesseract.
struct OCRSheet: View {
    @EnvironmentObject private var store: AppStore
    @Environment(\.dismiss) private var dismiss
    @ObservedObject private var run: VisionOCRRun = AppStore.visionOCRRun

    private enum Engine: String, CaseIterable, Identifiable {
        case vision, tesseract
        var id: String { rawValue }
    }

    @State private var engine: Engine = .vision
    @State private var selectedCodes: [String] = AppStore.defaultOCRLanguageCodes
    @State private var detectAutomatically = false
    @State private var showAllLanguages = false
    @State private var pages = PageRangeSelection()
    @State private var skipPagesWithText = true

    private static let featuredCodes = ["ar-SA", "en-US", "fr-FR", "de-DE", "es-ES", "it-IT", "pt-BR", "tr-TR", "ru-RU", "zh-Hans", "ja-JP", "ko-KR"]

    var body: some View {
        ToolSheetScaffold(
            tool: .ocr,
            primaryTitle: run.isRunning ? "Recognizing…" : "Recognize Text",
            canSubmit: canSubmit,
            width: 560,
            onSubmit: submit
        ) {
            if run.isRunning {
                progressSection
            } else {
                outcomeBanner
                engineSection
                languageSection
                pagesSection
            }
        }
        .onAppear {
            run.settle(storeBusy: store.busy)
            run.reset()
            VisionOCR.warmUp(languages: selectedCodes)
        }
        .onChange(of: selectedCodes) { _, codes in
            if !detectAutomatically { VisionOCR.warmUp(languages: codes) }
        }
        .onChange(of: store.busy) { _, busy in
            // The engine step finished (success closes the sheet; a failure
            // was already reported by AppStore).
            guard !busy else { return }
            if run.phase == .writing {
                run.settle(storeBusy: false)
                dismiss()
            }
        }
        .onChange(of: run.phase) { _, phase in
            if case .finished = phase { dismiss() }
        }
        .onDisappear {
            // Closing the sheet mid-recognition means "stop".
            if run.phase == .recognizing { run.cancel() }
        }
    }

    // MARK: Sections

    private var engineSection: some View {
        FormSection("Engine") {
            HStack(spacing: Theme.Spacing.s) {
                engineCard(
                    .vision,
                    title: "Apple Vision",
                    detail: "On-device, fast, excellent Arabic",
                    icon: "eye",
                    badge: "Recommended"
                )
                engineCard(
                    .tesseract,
                    title: "Tesseract",
                    detail: "OCRmyPDF; deskews and rotates pages",
                    icon: "text.viewfinder",
                    badge: nil
                )
            }
        }
    }

    private func engineCard(_ value: Engine, title: String, detail: String, icon: String, badge: String?) -> some View {
        let selected = engine == value
        return Button {
            engine = value
        } label: {
            HStack(alignment: .top, spacing: Theme.Spacing.s) {
                IconTile(systemImage: icon, tint: selected ? Theme.accent : Theme.textSecondary, size: 30)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(title)
                            .font(Theme.Font.headline)
                            .foregroundStyle(Theme.textPrimary)
                        if let badge {
                            Badge(text: badge, tint: Theme.success)
                        }
                    }
                    Text(detail)
                        .font(Theme.Font.caption)
                        .foregroundStyle(Theme.textSecondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            .padding(Theme.Spacing.s + 2)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                    .fill(selected ? Theme.accentSoft : Theme.surface)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.Radius.medium, style: .continuous)
                    .strokeBorder(selected ? Theme.accent.opacity(0.55) : Theme.outline, lineWidth: selected ? 1.5 : 1)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var languageSection: some View {
        FormSection("Languages", footnote: languageFootnote) {
            if engine == .vision {
                Toggle("Detect languages automatically", isOn: $detectAutomatically)
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    .font(Theme.Font.body)
            }
            if AppStore.ocrLanguages.isEmpty {
                InfoBanner(kind: .warning, text: "Apple Vision text recognition is not available on this Mac.")
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 6)], alignment: .leading, spacing: 6) {
                    ForEach(visibleLanguages) { language in
                        languageChip(language)
                    }
                }
                .disabled(engine == .vision && detectAutomatically)
                .opacity(engine == .vision && detectAutomatically ? 0.45 : 1)
                if AppStore.ocrLanguages.count > Self.featuredCodes.count {
                    Button(showAllLanguages ? "Show fewer languages" : "Show all \(AppStore.ocrLanguages.count) languages") {
                        withAnimation(.easeOut(duration: 0.15)) { showAllLanguages.toggle() }
                    }
                    .buttonStyle(.link)
                    .font(Theme.Font.callout)
                }
            }
            if let languageError {
                InfoBanner(kind: .warning, text: languageError)
            }
        }
    }

    private func languageChip(_ language: OCRLanguage) -> some View {
        let order = selectedCodes.firstIndex(of: language.code)
        let selected = order != nil
        let unavailable = engine == .tesseract && language.tesseractCode == nil
        return Button {
            toggle(language.code)
        } label: {
            HStack(spacing: 6) {
                ZStack {
                    Circle()
                        .fill(selected ? Theme.accent : Color.clear)
                        .overlay(Circle().strokeBorder(selected ? Theme.accent : Theme.outline, lineWidth: 1))
                        .frame(width: 16, height: 16)
                    if let order {
                        Text("\(order + 1)")
                            .font(.system(size: 9, weight: .bold))
                            .foregroundStyle(Theme.onAccent)
                    }
                }
                VStack(alignment: .leading, spacing: 0) {
                    Text(language.name)
                        .font(Theme.Font.callout.weight(selected ? .semibold : .regular))
                        .foregroundStyle(Theme.textPrimary)
                        .lineLimit(1)
                    if !language.nativeName.isEmpty {
                        Text(language.nativeName)
                            .font(Theme.Font.caption)
                            .foregroundStyle(Theme.textSecondary)
                            .lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: Theme.Radius.small + 1, style: .continuous)
                    .fill(selected ? Theme.accentSoft : Theme.surface)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.Radius.small + 1, style: .continuous)
                    .strokeBorder(selected ? Theme.accent.opacity(0.4) : Theme.separator, lineWidth: 1)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(unavailable)
        .opacity(unavailable ? 0.4 : 1)
        .help(unavailable ? "Tesseract has no model for \(language.name)" : language.code)
    }

    private var pagesSection: some View {
        FormSection("Pages") {
            PageRangePicker(selection: $pages, pageCount: store.pageCount)
            if let pageError {
                Text(pageError)
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.danger)
            }
            Toggle("Skip pages that already contain text", isOn: $skipPagesWithText)
                .toggleStyle(.checkbox)
                .font(Theme.Font.body)
            if engine == .tesseract && pages.mode == .custom {
                InfoBanner(kind: .warning, text: "Tesseract runs on the whole document or the current page. Choose one of those, or use Apple Vision for a page range.")
            }
        }
    }

    private var progressSection: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.l) {
            HStack(spacing: Theme.Spacing.m) {
                ProgressView()
                    .controlSize(.small)
                VStack(alignment: .leading, spacing: 2) {
                    Text(progressTitle)
                        .font(Theme.Font.headline)
                        .foregroundStyle(Theme.textPrimary)
                    Text(progressDetail)
                        .font(Theme.Font.callout)
                        .foregroundStyle(Theme.textSecondary)
                }
                Spacer()
                if run.totalPages > 0 {
                    Badge(text: "\(run.completedPages)/\(run.totalPages)")
                }
            }
            ProgressView(value: Double(run.completedPages), total: Double(max(run.totalPages, 1)))
                .progressViewStyle(.linear)
                .tint(Theme.accent)
            HStack {
                Text(run.phase == .recognizing
                     ? "Recognition runs on this Mac; nothing is uploaded."
                     : "Almost done.")
                    .font(Theme.Font.caption)
                    .foregroundStyle(Theme.textTertiary)
                Spacer()
                Button("Stop") { run.cancel() }
                    .buttonStyle(SecondaryButtonStyle())
                    .disabled(run.phase != .recognizing)
            }
        }
        .padding(Theme.Spacing.l)
        .background(
            RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                .fill(Theme.surfaceRaised.opacity(0.6))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.Radius.large, style: .continuous)
                .strokeBorder(Theme.separator, lineWidth: 1)
        )
    }

    @ViewBuilder private var outcomeBanner: some View {
        switch run.phase {
        case .cancelled:
            InfoBanner(kind: .info, text: "Recognition stopped. The document was not changed.")
        case .nothingFound(let message):
            InfoBanner(kind: .info, text: message)
        case .failed(let message):
            InfoBanner(kind: .danger, text: message)
        default:
            EmptyView()
        }
    }

    // MARK: Logic

    private var visibleLanguages: [OCRLanguage] {
        let all = AppStore.ocrLanguages
        guard !showAllLanguages else { return all }
        let featured = all.filter { Self.featuredCodes.contains($0.code) || selectedCodes.contains($0.code) }
        return featured.isEmpty ? all : featured
    }

    private var effectiveCodes: [String] {
        engine == .vision && detectAutomatically ? [] : selectedCodes
    }

    private var languageFootnote: String {
        if engine == .vision && detectAutomatically {
            return "Vision picks the languages itself. Choosing them is more accurate for Arabic."
        }
        return "Numbers show priority: the first language guides recognition. Mixed Arabic and English pages work best with both selected."
    }

    private var languageError: String? {
        if engine == .vision && detectAutomatically { return nil }
        if selectedCodes.isEmpty { return "Choose at least one language." }
        if engine == .tesseract && selectedLanguages.allSatisfy({ $0.tesseractCode == nil }) {
            return "Tesseract has no model for the selected languages."
        }
        return nil
    }

    private var pageError: String? {
        guard pages.mode == .custom else { return nil }
        let spec = pages.custom.trimmingCharacters(in: .whitespaces)
        guard !spec.isEmpty else { return "Enter pages such as 1-3, 5." }
        return AppStore.ocrPageIndices(from: spec, pageCount: store.pageCount) == nil
            ? "Use pages between 1 and \(store.pageCount), e.g. 1-3, 5."
            : nil
    }

    private var selectedLanguages: [OCRLanguage] {
        selectedCodes.compactMap { code in AppStore.ocrLanguages.first { $0.code == code } }
    }

    private var canSubmit: Bool {
        guard !run.isRunning, !store.busy, store.document != nil else { return false }
        guard languageError == nil, pageError == nil else { return false }
        if engine == .vision { return !AppStore.ocrLanguages.isEmpty }
        return pages.mode != .custom
    }

    private var progressTitle: String {
        switch run.phase {
        case .writing: "Adding searchable text"
        default:
            run.totalPages > 0
                ? "Recognizing page \(min(run.completedPages + 1, run.totalPages)) of \(run.totalPages)"
                : "Preparing pages"
        }
    }

    private var progressDetail: String {
        let languages = effectiveCodes.isEmpty
            ? "Automatic language detection"
            : selectedLanguages.map(\.name).joined(separator: " + ")
        return run.phase == .writing ? "Embedding an invisible text layer" : languages
    }

    private func toggle(_ code: String) {
        if let index = selectedCodes.firstIndex(of: code) {
            selectedCodes.remove(at: index)
        } else {
            selectedCodes.append(code)
        }
    }

    private func submit() {
        switch engine {
        case .vision:
            store.recognizeTextWithVision(
                languages: effectiveCodes,
                pageSpec: pages.spec(currentPage: store.currentPageNumber),
                skipPagesWithText: skipPagesWithText
            )
            // The sheet stays open to show progress; it closes when the new
            // version is in place.
        case .tesseract:
            store.recognizeTextWithTesseract(
                languages: selectedLanguages,
                currentPageOnly: pages.mode == .current,
                skipPagesWithText: skipPagesWithText
            )
            dismiss()
        }
    }
}
