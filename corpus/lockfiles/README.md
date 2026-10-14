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
Preflight reads this list to check the site's home courts.

A derived lockfile copies an earlier committed lockfile's pins, sidecars, and
index documents byte for byte, extends `courts[]`, names that lockfile in
`base:`, and carries `reason: tranche`. The corpus cut command's
`--base <label> --add-courts <ids>` form writes it. Its base must be a
lockfile in this same directory with an earlier label date.
