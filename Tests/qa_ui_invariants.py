#!/usr/bin/env python3
"""Source-level regression checks for the behavioral rules Sam cares about.

These guard the interaction contract:
- Reader mode never routes clicks into edit operations.
- Edit mode always starts on the Text tool, never Redact.
- Redaction only happens with the Redact tool explicitly active, and asks first.
- The inline text editor stays tight and transparent.
- Every document mutation flows through the version stack (undo/redo).
- A plain PDF copy export is always available.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Sources" / "SamPDFStudio"


def read(relative: str) -> str:
    return (SRC / relative).read_text(encoding="utf-8")


def assert_contains(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise AssertionError(f"Missing invariant: {label}")


def assert_absent(text: str, needle: str, label: str) -> None:
    if needle in text:
        raise AssertionError(f"Forbidden pattern present: {label}")


def main() -> int:
    app_store = read("Stores/AppStore.swift")
    # The window chrome is split across ContentView and Views/Shell/*.
    content_view = read("Views/ContentView.swift") + "".join(
        f.read_text(encoding="utf-8") for f in sorted((SRC / "Views" / "Shell").glob("*.swift"))
    )
    workspace_view = read("Views/PDFWorkspaceView.swift")
    preview_view = read("Views/PDFPreviewView.swift")
    operations = read("Models/PDFOperation.swift")
    session = read("Models/DocumentSession.swift")

    # Reader mode stays a pure reader; canvas edit paths are explicitly gated.
    assert_contains(workspace_view, "isEditing: store.selectedOperation == .edit", "preview edit mode is explicit")
    assert_contains(workspace_view, "isAnnotating: store.selectedOperation == .annotate", "preview annotate mode is explicit")
    assert_contains(preview_view, "let isEditing: Bool", "preview receives edit mode")
    assert_contains(preview_view, "var isEditingEnabled = false", "PDFView tracks edit mode")
    assert_contains(preview_view, "var isAnnotatingEnabled = false", "PDFView tracks annotate mode")
    assert_contains(preview_view, "if isEditingEnabled {", "edit mouse paths are gated")
    assert_contains(preview_view, "if isAnnotatingEnabled", "annotate mouse paths are gated")
    assert_contains(preview_view, "super.mouseDown(with: event)", "ungated clicks fall through to normal reading")
    assert_contains(preview_view, "view.isEditingEnabled = isEditing", "SwiftUI state reaches AppKit PDF view")

    # Inline text editor (new text boxes) stays tight and transparent.
    assert_contains(preview_view, "drawsBackground = false", "inline text editor has no white fill")
    assert_contains(preview_view, "backgroundColor = .clear", "inline text editor is transparent")
    assert_contains(preview_view, "layer?.borderWidth = 0", "inline text editor has no heavy border")

    # Block editing works in place: click a paragraph, edit in place,
    # drag the frame to move, Esc cancels.
    assert_contains(preview_view, "class BlockEditorView", "block editor exists")
    assert_contains(preview_view, "characterIndexForInsertion", "click places the caret inside the block")
    assert_contains(preview_view, "onMoved", "block frame is draggable")
    assert_contains(app_store, "func editBlock", "store opens blocks for editing")
    assert_contains(app_store, "func commitBlockEdit", "store commits block edits")
    assert_contains(app_store, "func moveActiveBlock", "store moves blocks")
    engine_src = (ROOT / "Engine" / "pdf_engine.py").read_text(encoding="utf-8")
    assert_contains(engine_src, "def cmd_block_at", "engine finds blocks at a click")
    assert_contains(engine_src, "def cmd_replace_block", "engine re-renders whole blocks")

    # Escape cancels the block editor by EVERY route: the editor's own key
    # handling, AppKit's cancel action, the PDF view, a key monitor that runs
    # before the window can swallow the key, and the SwiftUI style panel.
    assert_contains(preview_view, "override func cancelOperation", "editors answer AppKit's cancel action")
    assert_contains(preview_view, "func cancelLiveEditors", "one place cancels every live editor")
    assert_contains(preview_view, "if event.keyCode == 53, cancelLiveEditors()", "PDF view routes Escape to cancel")
    assert_contains(preview_view, "addLocalMonitorForEvents", "Escape is caught before the window can swallow it")
    assert_contains(preview_view, "blockEditor?.textView.abandon()", "cancel closes editors WITHOUT committing")
    assert_contains(content_view, ".onExitCommand { store.cancelActiveEditing() }", "the style panel cancels on Escape")
    assert_contains(app_store, "func cancelActiveEditing", "store exposes a cancel-everything entry point")

    # Clicking away from an untouched block must never rewrite the document —
    # but a dragged resize IS a change, even with the same text and style.
    assert_contains(
        app_store,
        "guard textChanged || block.styleChanged || block.wasResized else {",
        "unchanged block edits are a no-op",
    )
    assert_contains(app_store, "renderRect: block.resizedEngineRectString", "a resized block commits into its new box")
    assert_contains(preview_view, "func handle(at point: NSPoint) -> ResizeHandle?", "frame handles are hit-testable, not decorative")
    assert_contains(preview_view, "func resizedContentFrame", "the resized frame is read back for the commit")

    # The Text style panel floats where the user parks it (it used to cover
    # the very text being edited, top-right, and swallow clicks).
    assert_contains(app_store, "var textPanelOffset", "panel position survives view updates")
    assert_contains(content_view, "dragHeader(\"Text\")", "the panel header is the drag handle")
    assert_contains(content_view, "func clampedOffset", "the panel stays inside the window")

    # Tool gating in the store.
    assert_contains(app_store, "guard selectedOperation == .edit, selectedEditTool == .contentText", "text replacement requires text tool")
    assert_contains(app_store, "guard selectedOperation == .edit, selectedEditTool == .grab", "region actions require grab tool")
    assert_contains(app_store, "guard selectedOperation == .edit, selectedEditTool == .redact", "redaction requires redact tool")
    assert_contains(app_store, "guard selectedOperation == .annotate", "markup requires annotate mode")

    # Edit mode must open on the Text tool and never silently switch to Redact.
    assert_contains(app_store, "selectedEditTool = .contentText", "edit mode defaults to the text tool")
    assert_absent(app_store, "selectedEditTool = .redact", "AppStore must not silently switch into redaction mode")
    assert_absent(app_store, "func useSelectedTextForRedact", "hidden selected-text-to-redact helper must stay removed")

    # Redaction is confirmed with the user and never labelled.
    redact_body = app_store.split("func redactSelectedText")[1].split("func ")[0]
    if "confirm(" not in redact_body:
        raise AssertionError("Redaction must ask for confirmation before running")
    if 'label: ""' not in redact_body:
        raise AssertionError("Redaction must not stamp a visible label")

    # The redact button in the tool strip only renders for the redact tool.
    redact_calls = [m.start() for m in re.finditer(r"store\.redactSelectedText\(\)", content_view)]
    for position in redact_calls:
        prefix = content_view[:position]
        case_index = prefix.rfind("case .")
        if case_index == -1 or not prefix[case_index:].startswith("case .redact"):
            raise AssertionError("Redact button must be gated under the redact tool case")

    # Every mutation goes through the version stack.
    assert_contains(app_store, "session.commit(output)", "engine ops commit versions")
    assert_contains(app_store, "session.commit(url)", "PDFKit ops commit versions")
    assert_contains(session, "func undo()", "session supports undo")
    assert_contains(session, "func redo()", "session supports redo")
    assert_contains(app_store, "func undo()", "store exposes undo")
    assert_contains(app_store, "func redo()", "store exposes redo")

    # Saving is explicit; the original is never touched by ordinary operations.
    assert_contains(session, "func saveToOriginal()", "save writes back to the original explicitly")
    ordinary_ops = app_store.split("func save()")[0]
    assert_absent(ordinary_ops, "saveToOriginal", "no operation may auto-save over the original")

    # A plain PDF copy export stays available.
    assert_contains(app_store, "func exportPDFCopy()", "PDF export copies the current edited PDF")
    assert_contains(content_view, "store.exportPDFCopy()", "PDF export action is visible")

    # Mode list stays lean — no Jobs/engine plumbing exposed as a mode.
    assert_absent(operations, "case jobs", "jobs must not be a top-level mode")

    # Regressions found in the manual UI smoke (Oct 2026).
    palette = read("Views/Shell/ToolsHubView.swift")
    assert_absent(palette, ".id(index)", "palette rows keyed by position show stale entries when filtered")
    assert_contains(palette, ".id(command.id)", "palette rows are keyed by command")
    grid = read("Views/PagesGridView.swift")
    assert_absent(grid, "toggleSelection(index)", "plain click in the page grid must not add to the selection")
    assert_contains(grid, "flags.contains(.command)", "page grid ⌘-click toggles")
    load_pdf = app_store.split("func loadPDF(")[1].split("\n    func ")[0]
    assert_contains(load_pdf, "activeTool = nil", "opening a file closes a stale tool sheet")
    assert_contains(load_pdf, "view.go(to: first)", "opening a file starts at page 1")
    move_page = app_store.split("func movePage(")[1].split("\n    func ")[0]
    assert_contains(move_page, "item.destination = PDFDestination(page: page", "reordering keeps bookmarks on the page")
    tools = read("Stores/AppStore+Tools.swift")
    print_doc = tools.split("func printDocument()")[1].split("\n    func ")[0]
    assert_absent(print_doc, "let view = pdfViewProxy.pdfView else", "system print must not need the canvas PDFView")
    bookmarks = read("Features/Document/DocumentToolSheets.swift")
    assert_absent(bookmarks, "entries.swapAt(index, target)", "bookmark move must carry the whole subtree")

    print("UI invariants ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
