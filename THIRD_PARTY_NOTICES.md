# Third-party notices

The dashboard bundles these browser libraries so it works without a CDN.
Their upstream licenses apply to the bundled files and are retained below.

| Component | Bundled files | License |
| --- | --- | --- |
| Mermaid 10.9.3 | `web/vendor/mermaid.min.js` | MIT; see [`web/vendor/mermaid/LICENSE`](web/vendor/mermaid/LICENSE) and its [dependency license inventory](web/vendor/mermaid/THIRD_PARTY_NOTICES.md) |
| KaTeX 0.16.21 | `web/vendor/katex/` JavaScript, CSS, and fonts | MIT; see [`web/vendor/katex/LICENSE`](web/vendor/katex/LICENSE) |
| Prettier 3.9.8 | `web/vendor/prettier/` | MIT; see [`web/vendor/prettier/LICENSE`](web/vendor/prettier/LICENSE) |
| lottie-web 5.13.0 | `web/vendor/lottie/lottie_light.min.js` | MIT; see [`web/vendor/lottie/LICENSE.md`](web/vendor/lottie/LICENSE.md) |

The chat-loading artwork at `animations/chat-loading-neobrutalist.json` is
project-provided and is covered by the root [`LICENSE`](LICENSE).

The Docker image installs OpenCode from npm at the version pinned in
[`Dockerfile`](Dockerfile); npm retains its package license and dependency
metadata inside the image.
