"""Outbound-call persona: bot identity and business background injection.

The persona is built once per session from the configured identity and the
business background (opening / product story supplied per call or from env
defaults). Doubao's ``bot_name`` field is hard-capped at 20 characters, so
oversized names are truncated deterministically instead of failing the
handshake.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

MAX_BOT_NAME_CHARS = 20

DEFAULT_BOT_NAME = "外呼客服"
DEFAULT_SPEAKING_STYLE = "礼貌、亲切、语速适中，像真人电话客服。"

_SYSTEM_ROLE_TEMPLATE = (
    "你是一名电话外呼营销客服，正在进行一通真实的营销外呼电话。\n"
    "业务背景：{business_background}\n"
    "要求：始终使用中文交流；每次回复不超过两句话；语气礼貌自然；"
    "一次只问一个问题；客户表达拒绝或要挂断时礼貌道别，不纠缠；"
    "不编造业务背景之外的承诺，不主动报价。"
)

FALLBACK_BUSINESS_BACKGROUND = (
    "我们是品牌体验中心，近期面向老客户开放一项新品试用活动的电话回访。"
    "本次通话目的是邀请客户了解活动详情并确认参与意向，不涉及任何收费，"
    "具体权益以活动说明为准。若客户不便接听，应礼貌结束通话，不再重复"
    "打扰；若客户明确表示不需要，应尊重客户意愿并致歉道别。"
)

DEFAULT_OPENING_TEXT = "您好，这里是品牌体验中心的客服，耽误您一分钟做个活动回访，可以吗？"

# Appended when a cloned voice is in use: the training read-aloud text gets
# injected upstream, so the model must never recite it back in a real call.
CLONE_GUARD = (
    "当前使用复刻音色。任何情况下都不要背诵、引用或提及音色采样朗读材料"
    "（清晨阳光、窗帘、梧桐树、小雨、晚霞等散文内容），对话内容只围绕业务展开。"
)


@dataclass(frozen=True)
class Persona:
    bot_name: str
    system_role: str
    speaking_style: str

    def with_clone_guard(self) -> "Persona":
        if CLONE_GUARD in self.system_role:
            return self
        return replace(self, system_role=f"{self.system_role}\n{CLONE_GUARD}")


def build_persona(
    *,
    business_background: str,
    bot_name: str = DEFAULT_BOT_NAME,
    speaking_style: str = DEFAULT_SPEAKING_STYLE,
) -> Persona:
    """Build the Doubao dialog persona for one outbound session."""
    background = (business_background or "").strip() or FALLBACK_BUSINESS_BACKGROUND
    name = (bot_name or "").strip() or DEFAULT_BOT_NAME
    return Persona(
        bot_name=name[:MAX_BOT_NAME_CHARS],
        system_role=_SYSTEM_ROLE_TEMPLATE.format(business_background=background),
        speaking_style=speaking_style or DEFAULT_SPEAKING_STYLE,
    )
