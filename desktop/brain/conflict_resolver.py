"""
brain/conflict_resolver.py - Stage 3.6C: Deterministic Cross-Category Memory Conflict Resolution.

Implements pure in-memory conflict resolution for conversational chat and agent mode:
- Canonical slot and entity extraction
- Singleton, multi-valued, and scoped fact classification
- 8-level deterministic precedence hierarchy
- Equivalent-fact deduplication
- Singleton contradiction resolution via recency and structured dominance
- Explicit correction and retraction handling
- Modernized legacy memory reconciliation
- Thread-safe session-only entity suppression registry (Tier 2)
- Structured resolution reporting for privacy-safe telemetry and tests

All retrieval and budgeting operations are strictly read-only; no database modifications are performed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ==========================================================
# ENUMS & DATA STRUCTURES
# ==========================================================

class ConflictRelation(str, Enum):
    EQUIVALENT = "equivalent"          # Same slot, semantically identical value -> deduplicate
    CONTRADICTORY = "contradictory"    # Singleton slot, mutually exclusive values -> resolve precedence
    SUPERSEDED = "superseded"          # Newer explicit record replaces older
    CONTEXTUAL = "contextual"          # Overlapping entity, disjoint scopes -> preserve both
    COEXISTING = "coexisting"          # Multi-valued non-conflicting facts -> preserve both
    INDEPENDENT = "independent"        # Distinct slots/entities -> preserve both


class SlotCardinality(str, Enum):
    SINGLETON = "singleton"            # Strict 1:1 (theme, preferred editor, current project, city, name)
    MULTI_VALUED = "multi_valued"      # 1:N (general tools, general likes, project portfolio)
    SCOPED = "scoped"                  # 1:1 per scope (tool per task, preference per time of day)


@dataclass(frozen=True)
class FactSlot:
    category: str                      # "identity", "project", "tool", "preference", "relationship"
    slot_name: str                     # Canonical slot identifier (e.g. "city", "theme", "current", "preferred_editor")
    cardinality: SlotCardinality
    scope: Optional[str] = None        # e.g. "ml", "backend", "morning"


@dataclass
class ConflictResolutionReport:
    total_candidates: int = 0
    resolved_count: int = 0
    suppressed_count: int = 0
    deduplicated_count: int = 0
    conflicts_resolved: List[Dict[str, Any]] = field(default_factory=list)
    dropped_items: List[Dict[str, Any]] = field(default_factory=list)


# ==========================================================
# SESSION-ONLY SUPPRESSION REGISTRY (TIER 2)
# ==========================================================

_SESSION_RETIRED_ENTITIES: Dict[str, Set[str]] = {
    "tool": set(),
    "project": set(),
    "preference": set(),
    "general": set(),
}


def register_session_retraction(category: str, entity_name: str) -> None:
    """
    Register an entity retraction in the in-memory session store (Tier 2).
    Purely in-memory; performs zero database writes.
    Suppresses matching cross-category mentions for the remainder of the process session.
    """
    cat_clean = category.strip().lower()
    name_clean = entity_name.strip().lower()
    if not name_clean:
        return
    # Strip common leading words for clean entity tracking
    for prefix in ("tool_", "like_", "project_", "tool ", "project ", "preference ", "like "):
        if name_clean.startswith(prefix) and len(name_clean) > len(prefix):
            name_clean = name_clean[len(prefix):].strip()
            break
    if cat_clean in _SESSION_RETIRED_ENTITIES:
        _SESSION_RETIRED_ENTITIES[cat_clean].add(name_clean)
    _SESSION_RETIRED_ENTITIES["general"].add(name_clean)


def unregister_session_retraction(category: str, entity_name: str) -> None:
    """
    Unregister an entity from session retractions if explicitly stored or restored.
    Ensures active structured memories are never suppressed by stale session retractions.
    """
    cat_clean = category.strip().lower()
    name_clean = entity_name.strip().lower()
    for prefix in ("tool_", "like_", "project_", "tool ", "project ", "preference ", "like "):
        if name_clean.startswith(prefix) and len(name_clean) > len(prefix):
            name_clean = name_clean[len(prefix):].strip()
            break
    if cat_clean in _SESSION_RETIRED_ENTITIES:
        _SESSION_RETIRED_ENTITIES[cat_clean].discard(name_clean)
    _SESSION_RETIRED_ENTITIES["general"].discard(name_clean)


def get_session_retractions(category: Optional[str] = None) -> Set[str]:
    """Retrieve active session retractions for a category or across all categories."""
    if category:
        cat_clean = category.strip().lower()
        return set(_SESSION_RETIRED_ENTITIES.get(cat_clean, set())) | set(_SESSION_RETIRED_ENTITIES.get("general", set()))
    combined: Set[str] = set()
    for s in _SESSION_RETIRED_ENTITIES.values():
        combined.update(s)
    return combined


def clear_session_retractions() -> None:
    """Deterministic reset of session retractions (for testing and session termination)."""
    for s in _SESSION_RETIRED_ENTITIES.values():
        s.clear()


def is_session_retired(text: str, category: Optional[str] = None) -> bool:
    """
    Check if text asserts or refers to an entity retired in the active session.
    Matches whole word boundaries only to prevent false positives (e.g. 'Go' vs 'Good' or 'Going').
    """
    retired_set = get_session_retractions(category)
    if not retired_set:
        return False

    text_lower = text.lower().strip()
    for entity in retired_set:
        e_clean = entity.strip().lower()
        if not e_clean or len(e_clean) < 2:
            continue
        # Use word-boundary matching so short entity names (e.g. 'go', 'git', 'c') don't match substrings
        pattern = rf"\b{re.escape(e_clean)}\b"
        if re.search(pattern, text_lower):
            return True

    return False


# ==========================================================
# TIMESTAMP PARSING & VALIDATION
# ==========================================================

def parse_iso_timestamp(ts: Optional[Any]) -> Optional[datetime]:
    """
    Safely parse an ISO 8601 timestamp string into a datetime object.
    Returns None if missing, empty, invalid type, or malformed.
    Guaranteed never to raise exceptions.
    """
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.strip())
    except (ValueError, TypeError, OverflowError):
        return None


# ==========================================================
# VALUE NORMALIZATION & SCOPE PARSING
# ==========================================================

_LEADING_PREFIX_PATTERN = re.compile(
    r"^(?:i\s+use|i\s+prefer|i\s+like|i\s+love|my\s+favorite\s+\w+\s+is|working\s+on|"
    r"i\s+am\s+working\s+on|i'm\s+working\s+on|my\s+name\s+is|call\s+me|i\s+live\s+in|"
    r"i\s+reside\s+in|i\s+am\s+living\s+in|i'm\s+living\s+in|my\s+current\s+project\s+is|"
    r"my\s+birthday\s+is|i\s+was\s+born\s+on|my\s+preferred\s+\w+\s+is)\s+",
    re.IGNORECASE,
)

_THEME_DARK_PATTERN = re.compile(r"\b(?:dark\s+mode|dark\s+theme|dark)\b", re.IGNORECASE)
_THEME_LIGHT_PATTERN = re.compile(r"\b(?:light\s+mode|light\s+theme|light)\b", re.IGNORECASE)

_SCOPE_PATTERN = re.compile(
    r"\b(?:for|in|at|during|when)\s+([a-z0-9_\-\s]{2,30})$",
    re.IGNORECASE,
)


def normalize_semantic_value(text: str) -> str:
    """
    Normalize fact value for semantic comparison and deduplication.
    Strips conversational prefixes, punctuation, and extra whitespace.
    """
    raw = text.lower().strip()
    norm = _LEADING_PREFIX_PATTERN.sub("", raw)
    return norm.strip(" .,!?:;'\"")


def extract_scope_qualifier(text: str) -> Tuple[str, Optional[str]]:
    """
    Extract an optional task/time scope qualifier from a fact statement.
    e.g. 'Python for machine learning' -> ('Python', 'machine learning')
         'tea in the morning' -> ('tea', 'morning')
    Returns: (base_value, scope_or_none)
    """
    clean = text.strip()
    match = _SCOPE_PATTERN.search(clean)
    if match:
        scope = match.group(1).strip().lower()
        # Filter out common stop words / non-scopes
        if scope not in ("all", "now", "it", "me", "sure", "work"):
            base = clean[:match.start()].strip()
            if len(base) >= 2:
                return base, scope
    return clean, None


# ==========================================================
# CANONICAL FACT SLOT CLASSIFICATION
# ==========================================================

def extract_fact_slot(item: Dict[str, Any]) -> FactSlot:
    """
    Map a candidate memory item (dict or MemoryBudgetItem) to its canonical FactSlot and cardinality.
    Supports both structured memories and legacy text notes.
    """
    cat = (item.get("category") or "").strip().lower()
    key = (item.get("key") or "").strip().lower()
    val = (item.get("value") or item.get("content") or "").strip()
    src = item.get("source", "structured")

    val_lower = val.lower()
    base_val, scope = extract_scope_qualifier(val)

    # 1. Identity Category (Strict Singletons)
    is_identity_legacy = (src == "legacy" and (
        any(val_lower.startswith(p) for p in ("my name is", "call me", "my birthday is", "i was born on"))
        or bool(re.search(r"\b(?:i\s+(?:currently\s+)?live\s+in|(?:i\s+am|i'm)\s+(?:currently\s+)?living\s+in|i\s+reside\s+in)\b", val_lower))
    ))
    if cat == "identity" or is_identity_legacy:
        if key == "name" or val_lower.startswith("my name is") or val_lower.startswith("call me"):
            return FactSlot("identity", "name", SlotCardinality.SINGLETON)
        if key in ("city", "location", "residence") or re.search(r"\b(?:live\s+in|reside\s+in|living\s+in)\b", val_lower):
            # Check for transient travel indicators ("this week", "visiting", "temporarily", "vacation")
            if re.search(r"\b(?:this\s+week|visiting|temporarily|vacation|trip|staying\s+in)\b", val_lower):
                return FactSlot("identity", "transient_location", SlotCardinality.SCOPED, scope="temporary")
            return FactSlot("identity", "city", SlotCardinality.SINGLETON)
        if key == "birthday" or val_lower.startswith("my birthday is") or val_lower.startswith("i was born on"):
            return FactSlot("identity", "birthday", SlotCardinality.SINGLETON)
        if key == "creator" or "creator" in val_lower:
            return FactSlot("identity", "creator", SlotCardinality.SINGLETON)
        return FactSlot("identity", key or "general_identity", SlotCardinality.SINGLETON)

    # 2. Relationship Category
    if cat == "relationship" or (src == "legacy" and "creator" in val_lower):
        return FactSlot("relationship", key or "creator", SlotCardinality.SINGLETON)

    # 3. Project Category
    if cat == "project" or (src == "legacy" and ("working on" in val_lower or "current project" in val_lower)):
        if key == "current" or "my current project is" in val_lower or val_lower.startswith("current project:"):
            return FactSlot("project", "current", SlotCardinality.SINGLETON)
        slug = key or re.sub(r"[^a-z0-9]+", "_", base_val.lower()).strip("_")
        return FactSlot("project", f"project_{slug}", SlotCardinality.MULTI_VALUED)

    # 4. Tool Category
    if cat == "tool" or (src == "legacy" and ("i use " in val_lower or "my preferred " in val_lower)):
        if key.startswith("preferred_") or "my preferred " in val_lower:
            role = key.replace("preferred_", "") if key.startswith("preferred_") else "tool"
            m = re.search(r"my preferred (\w+) is", val_lower)
            if m:
                role = m.group(1).lower()
            return FactSlot("tool", f"preferred_{role}", SlotCardinality.SINGLETON)
        if scope:
            return FactSlot("tool", f"tool_{re.sub(r'[^a-z0-9]+', '_', base_val.lower()).strip('_')}", SlotCardinality.SCOPED, scope=scope)
        slug = key.replace("tool_", "") if key.startswith("tool_") else re.sub(r"[^a-z0-9]+", "_", val_lower).strip("_")
        return FactSlot("tool", f"tool_{slug}", SlotCardinality.MULTI_VALUED)

    # 5. Preference Category
    if cat == "preference" or (src == "legacy" and any(p in val_lower for p in ("like ", "love ", "prefer ", "favorite ", "dark mode", "light mode"))):
        # UI Theme check
        if key in ("theme", "ui_theme") or _THEME_DARK_PATTERN.search(val_lower) or _THEME_LIGHT_PATTERN.search(val_lower):
            return FactSlot("preference", "theme", SlotCardinality.SINGLETON)
        if key.startswith("favorite_") or "favorite" in val_lower:
            item_name = key.replace("favorite_", "") if key.startswith("favorite_") else "item"
            m = re.search(r"my favorite (\w+) is", val_lower)
            if m:
                item_name = m.group(1).lower()
            return FactSlot("preference", f"favorite_{item_name}", SlotCardinality.SINGLETON)
        if scope:
            return FactSlot("preference", f"pref_{re.sub(r'[^a-z0-9]+', '_', base_val.lower()).strip('_')}", SlotCardinality.SCOPED, scope=scope)
        slug = key.replace("like_", "") if key.startswith("like_") else re.sub(r"[^a-z0-9]+", "_", val_lower).strip("_")
        return FactSlot("preference", f"like_{slug}", SlotCardinality.MULTI_VALUED)

    # 6. Default / General Fallback
    return FactSlot(cat or "general", key or "general_fact", SlotCardinality.MULTI_VALUED)


# ==========================================================
# PAIRWISE RELATION CLASSIFIER
# ==========================================================

def classify_relation(
    item_a: Dict[str, Any],
    item_b: Dict[str, Any],
) -> Tuple[ConflictRelation, Optional[FactSlot]]:
    """
    Classify the semantic relationship between two candidate facts.
    Returns: (ConflictRelation, FactSlot)
    """
    slot_a = extract_fact_slot(item_a)
    slot_b = extract_fact_slot(item_b)

    val_a = normalize_semantic_value(item_a.get("value") or item_a.get("content") or "")
    val_b = normalize_semantic_value(item_b.get("value") or item_b.get("content") or "")

    # Check 1: Identical normalized value
    if val_a == val_b and val_a:
        # Same slot or compatible category -> EQUIVALENT
        if slot_a.category == slot_b.category or slot_a.slot_name == slot_b.slot_name:
            return ConflictRelation.EQUIVALENT, slot_a
        # Cross-category identical entity (e.g. tool 'docker' and preference 'docker')
        return ConflictRelation.EQUIVALENT, slot_a

    # Check 2: Different values for the same slot
    if slot_a.category == slot_b.category and slot_a.slot_name == slot_b.slot_name:
        # Singleton slot with different values -> CONTRADICTORY
        if slot_a.cardinality == SlotCardinality.SINGLETON:
            return ConflictRelation.CONTRADICTORY, slot_a
        # Scoped slot
        if slot_a.cardinality == SlotCardinality.SCOPED:
            if slot_a.scope == slot_b.scope:
                return ConflictRelation.CONTRADICTORY, slot_a
            return ConflictRelation.CONTEXTUAL, slot_a
        # Multi-valued slot
        if slot_a.cardinality == SlotCardinality.MULTI_VALUED:
            return ConflictRelation.COEXISTING, slot_a

    # Check 3: Scoped context with different scopes
    if slot_a.scope and slot_b.scope and slot_a.scope != slot_b.scope:
        if slot_a.slot_name == slot_b.slot_name:
            return ConflictRelation.CONTEXTUAL, slot_a

    # Check 4: Cross-category theme check
    if slot_a.slot_name == "theme" and slot_b.slot_name == "theme":
        return ConflictRelation.CONTRADICTORY, slot_a

    return ConflictRelation.INDEPENDENT, None


# ==========================================================
# PRECEDENCE RESOLUTION ENGINE (LEVELS 0-7)
# ==========================================================

def compare_singleton_precedence(
    item_a: Dict[str, Any],
    item_b: Dict[str, Any],
    active_turn: Optional[str] = None,
) -> int:
    """
    Compare two contradictory candidates for the same Singleton slot using the 8-Level Precedence Policy.
    Returns:
       1 if item_a wins
      -1 if item_b wins
       0 if tied
    """
    val_a = normalize_semantic_value(item_a.get("value") or item_a.get("content") or "")
    val_b = normalize_semantic_value(item_b.get("value") or item_b.get("content") or "")

    # Level 0 & Level 2: Active turn assertion dominance
    if active_turn and isinstance(active_turn, str):
        from brain.memory_classifier import _is_uncertain_or_question
        # Only assertive statements can dominate (Level 0 exclusion of questions)
        if not _is_uncertain_or_question(active_turn):
            turn_norm = normalize_semantic_value(active_turn)
            a_in_turn = val_a in turn_norm
            b_in_turn = val_b in turn_norm
            if a_in_turn and not b_in_turn:
                return 1
            if b_in_turn and not a_in_turn:
                return -1

    # Level 3: Structured Knowledge Dominance over Legacy
    src_a = item_a.get("source", "structured")
    src_b = item_b.get("source", "structured")
    if src_a == "structured" and src_b == "legacy":
        return 1
    if src_b == "structured" and src_a == "legacy":
        return -1

    # Level 5: Timestamp Recency for Singleton Slots
    ts_a = parse_iso_timestamp(item_a.get("updated_at") or item_a.get("created_at"))
    ts_b = parse_iso_timestamp(item_b.get("updated_at") or item_b.get("created_at"))

    if ts_a and ts_b:
        if ts_a > ts_b:
            return 1
        if ts_b > ts_a:
            return -1
    elif ts_a and not ts_b:
        return 1
    elif ts_b and not ts_a:
        return -1

    # Level 7: Deterministic Tie-Breaking (Relevance Score, ID, Key)
    score_a = float(item_a.get("score") or item_a.get("relevance_score") or 0.0)
    score_b = float(item_b.get("score") or item_b.get("relevance_score") or 0.0)
    if score_a > score_b:
        return 1
    if score_b > score_a:
        return -1

    id_a = int(item_a.get("id") or 0)
    id_b = int(item_b.get("id") or 0)
    if id_a > id_b:
        return 1
    if id_b > id_a:
        return -1

    key_a = item_a.get("key") or ""
    key_b = item_b.get("key") or ""
    if key_a < key_b:
        return 1
    if key_b < key_a:
        return -1

    return 0


# ==========================================================
# PUBLIC IN-MEMORY CONFLICT RESOLUTION PIPELINE
# ==========================================================

def resolve_memory_conflicts(
    candidates: List[Any],
    query: Optional[str] = None,
    active_turn: Optional[str] = None,
) -> Tuple[List[Any], ConflictResolutionReport]:
    """
    Pure in-memory conflict resolution pipeline implementing the 8-Level Precedence Hierarchy.
    Operates on candidate memory items (dict or MemoryBudgetItem).
    - Excludes uncertain queries (Level 0).
    - Applies session retractions (Level 1 / Tier 2).
    - Resolves Singleton contradictions via recency and structured dominance (Levels 3, 5).
    - Preserves multi-valued coexistence (Level 6).
    - Returns non-conflicting facts in deterministic order and a detailed report.
    - GUARANTEED PURE: Exactly 0 SQLite writes.
    """
    report = ConflictResolutionReport(total_candidates=len(candidates))
    if not candidates:
        return [], report

    # 1. Normalize items to dict view while preserving original objects
    item_wrappers: List[Tuple[Any, Dict[str, Any]]] = []
    for cand in candidates:
        if isinstance(cand, dict):
            d = dict(cand)
        else:
            # MemoryBudgetItem or other dataclass
            d = {
                "source": getattr(cand, "source", "structured"),
                "category": getattr(cand, "category", ""),
                "key": getattr(cand, "key", ""),
                "value": getattr(cand, "value", ""),
                "priority_tier": getattr(cand, "priority_tier", 4),
                "relevance_score": getattr(cand, "relevance_score", 0.0),
                "id": getattr(cand, "id", None),
                "confidence": getattr(cand, "confidence", 1.0),
                "created_at": getattr(cand, "created_at", None),
                "updated_at": getattr(cand, "updated_at", None),
            }
        item_wrappers.append((cand, d))

    # 2. Filter session retractions (Level 1 / Tier 2)
    # Applies to legacy notes and cross-category mentions for entities retired in this session
    active_candidates: List[Tuple[Any, Dict[str, Any]]] = []
    for orig, d in item_wrappers:
        val_text = d.get("value") or d.get("content") or ""
        key_text = d.get("key") or ""
        cat = d.get("category", "")
        if is_session_retired(val_text) or (key_text and is_session_retired(key_text)):
            report.suppressed_count += 1
            report.dropped_items.append({
                "category": cat,
                "key": d.get("key"),
                "value": val_text,
                "reason": "session_retired",
            })
            continue
        active_candidates.append((orig, d))

    # 3. Group candidates by FactSlot and resolve Singleton conflicts
    singleton_winners: Dict[Tuple[str, str], Tuple[Any, Dict[str, Any]]] = {}
    resolved_candidates: List[Tuple[Any, Dict[str, Any]]] = []
    seen_semantic_values: Set[Tuple[str, str]] = set()

    for orig, d in active_candidates:
        slot = extract_fact_slot(d)
        norm_val = normalize_semantic_value(d.get("value") or d.get("content") or "")

        if slot.cardinality == SlotCardinality.SINGLETON:
            slot_id = (slot.category, slot.slot_name)
            if slot_id not in singleton_winners:
                singleton_winners[slot_id] = (orig, d)
            else:
                existing_orig, existing_d = singleton_winners[slot_id]
                existing_val = normalize_semantic_value(existing_d.get("value") or existing_d.get("content") or "")

                # Check equivalence vs contradiction
                if norm_val == existing_val:
                    # Deduplicate: prefer structured over legacy
                    report.deduplicated_count += 1
                    cmp_res = compare_singleton_precedence(d, existing_d, active_turn=active_turn)
                    if cmp_res > 0:
                        singleton_winners[slot_id] = (orig, d)
                else:
                    # Contradiction: apply precedence rules
                    report.suppressed_count += 1
                    cmp_res = compare_singleton_precedence(d, existing_d, active_turn=active_turn)
                    loser = existing_d if cmp_res > 0 else d
                    winner = d if cmp_res > 0 else existing_d
                    report.conflicts_resolved.append({
                        "slot": f"{slot.category}.{slot.slot_name}",
                        "winner": winner.get("value") or winner.get("content"),
                        "suppressed": loser.get("value") or loser.get("content"),
                        "reason": "singleton_precedence",
                    })
                    report.dropped_items.append({
                        "category": loser.get("category"),
                        "key": loser.get("key"),
                        "value": loser.get("value") or loser.get("content"),
                        "reason": "singleton_superseded",
                    })
                    if cmp_res > 0:
                        singleton_winners[slot_id] = (orig, d)
        else:
            # Multi-valued or Scoped slots: deduplicate exact semantic values
            val_id = (slot.category, norm_val)
            if val_id in seen_semantic_values:
                report.deduplicated_count += 1
                report.dropped_items.append({
                    "category": slot.category,
                    "key": d.get("key"),
                    "value": d.get("value") or d.get("content"),
                    "reason": "duplicate_value",
                })
                continue
            seen_semantic_values.add(val_id)
            resolved_candidates.append((orig, d))

    # Add all winning singletons
    for orig, d in singleton_winners.values():
        resolved_candidates.append((orig, d))

    # 4. Maintain original deterministic candidate ordering
    orig_indices = {id(cand): i for i, cand in enumerate(candidates)}
    resolved_candidates.sort(key=lambda pair: orig_indices.get(id(pair[0]), 0))

    final_items = [orig for orig, _ in resolved_candidates]
    report.resolved_count = len(final_items)

    return final_items, report


# ==========================================================
# MODERNIZED LEGACY RECONCILER
# ==========================================================

def is_legacy_reconciled(
    content: str,
    active_structured: List[Dict[str, Any]],
    retired_entities: Optional[Set[str]] = None,
) -> bool:
    """
    Modernized replacement for _is_legacy_conflict().
    Determines whether a legacy memory string should be suppressed against active structured memories.
    Suppresses:
    1. Assertions of entities retired in the active session.
    2. Contradictory assertions for Singleton slots (city, name, birthday, editor, theme, current project).
    3. Exact semantic duplicates of active structured facts.
    Preserves:
    - Multi-valued coexisting facts (tools, portfolio projects, varied likes).
    - Third-party notes, family, and historical background context.
    - Questions and uncertain statements.
    """
    clean_content = content.strip()
    if not clean_content:
        return True

    lower = clean_content.lower()

    # Step 1: Session retirement suppression
    if retired_entities or get_session_retractions():
        active_retired = retired_entities or get_session_retractions()
        for ent in active_retired:
            e_clean = ent.strip().lower()
            if e_clean and len(e_clean) >= 2:
                if re.search(rf"\b{re.escape(e_clean)}\b", lower):
                    return True

    # Step 2: Build slot lookup table for active structured memories
    structured_singletons: Dict[Tuple[str, str], str] = {}
    structured_semantic_values: Set[str] = set()

    for s in active_structured:
        s_slot = extract_fact_slot(s)
        s_val = normalize_semantic_value(s.get("value", ""))
        structured_semantic_values.add(s_val)
        if s_slot.cardinality == SlotCardinality.SINGLETON:
            structured_singletons[(s_slot.category, s_slot.slot_name)] = s_val

    # Step 3: Exact semantic duplicate suppression
    legacy_norm = normalize_semantic_value(clean_content)
    if legacy_norm in structured_semantic_values:
        return True

    # Step 4: Singleton contradiction suppression
    legacy_slot = extract_fact_slot({"content": clean_content, "source": "legacy"})
    if legacy_slot.cardinality == SlotCardinality.SINGLETON:
        slot_key = (legacy_slot.category, legacy_slot.slot_name)
        if slot_key in structured_singletons:
            active_val = structured_singletons[slot_key]
            if legacy_norm != active_val:
                # 4a. Identity City: Only suppress first-person residence claims
                if legacy_slot.slot_name == "city":
                    if re.search(
                        r"\b(?:i\s+(?:currently\s+)?live\s+in|(?:i\s+am|i'm)\s+(?:currently\s+)?living\s+in|i\s+reside\s+in)\b",
                        lower,
                    ):
                        if active_val not in lower:
                            return True
                    return False

                # 4b. Identity Name: Only suppress first-person name claims
                if legacy_slot.slot_name == "name":
                    if lower.startswith("my name is ") or lower.startswith("call me "):
                        if active_val not in lower:
                            return True
                    return False

                # 4c. Primary Project: "my current project is X"
                if legacy_slot.slot_name == "current":
                    if "current project" in lower:
                        if active_val not in lower:
                            return True

                # 4d. Preferred Tool: "my preferred X is Y"
                if legacy_slot.slot_name.startswith("preferred_"):
                    if "preferred" in lower or "favorite" in lower:
                        if active_val not in lower:
                            return True

                # 4e. Birthday: "my birthday is X"
                if legacy_slot.slot_name == "birthday":
                    if "birthday" in lower or "born on" in lower:
                        if active_val not in lower:
                            return True

                # 4f. UI Theme: dark vs light
                if legacy_slot.slot_name == "theme":
                    if _THEME_DARK_PATTERN.search(lower) and _THEME_LIGHT_PATTERN.search(active_val):
                        return True
                    if _THEME_LIGHT_PATTERN.search(lower) and _THEME_DARK_PATTERN.search(active_val):
                        return True

    # Step 5: Literal duplicates (Stage 3.4/3.5 baseline compatibility)
    for s in active_structured:
        v = s.get("value", "").lower().strip()
        if v and lower in (f"my name is {v}", f"i like {v}", f"i use {v}", f"my preferred tool is {v}"):
            return True

    return False
