#!/usr/bin/env bash
set -euo pipefail

PATCH_DIR="./patches"
EXCLUDE_PATHS=("tests/" "docs/" "README.md")

SITE_DIR=$(python3 -c "import site; print(site.getsitepackages()[0])")

if [ ! -d "$SITE_DIR" ]; then
    echo "site-packages directory not found: $SITE_DIR"
    exit 1
fi

echo "Zastosowanie patchy w: $SITE_DIR"
    
if ! command -v filterdiff &> /dev/null; then
    echo "'filterdiff' program is missing. Please install the 'patchutils' package."
    exit 1
fi

EXCLUDE_ARGS=()
for path in "${EXCLUDE_PATHS[@]}"; do
    EXCLUDE_ARGS+=(--exclude="*/$path*")
done

for PATCH_FILE in "$PATCH_DIR"/*.patch; do
    [ -e "$PATCH_FILE" ] || continue
    echo "Nakładanie patcha: $(basename "$PATCH_FILE")"
    (
        filterdiff "${EXCLUDE_ARGS[@]}" "$PATCH_FILE" | patch --forward -d "$SITE_DIR" -p1
    )
done

echo "Patches applied."

