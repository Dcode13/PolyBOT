"""Bounded discovery selection that keeps a market's two outcomes together."""

from __future__ import annotations


def _groups(markets: list[dict]) -> list[list[dict]]:
    unique, seen = [], set()
    for market in markets:
        token = market.get("token_id")
        if token in seen:
            continue
        seen.add(token)
        unique.append(market)

    members: dict[tuple, list[dict]] = {}
    for market in unique:
        key = (market.get("event_id"), market.get("market_id"))
        members.setdefault(key, []).append(market)
    pairs = {}
    for key, rows in members.items():
        outcomes = {str(row.get("outcome") or "").strip().casefold() for row in rows}
        if all(key) and len(rows) == 2 and len(outcomes) == 2 and "" not in outcomes:
            pairs[key] = rows

    groups, emitted = [], set()
    for market in unique:
        key = (market.get("event_id"), market.get("market_id"))
        if key not in pairs:
            groups.append([market])
        elif key not in emitted:
            groups.append(pairs[key])
            emitted.add(key)
    return groups


def select_markets(eligible_sorted: list[dict], cursor: int, limit: int,
                   spread: bool) -> list[dict]:
    """Select at most ``limit`` tokens, without interpreting outcome direction.

    The stable cursor order groups valid binary markets in first-seen order.
    Resuming midway through a pair starts with its unprocessed outcome; its
    preceding partner remains at wraparound. Only the cursor seam or the final
    token allowance may split a valid pair. Malformed groups stay singletons.
    Spread sampling preserves a near-term slice and reaches the far horizon.
    """
    groups = _groups(eligible_sorted)
    total = sum(map(len, groups))
    if not total or limit <= 0:
        return []
    offset = cursor % total
    for index, group in enumerate(groups):
        if offset < len(group):
            rotated = [group[offset:]] + groups[index + 1:] + groups[:index]
            if offset:
                rotated.append(group[:offset])
            break
        offset -= len(group)
    if not spread or total <= limit:
        return [market for group in rotated for market in group][:limit]

    selected: list[dict] = []
    near_target = max(1, limit // 3)
    next_group = 0
    while next_group < len(rotated) and len(selected) < near_target:
        selected.extend(rotated[next_group][:limit - len(selected)])
        next_group += 1
    remaining = rotated[next_group:]
    budget = limit - len(selected)
    if not budget or not remaining:
        return selected

    # Sample groups, not individual outcome tokens. With all singleton groups
    # this is exactly the previous evenly spaced near/far token selection.
    largest = max(map(len, remaining))
    count = min(len(remaining), (budget + largest - 1) // largest)
    indices = ([len(remaining) - 1] if count == 1 else
               [i * (len(remaining) - 1) // (count - 1) for i in range(count)])
    for index in indices:
        selected.extend(remaining[index][:limit - len(selected)])
    # Mixed singleton/pair input may leave unused capacity. Fill it without
    # duplicating sampled tokens or displacing the already selected far group.
    sampled = set(indices)
    for index, group in enumerate(remaining):
        if len(selected) >= limit:
            break
        if index not in sampled:
            selected.extend(group[:limit - len(selected)])
    return selected


def next_scan_cursor(eligible_sorted: list[dict], cursor: int,
                     analyzed_rows: list[dict]) -> int:
    """Resume an unfinished pair; otherwise retain the normal cursor advance.

    A second outcome whose first partner is absent is already a resumed suffix.
    Sending that cursor backward would trap one-token scans in the same pair.
    """
    next_cursor = cursor + len(analyzed_rows)
    if not analyzed_rows:
        return next_cursor
    examined = {row.get("token_id") for row in analyzed_rows}
    last_token = analyzed_rows[-1].get("token_id")
    offset = 0
    for group in _groups(eligible_sorted):
        if (len(group) == 2 and group[0].get("token_id") == last_token
                and group[1].get("token_id") not in examined):
            return offset + 1
        offset += len(group)
    return next_cursor
