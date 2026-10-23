import SwiftUI

struct ProtectSheet: View {
    var body: some View { ToolPlaceholder(tool: .protect) }
}

struct UnlockSheet: View {
    var body: some View { ToolPlaceholder(tool: .unlock) }
}

struct FlattenSheet: View {
    var body: some View { ToolPlaceholder(tool: .flatten) }
}

struct MetadataSheet: View {
    var body: some View { ToolPlaceholder(tool: .metadata) }
}

struct BookmarksSheet: View {
    var body: some View { ToolPlaceholder(tool: .bookmarks) }
}

