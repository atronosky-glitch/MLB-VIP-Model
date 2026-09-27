"""Player-name normalization for matching a recommendation to a result box score.

Used ONLY for player lookup. Team names keep each results module's own
``normalize_name`` so team matching behaves exactly as before.

Two safe folds, nothing else:
  * diacritics ("Yandy Díaz" == "Yandy Diaz", "José Ramírez" == "Jose Ramirez")
  * one trailing generational suffix (Jr, Sr, II, III, IV)

Deliberately NOT done: stripping other text (a provider's " Any" or "-DUP"
markers), nickname expansion, or fuzzy matching. Callers keep their "exactly one
match" rule, so two people who collapse to the same key stay unresolved.
"""

from __future__ import annotations

import re
import unicodedata

_SUFFIX = re.compile(r"\s+(?:jr|sr|ii|iii|iv)$")


def normalize_player_name(value: str | None) -> str:
    folded = "".join(
        ch for ch in unicodedata.normalize("NFKD", value or "") if not unicodedata.combining(ch)
    ).casefold()
    key = re.sub(r"[^a-z0-9]+", " ", folded).strip()
    return _SUFFIX.sub("", key)
