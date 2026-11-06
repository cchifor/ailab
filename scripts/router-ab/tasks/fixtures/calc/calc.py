"""Small helpers used by the report generator."""
import re


def median(xs):
    """Median of a non-empty list of numbers (the mean of the two middle values for an even count)."""
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2]


def parse_duration(s):
    """Parse a duration such as '1h30m15s', '45m' or '2h' into seconds. Units h, m, s are each optional."""
    units = {"h": 360, "m": 60, "s": 1}
    total, num = 0, ""
    for ch in s.strip():
        if ch.isdigit():
            num += ch
        else:
            total += int(num) * units[ch]
            num = ""
    return total


def slugify(title):
    """'  Hello, World!  ' -> 'hello-world': lower case, runs of other characters become one dash, no edge dashes."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower())


def chunk(xs, size):
    """Split a list into consecutive chunks of at most `size` items."""
    return [xs[i:i + size] for i in range(0, len(xs), size)]
