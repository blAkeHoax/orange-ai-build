"""
Orange AI
Memory Classifier V1

This module analyzes user messages and extracts
long-term memories that are worth storing.

It DOES NOT write to the database.

It simply returns structured memory objects.

Example

Input:
"My name is Sayan."

Output:

[
    {
        "category":"identity",
        "key":"name",
        "value":"Sayan",
        "confidence":1.0
    }
]
"""

from __future__ import annotations

import hashlib
import re

from typing import List, Dict


# ==========================================================
# MEMORY OBJECT
# ==========================================================

Memory = Dict[str, object]


# ==========================================================
# REGEX PATTERNS
# ==========================================================

NAME_PATTERNS = [

    re.compile(
        r"\bmy name is ([a-z][a-z'-]*(?:\s+[a-z][a-z'-]*){0,3})(?:[.,;!?]|$)",
        re.IGNORECASE
    ),

    re.compile(
        r"\bcall me ([a-z][a-z'-]*(?:\s+[a-z][a-z'-]*){0,3})(?:[.,;!?]|$)",
        re.IGNORECASE
    ),

    # "i am"/"i'm" ARE NOT used as name signals here on their own --
    # "I am tired", "I'm not sure", "I'm going to bed" all start the same
    # way and are not name statements. Matching r"i am (.+)" unconditionally
    # was overwriting the stored name with whatever clause followed "I am",
    # since remember_structured() does an UPSERT on (category, key).
    # Instead we only accept "i am"/"i'm" as a name signal when what follows
    # looks like an actual name: 1-3 short word-like tokens, nothing that
    # looks like the start of an ordinary sentence (no verbs/articles/etc).
    # _looks_like_name() below does that check; these patterns just capture
    # the candidate text for it to validate.
    re.compile(
        r"\bi am ([a-z][a-z'-]*(?:\s+[a-z][a-z'-]*){0,2})(?:[.,!?]|$)",
        re.IGNORECASE
    ),

    re.compile(
        r"\bi'm ([a-z][a-z'-]*(?:\s+[a-z][a-z'-]*){0,2})(?:[.,!?]|$)",
        re.IGNORECASE
    ),
]

# Common words that show up right after "I am"/"I'm" in ordinary sentences
# that are NOT name statements ("I am tired", "I'm not sure", "I'm going to
# the store"). If the captured text starts with one of these, or contains
# one of these anywhere, it's almost certainly not a name.
_NAME_FALSE_POSITIVE_WORDS = {
    "not", "no", "going", "gonna", "trying", "feeling", "felling", "sure",
    "happy", "sad", "tired", "okay", "ok", "fine", "good", "bad", "done",
    "ready", "here", "there", "thinking", "wondering", "looking", "working",
    "about", "still", "just", "justr", "jus", "jusr", "also", "really", "very", "so", "kind",
    "sorry", "glad", "afraid", "worried", "excited", "bored", "busy",
    "free", "available", "interested", "curious", "confused", "lost",
    "going", "leaving", "staying", "waiting", "hoping", "planning",
    "catching", "catch", "running", "run", "doing", "getting", "eating",
    "making", "playing", "reading", "writing", "coding", "learning",
    "chilling", "relaxing", "heading", "coming", "talking", "speaking",
    # Prepositions / particles
    "up", "down", "in", "out", "on", "off", "over", "under", "with",
    "without", "for", "to", "from", "at", "by", "into", "onto",
    # Pronouns/possessives/articles/fillers
    "your", "his", "her", "their", "our", "its", "the", "a", "an",
    "this", "that", "these", "those", "who", "what", "which",
    "something", "anything", "everything", "nothing", "someone", "anyone",
    "actually", "basically", "literally", "simply",
}

_NAME_PARTICLES_AND_PREPOSITIONS = {
    "up", "down", "in", "out", "on", "off", "over", "under", "with",
    "without", "for", "to", "from", "at", "by", "into", "onto",
}


def _looks_like_name(candidate: str) -> bool:
    """
    Sanity check for text captured after "i am"/"i'm" before treating it as
    a name. Real name statements ("I am Sayan", "I'm Sayan Dey") are short
    (1-3 words) and don't contain ordinary sentence words, verbs, or particles.
    """
    words = candidate.strip().split()

    if not words or len(words) > 3:
        return False

    lowered_words = [w.lower().strip(".,!?;:\"'-") for w in words]

    # Rejection if any word is an obvious conversational filler or stop word
    if set(lowered_words) & _NAME_FALSE_POSITIVE_WORDS:
        return False

    # Rejection if last word is a preposition/particle (e.g. "catching up", "heading out")
    if lowered_words[-1] in _NAME_PARTICLES_AND_PREPOSITIONS:
        return False

    # Rejection of progressive verb participles ending in -ing
    for w in lowered_words:
        if len(w) > 4 and w.endswith("ing"):
            return False

    return True


BIRTHDAY_PATTERNS = [

    re.compile(
        r"my birthday is (.+)",
        re.IGNORECASE
    ),

    re.compile(
        r"i was born on (.+)",
        re.IGNORECASE
    ),
]


CONTRASTIVE_PREF_PATTERNS = [
    re.compile(
        r"\b(?:actually,?\s+)?i\s+(?:actually\s+)?prefer\s+(.+?)\s+(?:over|instead\s+of|rather\s+than)\s+(.+?)(?:[.,;!]|$)",
        re.IGNORECASE
    ),
    re.compile(
        r"\b(?:actually,?\s+)?i\s+(?:actually\s+)?like\s+(.+?)\s+(?:more\s+than|better\s+than)\s+(.+?)(?:[.,;!]|$)",
        re.IGNORECASE
    ),
]

PREF_RETIREMENT_PATTERNS = [
    re.compile(
        r"\bi\s+(?:don't|do\s+not)\s+like\s+(.+?)(?:\s+anymore)?(?:[.,;!]|$)",
        re.IGNORECASE
    ),
    re.compile(
        r"\bi\s+dislike\s+(.+?)(?:\s+anymore)?(?:[.,;!]|$)",
        re.IGNORECASE
    ),
    re.compile(
        r"\bi\s+no\s+longer\s+like\s+(.+?)(?:[.,;!]|$)",
        re.IGNORECASE
    ),
]

STANDARD_PREF_PATTERNS = [
    re.compile(
        r"\b(?:actually,?\s+)?i\s+(?:actually\s+)?prefer\s+(.+)",
        re.IGNORECASE
    ),
    re.compile(
        r"\b(?:actually,?\s+)?i\s+(?:actually\s+)?like\s+(.+)",
        re.IGNORECASE
    ),
    re.compile(
        r"\b(?:actually,?\s+)?i\s+(?:actually\s+)?love\s+(.+)",
        re.IGNORECASE
    ),
]

LIKE_PATTERNS = STANDARD_PREF_PATTERNS


FAVORITE_PATTERNS = [

    re.compile(
        r"my favorite (.+?) is (.+)",
        re.IGNORECASE
    ),
]


PROJECT_RETIREMENT_PATTERNS = [
    re.compile(
        r"\bi\s+(?:am\s+no\s+longer|'m\s+no\s+longer)\s+working\s+on\s+(.+)",
        re.IGNORECASE
    ),
    re.compile(
        r"\bi\s+stopped\s+working\s+on\s+(.+)",
        re.IGNORECASE
    ),
]

PROJECT_STORE_PATTERNS = [
    re.compile(
        r"\b(?:i\s+am|i'm)\s+working\s+on\s+(.+)",
        re.IGNORECASE
    ),
    re.compile(
        r"\bmy\s+current\s+project\s+is\s+(.+)",
        re.IGNORECASE
    ),
]

PROJECT_PATTERNS = PROJECT_STORE_PATTERNS


GOAL_PATTERNS = [

    re.compile(
        r"i want to (.+)",
        re.IGNORECASE
    ),

    re.compile(
        r"my goal is to (.+)",
        re.IGNORECASE
    ),
]

# "i want to (.+)" matches both real goals ("I want to build AGI") and
# ordinary in-the-moment requests ("I want to know what time it is",
# "I want to delete that file") that aren't goals worth remembering forever.
# Requests like this are usually short, and start with a small set of verbs
# about immediate actions/knowledge rather than aspirations. This isn't a
# perfect classifier, but it filters out the clearest false positives.
_GOAL_FALSE_POSITIVE_STARTS = (
    "know", "see", "check", "find out", "delete", "open", "close",
    "read", "show", "tell", "ask", "go to", "leave", "stop", "cancel",
    "undo", "redo", "exit", "quit", "rename", "move", "copy", "search",
)


def _looks_like_goal(candidate: str) -> bool:
    """Reject short, immediate-action phrases that aren't really goals."""
    lowered = candidate.strip().lower()

    if not lowered:
        return False

    for start in _GOAL_FALSE_POSITIVE_STARTS:
        if lowered.startswith(start):
            return False

    return True


CONTRASTIVE_TOOL_PATTERNS = [
    re.compile(
        r"\binstead\s+of\s+(.+?),\s*i\s+use\s+(.+?)(?:[.,;!]|$)",
        re.IGNORECASE
    ),
    re.compile(
        r"\bi\s+use\s+(.+?)\s+instead\s+of\s+(.+?)(?:[.,;!]|$)",
        re.IGNORECASE
    ),
]

TOOL_RETIREMENT_PATTERNS = [
    re.compile(
        r"\b(?:i\s+am|i'm)\s+no\s+longer\s+using\s+(.+?)(?:[.,;!]|$)",
        re.IGNORECASE
    ),
    re.compile(
        r"\b(?:i\s+am|i'm)\s+not\s+using\s+(.+?)(?:\s+anymore)?(?:[.,;!]|$)",
        re.IGNORECASE
    ),
    re.compile(
        r"\bi\s+no\s+longer\s+use\s+(.+?)(?:[.,;!]|$)",
        re.IGNORECASE
    ),
    re.compile(
        r"\bi\s+stopped\s+using\s+(.+?)(?:[.,;!]|$)",
        re.IGNORECASE
    ),
    re.compile(
        r"\bi\s+(?:don't|do\s+not)\s+use\s+(.+?)(?:\s+anymore)?(?:[.,;!]|$)",
        re.IGNORECASE
    ),
]

TOOL_STORE_PATTERNS = [
    re.compile(
        r"\bi\s+use\s+(.+)",
        re.IGNORECASE
    ),
    re.compile(
        r"\bmy\s+preferred\s+(.+?)\s+is\s+(.+)",
        re.IGNORECASE
    ),
]

TOOL_PATTERNS = TOOL_STORE_PATTERNS


CREATOR_PATTERNS = [

    re.compile(
        r"i am your creator",
        re.IGNORECASE
    ),

    re.compile(
        r"i created you",
        re.IGNORECASE
    ),

    re.compile(
        r"i'm creating you",
        re.IGNORECASE
    ),
]


# ==========================================================
# HELPERS
# ==========================================================

# ==========================================================
# UNCERTAINTY & CLAUSE SPLITTING HELPERS (STAGE 3.4)
# ==========================================================

_UNCERTAINTY_PATTERNS = [
    re.compile(r"\b(?:i\s+am|i'm)\s+not\s+sure\b", re.IGNORECASE),
    re.compile(r"\bnot\s+sure\s+(?:if|whether)\b", re.IGNORECASE),
    re.compile(r"\b(?:don't|do\s+not)\s+know\s+(?:if|whether)\b", re.IGNORECASE),
    re.compile(r"\b(?:don't|do\s+not)\s+think\s+i\b", re.IGNORECASE),
    re.compile(r"\b(?:don't|do\s+not)\s+believe\s+i\b", re.IGNORECASE),
    re.compile(r"\bwonder\s+(?:if|whether)\b", re.IGNORECASE),
    re.compile(r"\b(?:maybe|perhaps)\s+i\b", re.IGNORECASE),
    re.compile(r"\bcould\s+it\s+be\s+that\b", re.IGNORECASE),
    re.compile(r"^\s*if\s+i\s+(?:like|use|prefer)\b", re.IGNORECASE),
]

_QUESTION_STARTERS = (
    "do i ", "did i ", "should i ", "can i ", "could i ", "would i ",
    "will i ", "is my ", "are my ", "am i ", "why do i ", "why would i ",
    "how do i ", "what is my ", "what are my ", "who am i", "where do i "
)


def _is_uncertain_or_question(clause: str) -> bool:
    s = clause.strip()
    if not s:
        return True
    if s.endswith("?"):
        return True
    lower = s.lower()
    for q in _QUESTION_STARTERS:
        if lower.startswith(q):
            return True
    for p in _UNCERTAINTY_PATTERNS:
        if p.search(lower):
            return True
    return False


def _split_clauses(text: str) -> List[str]:
    raw_clauses = re.split(r";\s*|\s+(?:and|but)\s+(?=(?:i\b|my\b|call\b))", text, flags=re.IGNORECASE)
    cleaned = []
    for c in raw_clauses:
        c_str = c.strip()
        if c_str:
            cleaned.append(c_str)
    return cleaned or [text]


def _slugify_key(text: str, prefix: str = "", max_chars: int = 32) -> str:
    """
    Generate a stable, deterministic, collision-resistant memory_key from entity text.
    Preserves equivalent phrasing (e.g. 'Project Zephyr' and 'Zephyr' resolve to 'zephyr').
    Handles capitalization, punctuation, whitespace, and parentheticals.
    Appends a deterministic 6-character hex hash if the canonical slug exceeds max_chars
    to guarantee distinct entities never collide.
    """
    s = text.strip().lower()

    # Strip parentheticals for key generation if not the entire text
    no_parens = re.sub(r"\(.*?\)", "", s).strip()
    if no_parens:
        s = no_parens

    # Strip common leading filler/stop words for equivalent phrasing
    fillers = ["project ", "a ", "an ", "the ", "my ", "to "]
    for filler in fillers:
        if s.startswith(filler) and len(s) > len(filler):
            s = s[len(filler):].strip()
            break

    s = s.replace("'", "")
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w]

    # Stop words filtering if multiple words
    stop_words = {"a", "an", "the", "in", "on", "at", "to", "for", "of", "with", "and"}
    if len(words) > 1:
        filtered = [w for w in words if w not in stop_words]
        if filtered:
            words = filtered

    raw_canonical = "_".join(words)
    if not raw_canonical:
        raw_canonical = "item"

    full_direct = f"{prefix}{raw_canonical}"
    if len(full_direct) <= max_chars:
        return full_direct
    else:
        # Long or similar text exceeds max_chars:
        # Append deterministic 6-character hash suffix of canonical text
        hash_suffix = hashlib.sha256(s.encode("utf-8")).hexdigest()[:6]
        avail = max(4, max_chars - len(prefix) - 7)
        base = raw_canonical[:avail].rstrip("_")
        return f"{prefix}{base}_{hash_suffix}"


def _memory(
    category: str,
    key: str,
    value: str,
    confidence: float = 1.0,
    action: str = "store",
) -> Memory:

    return {

        "category": category,

        "key": key,

        "value": value.strip(),

        "confidence": confidence,

        "action": action,
    }


def _match(
    patterns,
    text,
):

    for pattern in patterns:

        result = pattern.search(text)

        if result:

            return result

    return None


# ==========================================================
# EXTRACTION FUNCTIONS
# ==========================================================

def extract_name(text: str) -> List[Memory]:

    memories = []

    # The first two patterns ("my name is", "call me") are unambiguous --
    # if someone says that, it's a name, full stop. The "i am"/"i'm"
    # patterns are NOT unambiguous ("I am tired" matches the regex too),
    # so for those we additionally require the captured text to pass
    # _looks_like_name() before trusting it. Checking patterns individually
    # (rather than via the generic _match() helper) so we know which rule
    # fired and can apply the right level of scrutiny.

    unambiguous_patterns = NAME_PATTERNS[:2]
    ambiguous_patterns = NAME_PATTERNS[2:]

    match = _match(unambiguous_patterns, text)

    if match:
        value = match.group(1).strip().rstrip(".,;!?:")

        if len(value) >= 2:
            memories.append(_memory("identity", "name", value, 1.0))
            return memories

    match = _match(ambiguous_patterns, text)

    if match:
        value = match.group(1).strip().rstrip(".,;!?:")

        if len(value) >= 2 and _looks_like_name(value):
            # Slightly lower confidence than an explicit "my name is" --
            # this is still an inference, just a well-constrained one.
            memories.append(_memory("identity", "name", value, 0.8))

    return memories


def extract_birthday(text: str) -> List[Memory]:

    memories = []

    match = _match(
        BIRTHDAY_PATTERNS,
        text,
    )

    if not match:
        return memories

    memories.append(

        _memory(

            "identity",

            "birthday",

            match.group(1),

            1.0,
        )
    )

    return memories

# ==========================================================
# PREFERENCES
# ==========================================================

def extract_likes(text: str) -> List[Memory]:

    memories = []

    # 1. Contrastive preference: "I prefer coffee over tea"
    for p in CONTRASTIVE_PREF_PATTERNS:
        m = p.search(text)
        if m:
            preferred = m.group(1).strip().rstrip(".,;!")
            retired = m.group(2).strip().rstrip(".,;!")
            if len(preferred) >= 2:
                memories.append(_memory("preference", _slugify_key(preferred, prefix="like_"), preferred, 1.0, action="store"))
            if len(retired) >= 2:
                memories.append(_memory("preference", _slugify_key(retired, prefix="like_"), retired, 1.0, action="retire"))
            return memories

    # 2. Preference retirement: "I don't like tea anymore"
    for p in PREF_RETIREMENT_PATTERNS:
        m = p.search(text)
        if m:
            val = m.group(1).strip().rstrip(".,;!")
            if len(val) >= 2:
                memories.append(_memory("preference", _slugify_key(val, prefix="like_"), val, 1.0, action="retire"))
                return memories

    # 3. Standard preference statements
    for p in STANDARD_PREF_PATTERNS:
        m = p.search(text)
        if m:
            val = m.group(1).strip().rstrip(".,;!")
            if len(val) >= 2:
                memories.append(_memory("preference", _slugify_key(val, prefix="like_"), val, 1.0, action="store"))
                return memories

    return memories


def extract_favorites(text: str) -> List[Memory]:

    memories = []

    match = _match(
        FAVORITE_PATTERNS,
        text,
    )

    if not match:
        return memories

    item = match.group(1).strip().lower()

    value = match.group(2).strip().rstrip(".,;!?:")

    key = f"favorite_{item.replace(' ', '_')}"

    memories.append(

        _memory(

            "preference",

            key,

            value,

            1.0,
        )
    )

    return memories


# ==========================================================
# PROJECTS
# ==========================================================

def extract_projects(text: str) -> List[Memory]:

    memories = []

    # 1. Project retirement
    for p in PROJECT_RETIREMENT_PATTERNS:
        m = p.search(text)
        if m:
            val = m.group(1).strip().rstrip(".,;!")
            key = _slugify_key(val)
            memories.append(_memory("project", key, val, 1.0, action="retire"))
            return memories

    # 2. Project store
    for p in PROJECT_STORE_PATTERNS:
        m = p.search(text)
        if m:
            raw_val = m.group(1).strip()
            # Strip trailing sentence punctuation if unquoted and outside parens
            if raw_val.endswith((".", ";", "!")) and not raw_val.endswith(")"):
                raw_val = raw_val.rstrip(".,;!")
            if p == PROJECT_STORE_PATTERNS[1]:
                # Explicit "my current project is X" -> canonical focus key "current"
                memories.append(_memory("project", "current", raw_val, 1.0, action="store"))
            else:
                key = _slugify_key(raw_val)
                memories.append(_memory("project", key, raw_val, 1.0, action="store"))
            return memories

    return memories


# ==========================================================
# GOALS
# ==========================================================

def extract_goals(text: str) -> List[Memory]:

    memories = []

    match = _match(
        GOAL_PATTERNS,
        text,
    )

    if not match:
        return memories

    value = match.group(1).strip()

    if not _looks_like_goal(value):
        return memories

    key = _slugify_key(value, prefix="goal_")

    memories.append(

        _memory(

            "goal",

            key,

            value,

            0.95,
        )
    )

    return memories


# ==========================================================
# TOOLS / SOFTWARE
# ==========================================================

def extract_tools(text: str) -> List[Memory]:

    memories = []

    # 1. Contrastive tools: "Instead of Python, I use Go" / "I use Go instead of Python"
    for p in CONTRASTIVE_TOOL_PATTERNS:
        m = p.search(text)
        if m:
            if "instead of" in text.lower() and text.lower().startswith("instead"):
                retired = m.group(1).strip().rstrip(".,;!")
                stored = m.group(2).strip().rstrip(".,;!")
            else:
                stored = m.group(1).strip().rstrip(".,;!")
                retired = m.group(2).strip().rstrip(".,;!")
            if len(stored) >= 2:
                memories.append(_memory("tool", _slugify_key(stored, prefix="tool_"), stored, 0.9, action="store"))
            if len(retired) >= 2:
                memories.append(_memory("tool", _slugify_key(retired, prefix="tool_"), retired, 0.9, action="retire"))
            return memories

    # 2. Tool retirement: "I no longer use Python", "I stopped using Python"
    for p in TOOL_RETIREMENT_PATTERNS:
        m = p.search(text)
        if m:
            val = m.group(1).strip().rstrip(".,;!")
            val = re.sub(r"\s+anymore$", "", val, flags=re.IGNORECASE).strip()
            val = re.sub(r"\s+for\s+.+$", "", val, flags=re.IGNORECASE).strip()
            if len(val) >= 2:
                memories.append(_memory("tool", _slugify_key(val, prefix="tool_"), val, 0.9, action="retire"))
                return memories

    # 3. Standard tool store: "I use Python"
    for p in TOOL_STORE_PATTERNS:
        m = p.search(text)
        if m:
            if len(m.groups()) == 1:
                val = m.group(1).strip().rstrip(".,;!")
                if not re.search(r"\b(?:not|no|never|don't)\b", val, re.IGNORECASE):
                    key = _slugify_key(val, prefix="tool_")
                    memories.append(_memory("tool", key, val, 0.9, action="store"))
                    return memories
            else:
                category = m.group(1).strip().lower().replace(" ", "_")
                val = m.group(2).strip().rstrip(".,;!")
                key = f"preferred_{category}"
                memories.append(_memory("tool", key, val, 1.0, action="store"))
                return memories

    return memories


# ==========================================================
# RELATIONSHIPS
# ==========================================================

def extract_relationships(text: str) -> List[Memory]:

    memories = []

    match = _match(
        CREATOR_PATTERNS,
        text,
    )

    if not match:
        return memories

    memories.append(

        _memory(

            "relationship",

            "creator",

            "user",

            1.0,
        )
    )

    return memories


# ==========================================================
# DUPLICATE REMOVAL
# ==========================================================

def remove_duplicates(memories: List[Memory]) -> List[Memory]:

    seen = set()

    cleaned = []

    for memory in memories:

        identifier = (

            memory["category"],

            memory["key"],

            memory.get("action", "store"),

            memory["value"],
        )

        if identifier in seen:
            continue

        seen.add(identifier)

        cleaned.append(memory)

    return cleaned

# ==========================================================
# MAIN CLASSIFIER
# ==========================================================

EXTRACTORS = [

    extract_name,

    extract_birthday,

    extract_likes,

    extract_favorites,

    extract_projects,

    extract_goals,

    extract_tools,

    extract_relationships,

]


def classify_memory(text: str) -> List[Memory]:
    """
    Analyze a user message and return structured memories.

    Returns:
        List[Memory]

    Example:

    [
        {
            "category": "identity",
            "key": "name",
            "value": "Sayan Dey",
            "confidence": 1.0,
            "action": "store",
        }
    ]
    """

    text = text.strip()

    if not text:
        return []

    clauses = _split_clauses(text)
    memories: List[Memory] = []

    for clause in clauses:
        clause_str = clause.strip()
        if not clause_str or _is_uncertain_or_question(clause_str):
            continue

        for extractor in EXTRACTORS:
            try:
                result = extractor(clause_str)
                if result:
                    memories.extend(result)
            except Exception:
                continue

    memories = remove_duplicates(memories)

    memories.sort(
        key=lambda memory: (
            memory["category"],
            memory["key"],
            -memory["confidence"],
        )
    )

    return memories


# ==========================================================
# QUICK CHECK
# ==========================================================

def has_memory(text: str) -> bool:
    """
    Returns True if the sentence contains
    at least one long-term memory.
    """

    return len(classify_memory(text)) > 0


# ==========================================================
# PRETTY PRINT (Debugging)
# ==========================================================

def print_memories(memories: List[Memory]):

    if not memories:

        print("No memories detected.")

        return

    print()

    print("=" * 50)

    print("Detected Memories")

    print("=" * 50)

    for memory in memories:

        print()

        print(f"Category   : {memory['category']}")

        print(f"Key        : {memory['key']}")

        print(f"Value      : {memory['value']}")

        print(f"Confidence : {memory['confidence']}")

    print()

    print("=" * 50)


# ==========================================================
# SELF TEST
# ==========================================================

if __name__ == "__main__":

    samples = [

        "My name is Sayan Dey.",

        "I like Spider-Man.",

        "My favorite editor is VS Code.",

        "I am working on Orange AI.",

        "I want to build AGI.",

        "I use Python.",

        "I am your creator.",

        "My birthday is 8 April 1998.",

        "My name is Sayan and my favorite superhero is Spider-Man.",

    ]

    for sentence in samples:

        print()

        print("-" * 60)

        print(sentence)

        print("-" * 60)

        print_memories(classify_memory(sentence))