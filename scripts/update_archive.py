#!/usr/bin/env python3
"""Build / update a blameable git archive of the MTG Comprehensive Rules.

Every released version of the Comprehensive Rules (CR) is committed, in
chronological order, as an overwrite of a single tracked file
(``comprehensive-rules.txt``). Each commit is backdated to the day that version
was published, so ``git blame`` shows which set release last touched any given
rule and ``git log -p`` shows the diff for every update.

Data comes from the public Academy Ruins API (https://api.academyruins.com):

  * ``GET /metadata/cr``                  -> list of every CR version
  * ``GET /file/cr/{setCode}?format=txt`` -> the raw document for one version

To make blame and diffs reflect *real* rule changes rather than formatting
churn, each document is passed through ``normalize()`` before it is committed
(see that function for the specific transforms). The script is idempotent: it
reads ``versions.json`` (an ordered manifest of versions already committed) and
only fetches + commits versions that are new.

Requires ``curl`` and ``git`` on PATH. Standard library only otherwise.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

API = "https://api.academyruins.com"
METADATA_URL = f"{API}/metadata/cr"
RAW_FILE = "comprehensive-rules.txt"
MANIFEST_FILE = "versions.json"

# Set codes to leave out of the history. 5ED (1997) predates the modern rules
# format and is too structurally different from the rest to be useful here.
EXCLUDE = {"5ED"}

REPO_ROOT = Path(__file__).resolve().parent.parent
# Local mirror of the untouched upstream documents, kept as a sibling of the
# repo so rebuilds read from disk instead of re-hitting the Academy Ruins API.
RAW_CACHE = REPO_ROOT.parent.parent / "RawCRDocs"
USER_AGENT = "CompRulesHistory-archiver/1.0 (+https://academyruins.com/archives)"
ATTRIBUTION = "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"


# --------------------------------------------------------------------------- #
# Normalization
#
# The archive uses three inconsistent conventions for the same characters:
#   * Unicode        (™ ® — “ ”)                 -- old + modern documents
#   * ASCII spelled  ((TM) (R) -- " ")           -- the 2008-2014 documents
#   * bare stripped  (™->T, ®->r, —->-)          -- the 2001-2006 documents
# Boundaries between conventions otherwise produce huge spurious diffs. We
# canonicalize every document to one ASCII form so only real edits show up.
# --------------------------------------------------------------------------- #

# Unicode char -> canonical ASCII spelling.
SYMBOL_MAP = {
    "™": "(TM)",  # ™
    "®": "(R)",   # ®
    "©": "(C)",   # ©
    "—": "--",    # — em dash
    "–": "-",     # – en dash
    "‘": "'", "’": "'", "‚": "'", "‛": "'",  # single quotes
    "“": '"', "”": '"', "„": '"', "‟": '"',  # double quotes
    "…": "...",   # … ellipsis
    " ": " ",     # non-breaking space
}

# Reverse of the multi-char spellings, used to recognize a bare-stripped line
# ("Magic: The Gathering(TM)" -> "Magic: The GatheringT") when repairing.
BARE_MAP = [("(TM)", "T"), ("(R)", "r"), ("(C)", "c"), ("--", "-")]

RULE_RE = re.compile(r"^(\d{3}\.\d+[a-z.]) (.*)$")
# Divider/banner lines made of only dashes or only equals (6ED's old layout).
BANNER_RE = re.compile(r"^\s*(-{3,}|={3,})\s*$")
# The first real rule (100.1) -- everything before it (title, intro, contents)
# is preamble and is left as-is; the rules body after it gets unwrapped.
FIRST_RULE_RE = re.compile(r"^\s*100\.1\.\s+\S")

# A few early documents (APC, ODY, TOR) are hard word-wrapped at ~101 columns,
# so a single rule spans several lines. Every other document keeps one rule per
# line, so we rejoin the wrapped ones to match (and to make diffs meaningful).
WRAP_MAX_LINE = 150  # docs whose longest line is <= this are treated as wrapped


def normalize_eol(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def strip_banner_lines(text: str) -> str:
    """Drop all-dash / all-equals divider lines (6ED's old layout). Done before
    unwrapping so these lines don't interfere with rejoining wrapped rules."""
    return "\n".join(l for l in text.split("\n") if not BANNER_RE.match(l))


def is_wrapped(text: str) -> bool:
    lens = [len(l) for l in text.split("\n") if l.strip()]
    return bool(lens) and max(lens) <= WRAP_MAX_LINE


def unwrap(text: str) -> str:
    """Rejoin a hard word-wrapped document so each rule/paragraph is one line.

    Three regions, split by markers:
      * preamble (before the first rule, 100.1) -- left untouched;
      * rules body (100.1 up to the "Glossary" line) -- rules are blank-line
        separated, so each run of non-blank lines is joined into one, keeping a
        break before an ``Example:`` line;
      * glossary (the "Glossary" line up to "Credits") -- each entry is a term
        line followed by its definition, so the first non-blank line after a
        blank (the term) is kept on its own line and the rest (the definition)
        is merged;
      * credits (the "Credits" line to the end) -- left untouched.
    """
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if FIRST_RULE_RE.match(l)), 0)
    gloss = next((i for i, l in enumerate(lines) if i >= start and l.strip() == "Glossary"),
                 len(lines))
    credits = next((i for i, l in enumerate(lines) if i >= gloss and l.strip() == "Credits"),
                   len(lines))

    out = list(lines[:start])

    body_base = len(out)
    for raw in lines[start:gloss]:
        s = raw.strip()
        if s and len(out) > body_base and out[-1] != "" and not s.startswith("Example:"):
            out[-1] += " " + s
        else:
            out.append(s)

    since_blank = 0
    for raw in lines[gloss:credits]:
        s = raw.strip()
        if s == "":
            out.append("")
            since_blank = 0
            continue
        since_blank += 1
        if since_blank <= 2:            # 1 = term (kept), 2 = start of definition
            out.append(s)
        else:                           # 3+ = wrapped continuation of the definition
            out[-1] += " " + s

    out.extend(lines[credits:])         # credits left untouched
    return "\n".join(out)


def canonicalize(text: str) -> str:
    """Fold all special characters to a single ASCII spelling and blank out
    whitespace-only lines."""
    for uni, ascii_ in SYMBOL_MAP.items():
        text = text.replace(uni, ascii_)
    # The 2008-2014 documents wrap italicized terms in underscores (_Magic_,
    # __Magic: The Gathering__); no other era does, so drop all underscores to
    # keep those docs consistent with their neighbors.
    text = text.replace("_", "")
    # Fold accented letters to their base (á -> a) and drop any stray non-ASCII.
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    lines = ["" if ln.strip() == "" else ln for ln in text.split("\n")]
    return "\n".join(lines)


def to_bare(line: str) -> str:
    for spelled, bare in BARE_MAP:
        line = line.replace(spelled, bare)
    return line


def repair_stripped(target: str, reference: str) -> str:
    """Restore canonical symbols in a bare-stripped document using the previous
    (already canonical) document as an oracle.

    We compare the target against ``bare_ref`` -- the reference put through the
    same lossy stripping. Lines that then match are the same line with symbols
    lost, so we emit the canonical reference line; everything else (genuine
    changes) is kept verbatim. Conservative: never fabricates a change.
    """
    tgt = target.split("\n")
    ref = reference.split("\n")
    bare_ref = [to_bare(l) for l in ref]
    out: list[str] = []
    sm = difflib.SequenceMatcher(a=bare_ref, b=tgt, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(ref[i1:i2])   # canonical spelling
        else:
            out.extend(tgt[j1:j2])   # keep the target's own text
    return "\n".join(out)


def split_rule_numbers(text: str) -> str:
    """Put each rule number on its own line, ahead of its text. This decouples a
    rule's text from its number so a renumber no longer rewrites every following
    rule (dramatically shrinks version-to-version diffs and preserves blame)."""
    out: list[str] = []
    for ln in text.split("\n"):
        m = RULE_RE.match(ln)
        if m:
            out.append(m.group(1))
            out.append(m.group(2))
        else:
            out.append(ln)
    return "\n".join(out)


def normalize(document: bytes, prev_reference: str | None) -> tuple[str, str]:
    """Full pipeline for one document. Returns (stored_text, reference_text).

    ``stored_text`` is what gets committed; ``reference_text`` is the canonical,
    pre-split form kept to repair the next bare-stripped document.
    """
    raw = document.decode("utf-8")
    had_unicode = any(ord(c) > 127 for c in raw)

    text = strip_banner_lines(normalize_eol(raw))
    if is_wrapped(text):
        text = unwrap(text)
    text = canonicalize(text)

    # Bare-stripped docs (originally pure ASCII and lacking the "(TM)" spelling
    # used by the multi-char era) need their T/r/- restored from the neighbor.
    if prev_reference is not None and not had_unicode and "(TM)" not in text:
        text = repair_stripped(text, prev_reference)

    reference_text = text
    return split_rule_numbers(text), reference_text


# --------------------------------------------------------------------------- #
# Fetching, manifest, committing
# --------------------------------------------------------------------------- #

def curl_bytes(url: str) -> bytes | None:
    proc = subprocess.run(["curl", "-fsS", "-A", USER_AGENT, url], capture_output=True)
    return proc.stdout if proc.returncode == 0 else None


def get_document(code: str) -> bytes | None:
    """Return the raw txt bytes for one set, reading the local ``RawCRDocs``
    mirror when possible and downloading (then caching) otherwise. When no txt
    exists upstream, the PDF is cached for completeness and None is returned."""
    RAW_CACHE.mkdir(parents=True, exist_ok=True)
    txt = RAW_CACHE / f"{code}.txt"
    if txt.exists():
        return txt.read_bytes()
    data = curl_bytes(f"{API}/file/cr/{code}?format=txt")
    if data is not None and len(data) >= 1000:
        txt.write_bytes(data)
        return data
    pdf = RAW_CACHE / f"{code}.pdf"
    if not pdf.exists():
        blob = curl_bytes(f"{API}/file/cr/{code}?format=pdf")
        if blob:
            pdf.write_bytes(blob)
    return None


def fetch_metadata() -> list[dict]:
    raw = curl_bytes(METADATA_URL)
    if raw is None:
        sys.exit("ERROR: could not fetch CR metadata from the API.")
    RAW_CACHE.mkdir(parents=True, exist_ok=True)
    (RAW_CACHE / "metadata.json").write_bytes(raw)
    data = json.loads(raw.decode("utf-8"))["data"]
    # API returns newest-first; sort ascending by publication day (stable).
    return sorted(data, key=lambda v: v["creationDay"])


def load_manifest() -> list[dict]:
    path = REPO_ROOT / MANIFEST_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def write_manifest(manifest: list[dict]) -> None:
    (REPO_ROOT / MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def effective_date_line(text: str) -> str | None:
    for line in text.splitlines()[:30]:
        stripped = line.strip()
        if "effective as of" in stripped.lower() or "current as of" in stripped.lower():
            return stripped
    return None


def month_year(day: str) -> str:
    return datetime.strptime(day, "%Y-%m-%d").strftime("%B %Y")


def git(*args: str, env: dict | None = None) -> None:
    subprocess.run(["git", "-C", str(REPO_ROOT), *args], check=True,
                   env=env, stdout=subprocess.DEVNULL)


def main() -> None:
    versions = fetch_metadata()
    manifest = load_manifest()
    done = {v["setCode"] for v in manifest}

    # Reference for repairing a bare-stripped document is the previous processed
    # document. On an incremental run, seed it from the current committed file.
    prev_reference: str | None = None
    current = REPO_ROOT / RAW_FILE
    if manifest and current.exists():
        # Re-derive the canonical (pre-split) form is unnecessary here: the last
        # committed doc is already canonical, and joined rule lines still match
        # under difflib well enough for the rare late bare-stripped case. Modern
        # docs are never bare-stripped, so this only matters mid-history.
        prev_reference = current.read_text(encoding="utf-8")

    pending = [v for v in versions if v["setCode"] not in done]
    print(f"{len(versions)} versions known, {len(done)} already committed, {len(pending)} to add.")

    committed = 0
    for v in pending:
        code, name, day = v["setCode"], v["setName"], v["creationDay"]
        if code in EXCLUDE:
            print(f"  SKIP {code} ({name}, {day}): excluded from the archive")
            continue
        document = get_document(code)
        if document is None:
            print(f"  SKIP {code} ({name}, {day}): no txt document available")
            continue

        stored, prev_reference = normalize(document, prev_reference)
        (REPO_ROOT / RAW_FILE).write_text(stored, encoding="utf-8", newline="\n")

        manifest.append({"setCode": code, "setName": name, "creationDay": day})
        write_manifest(manifest)

        subject = f"[{month_year(day)}] {name} {code}"
        body = []
        eff = effective_date_line(stored)
        if eff:
            body.append(eff)
        body.append(f"Published: {day}")
        body.append(f"Source: {API}/file/cr/{code}")
        message = subject + "\n\n" + "\n".join(body) + "\n\n" + ATTRIBUTION

        git("add", RAW_FILE, MANIFEST_FILE)
        git_date = f"{day} 12:00:00 +0000"
        env = {**os.environ, "GIT_AUTHOR_DATE": git_date, "GIT_COMMITTER_DATE": git_date}
        git("commit", "-m", message, env=env)
        print(f"  COMMIT {subject}")
        committed += 1

    print(f"Done. {committed} new version(s) committed.")


if __name__ == "__main__":
    main()
