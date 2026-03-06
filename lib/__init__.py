# nicktools lib/ -- shared foundation modules with strict dependency layers.
# Layer 0: paths, db, io (no internal deps)
# Layer 1: urls, entries, browsing (depends on Layer 0 only)
# Layer 2: sources, archives (depends on Layer 0 + 1)
