#!/usr/bin/env python3
"""diversity.py - keep synthetic text from collapsing into a few templates (pure Python, unit-testable).

Two mechanisms
1. Generation (emails / tickets): every request gets a seed and a short "variation" block appended at the END of the prompt
   (so the static prefix stays KV-cache friendly): an opening style, one concrete detail, and the openings that were already
   used too often. An OpeningTracker counts the first words of everything generated so far; a text whose opening was seen
   `max_repeat` times is re-sampled (new seed, higher temperature).
2. Contrastive questions: the LLM writes QUESTION SKELETONS with a placeholder (`{X}` = business term, `{C}` = screen name).
   The script fills the placeholders itself, so both questions of a pair are identical except for the distinguishing term
   and the labels remain deterministic. A SkeletonBank hands out the least-used skeleton first.
"""
import re
import threading
from collections import Counter, deque

OPENING_STYLES = [
    "start with a short greeting, then the problem",
    "start directly with the problem, no greeting",
    "start with a question",
    "start with when the problem began",
    "start by saying who is affected",
    "start with what you were trying to do",
    "start with what you already tried",
    "start with the consequence for the business",
]
DETAILS = [
    "mention that you already refreshed the page", "mention that a colleague sees the same thing",
    "mention a customer or supplier waiting (no real names)", "mention it is close to month-end closing",
    "mention that it worked last week", "mention you are working from home today",
    "mention that you only noticed it after lunch", "mention that you were asked by your manager to check it",
    "mention that this is the first time you see it", "mention a shipping deadline this week",
    "do not add extra details", "mention that the factory is waiting for an answer",
]
SKELETON_STYLES = [
    "formal office tone", "casual chat tone", "terse developer shorthand", "frustrated user",
    "new employee who is unsure", "support engineer asking a colleague", "audit or compliance wording",
    "question hidden inside a longer sentence",
]
ALLOWED_WORDS = set("""the a an of in on at to for is are be it its this that which what where when how do does did can could you we i my
our and or by from with as if so not then there here has have table field column database screen page form value stored saved
store stores keep keeps kept update updated updates written write read reads data entry entered enter input system record
records row rows save saves when whenever please tell show me us find look name used use uses""".split())

GREETING_RE = re.compile(r"^\s*(hi|hello|hey|dear|good\s+(morning|afternoon|evening))\b[^,\n.!?]*[,\n.!?]*\s*", re.I)


def opening_key(text, surface, n=6):
    """First n normalised words of the body (greeting, Subject / Title lines removed)."""
    t = text.strip()
    if surface == "email":
        t = re.sub(r"^\s*Subject:[^\n]*\n+", "", t, flags=re.I)
    elif surface == "ticket":
        m = re.search(r"Description:\s*(.*)", t, re.S | re.I)
        t = m.group(1) if m else t
    t = GREETING_RE.sub("", t, count=1)
    return " ".join(re.findall(r"[a-z0-9']+", t.lower())[:n])


class OpeningTracker:
    def __init__(self, recent=60):
        self._lock = threading.Lock()
        self.counts = Counter()
        self.recent = deque(maxlen=recent)

    def count(self, key):
        with self._lock:
            return self.counts[key]

    def add(self, key):
        if key:
            with self._lock:
                self.counts[key] += 1
                self.recent.append(key)

    def frequent(self, k=3, min_count=2):
        with self._lock:
            return [w for w, c in self.counts.most_common(k) if c >= min_count]

    def distinct_ratio(self):
        with self._lock:
            n = sum(self.counts.values())
            return len(self.counts) / n if n else 1.0


def variation_hint(rng, tracker, attempt=0):
    """Text appended to the END of a prompt. attempt > 0: the previous text repeated an opening, so be stricter."""
    parts = ["Variation for this text (never mention these instructions): %s; %s." % (rng.choice(OPENING_STYLES), rng.choice(DETAILS))]
    avoid = tracker.frequent()
    if avoid:
        parts.append("Do not start with any of these openings: %s." % "; ".join("'%s ...'" % a for a in avoid))
    if attempt:
        parts.append("The previous attempt started like an earlier text: begin in a clearly different way.")
    return "\n" + " ".join(parts) + "\n"


# ----------------------------------------------------------------- skeletons
BULLET_RE = re.compile(r"^\s*(?:[-*\u2022]+|\d+[.)])\s*")


def clean_skeleton(line):
    s = BULLET_RE.sub("", line.strip()).strip(' "\'`')
    return " ".join(s.split())


def skeleton_ok(s, kind, banned_tokens):
    """kind 'minimal' needs exactly one {X}; 'context' needs exactly one {X} and one {C}; nothing else in braces."""
    if not s or "\n" in s:
        return False
    if s.count("{X}") != 1 or s.count("{C}") != (1 if kind == "context" else 0):
        return False
    rest = s.replace("{X}", " ").replace("{C}", " ")
    if "{" in rest or "}" in rest or re.search(r"\d", rest):
        return False
    words = re.findall(r"[A-Za-z']+", rest)
    if not 3 <= len(words) <= 32:
        return False
    toks = {w.lower() for w in words}
    return not (toks & (banned_tokens - ALLOWED_WORDS))


class SkeletonBank:
    """Skeletons per kind with usage counts: pick() returns a least-used one (ties broken at random)."""

    def __init__(self):
        self.items = {"minimal": [], "context": []}
        self.usage = Counter()

    def add(self, kind, skeleton):
        key = re.sub(r"\W+", " ", skeleton.lower()).strip()
        if all(re.sub(r"\W+", " ", x.lower()).strip() != key for x in self.items[kind]):
            self.items[kind].append(skeleton)
            return True
        return False

    def size(self, kind):
        return len(self.items[kind])

    def pick(self, kind, rng):
        pool = self.items[kind]
        low = min(self.usage[(kind, s)] for s in pool)
        s = rng.choice([x for x in pool if self.usage[(kind, x)] == low])
        self.usage[(kind, s)] += 1
        return s

    def to_json(self):
        return {"minimal": self.items["minimal"], "context": self.items["context"]}

    @classmethod
    def from_json(cls, d):
        b = cls()
        for k in ("minimal", "context"):
            for s in d.get(k, []):
                b.add(k, s)
        return b
