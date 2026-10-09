# Third-party licenses

GIDEON's own code is public domain (see `LICENSE`). Everything GIDEON ships
beside it keeps its own license: Open WebUI, the serving engines, the models,
and every upstream image mirrored by digest into the release registry.

This file enumerates those components. The images and models pinned in
`images.lock` and `models.lock` are not yet enumerated; each release that pins
one adds its entry here. The one entry today is a labelled dataset that a
development tool reads.

| Component | Pin | License | Notes |
|---|---|---|---|
| CaseHOLD Overruling sentences (Zheng et al. 2021), LegalBench's `overruling` task copy (Guha et al. 2023) | `huggingface.co`, repository `datasets/nguha/legalbench` at the revision `tools/treatment/` pins, with each file's size and sha256 | CC-BY-4.0, as LegalBench states for the task; the original authors publish no license line for the dataset | Read by `python3 -m tools.treatment` on a development seat to measure the treatment pattern set's precision; never shipped, served, or copied into the repository |
