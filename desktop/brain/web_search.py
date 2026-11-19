"""
brain/web_search.py - Provider-independent web search for Orange AI.

Queries web search sources directly using standard library HTTP requests.
Does not depend on any cloud LLM provider or proprietary SDK.
"""

import json
import re
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional
from brain.logger import logger


def search_web(query: str, timeout: float = 10.0) -> Optional[str]:
    """
    Search the web for the given query and return a structured summary of results.
    Returns None on connection failure or if no results could be retrieved.
    """
    clean_query = query.strip()
    if not clean_query:
        return None

    # 1. First attempt: DuckDuckGo Instant Answer API (JSON)
    try:
        params = urllib.parse.urlencode({
            "q": clean_query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
        })
        url = f"https://api.duckduckgo.com/?{params}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "OrangeAI/1.0 (Windows; Desktop Assistant)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                raw_data = response.read().decode("utf-8", errors="replace")
                data = json.loads(raw_data)

                # Check AbstractText
                abstract = data.get("AbstractText", "").strip()
                source = data.get("AbstractSource", "")
                abstract_url = data.get("AbstractURL", "")

                if abstract:
                    result = f"🔍 Search result for '{clean_query}':\n\n{abstract}"
                    if source and abstract_url:
                        result += f"\n\nSource: {source} ({abstract_url})"
                    return result

                # Check Answer
                answer = data.get("Answer", "").strip()
                if answer:
                    return f"🔍 Search result for '{clean_query}':\n\n{answer}"

                # Check Definition
                definition = data.get("Definition", "").strip()
                if definition:
                    return f"🔍 Search result for '{clean_query}':\n\n{definition}"

                # Check RelatedTopics
                related = data.get("RelatedTopics", [])
                snippets = []
                for topic in related:
                    if isinstance(topic, dict) and "Text" in topic and topic["Text"].strip():
                        snippets.append(topic["Text"].strip())
                    elif isinstance(topic, dict) and "Topics" in topic:
                        for subtopic in topic.get("Topics", []):
                            if isinstance(subtopic, dict) and "Text" in subtopic and subtopic["Text"].strip():
                                snippets.append(subtopic["Text"].strip())
                    if len(snippets) >= 3:
                        break

                if snippets:
                    formatted_snippets = "\n\n".join(f"- {s}" for s in snippets)
                    return f"🔍 Search results for '{clean_query}':\n\n{formatted_snippets}"

    except Exception as e:
        logger.debug("DuckDuckGo Instant Answer API failed: %s", e)

    # 2. Fallback: DuckDuckGo HTML / Lite endpoint
    try:
        params = urllib.parse.urlencode({"q": clean_query})
        url = f"https://html.duckduckgo.com/html/?{params}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                html = response.read().decode("utf-8", errors="replace")
                # Extract snippets from class="result__snippet"
                snippets = re.findall(
                    r'<a[^>]*class=["\']result__snippet["\'][^>]*>(.*?)</a>',
                    html,
                    re.DOTALL,
                )
                if not snippets:
                    snippets = re.findall(
                        r'<td[^>]*class=["\']result-snippet["\'][^>]*>(.*?)</td>',
                        html,
                        re.DOTALL,
                    )

                clean_snippets = []
                for s in snippets[:3]:
                    cleaned = re.sub(r"<[^>]+>", "", s).strip()
                    if cleaned:
                        clean_snippets.append(cleaned)

                if clean_snippets:
                    formatted_snippets = "\n\n".join(f"- {s}" for s in clean_snippets)
                    return f"🔍 Search results for '{clean_query}':\n\n{formatted_snippets}"

    except Exception as e:
        logger.warning("DuckDuckGo HTML search fallback failed: %s", e)

    return None
