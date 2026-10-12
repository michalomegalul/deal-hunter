"""A small robots.txt matcher with the `*` and `$` wildcards (urllib.robotparser has neither).

Used to check the URLs the shop modules build against copies of the real robots.txt files
(tests/fixtures/mironet_robots.txt, datart_robots.txt): the longest matching rule wins, Allow on a tie.
"""

import re
from urllib.parse import urlsplit


def parse(text: str) -> list[tuple[bool, re.Pattern]]:
    """[(allow?, compiled pattern)] of the `User-agent: *` group."""
    rules, agents, in_rules, group = [], [], False, []
    for raw in text.splitlines():
        line = raw.split("#")[0].strip()
        if ":" not in line:
            continue
        field, _, value = (part.strip() for part in line.partition(":"))
        field = field.lower()
        if field == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value)
        elif field in ("allow", "disallow") and value:
            in_rules = True
            if "*" in agents:
                group.append((field == "allow", value))
    for allow, value in group:
        pattern = re.escape(value).replace(r"\*", ".*")
        rules.append((allow, re.compile(pattern[:-2] + "$" if pattern.endswith(r"\$") else pattern)))
    return rules


def blocked(rules: list[tuple[bool, re.Pattern]], url: str) -> bool:
    """Does robots.txt forbid fetching `url`?"""
    parts = urlsplit(url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    best_len, best_allow = -1, True
    for allow, pattern in rules:
        m = pattern.match(target)
        if m and (len(pattern.pattern) > best_len or (len(pattern.pattern) == best_len and allow)):
            best_len, best_allow = len(pattern.pattern), allow
    return not best_allow
