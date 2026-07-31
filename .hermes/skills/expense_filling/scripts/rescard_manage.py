"""
rescard_manage.py.py — 统一卡片构建模块

提供标准化的卡片构建函数，供所有子流程脚本调用。
所有卡片都遵循统一的 <res-card> 结构规范。

卡片类型（按 renderName 分类）：
    1. msg-system-error-card       - 系统异常/错误提示卡片
    2. msg-voucher-recognition-card - 凭证识别/填单结果卡片
    3. audit_result_card           - 稽核结果卡片
    4. budget_project_card         - 预算项目选择卡片
    5. integrity_audit_card        - 凭证完整性校验卡片
    6. MsgReservation              - 经济事项确认卡片
    7. person_selection_card       - 人员选择卡片
    8. (空字符串)                   - 凭证夹匹配卡片

使用示例：
    from rescard_manage import build_card, RenderName
    
    card = build_card(
        render_name=RenderName.ERROR,
        result="系统异常，请联系管理员"
    )
"""

import json
from typing import Any, Dict, Optional
from enum import Enum


# ==================== 枚举定义 ====================

class RenderName(str, Enum):
    """卡片渲染器名称枚举"""
    ERROR = "msg-system-error-card"
    VOUCHER_RECOGNITION = "msg-voucher-recognition-card"
    AUDIT_RESULT = "audit_result_card"
    BUDGET_PROJECT = "budget_project_card"
    INTEGRITY_AUDIT = "integrity_audit_card"
    ECONOMIC_MATTER = "MsgReservation"
    PERSON_SELECTION = "person_selection_card"
    VOUCHER_MATCH = ""  # 空字符串


# ==================== 基础工具 ====================

def _wrap_res_card(card_data: Dict[str, Any]) -> str:
    """
    将卡片数据包装为 <res-card> XML 字符串。
    
    :param card_data: 卡片数据字典
    :return: <res-card>...</res-card> 格式的字符串
    """
    content = json.dumps(card_data, ensure_ascii=False, indent=2)
    return f"<res-card>\n{content}\n</res-card>"


def build_card(
    render_name: RenderName,
    rootComponent: str = "base-warp",
    prop: str = "mode:single",
    result: str = "",
    data: Optional[Dict[str, Any]] = None,
    root_data: Optional[Dict[str, Any]] = None,
) -> str:
    """
    构建基础卡片结构并返回 <res-card> 字符串。
    
    :param render_name: 渲染器名称枚举
    :param data: 卡片数据体
    :param result: 结果文本（外层）
    :param root_data: 根数据（suggestionButtons 等）
    :param prop: 属性，默认为 mode:single
    :return: <res-card> 字符串
    """
    card = {
        "renderName": render_name.value,
        "rootComponent": rootComponent,
        "prop": prop,
    }
    
    if result:
        card["result"] = result
    
    if data:
        card["data"] = data
    
    if root_data:
        card["rootData"] = root_data
    
    return _wrap_res_card(card)