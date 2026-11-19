"""
brain/context_budget.py - Stage 3.6B: Deterministic Memory Context Budgeting & Compaction.

Implements bounded, deterministic memory context budgeting for conversational chat and agent mode.
Ceilings are enforced via a 5-tier priority hierarchy:
  Tier 1: Core Identity (IDENTITY, RELATIONSHIP)
  Tier 2: Active Project (PROJECT)
  Tier 3: Query-Relevant Facts (Relevance-ranked structured and legacy facts)
  Tier 4: General Preferences / Tools / Goals / Other Structured Facts
  Tier 5: Recent Reconciled Legacy Memories

All operations are strictly read-only and in-memory; no database modifications are performed.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Set, Tuple
import logging
import re
import sqlite3

logger = logging.getLogger(__name__)

DEFAULT_CHAT_BUDGET_CHARS: int = 1500
DEFAULT_AGENT_BUDGET_CHARS: int = 1200


@dataclass
class MemoryBudgetItem:
    source: str               # "structured" | "legacy"
    category: str             # "IDENTITY", "PROJECT", "PREFERENCE", "TOOL", "GOAL", "LEGACY", etc.
    key: str                  # e.g., "name", "city", "current", or "" for legacy
    value: str                # fact content
    priority_tier: int        # 1 to 5
    relevance_score: float = 0.0
    id: Optional[int] = None
    confidence: float = 1.0


@dataclass
class BudgetResult:
    formatted_profile: str
    formatted_legacy: str
    total_chars: int
    max_chars: int
    included_count: int
    dropped_count: int
    overflow: bool
    selected_items: List[Dict[str, Any]] = field(default_factory=list)
    dropped_items: List[Dict[str, Any]] = field(default_factory=list)
    agent_relevant_memories: List[Dict[str, Any]] = field(default_factory=list)
    combined_rendered_chars: int = 0


def render_structured_profile(items: List[MemoryBudgetItem]) -> str:
    """
    Format a list of structured items identically to memory.build_profile().
    Groups by category (uppercased header) and sorts deterministically by category, key, value.
    """
    if not items:
        return ""
    sorted_items = sorted(
        items,
        key=lambda it: (it.category.upper(), it.key.lower(), it.value.lower(), it.id or 0)
    )
    profile_lines = []
    current_category = None
    for it in sorted_items:
        cat_upper = it.category.upper()
        if cat_upper != current_category:
            current_category = cat_upper
            profile_lines.append(f"\n[{cat_upper}]")
        profile_lines.append(f"{it.key}: {it.value}")
    return "\n".join(profile_lines)


def render_legacy_memories(items: List[MemoryBudgetItem]) -> str:
    """
    Format a list of legacy items with '- ' bullet prefixes joined by newlines.
    Preserves selection order (Tier 3 query-relevant first, Tier 5 recent next).
    """
    if not items:
        return ""
    return "\n".join(f"- {it.value}" for it in items)


def render_agent_relevant_lines(items: List[Dict[str, Any]]) -> str:
    """
    Format a list of agent relevant memory dicts identically to format_decision_prompt().
    """
    if not items:
        return ""
    mem_lines = []
    for m in items:
        cat = m.get("category", "")
        k = m.get("key", "")
        v = m.get("value", "")
        if cat and k:
            mem_lines.append(f"- [{cat}] {k}: {v}")
        elif "content" in m:
            mem_lines.append(f"- {m['content']}")
        else:
            mem_lines.append(f"- {m}")
    return "\n".join(mem_lines)


def safe_bounded_fallback_profile(
    max_chars: int = DEFAULT_AGENT_BUDGET_CHARS,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """
    Safely produce a bounded structured profile within max_chars on failure paths.
    Guarantees no mid-string truncation (retains whole lines) and never exceeds max_chars.
    If database access or preparation fails, safely returns an empty string without raising.
    """
    try:
        from brain import memory
        if conn is None:
            conn = memory.get_connection()
        cursor = conn.execute(
            """
            SELECT category, memory_key, value
            FROM structured_memories
            ORDER BY category, memory_key
            """
        )
        rows = cursor.fetchall()
        if not rows:
            return ""

        profile_lines: List[str] = []
        current_category = None
        current_len = 0

        for cat, key, val in rows:
            cat_upper = cat.upper()
            header_line = f"\n[{cat_upper}]" if cat_upper != current_category else None
            val_line = f"{key}: {val}"

            add_len = (len(header_line) + 1 if header_line else 0) + len(val_line) + (1 if profile_lines else 0)
            if current_len + add_len > max_chars:
                break

            if header_line:
                current_category = cat_upper
                profile_lines.append(header_line)
                current_len += len(header_line) + (1 if len(profile_lines) > 1 else 0)

            profile_lines.append(val_line)
            current_len += len(val_line) + (1 if len(profile_lines) > 1 else 0)

        return "\n".join(profile_lines)
    except Exception as e:
        logger.warning("Safe bounded fallback profile generation failed: %s", e)
        return ""


def normalize_fact_text(text: str) -> str:
    """
    Normalize fact text for duplicate detection, matching Stage 3.6A retrieval normalization.
    """
    raw = text.lower().strip()
    norm = re.sub(
        r"^(?:i\s+use|i\s+prefer|i\s+like|my\s+favorite\s+\w+\s+is|working\s+on|i\s+am\s+working\s+on|my\s+name\s+is|i\s+live\s+in)\s+",
        "",
        raw,
    )
    return norm.strip(" .,!?:;")


class ContextBudgetManager:
    """
    Deterministic memory context budget manager.
    Enforces character-based ceilings through a 5-tier priority hierarchy.
    """

    def __init__(self, max_chars: int = DEFAULT_CHAT_BUDGET_CHARS):
        self.max_chars = max(0, int(max_chars))

    def budget(
        self,
        query: Optional[str] = None,
        conn: Optional[sqlite3.Connection] = None,
        max_legacy_items: int = 20,
    ) -> BudgetResult:
        """
        Produce a budgeted, compacted context for conversational chat (main.py).
        """
        from brain import memory

        if conn is None:
            conn = memory.get_connection()

        # 1. Fetch active structured memories
        cursor = conn.execute(
            """
            SELECT id, category, memory_key, value, confidence, created_at, updated_at
            FROM structured_memories
            ORDER BY category, memory_key
            """
        )
        structured_rows = cursor.fetchall()

        # Build active_by_cat for Stage 3.4/3.5 reconciliation suppression
        active_by_cat: Dict[str, List[Tuple[str, str]]] = {}
        structured_norm_values: Set[str] = set()

        for row in structured_rows:
            cat = row[1].lower()
            k = row[2].lower()
            v = row[3].strip()
            active_by_cat.setdefault(cat, []).append((k, v.lower()))
            structured_norm_values.add(normalize_fact_text(v))

        # 2. Query relevance search (Tier 3 candidate pool)
        query_relevant_items: List[Dict[str, Any]] = []
        if query and query.strip():
            try:
                query_relevant_items = memory.retrieve_relevant_context(
                    query=query.strip(),
                    limit=15,
                )
            except Exception as e:
                logger.warning("Error retrieving relevant memories for budget: %s", e)

        # Index query-relevant structured memories by (category.lower(), key.lower())
        relevant_structured_map: Dict[Tuple[str, str], float] = {}
        for r in query_relevant_items:
            src = r.get("source") or ("structured" if "key" in r else "legacy")
            if src == "structured":
                c = r.get("category", "").lower()
                k = r.get("key", "").lower()
                relevant_structured_map[(c, k)] = float(r.get("score", 1.0))

        # 3. Partition structured memories into Tiers
        tier1_items: List[MemoryBudgetItem] = []
        tier2_items: List[MemoryBudgetItem] = []
        tier3_items: List[MemoryBudgetItem] = []
        tier4_items: List[MemoryBudgetItem] = []

        seen_structured_keys: Set[Tuple[str, str]] = set()

        for row in structured_rows:
            rec_id, cat, key, val, conf, created_at, updated_at = row
            cat_lower = cat.lower()
            key_lower = key.lower()
            val_clean = val.strip()
            seen_structured_keys.add((cat_lower, key_lower))

            rel_score = relevant_structured_map.get((cat_lower, key_lower), 0.0)

            if cat_lower in ("identity", "relationship"):
                tier1_items.append(
                    MemoryBudgetItem(
                        source="structured",
                        category=cat,
                        key=key,
                        value=val_clean,
                        priority_tier=1,
                        relevance_score=rel_score,
                        id=rec_id,
                        confidence=float(conf or 1.0),
                    )
                )
            elif cat_lower == "project":
                tier2_items.append(
                    MemoryBudgetItem(
                        source="structured",
                        category=cat,
                        key=key,
                        value=val_clean,
                        priority_tier=2,
                        relevance_score=rel_score,
                        id=rec_id,
                        confidence=float(conf or 1.0),
                    )
                )
            elif rel_score > 0.0:
                tier3_items.append(
                    MemoryBudgetItem(
                        source="structured",
                        category=cat,
                        key=key,
                        value=val_clean,
                        priority_tier=3,
                        relevance_score=rel_score,
                        id=rec_id,
                        confidence=float(conf or 1.0),
                    )
                )
            else:
                tier4_items.append(
                    MemoryBudgetItem(
                        source="structured",
                        category=cat,
                        key=key,
                        value=val_clean,
                        priority_tier=4,
                        relevance_score=0.0,
                        id=rec_id,
                        confidence=float(conf or 1.0),
                    )
                )

        # 4. Partition legacy memories and query-relevant items
        seen_legacy_norms: Set[str] = set()
        seen_legacy_ids: Set[int] = set()

        for r in query_relevant_items:
            src = r.get("source") or ("structured" if "key" in r else "legacy")
            if src == "structured":
                c = r.get("category", "").lower()
                k = r.get("key", "").lower()
                val = (r.get("value") or "").strip()
                if (c, k) not in seen_structured_keys:
                    seen_structured_keys.add((c, k))
                    tier3_items.append(
                        MemoryBudgetItem(
                            source="structured",
                            category=r.get("category", "GENERAL"),
                            key=r.get("key", ""),
                            value=val,
                            priority_tier=3,
                            relevance_score=float(r.get("score", 1.0)),
                            id=r.get("id"),
                            confidence=float(r.get("confidence", 1.0)),
                        )
                    )
            elif src == "legacy":
                content = (r.get("content") or r.get("value") or "").strip()
                if not content:
                    continue
                # Reconciliation conflict suppression
                if memory._is_legacy_conflict(content, active_by_cat):
                    continue
                # Semantic deduplication against structured memories
                norm_c = normalize_fact_text(content)
                if norm_c in structured_norm_values or norm_c in seen_legacy_norms:
                    continue
                seen_legacy_norms.add(norm_c)
                rec_id = r.get("id")
                if rec_id is not None:
                    seen_legacy_ids.add(rec_id)
                tier3_items.append(
                    MemoryBudgetItem(
                        source="legacy",
                        category="LEGACY",
                        key="",
                        value=content,
                        priority_tier=3,
                        relevance_score=float(r.get("score", 1.0)),
                        id=rec_id,
                    )
                )

        # Tier 5: Recent reconciled legacy memories
        tier5_items: List[MemoryBudgetItem] = []
        cursor = conn.execute(
            """
            SELECT id, content
            FROM memories
            ORDER BY id DESC
            LIMIT ?
            """,
            (max_legacy_items,),
        )
        for row in cursor.fetchall():
            rec_id, content = row
            content_clean = content.strip()
            if not content_clean or rec_id in seen_legacy_ids:
                continue
            if memory._is_legacy_conflict(content_clean, active_by_cat):
                continue
            norm_c = normalize_fact_text(content_clean)
            if norm_c in structured_norm_values or norm_c in seen_legacy_norms:
                continue
            seen_legacy_norms.add(norm_c)
            seen_legacy_ids.add(rec_id)
            tier5_items.append(
                MemoryBudgetItem(
                    source="legacy",
                    category="LEGACY",
                    key="",
                    value=content_clean,
                    priority_tier=5,
                    relevance_score=0.0,
                    id=rec_id,
                )
            )

        # 5. Deterministic sorting within each Tier
        tier1_items.sort(key=lambda it: (it.key.lower(), it.value.lower(), it.id or 0))
        tier2_items.sort(key=lambda it: (0 if it.key.lower() == "current" else 1, it.key.lower(), it.value.lower(), it.id or 0))
        tier3_items.sort(
            key=lambda it: (
                -it.relevance_score,
                0 if it.source == "structured" else 1,
                -(it.id or 0),
                it.key.lower() if it.key else it.value.lower(),
            )
        )
        tier4_items.sort(key=lambda it: (it.category.lower(), it.key.lower(), it.value.lower(), it.id or 0))
        tier5_items.sort(key=lambda it: -(it.id or 0))

        candidates: List[MemoryBudgetItem] = tier1_items + tier2_items + tier3_items + tier4_items + tier5_items

        # Stage 3.6C: Pure in-memory cross-category conflict resolution prior to greedy packing
        from brain.conflict_resolver import resolve_memory_conflicts
        resolved_candidates, conflict_report = resolve_memory_conflicts(
            candidates,
            query=query,
            active_turn=query,
        )

        # 6. Safe greedy packing within self.max_chars
        accepted_items: List[MemoryBudgetItem] = []
        dropped_items: List[Dict[str, Any]] = []
        overflow = False

        for dropped in conflict_report.dropped_items:
            dropped_items.append({
                "source": "structured" if dropped.get("category") != "LEGACY" else "legacy",
                "category": dropped.get("category", ""),
                "key": dropped.get("key", ""),
                "value": dropped.get("value", ""),
                "priority_tier": 4,
                "reason": dropped.get("reason", "conflict_suppressed"),
                "chars": len(str(dropped.get("value", ""))),
            })

        for cand in resolved_candidates:
            # Test cost of candidate alone
            cand_alone_profile = render_structured_profile([cand]) if cand.source == "structured" else ""
            cand_alone_legacy = render_legacy_memories([cand]) if cand.source == "legacy" else ""
            cand_alone_chars = len(cand_alone_profile) + len(cand_alone_legacy)

            if cand_alone_chars > self.max_chars:
                overflow = True
                dropped_items.append({
                    "source": cand.source,
                    "category": cand.category,
                    "key": cand.key,
                    "value": cand.value,
                    "priority_tier": cand.priority_tier,
                    "reason": "oversized_fact",
                    "chars": cand_alone_chars,
                })
                continue

            test_structured = [it for it in accepted_items if it.source == "structured"]
            test_legacy = [it for it in accepted_items if it.source == "legacy"]

            if cand.source == "structured":
                test_structured.append(cand)
            else:
                test_legacy.append(cand)

            test_profile_str = render_structured_profile(test_structured)
            test_legacy_str = render_legacy_memories(test_legacy)
            test_total_chars = len(test_profile_str) + (len(test_legacy_str) if test_legacy_str else 0)

            if test_total_chars <= self.max_chars:
                accepted_items.append(cand)
            else:
                overflow = True
                dropped_items.append({
                    "source": cand.source,
                    "category": cand.category,
                    "key": cand.key,
                    "value": cand.value,
                    "priority_tier": cand.priority_tier,
                    "reason": "budget_exhausted",
                    "chars": cand_alone_chars,
                })

        # 7. Final rendered context and diagnostics
        final_structured = [it for it in accepted_items if it.source == "structured"]
        final_legacy = [it for it in accepted_items if it.source == "legacy"]

        formatted_profile = render_structured_profile(final_structured)
        formatted_legacy = render_legacy_memories(final_legacy)
        total_chars = len(formatted_profile) + (len(formatted_legacy) if formatted_legacy else 0)

        selected_dicts = [
            {
                "source": it.source,
                "category": it.category,
                "key": it.key,
                "value": it.value,
                "priority_tier": it.priority_tier,
                "relevance_score": it.relevance_score,
                "id": it.id,
            }
            for it in accepted_items
        ]

        result = BudgetResult(
            formatted_profile=formatted_profile,
            formatted_legacy=formatted_legacy,
            total_chars=total_chars,
            max_chars=self.max_chars,
            included_count=len(accepted_items),
            dropped_count=len(dropped_items),
            overflow=overflow,
            selected_items=selected_dicts,
            dropped_items=dropped_items,
            combined_rendered_chars=total_chars,
        )

        logger.info(
            "Memory context budgeted: %d/%d chars (%.1f%%) | Included: %d | Dropped: %d | Overflow: %s",
            result.total_chars,
            result.max_chars,
            (result.total_chars / result.max_chars * 100.0) if result.max_chars > 0 else 0.0,
            result.included_count,
            result.dropped_count,
            result.overflow,
        )

        return result

    def budget_agent(
        self,
        goal: str,
        conn: Optional[sqlite3.Connection] = None,
        max_relevant_items: int = 5,
    ) -> BudgetResult:
        """
        Budget memory context for agent loop (brain/agent_context.py).
        Enforces:
        1. Zero fact duplication across user_profile and relevant_memories.
        2. Strict combined budget ceiling: len(user_profile) + len(relevant_memories) <= max_chars.
        3. Maximum of max_relevant_items in agent_relevant_memories (default 5).
        4. Deterministic priority and atomic omission.
        """
        from brain import memory

        if conn is None:
            conn = memory.get_connection()

        # 1. Fetch structured memories
        cursor = conn.execute(
            """
            SELECT id, category, memory_key, value, confidence, created_at, updated_at
            FROM structured_memories
            ORDER BY category, memory_key
            """
        )
        structured_rows = cursor.fetchall()

        active_by_cat: Dict[str, List[Tuple[str, str]]] = {}
        structured_norm_values: Set[str] = set()
        seen_structured_keys: Set[Tuple[str, str]] = set()

        for row in structured_rows:
            cat = row[1].lower()
            k = row[2].lower()
            v = row[3].strip()
            active_by_cat.setdefault(cat, []).append((k, v.lower()))
            structured_norm_values.add(normalize_fact_text(v))
            seen_structured_keys.add((cat, k))

        # 2. Query relevance search (Tier 3 candidate pool)
        query_relevant_items: List[Dict[str, Any]] = []
        if goal and goal.strip():
            try:
                query_relevant_items = memory.retrieve_relevant_context(
                    query=goal.strip(),
                    limit=15,
                )
            except Exception as e:
                logger.warning("Error retrieving relevant memories for agent budget: %s", e)

        # Index query-relevant structured memories by (category.lower(), key.lower())
        relevant_structured_map: Dict[Tuple[str, str], float] = {}
        for r in query_relevant_items:
            src = r.get("source") or ("structured" if "key" in r else "legacy")
            if src == "structured":
                c = r.get("category", "").lower()
                k = r.get("key", "").lower()
                relevant_structured_map[(c, k)] = float(r.get("score", 1.0))

        # 3. Partition into candidates
        tier1_profile: List[MemoryBudgetItem] = []
        tier2_profile: List[MemoryBudgetItem] = []
        tier3_relevant: List[MemoryBudgetItem] = []
        tier4_profile: List[MemoryBudgetItem] = []
        tier5_relevant: List[MemoryBudgetItem] = []

        for row in structured_rows:
            rec_id, cat, key, val, conf, created_at, updated_at = row
            cat_lower = cat.lower()
            key_lower = key.lower()
            val_clean = val.strip()

            rel_score = relevant_structured_map.get((cat_lower, key_lower), 0.0)

            if rel_score > 0.0:
                # Query-relevant structured fact -> destined for agent_relevant_memories!
                tier3_relevant.append(
                    MemoryBudgetItem(
                        source="structured",
                        category=cat,
                        key=key,
                        value=val_clean,
                        priority_tier=3,
                        relevance_score=rel_score,
                        id=rec_id,
                        confidence=float(conf or 1.0),
                    )
                )
            elif cat_lower in ("identity", "relationship"):
                tier1_profile.append(
                    MemoryBudgetItem(
                        source="structured",
                        category=cat,
                        key=key,
                        value=val_clean,
                        priority_tier=1,
                        relevance_score=0.0,
                        id=rec_id,
                        confidence=float(conf or 1.0),
                    )
                )
            elif cat_lower == "project":
                tier2_profile.append(
                    MemoryBudgetItem(
                        source="structured",
                        category=cat,
                        key=key,
                        value=val_clean,
                        priority_tier=2,
                        relevance_score=0.0,
                        id=rec_id,
                        confidence=float(conf or 1.0),
                    )
                )
            else:
                tier4_profile.append(
                    MemoryBudgetItem(
                        source="structured",
                        category=cat,
                        key=key,
                        value=val_clean,
                        priority_tier=4,
                        relevance_score=0.0,
                        id=rec_id,
                        confidence=float(conf or 1.0),
                    )
                )

        # External query-relevant structured memories (e.g. from mocks or new sources)
        for r in query_relevant_items:
            src = r.get("source") or ("structured" if "key" in r else "legacy")
            if src == "structured":
                c = r.get("category", "").lower()
                k = r.get("key", "").lower()
                val = (r.get("value") or "").strip()
                if (c, k) not in seen_structured_keys:
                    seen_structured_keys.add((c, k))
                    tier3_relevant.append(
                        MemoryBudgetItem(
                            source="structured",
                            category=r.get("category", "GENERAL"),
                            key=r.get("key", ""),
                            value=val,
                            priority_tier=3,
                            relevance_score=float(r.get("score", 1.0)),
                            id=r.get("id"),
                            confidence=float(r.get("confidence", 1.0)),
                        )
                    )

        # Legacy candidates for Tier 3 and Tier 5
        seen_legacy_norms: Set[str] = set()
        seen_legacy_ids: Set[int] = set()

        for r in query_relevant_items:
            src = r.get("source") or ("structured" if "key" in r else "legacy")
            if src == "legacy":
                content = (r.get("content") or r.get("value") or "").strip()
                if not content:
                    continue
                if memory._is_legacy_conflict(content, active_by_cat):
                    continue
                norm_c = normalize_fact_text(content)
                if norm_c in structured_norm_values or norm_c in seen_legacy_norms:
                    continue
                seen_legacy_norms.add(norm_c)
                rec_id = r.get("id")
                if rec_id is not None:
                    seen_legacy_ids.add(rec_id)
                tier3_relevant.append(
                    MemoryBudgetItem(
                        source="legacy",
                        category="LEGACY",
                        key="",
                        value=content,
                        priority_tier=3,
                        relevance_score=float(r.get("score", 1.0)),
                        id=rec_id,
                    )
                )

        # Tier 5: Recent reconciled legacy memories
        cursor = conn.execute(
            """
            SELECT id, content
            FROM memories
            ORDER BY id DESC
            LIMIT ?
            """,
            (20,),
        )
        for row in cursor.fetchall():
            rec_id, content = row
            content_clean = content.strip()
            if not content_clean or rec_id in seen_legacy_ids:
                continue
            if memory._is_legacy_conflict(content_clean, active_by_cat):
                continue
            norm_c = normalize_fact_text(content_clean)
            if norm_c in structured_norm_values or norm_c in seen_legacy_norms:
                continue
            seen_legacy_norms.add(norm_c)
            seen_legacy_ids.add(rec_id)
            tier5_relevant.append(
                MemoryBudgetItem(
                    source="legacy",
                    category="LEGACY",
                    key="",
                    value=content_clean,
                    priority_tier=5,
                    relevance_score=0.0,
                    id=rec_id,
                )
            )

        # Sort each tier deterministically
        tier1_profile.sort(key=lambda it: (it.key.lower(), it.value.lower(), it.id or 0))
        tier2_profile.sort(key=lambda it: (0 if it.key.lower() == "current" else 1, it.key.lower(), it.value.lower(), it.id or 0))
        tier3_relevant.sort(
            key=lambda it: (
                -it.relevance_score,
                0 if it.source == "structured" else 1,
                -(it.id or 0),
                it.key.lower() if it.key else it.value.lower(),
            )
        )
        tier4_profile.sort(key=lambda it: (it.category.lower(), it.key.lower(), it.value.lower(), it.id or 0))
        tier5_relevant.sort(key=lambda it: -(it.id or 0))

        accepted_profile: List[MemoryBudgetItem] = []
        accepted_relevant: List[MemoryBudgetItem] = []
        dropped_items: List[Dict[str, Any]] = []
        overflow = False

        candidate_queue: List[Tuple[str, MemoryBudgetItem]] = []
        for it in tier1_profile:
            candidate_queue.append(("profile", it))
        for it in tier3_relevant:
            candidate_queue.append(("relevant", it))
        for it in tier2_profile:
            candidate_queue.append(("profile", it))
        for it in tier4_profile:
            candidate_queue.append(("profile", it))
        for it in tier5_relevant:
            candidate_queue.append(("relevant", it))

        # Stage 3.6C: Pure in-memory cross-category conflict resolution for agent context
        from brain.conflict_resolver import resolve_memory_conflicts
        raw_cands = [cand for _, cand in candidate_queue]
        resolved_cands, c_report = resolve_memory_conflicts(raw_cands, query=goal, active_turn=goal)
        resolved_ids = {id(c) for c in resolved_cands}

        filtered_queue = [(dest, cand) for dest, cand in candidate_queue if id(cand) in resolved_ids]
        candidate_queue = filtered_queue

        for dest, cand in candidate_queue:
            if dest == "relevant" and len(accepted_relevant) >= max_relevant_items:
                continue

            # Calculate cost alone
            if dest == "profile":
                cand_alone_str = render_structured_profile([cand])
            else:
                cand_alone_str = render_agent_relevant_lines([{
                    "category": cand.category,
                    "key": cand.key,
                    "value": cand.value,
                    "content": cand.value if cand.source == "legacy" else None,
                }])
            cand_alone_chars = len(cand_alone_str)

            if cand_alone_chars > self.max_chars:
                overflow = True
                dropped_items.append({
                    "source": cand.source,
                    "category": cand.category,
                    "key": cand.key,
                    "value": cand.value,
                    "priority_tier": cand.priority_tier,
                    "reason": "oversized_fact",
                    "chars": cand_alone_chars,
                })
                continue

            test_prof = list(accepted_profile)
            test_rel = list(accepted_relevant)

            if dest == "profile":
                test_prof.append(cand)
            else:
                test_rel.append(cand)

            prof_str = render_structured_profile(test_prof)
            rel_dict_list = [
                (
                    {
                        "category": it.category,
                        "key": it.key,
                        "value": it.value,
                    }
                    if it.source == "structured"
                    else {
                        "id": it.id,
                        "content": it.value,
                        "score": it.relevance_score,
                        "source": "legacy",
                    }
                )
                for it in test_rel
            ]
            rel_str = render_agent_relevant_lines(rel_dict_list)

            test_combined_chars = len(prof_str) + (len(rel_str) if rel_str else 0)

            if test_combined_chars <= self.max_chars:
                if dest == "profile":
                    accepted_profile.append(cand)
                else:
                    accepted_relevant.append(cand)
            else:
                overflow = True
                dropped_items.append({
                    "source": cand.source,
                    "category": cand.category,
                    "key": cand.key,
                    "value": cand.value,
                    "priority_tier": cand.priority_tier,
                    "reason": "budget_exhausted",
                    "chars": cand_alone_chars,
                })

        formatted_profile = render_structured_profile(accepted_profile)
        agent_relevant_dicts = [
            (
                {
                    "category": it.category,
                    "key": it.key,
                    "value": it.value,
                    "score": it.relevance_score,
                }
                if it.source == "structured"
                else {
                    "id": it.id,
                    "content": it.value,
                    "score": it.relevance_score,
                    "source": "legacy",
                }
            )
            for it in accepted_relevant
        ]
        formatted_relevant_block = render_agent_relevant_lines(agent_relevant_dicts)
        combined_chars = len(formatted_profile) + (len(formatted_relevant_block) if formatted_relevant_block else 0)

        all_selected = accepted_profile + accepted_relevant
        selected_dicts = [
            {
                "source": it.source,
                "category": it.category,
                "key": it.key,
                "value": it.value,
                "priority_tier": it.priority_tier,
                "relevance_score": it.relevance_score,
                "id": it.id,
            }
            for it in all_selected
        ]

        result = BudgetResult(
            formatted_profile=formatted_profile,
            formatted_legacy=formatted_relevant_block,
            total_chars=combined_chars,
            max_chars=self.max_chars,
            included_count=len(all_selected),
            dropped_count=len(dropped_items),
            overflow=overflow,
            selected_items=selected_dicts,
            dropped_items=dropped_items,
            agent_relevant_memories=agent_relevant_dicts,
            combined_rendered_chars=combined_chars,
        )

        logger.info(
            "Agent memory context budgeted: %d/%d chars (%.1f%%) | Included: %d | Dropped: %d | Overflow: %s",
            result.combined_rendered_chars,
            result.max_chars,
            (result.combined_rendered_chars / result.max_chars * 100.0) if result.max_chars > 0 else 0.0,
            result.included_count,
            result.dropped_count,
            result.overflow,
        )

        return result


def budget_chat_memory(
    query: Optional[str] = None,
    max_chars: int = DEFAULT_CHAT_BUDGET_CHARS,
    conn: Optional[sqlite3.Connection] = None,
) -> BudgetResult:
    """
    Budget memory context for conversational chat (main.py).
    Default budget: 1,500 characters.
    """
    manager = ContextBudgetManager(max_chars=max_chars)
    return manager.budget(query=query, conn=conn)


def budget_agent_memory(
    goal: str,
    max_chars: int = DEFAULT_AGENT_BUDGET_CHARS,
    conn: Optional[sqlite3.Connection] = None,
) -> BudgetResult:
    """
    Budget memory context for agent loop (brain/agent_context.py).
    Default budget: 1,200 characters.
    Enforces combined budget and strict non-duplication between profile and relevant memories.
    """
    manager = ContextBudgetManager(max_chars=max_chars)
    return manager.budget_agent(goal=goal, conn=conn)
