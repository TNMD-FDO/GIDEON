# Corpus lockfiles

Each lockfile pins the corpus sources that have joined by its label date.
Earlier lockfiles omit a source whose joined date is later; adding a source
requires a new cut with a full set of pins.

A fetched source's files come from upstream. A captured source is a page the
release maintainer saves from a browser because its upstream refuses every
scripted client. Its `base_url` is the page's address as provenance, never
fetched. Its snapshot date is the capture's date, its sidecar has one line,
and its one index document is the saved page, committed as
`<label>/<source>.<name>`. An office's install reads that committed copy and
fetches nothing for it.

`corpus-YYYY-MM-DD.yaml` is the readable lockfile; the date is the cut's UTC
date and each label is used once. Its companion directory has one
`<source>.sha256` sidecar and the index documents read for that source. Each
sidecar line is `path  sha256  size`, with two spaces between fields, in path
order. The path is relative to the dated source snapshot directory. The digest
and byte size cover the snapshot file; the YAML pins the sidecar's digest.

The optional `line` field follows `reason` and precedes `base` when present.
Its name is lowercase words and digits joined by single hyphens. Omission
means the unnamed default line; the literal `default` is refused. A hardware
profile's optional `corpus_line` in `models.lock` selects that line, the default
when absent. Preflight, index builds without an explicit label, and the CI
sibling's default eval sample read the newest committed lockfile on the
selected profile's line. Install and explicit index builds refuse a label on
another line. Labels remain unique across all lines: a second cut on the same
UTC date is refused, even when its line differs. Run it on a later date.

For a fetched source, an index document is the exact response the corpus cut
command read. A listing query can change after the cut, so it is committed
rather than listed in the sidecar. A captured page is both the sidecar's one
file and its index document; their name, digest, and size must agree. The YAML
pins each index document's URL, digest, and size.

The corpus cut command writes the lockfile and every companion byte. After a
cut, the snapshot and its pins stay fixed. `mirror_url` is the only field a
person may edit when the same pinned files are served from an allowed mirror;
the file digests and sizes still have to match. A captured source has no
`mirror_url`. A later change in source data gets a new label.

A snapshot directory on the box is never fetched again once whole, so an
upstream file of a fetched source replaced under an unchanged date is invisible to a later cut on
the box that holds it. It surfaces where the lockfile is installed from
upstream, as a digest mismatch naming the file, and the answer is a repin:
a `mirror_url` that serves the pinned bytes, or a new cut.

The `pipeline` value is the release's registered pipeline version. A change
to that version gets a new cut. `courts[]` lists the court
map IDs in `courts.yaml` that belong to the cut; the IDs must resolve there.
Preflight reads this list from the selected profile's line to check the site's home courts.

A derived lockfile copies an earlier committed lockfile's pins, sidecars, and
index documents byte for byte, extends `courts[]`, names that lockfile in
`base:`, inherits its line, and carries `reason: tranche`. The corpus cut command's
`--base <label> --add-courts <ids>` form writes it. Its base must be a
lockfile on the same line in this directory with an earlier label date.
Neither `--line` nor `--courts` is used with the derived form.

The corpus cut command's `--line <name> --courts <ids>` form starts a named
line with the typed case-law courts. Its first cut needs `--courts`, carries
same-snapshot companions and pins from the default line's newest lockfile
when present, and has reason `tranche` even when those snapshots are unchanged.
Later
`--line <name>` cuts carry that line's courts unless `--courts` supplies a new
set. The bare command extends the default line; over an empty directory it
uses the source definitions' first courts. A cut adds files without editing
`models.lock`. Commit the lockfile and companions here: a release ships a
named line once a profile's `corpus_line` names it. Every committed line must
have a profile, every profile's line a committed lockfile, and each line's
newest must use the release's pipeline version.
