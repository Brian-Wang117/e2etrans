"""Fixed-reply script library: data model, matching and validation.

A script intercepts a customer utterance before the large model answers:
triggers are matched as ordered subsequences (characters may be separated by
anything), the highest-priority hit wins, and the reply is played verbatim.
Validation enforces the requirement bounds (20-40 chars, farewell word, no
hard-coded prices) so generated or imported libraries can never smuggle in
uncontrolled content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

VERDICT_INTERESTED = "感兴趣"
VERDICT_NOT_INTERESTED = "不感兴趣"
VERDICT_NEUTRAL = "中立"
VERDICTS = frozenset({VERDICT_INTERESTED, VERDICT_NOT_INTERESTED, VERDICT_NEUTRAL})

MIN_PRIORITY = 0
MAX_PRIORITY = 10
MIN_REPLY_CHARS = 20
MAX_REPLY_CHARS = 40
FAREWELL_WORD = "再见"

# Hard-coded prices / amounts are banned inside triggers and replies (the
# scenario content must stay configurable, see requirement 1.3 / 4.2).
_PRICE_PATTERN = re.compile(r"(¥|￥|\$|\d+\s*(元|块钱|块|折))")
# Keep CJK, letters and digits; drop punctuation/whitespace before matching.
_NORMALIZE_DROP = re.compile(r"[^\w\u4e00-\u9fff]", re.UNICODE)


@dataclass(frozen=True)
class Script:
    category: str
    triggers: tuple[str, ...]
    reply: str
    end_call: bool
    verdict: str  # one of VERDICTS, or "" when the script does not classify
    priority: int
    description: str = ""
    id: int | None = None
    library_name: str = "builtin"


@dataclass(frozen=True)
class ScriptHit:
    script: Script
    trigger: str


BUILTIN_SCRIPTS: tuple[Script, ...] = (
    Script(
        category="身份否认",
        triggers=("打错了", "不是本人", "找错人"),
        reply="非常抱歉打扰到您了，可能是我们这边登记有误，祝您生活愉快，再见。",
        end_call=True,
        verdict=VERDICT_NOT_INTERESTED,
        priority=10,
        description="客户否认身份：道歉并立即结束",
    ),
    Script(
        category="投诉免打扰",
        triggers=("别再打", "骚扰", "拉黑", "投诉"),
        reply="很抱歉给您带来困扰，我会把您加入免打扰名单，不再来电，再见。",
        end_call=True,
        verdict=VERDICT_NOT_INTERESTED,
        priority=10,
        description="客户要求免打扰：道歉承诺并立即结束",
    ),
    Script(
        category="听不清",
        triggers=("听不清", "没听见"),
        reply="不好意思，可能是我语速有点快，我放慢一点再给您说一遍。",
        end_call=False,
        verdict=VERDICT_NEUTRAL,
        priority=5,
        description="客户没听清：致歉并重述，不结束通话",
    ),
)


def validate_script(script: Script) -> list[str]:
    """Return the list of requirement violations (empty means valid)."""
    problems: list[str] = []
    if not script.category or not script.category.strip():
        problems.append("category is empty")
    if not script.triggers:
        problems.append("triggers are empty")
    for trigger in script.triggers:
        if not isinstance(trigger, str) or not trigger.strip():
            problems.append("trigger is empty")
        elif _PRICE_PATTERN.search(trigger):
            problems.append("trigger contains a hard-coded price")
    reply = script.reply or ""
    if not (MIN_REPLY_CHARS <= len(reply) <= MAX_REPLY_CHARS):
        problems.append(
            f"reply length {len(reply)} is outside {MIN_REPLY_CHARS}-{MAX_REPLY_CHARS}"
        )
    if script.end_call and FAREWELL_WORD not in reply:
        problems.append("end-call reply must contain the farewell word")
    if _PRICE_PATTERN.search(reply):
        problems.append("reply contains a hard-coded price")
    if not (MIN_PRIORITY <= script.priority <= MAX_PRIORITY):
        problems.append("priority is outside 0-10")
    if script.verdict not in VERDICTS and script.verdict != "":
        problems.append("verdict is not in the allowed enumeration")
    return problems


def _normalize(text: str) -> str:
    return _NORMALIZE_DROP.sub("", text or "")


def _is_ordered_subsequence(needle: str, haystack: str) -> bool:
    if not needle:
        return False
    position = 0
    for char in haystack:
        if char == needle[position]:
            position += 1
            if position == len(needle):
                return True
    return False


class ScriptMatcher:
    """Pure matcher over an injected script collection.

    Only exposes ``match`` so the interception point can later be swapped
    (e.g. for a pre-ASR gate) without touching the call director.
    """

    def __init__(self, scripts: Sequence[Script]) -> None:
        self._scripts = tuple(scripts)

    @property
    def scripts(self) -> tuple[Script, ...]:
        return self._scripts

    def match(self, text: str) -> ScriptHit | None:
        haystack = _normalize(text)
        if not haystack:
            return None
        best: ScriptHit | None = None
        best_trigger_length = 0
        for script in self._scripts:
            for trigger in script.triggers:
                needle = _normalize(trigger)
                if not needle or not _is_ordered_subsequence(needle, haystack):
                    continue
                if (
                    best is None
                    or script.priority > best.script.priority
                    or (
                        script.priority == best.script.priority
                        and len(needle) > best_trigger_length
                    )
                ):
                    best = ScriptHit(script=script, trigger=trigger)
                    best_trigger_length = len(needle)
                break  # one trigger per script is enough
        return best
