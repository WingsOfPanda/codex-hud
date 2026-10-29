# Reproduce artifact acceptance

The native bundles are the original canonical exports. The AP bundles retain every receipt-bound input/output and its original mode. Git cannot preserve arbitrary file modes, so use these archives rather than reconstructing evidence from checked-out loose files.

1. Read the bundle SHA256 values in `qualification.json`, verify them, and use a clean checkout of product/tooling `097993a191d89f3a4c07736493a143cd5b1ac348`.
2. Import the chosen native bundle with `scripts/release_protocol.py import --bundle BUNDLE --sha256 HASH --output NATIVE_IMPORT`.
3. After checking the AP bundle digest, extract it into a fresh private directory with `tar --same-permissions -xzf AP_BUNDLE -C AP_DIRECTORY`. The receipt is `AP_DIRECTORY/ap/ap-receipt.json`.
4. Download the matching release archive and verify its handoff digest. Run `scripts/release_protocol.py accept --attempt NATIVE_IMPORT --source-root PRODUCT_CHECKOUT --archive RELEASE_ARCHIVE --ap-receipt AP_DIRECTORY/ap/ap-receipt.json --output ACCEPTED.json` using the selected Python3.12 interpreter.

Use fresh output paths. These are read-only acceptance/transport operations, not instructions to rebuild, retry original jobs, install, or invoke a live AP nudge. Native owner paths in receipts describe provenance; the canonical import resolves transported evidence and separately supplied archives.
