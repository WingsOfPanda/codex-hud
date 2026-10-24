# Publish an accepted release

Use this sequence after both targets pass core acceptance and the generated
handoff is verified, within the recorded publication grant. It uses existing
GitHub CLI/API operations and the read-only `scripts/release_publish.py` checker;
it is not an automatic publisher or authority to install/merge.

## Bind inputs before any network mutation

Record `RELEASE_REPO`, `RELEASE_TAG`, `RELEASE_CHECKOUT` (the separate companion
worktree), the full expected `RELEASE_COMPANION_COMMIT`, `RELEASE_PYTHON`,
`RELEASE_TOOLING` and a private `RELEASE_DIR` for API receipts.
The repository and tag must match the accepted handoff. Product source in the
handoff and companion/tag commit are distinct identities. Keep receipt outputs
outside frozen source/tooling and never update sealed acceptance files in place.

Prepare exactly five existing files: the accepted x86 and ARM archives, their
`.sha256` files, and `handoff.json`. Each checksum file contains the archive digest,
two spaces, the archive **basename**, and a newline. Hash the completed files;
`assets.json` is a JSON array with one row per file:

```json
[{"name":"ASSET_BASENAME","sha256":"64_LOWERCASE_HEX_CHARACTERS","size":123}]
```

Populate all five actual rows, including checksum files and handoff; `size` is an
integer byte count. Extra local metadata such as `path` is permitted by the checker.
Each archive name/digest must equal its handoff target. Verify the companion commit
contains the note and portable qualification evidence before publishing its tag.
Do not replace an existing tag with another commit.

## Reconcile and publish the tag

Use the companion note as the release body: the archive's source note was frozen
before qualification and is not rewritten afterward. Convert its relative links
to URLs bound to `RELEASE_TAG` before supplying `NOTE_FILE` below.

Check the release by tag and the remote tag before creation:

```sh
gh auth status
gh api "repos/$RELEASE_REPO/releases/tags/$RELEASE_TAG"
git -C "$RELEASE_CHECKOUT" ls-remote origin \
  "refs/tags/$RELEASE_TAG" "refs/tags/$RELEASE_TAG^{}"
```

An authenticated HTTP 404 establishes release absence; authentication, network
or other errors leave it unknown. An existing release resumes through its identity
and verification steps below. Resolve an existing tag to its commit as described
under [draft verification](#verify-the-draft-before-publication); a different
commit is a conflict, not permission to move the tag.

For an absent remote tag, verify that `origin` is `RELEASE_REPO` and the companion
commit contains the reviewed note/evidence. Inspect a pre-existing local tag too;
create it only when absent, otherwise require its resolved commit to match:

```sh
git -C "$RELEASE_CHECKOUT" tag "$RELEASE_TAG" "$RELEASE_COMPANION_COMMIT"
git -C "$RELEASE_CHECKOUT" push origin "refs/tags/$RELEASE_TAG"
```

After a push error, read the remote tag before deciding what remains. Continue
only when its resolved commit equals `RELEASE_COMPANION_COMMIT`.

## Create once, then identify the exact draft

Check the existing release by tag before creation. If it exists or an earlier
creation result is uncertain, inspect that release; do not create a second one.
When absent, use the five reviewed paths (shown as `ASSET_PATHS` below):

```sh
gh release create "$RELEASE_TAG" ASSET_PATHS --repo "$RELEASE_REPO" \
  --draft --verify-tag --title "Codex Plus $RELEASE_TAG" --notes-file NOTE_FILE
gh release view "$RELEASE_TAG" --repo "$RELEASE_REPO" \
  --json id,tagName,isDraft,url > "$RELEASE_DIR/view.json"
```

`ASSET_PATHS` and `NOTE_FILE` are placeholders for explicit arguments, not literal
defaults. Wait for upload completion before checking digests.

Take `RELEASE_NODE` from `view.json.id`, then collect its database ID:

```sh
gh api graphql -f id="$RELEASE_NODE" \
  -f query='query($id:ID!){node(id:$id){... on Release{id databaseId tagName isDraft}}}' \
  > "$RELEASE_DIR/node.json"
"$RELEASE_PYTHON" -B "$RELEASE_TOOLING/scripts/release_publish.py" identity \
  --view "$RELEASE_DIR/view.json" --graphql "$RELEASE_DIR/node.json" \
  --tag "$RELEASE_TAG" > "$RELEASE_DIR/identity.json"
```

The checker requires matching `id`, `tagName`, `isDraft` in the view and GraphQL
`data.node`, and a positive integer `databaseId`. Set `RELEASE_ID` from the validated
`identity.json.release_id`; never use a list position or guess from listing delay.

## Verify the draft before publication

```sh
gh api "repos/$RELEASE_REPO/releases/$RELEASE_ID" > "$RELEASE_DIR/draft-release.json"
gh api "repos/$RELEASE_REPO/git/ref/tags/$RELEASE_TAG" > "$RELEASE_DIR/tag-ref.json"
```

Resolve the remote tag's `object` to a commit. For an annotated tag, fetch
`repos/REPOSITORY/git/tags/OBJECT_SHA` and follow its `object`; require a final
`type: commit`. Supply that SHA as `RELEASE_TAG_COMMIT`, independently of the
locally expected `RELEASE_COMPANION_COMMIT`.

The REST release record must contain `id`, `tag_name`, boolean `draft`, and all
five `assets` with `name`, `state`, integer `size`, and `digest: sha256:HEX`.
Select the unique `handoff.json` asset ID from that record, then download its bytes:

```sh
gh api "repos/$RELEASE_REPO/releases/assets/$RELEASE_HANDOFF_ASSET_ID" \
  -H 'Accept: application/octet-stream' > "$RELEASE_DIR/downloaded-draft-handoff.json"
"$RELEASE_PYTHON" -B "$RELEASE_TOOLING/scripts/release_publish.py" verify \
  --handoff HANDOFF_FILE --assets "$RELEASE_DIR/assets.json" \
  --release "$RELEASE_DIR/draft-release.json" \
  --downloaded-handoff "$RELEASE_DIR/downloaded-draft-handoff.json" \
  --release-id "$RELEASE_ID" --tag-commit "$RELEASE_TAG_COMMIT" \
  --expected-commit "$RELEASE_COMPANION_COMMIT" --draft \
  > "$RELEASE_DIR/draft-verification.json"
```

`HANDOFF_FILE` is the sealed local handoff path. Require exit zero and
`verified: true`: the checker compares the exact five asset hashes/sizes/names,
remote tag commit and byte-identical downloaded handoff. A missing digest,
incomplete upload or mismatched record is a failed check, not a publishing waiver.

## Publish that ID and verify public state

Only after draft verification, publish the same validated database ID:

```sh
gh api "repos/$RELEASE_REPO/releases/$RELEASE_ID" -X PATCH -F draft=false
gh api "repos/$RELEASE_REPO/releases/$RELEASE_ID" > "$RELEASE_DIR/public-release.json"
gh api "repos/$RELEASE_REPO/releases/assets/$RELEASE_HANDOFF_ASSET_ID" \
  -H 'Accept: application/octet-stream' > "$RELEASE_DIR/downloaded-public-handoff.json"
```

Refresh the remote tag resolution, then verify public state with a separate receipt:

```sh
gh api "repos/$RELEASE_REPO/git/ref/tags/$RELEASE_TAG" > "$RELEASE_DIR/public-tag-ref.json"
```

Resolve this new response to `RELEASE_TAG_COMMIT` using the same commit/annotated-tag
rule above, then run:

```sh
"$RELEASE_PYTHON" -B "$RELEASE_TOOLING/scripts/release_publish.py" verify \
  --handoff HANDOFF_FILE --assets "$RELEASE_DIR/assets.json" \
  --release "$RELEASE_DIR/public-release.json" \
  --downloaded-handoff "$RELEASE_DIR/downloaded-public-handoff.json" \
  --release-id "$RELEASE_ID" --tag-commit "$RELEASE_TAG_COMMIT" \
  --expected-commit "$RELEASE_COMPANION_COMMIT" \
  > "$RELEASE_DIR/public-verification.json"
```

This check omits `--draft` and requires
`draft: false` and a nonempty `published_at` in addition to the identity/digest
checks. If a network mutation's outcome is
uncertain, read this same release ID
and reconcile preserved receipts before another mutation; never restart creation.

**Done:** public verification passes, and fleet-config receives the tag, URL and
verification reference. Preserve the release ID and API receipts. Main merge,
fleet rollout and running-session restarts remain separate outcomes and grants.
