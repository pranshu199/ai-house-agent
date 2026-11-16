"""Lead extraction scoring and Supabase persistence."""

import asyncio
import re

from app.integrations.nova_client import extract_call_lead
from app.integrations.supabase_client import persist_call_lead


_DAYS_PATTERN = re.compile(r"\b(?:within|in)\s+(\d+)\s+days?\b", re.IGNORECASE)
_WEEKS_PATTERN = re.compile(r"\b(?:within|in)\s+(\d+)\s+weeks?\b", re.IGNORECASE)


def _is_within_30_days(timeline: str | None) -> bool:
    if not timeline:
        return False
    value = timeline.casefold()
    if any(phrase in value for phrase in ("asap", "immediately", "right away")):
        return True

    match = _DAYS_PATTERN.search(value)
    if match:
        return int(match.group(1)) <= 30
    match = _WEEKS_PATTERN.search(value)
    if match:
        return int(match.group(1)) * 7 <= 30
    return False


def priority_for_score(score: int) -> str:
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise ValueError("Lead score must be an integer from 0 to 100")
    return "hot" if score >= 70 else "warm" if score >= 40 else "cold"


def score_lead(
    extraction: dict,
    transcript: list[dict[str, str]],
) -> tuple[int, str]:
    """Score only explicit extraction fields and caller-authored questions."""

    score = 0
    if extraction["intent"] == "viewing_request":
        score += 30
    if extraction["intent"] in {"buying", "selling"} and _is_within_30_days(
        extraction["timeline"]
    ):
        score += 25
    if extraction["interested_property"]:
        score += 15
    if (
        extraction["budget_min"] is not None
        or extraction["budget_max"] is not None
        or extraction["intent"] == "financing_question"
    ):
        score += 15
    if extraction["name"] or extraction["email"]:
        score += 10

    caller_question_marks = sum(
        entry.get("text", "").count("?")
        for entry in transcript
        if isinstance(entry, dict)
        and str(entry.get("role", "")).upper() == "USER"
        and isinstance(entry.get("text"), str)
    )
    if caller_question_marks >= 2:
        score += 5
    score = min(score, 100)
    return score, priority_for_score(score)


async def capture_call_lead(
    call_id: str,
    transcript: list[dict[str, str]],
) -> str:
    """Extract, score, and persist a lead for a finalized call."""

    extraction = await extract_call_lead(transcript)
    score, priority = score_lead(extraction, transcript)
    return await asyncio.to_thread(
        persist_call_lead,
        call_id,
        extraction,
        score,
        priority,
    )
