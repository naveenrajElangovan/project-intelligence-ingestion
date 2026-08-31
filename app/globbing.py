"""Path-glob matching with `**` that actually spans directories.

`fnmatch` has no concept of a path separator: its `*` matches `/` as happily as
any other character, and `**` is just two of those. The practical consequences
for an exclusion list are both wrong and silent:

* `**/project-documentation/**` does not exclude a top-level
  `project-documentation/`, because `**/` demands a literal slash before it.
* `**/*.docx` does not exclude `notes.docx` at the repository root, for the same
  reason.
* `*.kt` matches `src/main/Cart.kt`, because `*` crossed two directories to get
  there -- so a pattern meant for one directory quietly matches the whole tree.

An exclusion that does not exclude is worse than no exclusion, because the list
reads as if the case were handled. This translates a glob to a regex where `**`
spans segments, `*` and `?` do not, and a trailing `/**` also matches the
directory itself.
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    text = pattern.strip()
    # A leading "**/" must be optional, or the pattern only ever matches nested
    # paths and misses the top level -- the defect this module exists to fix.
    prefix_optional = text.startswith("**/")
    if prefix_optional:
        text = text[3:]
    # A trailing "/**" should match the directory itself as well as its contents.
    suffix_any = text.endswith("/**")
    if suffix_any:
        text = text[:-3]

    parts: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if text.startswith("**/", index):
            parts.append("(?:[^/]+/)*")
            index += 3
        elif text.startswith("**", index):
            parts.append(".*")
            index += 2
        elif character == "*":
            parts.append("[^/]*")
            index += 1
        elif character == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(character))
            index += 1

    body = "".join(parts)
    if prefix_optional:
        body = "(?:[^/]+/)*" + body
    if suffix_any:
        body = body + "(?:/.*)?"
    return re.compile(f"^{body}$")


def matches(path: str, pattern: str) -> bool:
    """Does this POSIX-style relative path match this glob?"""

    return bool(_compiled(pattern).fullmatch(path.strip("/")))


def matches_any(path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    return any(matches(path, pattern) for pattern in patterns)
