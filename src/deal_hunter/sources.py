"""The listing sources this app knows about, and the one thing they share: a global id space.

Every join, foreign key and cache key in the app assumes `listings.id` uniquely identifies one ad,
across all sources. Bazoš ids are the site's own ad id (unchanged, for the rows collected before
this module existed). Every other source's own id is only unique *within* that source, so it is
hashed together with the source name into the same bigint id space; the real, human id moves to
`external_id`, and (source, external_id) is the uniqueness that is actually enforced (a unique
index, see migrations/007_sources.sql) -- a hash collision in `id` would be a bug, not a correctness
requirement.
"""

import hashlib

KNOWN_SOURCES = ("bazos", "sbazar", "aukro", "fb")

# Sections (Section.kind in bazos.py) every source maps its own categories onto, so normalize.py,
# pricing.py and alerts.py don't need to know a listing's site of origin at all.
CANONICAL_SECTIONS = ("graficka", "pc", "notebook", "hdd")

_MASK = 0x7FFFFFFFFFFFFFFF  # fits a signed bigint


def make_id(source: str, native_id: str | int) -> int:
    """A stable bigint id for one (source, native id) pair.

    Bazoš keeps its literal ad id (int already, and every row saved before this existed has it).
    Everything else is `sha256(source:native_id)` truncated to 63 bits: different sources, or the
    same numeric id reused by two sources, can never collide with each other or with a Bazoš ad id.
    """
    if source == "bazos":
        return int(native_id)
    digest = hashlib.sha256(f"{source}:{native_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & _MASK
