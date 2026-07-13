"""Classify free-text from the TUI command bar into a routed :class:`Intent`.

The app pipes whatever the user types in the command bar through
:func:`classify`. Instead of always calling the feature planner, the returned
:class:`RoutedIntent` tells the app which backend path to take and carries the
small pieces of structured data each path needs (a PR number, a project name,
a job reference).

Design notes
------------
The classifier is *deterministic first*. Every intent is recognised by plain
regex/heuristics with an explicit confidence score. An optional ``llm``
callable is used *only* to break low-confidence ties -- it can never override a
high-confidence deterministic match. This keeps behaviour predictable and
testable while still allowing a smarter fallback when the heuristics are unsure.

Boundary highlights
-------------------
* PR_REVIEW deliberately requires either a review verb *near* a number/"pr", or
  an explicit ``#<num>`` / ``pr <num>`` shape. A bare substring "PR" (as in
  "implement a PR preview feature") must NOT route here -- see
  :func:`_match_pr_review` for the exact reasoning.
* MESSAGE_TO_JOB only fires when there is a *real* reference to an existing job
  (a ``job#<id>`` token, a "reply to ..."/"tell the ... job" phrase, a leading
  ``@handle``, or a substring match against ``known_job_titles``). Otherwise we
  fall through to FEATURE so ordinary requests are never hijacked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

__all__ = ["Intent", "RoutedIntent", "classify"]


class Intent(str, Enum):
    """The four backend routes the command bar can dispatch to."""

    FEATURE = "feature"
    PR_REVIEW = "pr_review"
    NEW_PROJECT = "new_project"
    MESSAGE_TO_JOB = "message_to_job"


@dataclass
class RoutedIntent:
    """Result of classifying one line of command-bar text.

    Attributes:
        intent: The chosen route.
        message: Cleaned text/description to hand to the downstream path.
        pr_number: Extracted PR number (PR_REVIEW only).
        project_name: Short slug/name if derivable (NEW_PROJECT only).
        job_ref: Job id fragment or matched title (MESSAGE_TO_JOB only).
        confidence: Heuristic confidence in ``[0, 1]``.
        rationale: Human-readable explanation for debugging/audit.
    """

    intent: Intent
    message: str = ""
    pr_number: int | None = None
    project_name: str | None = None
    job_ref: str | None = None
    confidence: float = 1.0
    rationale: str = ""


# --- Shared regex vocabulary -------------------------------------------------

# A review verb, i.e. an ask to *look at* something that already exists.
_REVIEW_VERB = r"\b(?:review|reviewing|look\s+at|looking\s+at|check(?:\s+out)?|take\s+a\s+look|audit|inspect)\b"
# "pr" / "pull request" as a whole word (never a substring of "preview" etc.).
_PR_WORD = r"(?:pull\s+request|\bpr\b)"
# A PR number, optionally hash-prefixed: matches "#123", "pr 45", "pr#9".
_PR_NUMBER = r"#?\s*(\d+)"


def _low(base: str = "") -> float:
    return 0.5


def classify(
    text: str,
    *,
    known_job_titles: list[str] | None = None,
    llm=None,
) -> RoutedIntent:
    """Classify ``text`` into a :class:`RoutedIntent`.

    Args:
        text: Raw command-bar input.
        known_job_titles: Titles/ids of currently-open jobs, used to recognise a
            MESSAGE_TO_JOB even when the wording is loose.
        llm: Optional ``callable(text) -> intent-value-str`` used ONLY to break
            low-confidence ties (confidence < 0.6). ``None`` = pure heuristic.

    Returns:
        A populated :class:`RoutedIntent`. Never raises on odd input.
    """
    known_job_titles = known_job_titles or []
    raw = text or ""
    stripped = raw.strip()

    # Empty / whitespace-only: let the caller deal with it.
    if not stripped:
        return RoutedIntent(
            intent=Intent.FEATURE,
            message="",
            confidence=0.1,
            rationale="empty input",
        )

    # Deterministic checks in priority order; first strong match wins.
    result = (
        _match_pr_review(stripped)
        or _match_new_project(stripped)
        or _match_message_to_job(stripped, known_job_titles)
        or _fallback_feature(stripped)
    )

    # Optional LLM tie-breaker: only when the heuristic is unsure.
    if llm is not None and result.confidence < 0.6:
        result = _apply_llm_tiebreak(result, raw, llm)

    return result


# --- PR_REVIEW ---------------------------------------------------------------


def _match_pr_review(text: str) -> RoutedIntent | None:
    """Detect a request to review an existing pull request.

    Boundary: a bare substring "PR" is not enough. We require ONE of:

    * an explicit hash-number ``#<n>`` anywhere, OR
    * the token ``pr``/``pull request`` immediately followed by a number
      (``pr 7``, ``pr#9``), OR
    * a review verb appearing together with ``pr``/``pull request``.

    This is what keeps "implement a PR preview feature" out of PR_REVIEW: it has
    the word "PR" but no number attached to it, no ``#<n>``, and its verb
    ("implement") is not a review verb. Conversely "review pr 7" matches on the
    verb+word rule, and "look at PR#9" matches on both the verb+word and the
    ``pr<num>`` rules.
    """
    has_pr_word = re.search(_PR_WORD, text, re.IGNORECASE) is not None
    has_review_verb = re.search(_REVIEW_VERB, text, re.IGNORECASE) is not None

    # "#123" style -- a hash immediately before digits is a strong PR signal.
    hash_num = re.search(r"#\s*(\d+)", text)
    # "pr 7" / "pr#9" / "pull request 45" -- number bound to the pr word.
    pr_num = re.search(_PR_WORD + r"\s*" + _PR_NUMBER, text, re.IGNORECASE)

    number: int | None = None
    reasons: list[str] = []
    strong = False

    if pr_num:
        number = int(pr_num.group(1))
        reasons.append("'pr'/'pull request' followed by a number")
        strong = True
    elif hash_num:
        number = int(hash_num.group(1))
        reasons.append("explicit #<number>")
        # A hash-number alone is meaningful; strong only if a PR word or review
        # verb is also present (otherwise it could be issue #12 in feature text).
        strong = has_pr_word or has_review_verb

    if has_review_verb and has_pr_word:
        reasons.append("review verb + 'pr'/'pull request'")
        strong = True
        if number is None:
            # e.g. "review the pr" with no number visible; grab any number.
            any_num = re.search(r"(\d+)", text)
            if any_num:
                number = int(any_num.group(1))

    # Only claim PR_REVIEW when we're actually confident it is about a PR.
    # A bare "#12" with no 'pr'/'pull request' word and no review verb (e.g.
    # "tell job#12 ..." or "fix issue #12") is NOT a PR review — fall through so
    # the message-to-job / feature matchers get their turn.
    if not strong:
        return None

    confidence = 0.95
    rationale = "PR_REVIEW: " + "; ".join(reasons)
    if number is not None:
        rationale += f" (pr_number={number})"

    return RoutedIntent(
        intent=Intent.PR_REVIEW,
        message=text,
        pr_number=number,
        confidence=confidence,
        rationale=rationale,
    )


# --- NEW_PROJECT -------------------------------------------------------------

_NEW_PROJECT_TRIGGERS = re.compile(
    r"""(
        start\s+a\s+new\s+project
      | create\s+a\s+new\s+(?:project|repo(?:sitory)?|package|service|app)
      | new\s+project
      | scaffold(?:\s+a)?
      | from\s+scratch
      | brand[\s-]?new
      | greenfield
      | bootstrap\s+a
      | spin\s+up\s+a\s+new
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Explicit name markers: "called foo", "named foo", or a trailing ": description".
_NAME_AFTER = re.compile(
    r"(?:called|named)\s+([A-Za-z0-9][\w.\-/ ]*?)(?=$|[.,;:]|\s+(?:from|in|using|with|that|which|for)\b)",
    re.IGNORECASE,
)
_STOPWORDS = {
    "a",
    "an",
    "the",
    "new",
    "project",
    "repo",
    "repository",
    "package",
    "service",
    "app",
    "cli",
    "from",
    "scratch",
    "in",
    "python",
    "using",
    "with",
    "that",
    "which",
    "for",
    "brand",
    "greenfield",
}


def _slugify(name: str, *, max_words: int = 4, strip_stopwords: bool = True) -> str | None:
    """Turn a free-text name into a short lowercase slug, or ``None``.

    ``strip_stopwords`` filters filler/type words ("new", "project", "cli",
    "app", ...) when deriving a slug from a description. It must be False for an
    explicitly-given name ("called todo-cli") where every token is intentional.
    """
    tokens = re.findall(r"[A-Za-z0-9]+", name.lower())
    if not tokens:
        return None
    if strip_stopwords:
        tokens = [t for t in tokens if t not in _STOPWORDS] or tokens
    slug = "-".join(tokens[:max_words])
    return slug or None


def _match_new_project(text: str) -> RoutedIntent | None:
    """Detect a request to scaffold a brand-new project/repo."""
    trigger = _NEW_PROJECT_TRIGGERS.search(text)
    if not trigger:
        return None

    project_name: str | None = None
    reasons = [f"trigger phrase '{trigger.group(1).strip()}'"]

    # 1) "called <name>" / "named <name>" wins if present. The user named it
    #    explicitly, so keep every token (don't strip type words like "cli").
    m = _NAME_AFTER.search(text)
    if m:
        project_name = _slugify(m.group(1), strip_stopwords=False)
        if project_name:
            reasons.append(f"explicit name marker -> '{project_name}'")

    # 2) Otherwise, text after a colon is usually the description; slug from it.
    description = text
    if project_name is None and ":" in text:
        _, _, after_colon = text.partition(":")
        after_colon = after_colon.strip()
        if after_colon:
            description = after_colon
            project_name = _slugify(after_colon)
            if project_name:
                reasons.append(f"colon description -> '{project_name}'")

    # 3) Last resort: slug the whole thing minus the trigger words.
    if project_name is None:
        residual = _NEW_PROJECT_TRIGGERS.sub(" ", text)
        project_name = _slugify(residual)
        if project_name:
            reasons.append(f"derived from remaining words -> '{project_name}'")

    return RoutedIntent(
        intent=Intent.NEW_PROJECT,
        message=description,
        project_name=project_name,
        confidence=0.9,
        rationale="NEW_PROJECT: " + "; ".join(reasons),
    )


# --- MESSAGE_TO_JOB ----------------------------------------------------------

# "job#12", "job 12", "job #ab12" -> capture the id fragment.
_JOB_TOKEN = re.compile(r"\bjob\s*#?\s*(\w+)", re.IGNORECASE)
# "@handle" at the very start of the message.
_AT_HANDLE = re.compile(r"^\s*@([\w.\-]+)\b")
# Reply/answer/tell phrasing that points at a job.
_REPLY_PHRASE = re.compile(
    r"\b(?:reply\s+to|respond\s+to|answer|tell|message)\b", re.IGNORECASE
)
# "tell the <name> job", "the <name> job" -> capture <name>.
_NAMED_JOB = re.compile(
    r"(?:the\s+)?([\w.\- ]+?)\s+job\b", re.IGNORECASE
)


def _strip_prefix(text: str, prefix_match: re.Match | None) -> str:
    """Return the instruction with a routing prefix removed."""
    if prefix_match is None:
        return text.strip()
    tail = text[prefix_match.end():]
    # Drop a leading connector/punctuation like ": ", ", ", "to ".
    tail = re.sub(r"^\s*[:,-]?\s*(?:to\s+)?", "", tail)
    return tail.strip() or text.strip()


def _match_message_to_job(text: str, known_job_titles: list[str]) -> RoutedIntent | None:
    """Detect an instruction aimed at an already-running job.

    Only fires when there is a concrete reference to an existing job. Recognised
    references, in order of specificity:

    1. ``job#<id>`` / ``job <id>`` token.
    2. Leading ``@handle``.
    3. A "reply to ... / tell ... job" phrase.
    4. A substring match against one of ``known_job_titles``.
    """
    # 1) Explicit job token.
    tok = _JOB_TOKEN.search(text)
    if tok:
        job_ref = tok.group(1)
        # Strip the whole "tell job#12 to" style prefix up to the token.
        instruction = _strip_prefix(text, tok)
        return RoutedIntent(
            intent=Intent.MESSAGE_TO_JOB,
            message=instruction,
            job_ref=job_ref,
            confidence=0.9,
            rationale=f"MESSAGE_TO_JOB: job token -> job_ref='{job_ref}'",
        )

    # 4a) Known-title match (checked early because it is a very strong signal,
    # including for the leading-@handle case like "@rebate-scenarios ...").
    title_hit = _match_known_title(text, known_job_titles)
    if title_hit is not None:
        matched_title, job_ref = title_hit
        at = _AT_HANDLE.search(text)
        instruction = _strip_prefix(text, at) if at else _strip_reply_prefix(text)
        return RoutedIntent(
            intent=Intent.MESSAGE_TO_JOB,
            message=instruction,
            job_ref=job_ref,
            confidence=0.85,
            rationale=f"MESSAGE_TO_JOB: matched known job title '{matched_title}'",
        )

    # 2) Leading @handle with no known-title match: still a job reference.
    at = _AT_HANDLE.search(text)
    if at:
        job_ref = at.group(1)
        instruction = _strip_prefix(text, at)
        return RoutedIntent(
            intent=Intent.MESSAGE_TO_JOB,
            message=instruction,
            job_ref=job_ref,
            confidence=0.7,
            rationale=f"MESSAGE_TO_JOB: leading @handle -> job_ref='{job_ref}'",
        )

    # 3) "reply to the <name> job: ..." / "tell the <name> job ...".
    if _REPLY_PHRASE.search(text):
        named = _NAMED_JOB.search(text)
        if named:
            candidate = named.group(1).strip()
            # Avoid capturing filler like "the" only.
            job_ref = candidate if candidate.lower() not in {"the", "this", "that"} else None
            if job_ref:
                instruction = _strip_reply_prefix(text)
                return RoutedIntent(
                    intent=Intent.MESSAGE_TO_JOB,
                    message=instruction,
                    job_ref=job_ref,
                    confidence=0.75,
                    rationale=f"MESSAGE_TO_JOB: reply phrase + '<name> job' -> job_ref='{job_ref}'",
                )

    return None


def _strip_reply_prefix(text: str) -> str:
    """Strip a 'reply to the X job:' / 'tell the X job' lead-in."""
    # Everything up to and including the first ':' is treated as the routing
    # prefix when a reply phrase is present.
    if ":" in text:
        _, _, tail = text.partition(":")
        tail = tail.strip()
        if tail:
            return tail
    # No colon: strip up through "... job".
    m = _NAMED_JOB.search(text)
    if m:
        tail = text[m.end():].strip()
        tail = re.sub(r"^\s*(?:to\s+)?", "", tail)
        if tail:
            return tail
    return text.strip()


def _match_known_title(
    text: str, known_job_titles: list[str]
) -> tuple[str, str] | None:
    """Return ``(matched_title, job_ref)`` if the text references a known job.

    Matching is case-insensitive and tolerant of ``-``/``_``/space differences,
    so "@rebate-scenarios" matches the open job titled "rebate scenarios".
    """
    if not known_job_titles:
        return None

    def norm(s: str) -> str:
        return re.sub(r"[\s_\-]+", " ", s.lower()).strip()

    haystack = norm(text)
    haystack_squashed = haystack.replace(" ", "")

    for title in known_job_titles:
        nt = norm(title)
        if not nt:
            continue
        if nt in haystack or nt.replace(" ", "") in haystack_squashed:
            # job_ref is the matched title's first token/slug for downstream use.
            slug = re.sub(r"\s+", "-", nt)
            return title, slug
    return None


# --- FEATURE (fallback) ------------------------------------------------------


def _fallback_feature(text: str) -> RoutedIntent:
    """Default route: a general coding outcome handed to the planner."""
    return RoutedIntent(
        intent=Intent.FEATURE,
        message=text,
        # A catch-all, not a positive identification: keep it below the LLM
        # tie-break threshold (0.6) so an optional classifier can reconsider
        # genuinely ambiguous input. Deterministic strong matches stay >= 0.9.
        confidence=0.55,
        rationale="FEATURE: no stronger intent matched (default route)",
    )


# --- LLM tie-breaker ---------------------------------------------------------

_VALID_INTENT_VALUES = {i.value for i in Intent}


def _apply_llm_tiebreak(result: RoutedIntent, raw_text: str, llm) -> RoutedIntent:
    """Consult ``llm`` to resolve a low-confidence classification.

    The ``llm`` callable returns an intent *value* string. If it returns a valid
    intent, we adopt it, bump confidence, and record the override in the
    rationale. Any error or invalid value leaves the heuristic result untouched.
    """
    try:
        suggestion = llm(raw_text)
    except Exception:  # pragma: no cover - defensive; llm is caller-supplied.
        return result

    if not isinstance(suggestion, str):
        return result
    key = suggestion.strip().lower()
    if key not in _VALID_INTENT_VALUES:
        return result

    new_intent = Intent(key)
    if new_intent == result.intent:
        # LLM agreed; nudge confidence up a little.
        result.confidence = max(result.confidence, 0.65)
        result.rationale += " | llm agreed"
        return result

    # LLM overrides the low-confidence heuristic. Re-run the specific extractor
    # so structured fields (pr_number, etc.) are populated where possible.
    overridden = _reextract_for(new_intent, raw_text.strip())
    overridden.confidence = 0.7
    overridden.rationale = (
        f"llm tie-breaker overrode low-confidence "
        f"{result.intent.value} -> {new_intent.value}; "
        f"prior: {result.rationale}"
    )
    return overridden


def _reextract_for(intent: Intent, text: str) -> RoutedIntent:
    """Best-effort field extraction for an LLM-chosen intent."""
    if intent == Intent.PR_REVIEW:
        num = re.search(r"#?\s*(\d+)", text)
        return RoutedIntent(
            intent=Intent.PR_REVIEW,
            message=text,
            pr_number=int(num.group(1)) if num else None,
        )
    if intent == Intent.NEW_PROJECT:
        m = _NAME_AFTER.search(text)
        name = _slugify(m.group(1)) if m else _slugify(text)
        return RoutedIntent(intent=Intent.NEW_PROJECT, message=text, project_name=name)
    if intent == Intent.MESSAGE_TO_JOB:
        tok = _JOB_TOKEN.search(text)
        return RoutedIntent(
            intent=Intent.MESSAGE_TO_JOB,
            message=text,
            job_ref=tok.group(1) if tok else None,
        )
    return RoutedIntent(intent=Intent.FEATURE, message=text)
