# KaTeX vendored runtime

This directory contains the browser distribution from `katex@0.18.5`, obtained
from the official npm package. It is kept in the plugin so the archive page can
render formulas without a CDN or any other runtime network request.

Included files:

- `katex.min.js` and `katex.min.css`
- `contrib/auto-render.min.js`
- the complete `dist/fonts/` directory
- the upstream MIT `LICENSE`

Upstream: https://github.com/KaTeX/KaTeX

SHA-256 checksums for the JavaScript and CSS entry points:

```text
30c9f7c07bf54d341ffd8f16dc6632766f12b16d0a064e2a08f2d6b1744396a6  katex.min.js
5bc44ab327592b75fcf2d412a1b396ebf20203bfe826a1966fb8ab03f8b08bb4  katex.min.css
e5372d199bcdae8b4de71d0f7ceba72a4ba12774a27c60a6f1f77d03b3228ee4  contrib/auto-render.min.js
```
