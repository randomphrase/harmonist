#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

# npm ci owns version and integrity checks; copy only the files we serve.
mkdir -p static/vendor
cp node_modules/htmx.org/dist/htmx.min.js static/vendor/htmx.min.js
cp node_modules/idiomorph/dist/idiomorph-ext.min.js static/vendor/idiomorph-ext.min.js
cp node_modules/htmx.org/LICENSE static/vendor/htmx.LICENSE
cp node_modules/idiomorph/LICENSE static/vendor/idiomorph.LICENSE

# Web fonts (#1029): each package's stylesheet, the font files it names, and
# its licence. The stylesheet's URLs are relative, so the files keep its layout.
for font in inter fira-code; do
    src="node_modules/@fontsource-variable/$font"
    dst="static/vendor/$font"
    rm -rf "$dst"
    mkdir -p "$dst/files"
    cp "$src/index.css" "$dst/index.css"
    cp "$src/LICENSE" "$dst/LICENSE"
    for file in $(grep -o 'files/[^)]*' "$src/index.css"); do
        cp "$src/$file" "$dst/$file"
    done
done
