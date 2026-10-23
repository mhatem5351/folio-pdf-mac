# Source me: shared build/test environment for Folio PDF development.
#   source script/dev-env.sh            # main checkout
#   FOLIO_BUILD_TAG=forms source script/dev-env.sh   # parallel checkout, own build dir
# The macOS 27 SDK makes SwiftUI @State a macro whose plugin ships only with
# full Xcode; Command Line Tools builds therefore use the 26.5 SDK.
export SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk
export FOLIO_VENV_DIR="$HOME/Library/Application Support/FolioPDF/engine-venv"
export FOLIO_SCRATCH_DIR="$HOME/Library/Caches/FolioPDF/build${FOLIO_BUILD_TAG:+-$FOLIO_BUILD_TAG}"
# ".noindex" keeps Spotlight/Launchpad from listing dev builds next to the installed app.
export FOLIO_DIST_DIR="$HOME/Builds/FolioPDF${FOLIO_BUILD_TAG:+-$FOLIO_BUILD_TAG}.noindex"
export FOLIO_PY="$FOLIO_VENV_DIR/bin/python3"
alias foliobuild='swift build --scratch-path "$FOLIO_SCRATCH_DIR" 2>&1 | grep -E "error|warning: unre|Build complete"'
