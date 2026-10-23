# Source me: shared build/test environment for PDF Studio development.
#   source script/dev-env.sh            # main checkout
#   PS_BUILD_TAG=forms source script/dev-env.sh   # parallel checkout, own build dir
# The macOS 27 SDK makes SwiftUI @State a macro whose plugin ships only with
# full Xcode; Command Line Tools builds therefore use the 26.5 SDK.
export SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk
export SAMPDF_VENV_DIR="$HOME/Library/Application Support/PDFStudio/engine-venv"
export SAMPDF_SCRATCH_DIR="$HOME/Library/Caches/PDFStudio/build${PS_BUILD_TAG:+-$PS_BUILD_TAG}"
# ".noindex" keeps Spotlight/Launchpad from listing dev builds next to the installed app.
export SAMPDF_DIST_DIR="$HOME/Builds/PDFStudio${PS_BUILD_TAG:+-$PS_BUILD_TAG}.noindex"
export PS_PY="$SAMPDF_VENV_DIR/bin/python3"
alias psbuild='swift build --scratch-path "$SAMPDF_SCRATCH_DIR" 2>&1 | grep -E "error|warning: unre|Build complete"'
