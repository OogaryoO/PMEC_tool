"""
助理能做的動作，與「開放到哪個階段」的關卡。

四個階段（brain outputs/reports/2026-11-11-brain-tool-adoption.md §3；Gary 11-11 決定第一版只讀＋擬稿）：
    1 只讀：查資料、回答、附出處
    2 擬稿：寫草稿，存到員工自己的草稿區
    3 改檔：修改共用文件，每次都要員工按確認；保留修改前版本
    4 寄信：員工按確認才寄出；第一次寄給某個對外地址時再多確認一次

tenant.toml 的 stage 決定目前開放到哪一階段。目前只實作關卡本身；擬稿、改檔、寄信的實際動作
等訪談第 16、17 題確定用途與條件後再做（docs/INTERVIEW_TO_CONFIG.md）。
OWASP「AI 權力過大」的建議：只給需要的功能、只給需要的權限、高影響動作要人核准。
"""
from __future__ import annotations

from typing import Dict

from src.tenant import STAGE_NAMES, TenantConfig

# 動作 → 最低階段
ACTION_MIN_STAGE: Dict[str, int] = {
    "answer": 1,
    "draft": 2,
    "edit_file": 3,
    "send_email": 4,
}
# 必須由員工逐次確認的動作
REQUIRES_CONFIRMATION = {"edit_file", "send_email"}


class ActionNotAllowed(Exception):
    """動作未開放或未經確認，訊息可直接呈現給使用者。"""


def check_action(tenant: TenantConfig, action: str, confirmed: bool = False) -> None:
    """動作不允許時拋出 ActionNotAllowed；允許時不回傳任何東西。"""
    if action not in ACTION_MIN_STAGE:
        raise ActionNotAllowed(f"不認得的動作 `{action}`。")
    need = ACTION_MIN_STAGE[action]
    if tenant.stage < need:
        raise ActionNotAllowed(
            f"目前開放到第 {tenant.stage} 階段（{STAGE_NAMES[tenant.stage]}），"
            f"「{action}」要第 {need} 階段（{STAGE_NAMES[need]}）才開放。"
        )
    if action in REQUIRES_CONFIRMATION and not confirmed:
        raise ActionNotAllowed(f"「{action}」每次都需要你按確認後才會執行。")
