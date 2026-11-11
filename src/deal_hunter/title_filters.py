"""Title-filter regex shared by every normalizer.

`WANTED` ("koupim", "shanim", ...) flags a wanted-ad rather than an offer, and is declared
byte-identical in apple.py, storage.py, laptop_spec.py, office.py and normalize.py, so it lives
here once. Each normalizer's `ACCESSORY` regex is domain-specific (different parts for GPUs,
disks, laptops, office PCs and Apple gear) and stays local to its own module.
"""

import re

WANTED = re.compile(
    r"\b(koupim|kupim|shanim|hledam|vykup|vykupuji|vykupujeme|vykoupim|poptavam|sezenu|koupime)\b"
)
