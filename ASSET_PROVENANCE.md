# Asset provenance

The font and icon files below retain their upstream licenses. They are not
relicensed under the application MIT license. File counts include duplicate
copies used by the foundation previews. No recording is distributed.

## Fonts

| Family | Files | Font/package version | Exact upstream SHA256 matches | License |
|---|---:|---|---:|---|
| [LXGW WenKai](https://github.com/lxgw/LxgwWenKai/tree/v1.250) | 291 | 1.250 | 0 | [SIL OFL 1.1](static/fonts/LICENSES/LXGWWenKai-OFL.txt) |
| [LXGW WenKai Mono](https://github.com/lxgw/LxgwWenKai/tree/v1.250) | 291 | 1.250 | 0 | [SIL OFL 1.1](static/fonts/LICENSES/LXGWWenKaiMono-OFL.txt) |
| [Noto Serif SC](https://www.npmjs.com/package/@fontsource/noto-serif-sc/v/5.3.1) | 404 | 2.003-H1; fontsource 5.3.1 | 401 | [SIL OFL 1.1](static/fonts/LICENSES/NotoSerifSC-OFL.txt) |
| [Cormorant Garamond](https://github.com/CatharsisFonts/Cormorant) | 26 | 4.000 / 4.001; fontsource 5.2.5 / 5.3.0 | 26 | [SIL OFL 1.1](static/fonts/LICENSES/CormorantGaramond-OFL.txt) |
| [Montserrat](https://github.com/JulietaUla/Montserrat) | 4 | 9.000; fontsource comparator 5.2.6 | 2 | [SIL OFL 1.1](static/fonts/LICENSES/Montserrat-OFL.txt) |
| [Courier Prime](https://github.com/quoteunquoteapps/CourierPrime) | 1 | 3.018; fontsource 5.2.6 | 1 | [SIL OFL 1.1](static/fonts/LICENSES/CourierPrime-OFL.txt) |
| [Pinyon Script](https://github.com/SorkinType/Pinyon) | 6 | 1.008; fontsource 5.3.0 | 6 | [SIL OFL 1.1](static/fonts/LICENSES/PinyonScript-OFL.txt) |

The original local fonts were read with fontTools, including name IDs 0
(copyright), 1/16 (family), 5 (version), 13 (license) and 14 (license URL).
Several web subsets omit ID 13; absence is not presented as an embedded license.
The license texts are supplied separately. Source SHA256 was checked for every
copied file. The Noto files match fontsource 5.3.1 except two TTF conversions
and one WOFF2 with different head/name serialization; that WOFF2 has identical
other font tables and identical decoded name strings. The six foundation
Cormorant copies match fontsource 5.2.5; the other 20 match 5.3.0.

LXGW 1.250 web subsets and two Montserrat 500 copies do not match the downloaded
upstream binaries byte for byte. Identification uses filenames and font name
metadata (family, version and upstream copyright), rather than claiming a hash
match. LXGW upstream v1.250 TTFs are comparison originals, not copies of these
web subsets. Subsetting and format conversion create modified font software.
The applicable license headers reviewed here declare no Reserved Font Names,
including LXGW **v1.250**; their generic RFN clauses do not themselves reserve
a name. No rename is therefore required for these files under those licenses.
This finding is version-specific and does not apply to later LXGW releases.
The redistributed web font binaries were not edited in this restoration.

Upstream license sources: [LXGW v1.250](https://github.com/lxgw/LxgwWenKai/blob/v1.250/OFL.txt),
[Google Fonts](https://github.com/google/fonts/tree/main/ofl), and the exact
fontsource packages named above. Original copyright notices also remain in
the font name tables; see THIRD_PARTY_NOTICES.md for attribution.

## Icons

23 embedded SVG groups match Iconoir 7.12.1 (13 distinct names); 22 standalone
SVG copies match Phosphor core 2.1.1 (11 distinct names). All 11 foundation CSS
mask references were restored to the matching SVG files.
The source did not record an npm package version. These are the exact audited
comparison releases; this is not a claim that a source lockfile identified them.
Comparison normalizes absolute/relative SVG path commands, numeric spelling,
line direction and polyline syntax, retaining all segments and coordinates.
No fuzzy image similarity or approximate coordinate matching was used.
Original SVG markup is retained for accepted groups. Unknown drawings stay
replaced by the public edition's elementary placeholders.

| Library / icon | Restored occurrences |
|---|---:|
| iconoir: `icons/regular/bubble-search.svg` | 1 |
| iconoir: `icons/regular/download.svg` | 1 |
| iconoir: `icons/regular/empty-page.svg` | 1 |
| iconoir: `icons/regular/menu.svg` | 1 |
| iconoir: `icons/regular/multiple-pages-empty.svg` | 1 |
| iconoir: `icons/regular/nav-arrow-down.svg` | 4 |
| iconoir: `icons/regular/nav-arrow-left.svg` | 1 |
| iconoir: `icons/regular/nav-arrow-right.svg` | 8 |
| iconoir: `icons/regular/phone.svg` | 1 |
| iconoir: `icons/regular/plus.svg` | 1 |
| iconoir: `icons/regular/search.svg` | 1 |
| iconoir: `icons/regular/timer-off.svg` | 1 |
| iconoir: `icons/solid/timer.svg` | 1 |
| phosphor: `assets/light/clipboard-text-light.svg` | 2 |
| phosphor: `assets/light/file-doc-light.svg` | 2 |
| phosphor: `assets/light/file-image-light.svg` | 2 |
| phosphor: `assets/light/file-light.svg` | 2 |
| phosphor: `assets/light/file-pdf-light.svg` | 2 |
| phosphor: `assets/light/file-text-light.svg` | 2 |
| phosphor: `assets/light/magnifying-glass-light.svg` | 2 |
| phosphor: `assets/light/pencil-simple-light.svg` | 2 |
| phosphor: `assets/light/play-light.svg` | 2 |
| phosphor: `assets/light/plus-light.svg` | 2 |
| phosphor: `assets/light/terminal-light.svg` | 2 |

128 unmatched source groups/fragments/data URIs remain excluded. This count
includes drawings that may be custom or modified, rather than asserting that
all were library icons. The newly authored neutral application icon and letter
avatars remain unchanged. The absent MP3 is not covered by these licenses.

Sources: [Iconoir 7.12.1](https://www.npmjs.com/package/iconoir/v/7.12.1),
[Phosphor core 2.1.1](https://www.npmjs.com/package/@phosphor-icons/core/v/2.1.1).
