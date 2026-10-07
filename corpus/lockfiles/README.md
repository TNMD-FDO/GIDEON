# Corpus lockfiles

Each lockfile pins every corpus source this release can acquire. The first cuts
pin case law alone because the other sources do not yet have acquisition paths.
Adding a source requires a new cut with a full set of pins.

`corpus-YYYY-MM-DD.yaml` is the readable lockfile; the date is the cut's UTC
date and each label is used once. Its companion directory has one
`<source>.sha256` sidecar and the index documents read for that source. Each
sidecar line is `path  sha256  size`, with two spaces between fields, in path
order. The path is relative to the dated source snapshot directory. The digest
and byte size cover the fetched file; the YAML pins the sidecar's digest.

An index document is committed as the exact response the corpus cut command
read. A listing query can change after the cut and is not a stable object under
the source's base URL, so it cannot be a sidecar line. The YAML pins each index
document's URL, digest, and size.

The corpus cut command writes the lockfile and every companion byte. After a
cut, the snapshot and its pins stay fixed. `mirror_url` is the only field a
person may edit when the same pinned files are served from an allowed mirror;
the file digests and sizes still have to match. A later change in source data
gets a new label.

A snapshot directory on the box is never fetched again once whole, so an
upstream file replaced under an unchanged date is invisible to a later cut on
the box that holds it. It surfaces where the lockfile is installed from
upstream, as a digest mismatch naming the file, and the answer is a repin:
a `mirror_url` that serves the pinned bytes, or a new cut.

The `pipeline` value is `0.0.0` until a parser and chunker have a released
version. A change to that version gets a new cut. `courts[]` lists the court
map IDs in `courts.yaml` that belong to the cut; the IDs must resolve there.
