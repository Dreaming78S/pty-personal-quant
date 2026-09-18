from __future__ import annotations

STAR_PREFIX = "688"
GEM_PREFIXES = ("300", "301")
BSE_PREFIXES = ("43", "83", "87", "88", "92")


def board_of(ts_code: str) -> str:
    code = ts_code.split(".")[0]
    if code.startswith(STAR_PREFIX):
        return "star"
    if code.startswith(GEM_PREFIXES):
        return "gem"
    if code.startswith(BSE_PREFIXES):
        return "bse"
    return "main"
