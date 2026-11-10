"""One-level material groups from shared name boundaries, without renaming nodes."""
from collections import Counter, defaultdict
from dataclasses import dataclass
import re

_BOUNDARIES = re.compile(r'[_:.\-]+|(?<=[^\W\d_])(?=\d)')


def sort_key(name):
    return tuple((1, int(part)) if part.isdecimal() else (0, part.casefold())
                 for part in re.split(r'(\d+)', name))


def prefixes(name):
    return tuple(dict.fromkeys(name[:match.end()] for match in _BOUNDARIES.finditer(name)
                               if match.end() < len(name) and
                               any(char.isalpha() for char in name[:match.end()])))


@dataclass(frozen=True)
class Group:
    prefix: str
    title: str
    records: tuple

    def label(self, record):
        return record.name[len(self.prefix):] if self.prefix else record.name


def group(records):
    records = tuple(records)
    candidates = [prefixes(record.name) for record in records]
    shared = Counter(prefix for values in candidates for prefix in values)
    buckets, singles = defaultdict(list), []
    for record, values in zip(records, candidates):
        common = [prefix for prefix in values if shared[prefix] > 1]
        # Keep complete name tokens (SET1/SET2) when a shared separator exists.
        # Letter-to-digit boundaries still group names such as lambert1/lambert2.
        separated = [prefix for prefix in common if prefix[-1] in '_:.-']
        common = separated or common
        if common:
            buckets[max(common, key=len)].append(record)
        else:
            singles.append(record)
    result = []
    titles = Counter(prefix.rstrip('_:.-') for prefix, members in buckets.items() if len(members) > 1)
    for prefix, members in buckets.items():
        if len(members) < 2:
            singles.extend(members)
            continue
        title = prefix.rstrip('_:.-')
        if titles[title] > 1:
            title = prefix
        result.append(Group(prefix, title, tuple(sorted(members, key=lambda record: sort_key(record.name)))))
    result.extend(Group('', record.name, (record,)) for record in singles)
    return tuple(sorted(result, key=lambda item: (sort_key(item.title), item.prefix)))
