# Apple Journal → Hindsight

`apple_journal_tract.py` imports Apple Journal HTML exports into the Hindsight
`default` bank. It is standard-library Python: no virtualenv or third-party
packages are required.

The importer stores written journal text, structured attachment metadata, and
Apple's included audio transcript. It never uploads binary media, OCRs images,
or transcribes raw audio/video. The local ledger contains hashes, operation
IDs, and statuses only; it does not duplicate journal text or media.

## Setup

```bash
cd /Users/sala/Downloads/Personal/Projects/apple_journal_tract
cp .env.example .env
# Set HINDSIGHT_MCP_URL in .env, or export it in the shell.
```

`HINDSIGHT_MCP_URL` may be the MCP endpoint (`.../mcp` or `.../mcp/<alias>`) or
the REST base. The private host is intentionally not embedded in source.

## Safe incremental workflow

Always pass both the export and bank explicitly for bulk operations.

```bash
python3 apple_journal_tract.py plan \
  --export /Users/sala/Downloads/AppleJournalEntries \
  --bank default

python3 apple_journal_tract.py review --plan output/sync-plan.json
python3 apple_journal_tract.py apply --plan output/sync-plan.json
python3 apple_journal_tract.py status --bank default

python3 apple_journal_tract.py retry-failed --bank default
python3 apple_journal_tract.py retry-failed --bank default --apply
```

`plan` reads the live bank document list and writes a content-free plan. An
unchanged source fingerprint is skipped only after the live document is found;
pending or failed operations are not treated as stored. Changed entries use
`update_mode=replace`. A stale plan is rejected if the export manifest, bank,
endpoint, or parser version changed. Missing source files are reported and
preserved in Hindsight rather than deleted.

For a renamed file, the importer can preserve identity only when the prior
body/attachment fingerprint match is unique. Ambiguous matches are reported as
`identity_conflict` and are not imported. Semantic recall results at or above
the review threshold (`0.85`, configured as `REVIEW_THRESHOLD`) are advisory
review flags and never suppress an exact-new entry.

Normal output shows filenames, actions, fingerprints, scores, and operation
states. Journal text is shown only through the explicit `review` command.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite covers parser regressions, Apple epoch conversion, Unicode and
malformed HTML, resource/sidecar matching, incremental classification, stale
plans, pending/failed operation handling, retry gating, missing entries,
ambiguous renames, and advisory semantic matches. The `fixtures/` directory is
synthetic and contains no personal journal text.

## Live rollout gate

The implementation does not automatically import the supplied export. Before
`apply`, verify Hindsight health and that the explicit destination bank exists,
inspect the content-free plan, and apply only that unchanged plan. The server
acknowledgement means an entry is **queued**, not stored; use `status` to wait
for server-reported `completed` operations and then run a second `plan`.
