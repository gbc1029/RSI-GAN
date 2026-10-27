"""Naive verdict extraction: free-form task output -> accept / reject.

Why this module exists
----------------------
The task agent is asked for a one-word verdict, but nothing in the contract
forces it: a planner rewrite of the task prompt can turn the answer into a full
review ("The setup is sound, but the experiments are thin -- I recommend
rejection.").  Exact whole-string matching then scores that answer 0 while
``coverage`` stays 1.0, so a FORMAT accident is recorded as a judgement error.

This module is the parse layer for that class of failure: it maps free text onto
the binary verdict a label domain expects and records **how** it decided, so a
recovered label is auditable instead of silent.  It never rewrites the source
prediction and never invents a label it cannot justify (``label=None`` on
ambiguity), which is what lets the caller report strict and lenient scores
side by side.

Cascade (first branch that decides wins; ``method`` names it)
-------------------------------------------------------------
    top_level     non-string payload unwrapped (dict field / last list item)
    json_field    <json>{"response"|"label"|...: ...}</json>, or a bare JSON object
    bare          the whole answer IS the label (quoted / punctuated / cased)
    labeled_line  "Decision: ...", "Final verdict -- ...", "结论：..."
    phrase        academic idioms ("deserves publication", "not suitable for")
    keyword       negation-aware scan of explicit verdict words
    indirect      naive cue lexicon vote (no verdict word appears at all)
    ambiguous     verdict words found but the polarity cannot be resolved
    unparsed      nothing usable (includes the literal DGM-H "None" sentinel)

Negation is the part naive matching gets wrong
---------------------------------------------
``"accept" in text`` reads "should not be accepted" and "I cannot recommend
acceptance" as accept.  Every decision below therefore runs
:func:`_is_negated` on the text BEFORE the verdict word, inside the same clause:

* odd number of negators in the window flips the polarity
  ("not accepted" -> reject, "no reason to reject" -> accept);
* contrast markers flip the *alternative*:
  "reject rather than accept" -> reject, "accept rather than reject" -> accept;
* reversal idioms count as negators ("a mistake to reject" -> accept);
* neutralizers are removed first so they never count
  ("not only acceptable but excellent" -> accept, "no doubt" -> nothing).

Known limits (deliberate: naive, and visible in ``candidates``)
--------------------------------------------------------------
* A clause that merely *mentions* both labels is dropped as a choice-space
  description ("reviewers may accept or reject"), but a long review can still
  contain a stray cue; decision-marked clauses ("I recommend", "overall",
  "in conclusion") are preferred to plain ones to counter that.
* No real parsing of scope: "the writing is acceptable but the experiments are
  weak" is resolved by the marked/last-clause rule, not semantics.
* The indirect lexicon is bipolar vocabulary, not a classifier.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

ACCEPT = "accept"
REJECT = "reject"
DEFAULT_LABELS: Tuple[str, str] = (ACCEPT, REJECT)

__all__ = ["ACCEPT", "REJECT", "DEFAULT_LABELS", "Verdict", "parse_verdict",
           "normalize_text"]


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------
# Strings that carry no answer at all.  "none" is the literal sentinel DGM-H's
# task agent used to fall back to; it used to survive scoring as a wrong answer
# with full coverage, so it is recognised explicitly here.
_EMPTY_SENTINELS = frozenset({
    "", "-", "--", "---", "?", "??", "n/a", "na", "nan", "none", "null", "nil",
    "unknown", "no answer", "no prediction", "tbd", "无", "空", "未作答",
})

# Fields to unwrap from a structured payload, most specific first.
_TEXT_KEYS = ("response", "prediction", "answer", "final_answer", "final",
              "verdict", "decision", "label", "recommendation", "conclusion",
              "output", "text", "content")

_FENCE_RE = re.compile(r"```[A-Za-z0-9_+.-]*[ \t]*\r?\n?(.*?)```", re.S)
_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9_-]{0,20}(?:\s[^<>]{0,80})?/?>")
_MD_PREFIX_RE = re.compile(r"(?m)^[ \t]*(?:[#>*+-]+|\d+[.)])[ \t]+")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_QUOTE_CHARS = "\"'`\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f\u300a\u300b"
_TRAIL_PUNCT = ".,;:!?\u3002\uff0c\uff1b\uff1a\uff01\uff1f\u2026"


def normalize_text(text: str) -> str:
    """Fold a raw answer into the form the text rules operate on.

    NFKC collapses full-width forms (``ＡＣＣＥＰＴ``, ``：``) to ASCII; markdown
    fences/quotes/tags/bullets are dropped but their CONTENT is kept, because a
    fenced review is still a review.  Newlines survive: line-oriented rules
    (``labeled_line``) need them.
    """
    s = unicodedata.normalize("NFKC", str(text))
    s = _FENCE_RE.sub(lambda m: m.group(1), s)
    s = _TAG_RE.sub(" ", s)
    s = _MD_PREFIX_RE.sub(" ", s)
    s = s.replace("**", " ").replace("__", " ").replace("\u00a0", " ")
    s = _WS_RE.sub(" ", s)
    return s.strip()


def _strip_edges(s: str) -> str:
    """Strip wrapping quotes/emphasis/trailing punctuation (the ``bare`` test)."""
    out = s.strip()
    for _ in range(8):
        before = out
        while out and out[0] in _QUOTE_CHARS + "*_ ":
            out = out[1:].lstrip()
        while out and out[-1] in _QUOTE_CHARS + "*_ " + _TRAIL_PUNCT:
            out = out[:-1].rstrip()
        if out == before:
            break
    return out


# ---------------------------------------------------------------------------
# verdict vocabulary
# ---------------------------------------------------------------------------
# NOTE: the alternatives are wrapped in \b...\b, so "acceptable" does NOT match
# inside "unacceptable" (no word boundary between "un" and "acceptable") -- the
# pre-negated forms are explicit members of the reject family on purpose.
_ACCEPT_WORDS = r"accept|accepts|accepted|accepting|acceptance|acceptable"
_REJECT_WORDS = (r"reject|rejects|rejected|rejecting|rejection|"
                 r"unacceptable|unaccepted|unpublishable")
_KEYWORD_RE = re.compile(
    rf"\b({_ACCEPT_WORDS})\b|\b({_REJECT_WORDS})\b", re.I)

# CJK verdict words, reject forms FIRST so "不接收" wins over "接收" (the engine
# takes the leftmost alternative and both start at different offsets; listing the
# negated form first keeps "不接收" from being read as "接收").
_CJK_KEYWORD_RE = re.compile(
    r"(不予录用|不接收|不接受|不同意|拒稿|拒绝|退稿|接收|录用|接受|同意)")

# "reviewers may accept or reject" describes the choice space, not the verdict.
_CHOICE_SPACE_RE = re.compile(
    r"\b(?:accept\w*|reject\w*)\b\s*(?:,|\bor\b|\band\b|/|\||\u6216)\s*"
    r"\b(?:accept\w*|reject\w*)\b", re.I)

# Negators / contrast markers / reversal idioms.  Multi-word forms are matched by
# the same expression, so "rather than" counts once.
_NEG_RE = re.compile(
    r"\b(?:not|no|never|neither|nor|none|nobody|nothing|nowhere"
    r"|cannot|can\s+not|can't|cant|won't|wont|wouldn't|shouldn't|couldn't"
    r"|don't|dont|doesn't|doesnt|didn't|didnt|isn't|isnt|aren't|arent"
    r"|wasn't|wasnt|weren't|werent|hasn't|haven't|hadn't"
    r"|hardly|barely|scarcely|without|lack|lacks|lacking|fail|fails|failed"
    r"|unable|rather\s+than|instead\s+of|as\s+opposed\s+to"
    r"|(?:rather|sooner|better)\s+[A-Za-z]+\s+than"
    r"|mistake\s+to|wrong\s+to|no\s+grounds?\s+to|no\s+reason\s+to)\b"
    r"|\b[A-Za-z]+n't\b",
    re.I)

# Removed BEFORE counting negators: these contain a negator without negating.
_NEUTRALIZE_RE = re.compile(
    r"\b(?:not\s+only|no\s+doubt|needless\s+to\s+say|not\s+to\s+mention"
    r"|nothing\s+short\s+of|no\s+matter)\b",
    re.I)

# Clauses: sentence punctuation (ASCII + CJK) and contrastive connectives.
_CLAUSE_SEP_RE = re.compile(
    r"[.!?;\u3002\uff01\uff1f\uff1b\n]+"
    r"|\b(?:but|however|although|though|yet|nevertheless|nonetheless|whereas)\b",
    re.I)

# Clauses that CARRY a decision (used to outrank a stray cue elsewhere).
_MARKED_RE = re.compile(
    r"\b(?:i|we)\s+(?:would\s+|will\s+|must\s+|therefore\s+|thus\s+|hence\s+"
    r"|finally\s+|overall\s+|personally\s+)?"
    r"(?:recommend|suggest|advocate|propose|vote|lean|conclude|believe|think)"
    r"|\b(?:my|our)\s+(?:final\s+|overall\s+)?"
    r"(?:decision|verdict|recommendation|assessment|judg[eo]?ment|conclusion)\b"
    r"|\b(?:in\s+conclusion|to\s+conclude|overall|in\s+summary|all\s+in\s+all"
    r"|final\s+verdict|bottom\s+line|as\s+a\s+result)\b"
    r"|(?:结论|综上|总体|总而言之|我的建议|最终决定|最终判断|因此)",
    re.I)

# A label marker opening a decision line.  The trailing \b keeps "Results
# indicate ..." from being read as a decision marker.
_LABELED_RE = re.compile(
    r"(?:^|\n)\s*(?:(?:final|overall|my|our)\s+|(?:最终|最后))?"
    r"(?:decision|verdict|recommendations?|answers?|conclusions?"
    r"|judg[eo]?ments?|assessments?|outcomes?|results?|labels?"
    r"|决定|结论|建议|最终答案|判定)\b"
    r"\s*(?:is|are|:|=|-{1,2}|\u2014|\u2013|\uff1a)?\s*(?P<frag>[^\n]{0,200})",
    re.I)

# Academic idioms that carry a verdict without using the verdict words at all.
# Polarity is FIXED per pattern; an extra negator in front still flips it (e.g.
# "I do not think it deserves publication").
_PHRASES: Tuple[Tuple["re.Pattern[str]", str], ...] = (
    (re.compile(r"\b(?:deserves?|worthy\s+of|merits?|meriting)\s+"
                r"(?:to\s+be\s+)?(?:publication|acceptance|accept\w*)\b", re.I),
     ACCEPT),
    (re.compile(r"\b(?:recommend|suggest|advocate|support)\w*\s+"
                r"(?:(?:it|this|the\s+[A-Za-z]+)\s+)?"
                r"(?:for|to\s+be\s+considered\s+for)\s+"
                r"(?:acceptance|publication|the\s+main\s+track)\b", re.I),
     ACCEPT),
    (re.compile(r"\bin\s+favou?r\s+of\s+(?:acceptance|publication)\b", re.I),
     ACCEPT),
    (re.compile(r"\b(?:met|meets?|meeting)\s+the\s+bar\b"
                r"|\babove\s+the\s+(?:acceptance\s+)?(?:bar|threshold)\b", re.I),
     ACCEPT),
    (re.compile(r"\bwithhold\w*\s+(?:acceptance|publication)\b"
                r"|\bagainst\s+(?:acceptance|publication)\b"
                r"|\bbelow\s+the\s+(?:acceptance\s+)?(?:bar|threshold)\b"
                r"|\b(?:does|do)\s+not\s+meet\s+the\s+bar\b", re.I),
     REJECT),
    (re.compile(r"\bnot\s+(?:yet\s+)?(?:ready|suitable|appropriate|fit)\s+"
                r"for\s+(?:publication|acceptance)\b"
                r"|\bunsuitable\s+for\s+publication\b", re.I),
     REJECT),
)

# Indirect cues (no verdict word appears).  Positive-core phrasing only: the
# negated variants ("not convincing") are produced by _is_negated, so adding
# them here would double-flip them back to accept.
_ACCEPT_CUES = (
    "novel", "original", "solid", "rigorous", "convincing", "compelling",
    "well-written", "well written", "well-motivated", "well motivated",
    "well-organized", "well organized", "thorough", "sufficient", "adequate",
    "sound", "insightful", "enjoyable", "promising", "valuable", "coherent",
    "reproducible", "strong contribution", "significant contribution",
    "clear contribution", "broad interest",
)
_REJECT_CUES = (
    "insufficient", "incremental", "unclear", "weak", "flawed", "fatal",
    "lacking", "trivial", "questionable", "overclaim", "superficial",
    "poorly written", "inadequate", "unsatisfactory", "marginal",
    "limited novelty", "major concern", "serious concern", "not enough",
    "needs substantial", "hard to reproduce", "cherry-picked",
)
# "substantial contribution" is accept-side, "substantial concerns" is not.
_CUE_BLOCK_RE = re.compile(
    r"^\s*(?:of|in|with|about|regarding|to|for|around)?\s*"
    r"(?:concern|weakness|flaw|limitation|problem|issue|doubt|drawback"
    r"|shortcoming|risk|error|question)s?\b",
    re.I)


@dataclass(frozen=True)
class Verdict:
    """One parse result.  ``method`` is the cascade branch that decided.

    ``label`` is None whenever the module refuses to guess; callers must treat
    that as "no verdict" (never as a wrong verdict).
    """

    label: Optional[str]
    method: str
    confidence: float
    raw: str
    evidence: str = ""
    negated: bool = False
    candidates: Tuple[Dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return self.label is not None

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "label": self.label,
            "method": self.method,
            "confidence": self.confidence,
            "negated": self.negated,
            "evidence": self.evidence,
        }
        if self.candidates:
            out["candidates"] = [dict(c) for c in self.candidates]
        return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
# Conjunct boundaries inside one clause.  A negator does NOT govern a word in a
# different conjunct: "the idea is not novel, and I cannot recommend acceptance"
# has two negators that mean opposite things, and counting parity over the whole
# clause would cancel them into "accept".  A bare comma is a boundary too -- in
# review prose a comma almost always opens a new clause ("the paper is not
# perfect, I recommend rejection"), and treating it as transparent would let a
# distant negator flip the verdict.
_SEGMENT_RE = re.compile(r",|\b(?:and|but|so)\b", re.I)


def _is_negated(prefix: str) -> bool:
    """True when the text right before a verdict word negates it.

    Scope rules, in order:

    * neutralizers are removed first, so "not only acceptable" never counts;
    * a single negator in the window governs when nothing separates it from the
      verdict word (``"cannot see how this could be accepted"`` -> reject); the
      window is 12 tokens because a genuine negation can be several words away;
    * a conjunct boundary (comma, ``and``, ``but``, ``so``) ENDS the scope: only
      negators after the last boundary govern, and if there are none the word is
      not negated (``"not novel, and the writing is unclear"`` keeps *unclear*
      reject-flavoured instead of flipping it to accept).
    """
    if not prefix:
        return False
    tail = " ".join(prefix.split()[-12:])
    tail = _NEUTRALIZE_RE.sub(" ", tail)
    matches = list(_NEG_RE.finditer(tail))
    if not matches:
        return False
    segs = list(_SEGMENT_RE.finditer(tail))
    if segs:
        cut = segs[-1].end()
        return len([m for m in matches if m.start() >= cut]) % 2 == 1
    return len(matches) % 2 == 1


def _clause_spans(text: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    start = 0
    for m in _CLAUSE_SEP_RE.finditer(text):
        if m.start() > start:
            spans.append((start, m.start()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return [(a, b) for a, b in spans if text[a:b].strip()]


def _clause_of(spans: Sequence[Tuple[int, int]], pos: int) -> Optional[int]:
    for idx, (a, b) in enumerate(spans):
        if a <= pos < b:
            return idx
    return None


def _frag(text: str) -> str:
    """Human-sized evidence snippet."""
    s = " ".join(text.split())
    return s if len(s) <= 160 else s[:157] + "..."


def _apply_polarity(base: str, prefix: str) -> Tuple[str, bool]:
    neg = _is_negated(prefix)
    if not neg:
        return base, False
    return (REJECT if base == ACCEPT else ACCEPT), True


def _pick(hits: List[Dict[str, Any]], text: str,
          spans: Sequence[Tuple[int, int]]) -> Optional[Dict[str, Any]]:
    """Choose among verdict-keyword hits: marked clause first, then last mention."""
    if not hits:
        return None
    marked = [h for h in hits
              if _MARKED_RE.search(text[spans[h["clause"]][0]:spans[h["clause"]][1]])]
    pool = marked or hits
    last_clause = max(h["clause"] for h in pool)
    same = [h for h in pool if h["clause"] == last_clause]
    n_acc = sum(1 for h in same if h["label"] == ACCEPT)
    n_rej = sum(1 for h in same if h["label"] == REJECT)
    if n_acc == n_rej:
        # Tie inside the deciding clause: the later mention wins ("... but
        # ultimately accept"), which is the common review shape.
        return max(same, key=lambda h: h["pos"])
    return next(h for h in same if h["label"] == (ACCEPT if n_acc > n_rej else REJECT))


def _scan_keywords(text: str) -> Dict[str, Any]:
    """Negation-aware scan of explicit verdict words."""
    spans = _clause_spans(text)
    hits: List[Dict[str, Any]] = []
    for m in _KEYWORD_RE.finditer(text):
        idx = _clause_of(spans, m.start())
        if idx is None:
            continue
        cs, ce = spans[idx]
        clause_text = text[cs:ce]
        if _CHOICE_SPACE_RE.search(clause_text):
            continue  # "accept or reject" describes the choice space
        base = ACCEPT if m.group(1) else REJECT
        label, neg = _apply_polarity(base, text[cs:m.start()])
        hits.append({"pos": m.start(), "token": m.group(0).lower(),
                     "base": base, "label": label, "negated": neg,
                     "clause": idx, "clause_text": _frag(clause_text)})
    for m in _CJK_KEYWORD_RE.finditer(text):
        idx = _clause_of(spans, m.start())
        if idx is None:
            continue
        token = m.group(1)
        base = REJECT if token in {
            "不予录用", "不接收", "不接受", "不同意", "拒稿", "拒绝", "退稿"} else ACCEPT
        hits.append({"pos": m.start(), "token": token, "base": base,
                     "label": base, "negated": False, "clause": idx,
                     "clause_text": _frag(text[spans[idx][0]:spans[idx][1]])})
    hits.sort(key=lambda h: h["pos"])
    return {"hits": hits, "spans": spans,
            "pick": _pick(hits, text, spans)}


def _scan_phrases(text: str) -> Optional[Dict[str, Any]]:
    spans = _clause_spans(text)
    found: List[Dict[str, Any]] = []
    for pattern, base in _PHRASES:
        for m in pattern.finditer(text):
            idx = _clause_of(spans, m.start())
            prefix = text[spans[idx][0]:m.start()] if idx is not None else ""
            label, neg = _apply_polarity(base, prefix)
            found.append({"pos": m.start(), "token": m.group(0).lower(),
                          "base": base, "label": label, "negated": neg,
                          "clause": idx if idx is not None else 0,
                          "clause_text": _frag(m.group(0))})
    if not found:
        return None
    found.sort(key=lambda h: h["pos"])
    return _pick(found, text, spans) if spans else found[-1]


def _scan_indirect(text: str) -> Dict[str, Any]:
    """Naive cue lexicon vote, used only when no verdict word appears."""
    votes: List[Dict[str, Any]] = []
    for cues, base in ((_ACCEPT_CUES, ACCEPT), (_REJECT_CUES, REJECT)):
        for cue in cues:
            pattern = re.compile(rf"\b{re.escape(cue)}\b", re.I)
            for m in pattern.finditer(text):
                if base == ACCEPT and _CUE_BLOCK_RE.match(text[m.end():m.end() + 40]):
                    continue  # "significant concerns" is not an endorsement
                label, neg = _apply_polarity(base, text[max(0, m.start() - 90):m.start()])
                votes.append({"pos": m.start(), "token": cue, "label": label,
                              "negated": neg})
    n_acc = sum(1 for v in votes if v["label"] == ACCEPT)
    n_rej = sum(1 for v in votes if v["label"] == REJECT)
    total = n_acc + n_rej
    if not total:
        return {"label": None, "method": "unparsed", "confidence": 0.0,
                "evidence": "", "negated": False, "votes": []}
    hi, lo = max(n_acc, n_rej), min(n_acc, n_rej)
    if hi < 1 or hi < 2 * lo - 1e-9:
        return {"label": None, "method": "ambiguous", "confidence": 0.0,
                "evidence": _frag(f"{n_acc} accept-cue / {n_rej} reject-cue"),
                "negated": False, "votes": votes}
    return {"label": ACCEPT if n_acc > n_rej else REJECT,
            "method": "indirect",
            "confidence": round(min(0.70, 0.35 + 0.10 * hi), 2),
            "evidence": _frag(f"{n_acc} accept-cue / {n_rej} reject-cue: "
                              + ", ".join(sorted({v["token"] for v in votes})[:6])),
            "negated": any(v["negated"] for v in votes),
            "votes": votes}


def _label_from_obj(obj: Any) -> Optional[Tuple[Any, str]]:
    """(value, field-name) of the first verdict-ish field in a JSON object."""
    if not isinstance(obj, dict):
        return None
    for key in _TEXT_KEYS:
        if key in obj:
            val = obj[key]
            if isinstance(val, (str, int, float)) and not isinstance(val, bool):
                return val, key
            if isinstance(val, dict):
                inner = _label_from_obj(val)
                if inner is not None:
                    return inner[0], f"{key}.{inner[1]}"
    for key, val in obj.items():
        if isinstance(val, str) and _strip_edges(val.lower()) in DEFAULT_LABELS:
            return val, str(key)
    return None


def _balanced_objects(raw: str, limit: int = 4) -> List[str]:
    """Brace-balanced ``{...}`` slices (naive: strings are not tracked)."""
    out: List[str] = []
    depth = 0
    start = -1
    for i, ch in enumerate(raw):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(raw[start:i + 1])
                if len(out) >= limit:
                    break
                start = -1
    return out


_JSON_TAG_RE = re.compile(r"<json>\s*(\{.*?\})\s*</json>", re.S | re.I)


def _json_objects(raw: str) -> List[Any]:
    blocks = [m.group(1) for m in _JSON_TAG_RE.finditer(raw)]
    blocks.extend(_balanced_objects(raw))
    objs: List[Any] = []
    for block in blocks:
        try:
            obj = json.loads(block)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, (dict, list)):
            objs.append(obj)
    return objs


def _translate(label: Optional[str], labels: Tuple[str, str]) -> Optional[str]:
    if label is None:
        return None
    return labels[0] if label == ACCEPT else labels[1]


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def parse_verdict(
    value: Any,
    *,
    labels: Sequence[str] = DEFAULT_LABELS,
    allow_indirect: bool = True,
    _depth: int = 0,
) -> Verdict:
    """Map a task answer onto a binary verdict, or ``label=None`` if undecidable.

    Every branch records its evidence, so a lenient score computed from this
    module can always be traced back to the text that produced it.
    """
    labels_t: Tuple[str, str] = (str(labels[0]), str(labels[1]))

    def done(label: Optional[str], method: str, conf: float, *, raw: str = "",
             evidence: str = "", negated: bool = False,
             candidates: Sequence[Dict[str, Any]] = ()) -> Verdict:
        return Verdict(_translate(label, labels_t), method, conf, raw,
                       evidence, negated, tuple(candidates))

    # ---- branch: top-level coercion -------------------------------------
    if value is None:
        return done(None, "empty", 0.0)
    if isinstance(value, bool):
        return done(None, "unparsed", 0.0, evidence=_frag(f"bool {value}"))
    if isinstance(value, (list, tuple)):
        if _depth >= 3:
            return done(None, "unparsed", 0.0)
        for item in reversed(value):
            if isinstance(item, str) and normalize_text(item).strip():
                return parse_verdict(item, labels=labels_t,
                                     allow_indirect=allow_indirect,
                                     _depth=_depth + 1)
            if isinstance(item, dict):
                inner = parse_verdict(item, labels=labels_t,
                                      allow_indirect=allow_indirect,
                                      _depth=_depth + 1)
                if inner.ok:
                    return inner
        return done(None, "unparsed", 0.0, evidence=_frag(f"{len(value)} empty items"))
    if isinstance(value, dict):
        if _depth >= 3:
            return done(None, "unparsed", 0.0)
        picked = _label_from_obj(value)
        if picked is None:
            return done(None, "unparsed", 0.0,
                        evidence=_frag(f"dict keys={list(value)[:6]}"))
        raw_val, field = picked
        inner = parse_verdict(raw_val, labels=labels_t,
                              allow_indirect=allow_indirect, _depth=_depth + 1)
        if inner.ok:
            return done(ACCEPT if inner.label == labels_t[0] else REJECT,
                        "top_level", max(inner.confidence, 0.9), raw=inner.raw,
                        evidence=f"field:{field} -> {inner.evidence}",
                        negated=inner.negated, candidates=inner.candidates)
        return inner
    if not isinstance(value, str):
        return parse_verdict(str(value), labels=labels_t,
                             allow_indirect=allow_indirect, _depth=_depth + 1)

    raw = value
    text = normalize_text(raw)
    if text.lower() in _EMPTY_SENTINELS or _strip_edges(text.lower()) in _EMPTY_SENTINELS:
        return done(None, "empty", 0.0, raw=raw, evidence=_frag(text))

    # ---- branch: <json>{"response": ...}</json> --------------------------
    for obj in _json_objects(raw):
        if isinstance(obj, list):
            inner = parse_verdict(obj, labels=labels_t,
                                  allow_indirect=allow_indirect, _depth=_depth + 1)
            if inner.ok:
                return done(ACCEPT if inner.label == labels_t[0] else REJECT,
                            "json_field", max(inner.confidence, 0.9), raw=raw,
                            evidence=f"json:list -> {inner.evidence}",
                            negated=inner.negated)
            continue
        picked = _label_from_obj(obj)
        if picked is None:
            continue
        raw_val, field = picked
        inner = parse_verdict(raw_val, labels=labels_t,
                              allow_indirect=allow_indirect, _depth=_depth + 1)
        if inner.ok:
            return done(ACCEPT if inner.label == labels_t[0] else REJECT,
                        "json_field", max(inner.confidence, 0.9), raw=raw,
                        evidence=f"json:{field} -> {inner.evidence}",
                        negated=inner.negated, candidates=inner.candidates)

    lowered = " ".join(text.lower().split())

    # ---- branch: the whole answer is the label ---------------------------
    bare = _strip_edges(lowered)
    if bare in (ACCEPT, "accepted", "acceptance", REJECT, "rejected", "rejection"):
        label = ACCEPT if bare.startswith("accept") else REJECT
        return done(label, "bare", 1.0, raw=raw, evidence=_frag(text))
    if re.fullmatch(r"(?:i\s+)?(?:would\s+)?(?:go\s+with\s+)?"
                    r"(accept|reject)", bare):
        label = ACCEPT if bare.endswith("accept") else REJECT
        return done(label, "bare", 0.98, raw=raw, evidence=_frag(text))

    # ---- branch: a labelled decision line -------------------------------
    for m in _LABELED_RE.finditer(text):
        frag = m.group("frag").strip()
        # "acceptance" inside "not recommended for acceptance" must still be
        # read through the negator, so the fragment is scanned, not matched.
        scan = _scan_keywords(frag)
        pick = scan["pick"]
        if pick is not None:
            return done(pick["label"], "labeled_line", 0.92, raw=raw,
                        evidence=_frag(f"{m.group(0).strip()} [{pick['token']}]"),
                        negated=bool(pick["negated"]),
                        candidates=scan["hits"][:6])
        phrase = _scan_phrases(frag)
        if phrase is not None:
            return done(phrase["label"], "labeled_line", 0.9, raw=raw,
                        evidence=_frag(f"{m.group(0).strip()} [{phrase['token']}]"),
                        negated=bool(phrase["negated"]))

    # ---- branch: academic idioms ----------------------------------------
    phrase = _scan_phrases(text)
    if phrase is not None:
        return done(phrase["label"], "phrase", 0.9, raw=raw,
                    evidence=_frag(phrase["token"]), negated=bool(phrase["negated"]))

    # ---- branch: explicit verdict words (negation-aware) -----------------
    scan = _scan_keywords(text)
    pick = scan["pick"]
    if pick is not None:
        return done(pick["label"], "keyword", 0.9 if len(scan["hits"]) == 1 else 0.85,
                    raw=raw,
                    evidence=_frag(f"{pick['clause_text']} [{pick['token']}]"),
                    negated=bool(pick["negated"]),
                    candidates=scan["hits"][:6])

    # ---- branch: indirect cue vote --------------------------------------
    if allow_indirect:
        indirect = _scan_indirect(text)
        if indirect["label"] is not None or indirect["method"] == "ambiguous":
            return done(indirect["label"], indirect["method"],
                        float(indirect["confidence"]), raw=raw,
                        evidence=indirect["evidence"],
                        negated=bool(indirect["negated"]),
                        candidates=indirect["votes"][:8])

    return done(None, "unparsed", 0.0, raw=raw,
                evidence=_frag(text[:160]) or "<empty after normalisation>")


# ---------------------------------------------------------------------------
# CLI (offline audit: which branch decides, and on what evidence)
# ---------------------------------------------------------------------------
def _main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover
    import argparse
    import csv
    from collections import Counter, defaultdict

    ap = argparse.ArgumentParser(
        prog="python -m gan.framework.verdict",
        description="Map free-form task answers onto accept/reject.")
    ap.add_argument("--text", help="parse one answer and print the decision")
    ap.add_argument("--predictions",
                    help="predictions.csv (question_id,prediction) to audit")
    ap.add_argument("--column", default="prediction")
    ap.add_argument("--limit", type=int, default=3, help="examples per method")
    args = ap.parse_args(argv)

    if args.text is not None:
        v = parse_verdict(args.text)
        print(json.dumps({**v.as_dict(), "raw_chars": len(v.raw)},
                         ensure_ascii=False, indent=2))
        return 0

    if not args.predictions:
        ap.error("one of --text / --predictions is required")
    per_method: Dict[str, List[str]] = defaultdict(list)
    counts: Counter = Counter()
    labels: Counter = Counter()
    with open(args.predictions, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            v = parse_verdict(row.get(args.column) or "")
            counts[v.method] += 1
            labels[str(v.label)] += 1
            if len(per_method[v.method]) < args.limit:
                per_method[v.method].append(
                    f"    {v.label!r} <- {v.evidence[:120]!r}")
    total = sum(counts.values()) or 1
    print(f"rows: {total}")
    print("labels: " + ", ".join(f"{k}={v}" for k, v in labels.most_common()))
    print("methods:")
    for method, n in counts.most_common():
        print(f"  {method:<13} {n:>6}  ({100.0 * n / total:5.1f}%)")
        for example in per_method[method]:
            print(example)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
