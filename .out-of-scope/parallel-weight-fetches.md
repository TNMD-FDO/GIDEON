# Parallel weight fetches in `models pull`

`gideon models pull` fetches one file at a time, one `wget` per file, and stays that way. A bounded pool of concurrent fetches, a per-connection throughput target, and any fetch-concurrency setting are out of scope.

## Why this is out of scope

Parallel connections help only where a single connection is capped below the link. On the box that is not so: measured on a Friday night inside the quiet window (slice-1 ticket 22, `.scratch/slice-1/assets/22-on-box.txt`), one plain `wget` to Hugging Face's CDN ran at 86–92 MB/s on the box's 1 Gb/s NIC, two together at 101 MB/s aggregate, four together at 92 MB/s aggregate with every one of the four slower than one alone. The ceiling is the link. The 36–49 MB/s that `v0.1.5`'s daytime proofs recorded (`assets/03-on-box.txt`, proofs a, b, and i, all between 13:12 and 13:49 Chicago on a working day) was the office's business-hours share of the same link, not a per-connection cap; a business-hours figure from a shared link is never a measurement of what a connection can do.

Against every window the product has, one connection is enough. The pinned set is the generator's 30.9 GB today and about 55 GB once slice 3's embedder and two rerankers join it: about 10 minutes at the quiet-window rate, about 25 at the worst daytime rate measured, against a 60-minute install budget in the acceptance harness and weekend-night maintenance windows (spec §21). The download is not unavailability either: apply runs the pull before `recreate`, and upgrade runs apply as its child, so the previous engine serves while the next set lands and only the recreate is the outage.

A receiving office's link changes nothing. A slower link is the cap in every case and parallel flows cannot add bandwidth a link does not have; run in business hours, they would only take a larger share of a shared link from the office's own users. The one shape parallelism serves — a link well above 1 Gb/s with the CDN capping each connection — is a fast install already. And the rule that ships figures (ADR-0017) asks for a measurement before a mechanism; the measurement says the mechanism buys about a tenth where installs run and nothing where they are slow.

Sequential fetching also keeps the pull's guarantees simple: one partial on disk at a time, the free-space walk crediting each mismatched blob's bytes at the point of its own fetch, per-file lines in fetch order, the pull record's five writes untouched by anything in flight.

## When to reconsider

Re-open with a new measurement, not with the old argument: a single-connection rate measured well below the link (under about half of it) in a maintenance window on a receiving office's box, or a pinned set that no longer fits its window at the rate that box measures. Take the measurement in the window the ruling is about, the way ticket 22's asset did — a plain `wget` per connection, streamed to `wc -c`, one, two, and four at once.

## Prior requests

- slice-1 ticket 22 (`.scratch/slice-1/issues/22-models-pull-parallel-fetches.md`): "`models pull` fetches in parallel when one connection is too slow" — closed `wontfix` 2026-09-05 on the measurement above
