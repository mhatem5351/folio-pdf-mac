import PDFKit
import SwiftUI

struct PDFWorkspaceView: View {
    @EnvironmentObject private var store: AppStore

    var body: some View {
        Group {
            if let document = store.document {
                if store.selectedOperation == .pages {
                    PagesGridView()
                } else {
                    PDFPreviewView(
                        document: document,
                        selectedText: $store.selectedPDFText,
                        selectedPage: $store.selectedPDFPage,
                        selectedRect: $store.selectedPDFRect,
                        isEditing: store.selectedOperation == .edit,
                        isAnnotating: store.selectedOperation == .annotate,
                        isFillSign: store.selectedOperation == .fillSign,
                        isRedline: store.selectedOperation == .redline,
                        blockMoveMode: store.blockMoveMode,
                        editTool: store.selectedEditTool,
                        annotateTool: store.selectedAnnotateTool,
                        fillSignTool: store.selectedFillSignTool,
                        redlineTool: store.selectedRedlineTool,
                        activeBlock: store.activeBlock,
                        activeBlockGuides: store.activeBlockGuides,
                        newTextDraft: store.newTextDraft,
                        pdfViewProxy: store.pdfViewProxy,
                        onEditBlock: { page, x, y in
                            store.editBlock(page: page, x: x, y: y)
                        },
                        onCommitBlock: { block, text in
                            store.commitBlockEdit(block: block, newText: text)
                        },
                        onCancelBlock: { block in
                            store.cancelBlockEdit(block: block)
                        },
                        onMoveBlock: { block, text, x, y in
                            store.moveActiveBlock(block: block, text: text, toEngineX: x, y: y)
                        },
                        onCommitNewText: { draft, text in
                            store.commitNewText(draft: draft, text: text)
                        },
                        onCancelNewText: { draft in
                            store.cancelNewText(draft: draft)
                        },
                        onBlockSelectionChanged: { range, length in
                            store.activeBlockSelection = range
                            store.activeBlockTextLength = length
                        },
                        onCopyRegion: {
                            store.copySelectedRegion()
                        },
                        onPasteRegion: { page, x, y in
                            store.pasteCopiedRegion(page: page, x: x, y: y)
                        },
                        onMoveRegion: { page, x, y in
                            store.moveSelectedRegion(page: page, x: x, y: y)
                        },
                        onGrabGuideRects: { store.guideRects(forPage: $0) },
                        liveActiveBlock: { store.activeBlock },
                        liveNewTextDraft: { store.newTextDraft },
                        onMarkupSelection: { selection in
                            store.applyMarkup(selection, kind: markupKind)
                        },
                        onPlaceNote: { pageIndex, point in
                            store.placeNote(pageIndex: pageIndex, pdfKitPoint: point)
                        },
                        onAddTextBox: { pageIndex, point, text in
                            store.addTextBox(pageIndex: pageIndex, pdfKitPoint: point, text: text)
                        },
                        onPlaceSignature: { page, x, y in
                            store.placeSignature(page: page, x: x, y: y)
                        },
                        onPlaceImage: { page, x, y in
                            store.placeImage(page: page, x: x, y: y)
                        },
                        onPlaceTextDraft: { page, x, y in
                            store.placeTextDraft(page: page, x: x, y: y)
                        },
                        onLinkRegion: { pageIndex, rect in
                            store.linkRegionSelected(pageIndex: pageIndex, pdfKitRect: rect)
                        },
                        onFillSignClick: { page, x, y in
                            store.fillSignClick(page: page, x: x, y: y)
                        },
                        onRedlineSelection: { selection in
                            store.applyRedline(selection)
                        },
                        onRedlineCaret: { page, x, y in
                            store.redlineCaret(page: page, x: x, y: y)
                        },
                        onNoteSelection: { selection in
                            store.noteOnSelection(selection)
                        },
                        onAnnotationEdited: { title in
                            store.annotationEdited(title: title)
                        }
                    )
                }
            } else {
                WelcomeView()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .overlay(alignment: .bottom) {
            if store.document != nil, store.selectedOperation != .pages {
                CanvasStatusPill()
                    .padding(.bottom, 16)
            }
        }
    }

    private var markupKind: AnnotationKind {
        switch store.selectedAnnotateTool {
        case .underline: .underline
        case .strikeout: .strikeout
        default: .highlight
        }
    }
}
