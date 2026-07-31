"""
预算项目选择子流程（budget-project）

供 main_scripts.py import 使用。负责将入参增量更新到会话状态，并返回预算项目选择卡片，
前端通过 bus_show_picker_projectInfo 业务组件唤起 picker，用户选择后由 C2A 隐式消息回传结果。

参数获取:
    token → get_current_variables()["token"]（传参可覆盖）
    会话  → get_session() / update_session()

参数:
    session_id (str)         已废弃，由 get_current_session_id() 内部获取
    token (str)              JWT 认证令牌，None 则自动获取
    voucher_folder_id (str)  凭证夹ID，传入后覆盖会话状态
    invoice_id_list (list)   票据ID列表，传入后增量追加到会话状态
    attachment_id_list (list) 附件ID列表，传入后增量追加到会话状态
    matter_uniq_code (str)   经济事项唯一码，传入后覆盖会话状态

返回:
    成功:  budget_project_card 卡片（<res-card>...</res-card> 字符串）
    失败: {"code": "E0001", "message": "..."}
"""

import json

try:
    from .main_scripts import get_current_variables, get_session, update_session
except ImportError:
    from main_scripts import get_current_variables, get_session, update_session
from hermes_logging import setup_logging
import logging
setup_logging()
logger = logging.getLogger(__name__)
def run(token: str = None, voucher_folder_id: str = "",
        invoice_id_list: list = None, attachment_id_list: list = None,
        matter_uniq_code: str = ""):
    """
    预算项目选择子流程入口。

    将入参增量合并到会话状态后，返回含 bus_show_picker_projectInfo 按钮的卡片。
    本子流程不查询接口、不判断项目唯一性，选择逻辑全部交由前端 picker 组件完成。

    :param token: 用户认证令牌，None 则自动获取
    :param voucher_folder_id: 凭证夹ID
    :param invoice_id_list: 票据ID列表
    :param attachment_id_list: 附件ID列表
    :param matter_uniq_code: 经济事项唯一码
    :return: "<res-card>...</res-card>" 或 {"code": "E0001", "message": "..."}
    """
    if token is None:
        try:
            token = get_current_variables().get("token", "")
        except Exception:
            token = ""

    invoice_id_list = invoice_id_list or []
    attachment_id_list = attachment_id_list or []

    # 加载会话状态
    session = get_session()
    vfd = session.get("voucher_folder_data", {}) or {}
    fd = session.get("form_data", {}) or {}

    # 增量合并入参到会话状态
    vfd_up = {}
    if voucher_folder_id:
        vfd_up["voucher_folder_id"] = voucher_folder_id
    if invoice_id_list:
        ids = list(vfd.get("invoice_id_list", []) or [])
        for c in invoice_id_list:
            if c and c not in ids:
                ids.append(c)
        vfd_up["invoice_id_list"] = ids
    if attachment_id_list:
        ids = list(vfd.get("attachment_id_list", []) or [])
        for c in attachment_id_list:
            if c and c not in ids:
                ids.append(c)
        vfd_up["attachment_id_list"] = ids
    if matter_uniq_code:
        vfd_up["matter_uniq_code"] = matter_uniq_code
    if vfd_up:
        update_session(voucher_folder_data=vfd_up)
        vfd.update(vfd_up)

    # 取会话最新值
    matter_uniq_code = vfd.get("matter_uniq_code", "") or matter_uniq_code

    # 前置校验
    if not token:
        return {"errorCode": "E0003", "message": "token不能为空"}
    if not matter_uniq_code:
        return {"errorCode": "E0003", "message": "缺少经济事项"}
    logger.info(f"预算项目run成功执行")
    return _picker_card(matter_uniq_code)


def _picker_card(matter_uniq_code: str) -> str:
    """
    构建预算项目选择卡片。

    卡片内含 bus_show_picker_projectInfo 业务组件按钮，用户点击后前端唤起 picker，
    通过 extra.params 将经济事项唯一码传递给业务组件，用户选择完成后由 C2A 隐式消息回传。

    :param matter_uniq_code: 经济事项唯一码，透传给前端业务组件
    :return: "<res-card>...</res-card>"
    """
    card_data = {
        "renderName": "",
        "rootComponent": "base-warp",
        "prop": "mode:single",
        "data": {
            "answer": "该类型使用科研经费，请选择您此次报销的预算项目：",
            "buttons": [
                {
                    "label": "选择预算项目",
                    "action": "choose_budget",
                    "type": "primary"

                }
            ],
        },
    }
    content = json.dumps(card_data, ensure_ascii=False)
    return f"<res-card>{content}</res-card>"
