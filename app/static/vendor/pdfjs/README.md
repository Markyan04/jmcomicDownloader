# PDF.js

Mozilla PDF.js / pdfjs-dist 6.4.299, legacy minified build.
Upstream: https://github.com/mozilla/pdf.js
Distribution: https://registry.npmjs.org/pdfjs-dist/-/pdfjs-dist-6.4.299.tgz
Package integrity: `sha512-AVl138zALtfaAPvADulE0PZThbYzCBS79nL4pOSL/6Sm/4AH5A21BD9VHt97OlCuzJuCpmeZtAtkinisF4Vb1g==`

Only the rendering library, matching Worker, CMaps, standard fonts and image
decoders are vendored. No generic viewer or build toolchain is required.
Files are unmodified; retain the licenses in this directory and its subdirectories.

To update, fetch a pinned official npm package, verify its published SHA-512
integrity, then replace this directory with the same subset of files. Keep the
library and Worker versions identical and test range loading and CSP in a browser.
