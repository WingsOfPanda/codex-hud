# Declaration-backed dependency notices

The post-0.3.0 audit found six selected dependencies without a separate full
publisher license file. They nevertheless publish explicit standard-license
declarations. Cargo documents `license` as the license under which a package is
released, independently of `license-file`; `OR` permits a choice of alternatives.
A missing filename alone therefore does not invalidate a published declaration.

The collector resolves these six versions through curated, checksum-bound
records. It supplies pinned standard terms, identifies them as canonical SPDX
texts rather than recovered publisher notices, and preserves the actual source
declarations, authors metadata and inherited notices. It does not invent
copyright holders or years, fill template placeholders, accept an unmerged later
license, or silently exempt an unrecognized missing license.

## Curated resolutions

| Package | Published declaration | Selected crate terms | Additional retained material |
| --- | --- | --- | --- |
| `debugserver-types 0.5.0` | MIT | Canonical MIT | Microsoft Debug Adapter Protocol v1.34.0 schema attribution, original documentation/code split grant, MIT and CC BY 3.0 US terms. |
| `deno_core_icudata 0.77.0` | MIT | Canonical MIT for the wrapper | Exact embedded ICU data provenance, its Unicode notice and the complete matching ICU upstream license bundle. |
| `eventsource-stream 0.2.3` | MIT OR Apache-2.0 | Apache-2.0 | Original declaration and authors metadata. |
| `fxhash 0.2.1` | Apache-2.0/MIT; source header explicitly offers either | Apache-2.0 | Actual 2015 Rust Project Developers copyright/licensing header and source provenance. |
| `io_tee 0.1.1` | MIT OR Apache-2.0 | Apache-2.0 | Original declaration; no invented authors field. |
| `linux-keyutils 0.2.4` | Apache-2.0 OR MIT | Apache-2.0 | Original declaration, README grant and authors metadata. |

Canonical terms use SPDX License List data v3.29.0 at immutable commit
`31ba1a50e5397e00a304dbadc76531740e89ee48`. Standard MIT and Apache appendix
placeholders remain unchanged. The bundle labels these terms as standard text
accompanying the declaration; it does not present a template copyright line as
a crate publisher's supplied notice. Authors remain authors metadata.

### Inherited Debug Adapter Protocol schema

The published `debugserver-types` schema matches Microsoft's v1.34.0 schema at
`a62c4164506dad981bbccd4d278c584255f1b5b8` after CRLF normalization. Its actual
grant licenses documentation under CC BY 3.0 US and code under MIT. The collector
preserves that split, Microsoft's copyright/trademark statement, the source
title and link, and both full standard texts. It does not reinterpret the split
as an OR expression or claim that the crate's MIT declaration covers the entire
upstream specification. The source schema is unchanged except for line endings.

### Embedded ICU data

The 10,822,192-byte `icudtl.dat` blob matches Chromium ICU
`ee5f27adc28bd3f15b2c293f726d14d2e336cbd5/common/icudtl.dat` exactly. SHA-256:
`1cf67874b5a87a8363a86fb3f81e3cbbed54d389062dab8fb52308d5cf8c8612`.
This is also the ICU gitlink in the pinned Rusty V8 v150.4.0 source.

The complete matching ICU license bundle has SHA-256
`451167c55c0fa447cc2d5632714f5e3c567fe4f1e1badefab2c1333852198aca`.
It includes Unicode and third-party notices. Retaining the complete upstream
bundle does not assert that every source/build tool listed there ships in the
data blob. The wrapper's MIT declaration is not substituted for the data's
notices. Gitiles `?format=TEXT` content is explicitly base64-decoded before
checking the decoded content hash and caching it.

## Acceptance boundaries

Selected full standard-license bodies and required component notices are
independent inputs. An ICU bundle containing MIT wording cannot satisfy the
wrapper's selected MIT-body requirement; a short fxhash header cannot substitute
for full Apache terms. Removing the Microsoft grant, attribution or CC terms
must fail even if the crate's MIT text remains.

Source/archive checksums, local declarations, selected alternatives and relevant
data/schema/header bytes bind each resolution to the actual selected dependency.
Unknown missing licenses and changed bindings continue to fail. Cold and warm
caches must emit identical bytes and provenance. A cache cannot turn encoded
HTTP content into an unverified license text.

Executed checks and both production-graph inventories are recorded in
[notice-resolution.json](evidence/notice-resolution.json). This follow-up resolves
the technical notice-input blocker recorded in the initial
[audit snapshot](audit-fixes.md); it does not itself build or qualify new release
archives, publish a release, install a package, or change dependencies.

Root acceptance passed 40 packaging tests, generated all five notice/provenance
files for each target, and verified 1,066 x86 and 1,063 ARM production packages.
Each graph includes all six curated resolutions. Repeat generation matched the
worker outputs and produced identical hashes with downloads forbidden. A real
empty-cache ICU fetch decoded and verified all 26,487 bytes; its warm repeat
needed no download. A synthetic package-staging fixture containing these actual
notice files passed inventory, tree and native/runtime provenance validation.
That fixture is not a release archive or an ARM execution test.

Sources:

- [Cargo license declarations and alternatives](https://doc.rust-lang.org/cargo/reference/manifest.html#the-license-and-license-file-fields)
- [Pinned SPDX standard texts](https://github.com/spdx/license-list-data/tree/31ba1a50e5397e00a304dbadc76531740e89ee48/text)
- [Microsoft's matching split grant](https://raw.githubusercontent.com/microsoft/debug-adapter-protocol/a62c4164506dad981bbccd4d278c584255f1b5b8/License.txt)
- [Matching ICU notice bundle](https://chromium.googlesource.com/chromium/deps/icu/+/ee5f27adc28bd3f15b2c293f726d14d2e336cbd5/LICENSE)
