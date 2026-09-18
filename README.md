# Comprehensive Rules History

A trackable, blameable git archive of the **Magic: The Gathering Comprehensive Rules** (CR).

Every released version of the CR is committed, in chronological order, as an overwrite of a single
file — [`comprehensive-rules.txt`](comprehensive-rules.txt). Each commit is **backdated** to the day
that version was published. Because every version rewrites the same file, git's own tooling becomes
a rules-history browser:

- **`git blame comprehensive-rules.txt`** — see which set release last changed any given rule.
- **`git log -p comprehensive-rules.txt`** — read the exact diff introduced by every update.
- **`git log --format='%ad %s' --date=short`** — a timeline of every release.

Commits are named like `[September 2001] Odyssey ODY` — `[Month Year] <Set name> <Set code>`.

## Data source

Documents come from the public [Academy Ruins](https://academyruins.com/archives) API
(`https://api.academyruins.com`), which archives the raw rules documents originally published by
Wizards of the Coast. Wizards of the Coast is the ultimate authority for the rules; this repository
is an unofficial historical mirror for study and reference. Each document is stored **verbatim** as
served by the API — no reformatting — so the archive faithfully mirrors the source.

## Updating

New CR releases can be appended by re-running the updater:

```bash
python scripts/update_archive.py
```

It reads [`versions.json`](versions.json) (the ordered manifest of versions already committed),
fetches only versions not yet present, and commits each one backdated to its publication day. The
script is idempotent — running it with nothing new to add makes no commits. Requires `curl` and
`git` on `PATH`.

Raw upstream documents are mirrored to a sibling `../RawCRDocs/` directory (one untouched
`.txt`/`.pdf` per version, plus a `metadata.json` snapshot). The updater reads from there when a
document is already cached and only downloads what's missing, so rebuilds don't re-hit the Academy
Ruins API. Delete a cached file to force a re-download.

## Normalization

Documents are not committed verbatim; each is normalized so blame/diffs track real rule changes:

- **LF line endings** (several upstream docs use CR or CRLF).
- **Each rule number on its own line**, ahead of its text — so renumbering a rule doesn't rewrite
  every rule after it.
- **Blank lines emptied** (some docs pad them with a non-breaking space).
- **Canonical ASCII punctuation/symbols** — the archive mixes Unicode (`™ — “ ”`), ASCII spellings
  (`(TM) -- " "`) and lossy strips (`™`→`T`) across eras; all are folded to one ASCII form
  (`™`→`(TM)`, `®`→`(R)`, `—`→`--`, curly quotes→straight, accents→base) so convention changes don't
  masquerade as rule changes. The ~9 lossy-stripped early documents are repaired against their
  neighbor.
- **Underscores removed** — the 2008–2014 documents mark italics with underscores (`_Magic_`); no
  other era does, so all underscores are dropped for consistency.
- **Banner lines removed** — divider rows made only of `-` or `=` (6ED's old layout) are dropped.
- **Word-wrapped documents rejoined** — a few early documents (6ED, APC, ODY, TOR) are hard-wrapped
  at ~78–101 columns, so a rule spans several lines. They're rejoined to one rule per line (and one
  glossary definition per line) to match every other document, so diffs reflect wording changes
  rather than re-wrapping. Preamble and credits are left as-is.

Early edition-to-edition diffs (e.g. 1999→2001) are still large — those are genuine rewrites where
the rules roughly doubled in size, not formatting artifacts.

## Notes

- `versions.json` is a machine- and human-readable manifest of every committed version
  (`setCode`, `setName`, `creationDay`), oldest first.
- The history begins with Classic Sixth Edition (`6ED`, April 1999). Fifth Edition (`5ED`, 1997)
  predates the modern rules format and is too structurally different to be useful here, so it is
  excluded (see `EXCLUDE` in the updater).
