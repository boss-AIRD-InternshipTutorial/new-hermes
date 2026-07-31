"""单据暂存子流程：读取已暂存单据并返回继续提报卡片。"""

import json
from typing import Any

import requests

try:
    from .main_scripts import get_config, get_current_variables, get_session
except ImportError:
    from main_scripts import get_config, get_current_variables, get_session


BASE_URL = get_config().get("base_url", "").rstrip("/")
GET_DRAFT_INFO_PATH = "/business/reimburse/base/getDraftInfoById"
REIMBURSE_DETAIL_PATH = "plugin://rs-pre-approal/pre-page"


def get_draft_info(form_id: str, token: str) -> dict[str, Any]:
    """获取最新暂存单。"""
    try:
        response = requests.post(
            f"{BASE_URL}{GET_DRAFT_INFO_PATH}",
            headers={"Authorization": token, "Content-Type": "application/json"},
            params={"id": form_id},
            timeout=120,
        )
    except requests.Timeout as exc:
        raise RuntimeError("获取暂存单接口调用超时") from exc
    if not response.ok:
        raise RuntimeError(
            f"获取暂存单失败: status={response.status_code}, body={response.text}"
        )
    result = response.json()
    if not isinstance(result, dict) or not result:
        raise LookupError(f"暂存单接口返回结果为空，id：{form_id}")
    return result


def build_card(draft: dict[str, Any], form_id: str,
               matter_uniq_code: str) -> str:
    """组装暂存成功卡片。"""
    basic_info = draft.get("basicInfo")
    basic_info = basic_info if isinstance(basic_info, dict) else {}
    route_params = {
        "draftId": str(draft.get("id") or form_id),
        "certCode": str(basic_info.get("reimbursementTypeCode") or ""),
        "certName": str(basic_info.get("reimbursementTypeName") or ""),
        "certType": "reimb",
        "economicMatterTypeId": str(
            matter_uniq_code or basic_info.get("economicMatterTypeId") or ""
        ),
    }
    card = {
        "result": "",
        "renderName": "MsgReservation",
        "rootComponent": "base-warp",
        "prop": "mode:single",
        "data": {
            "answer": "单据已为您暂存，您可随时进入单据继续提报。",
            "buttons": [
                {
                    "type": "primary",
                    "label": "查看单据并提交",
                    "showType": "button",
                    "action": "route",
                    "extra": {
                        "path": REIMBURSE_DETAIL_PATH,
                        "params": route_params,
                    },
                }
            ],
        },
    }
    return f"<res-card>{json.dumps(card, ensure_ascii=False)}</res-card>"


def error_card(message: str) -> str:
    payload = {
        "result": message,
        "renderName": "",
        "rootComponent": "base-warp",
        "prop": "mode:single",
    }
    return f"<res-card>\n{json.dumps(payload, ensure_ascii=False)}\n</res-card>"


def append_session_debug(message: str, session_data: Any,
                         session_loaded: bool) -> str:
    if not session_loaded:
        return f"{message}\nsession获取状态：未成功获取"
    return (
        f"{message}\nsession获取状态：已获取\nsession值："
        f"{json.dumps(session_data, ensure_ascii=False, default=str)}"
    )


def run() -> str:
    """skill 入口：暂存当前单据并返回继续提报卡片。"""
    session_data: Any = None
    session_loaded = False
    try:
        current_variables = get_current_variables()
        token = current_variables.get("token") or current_variables.get("x_agent_token", "")
        if not token:
            raise ValueError("缺少 token，AI需重新决策")

        session_data = get_session()
        session_loaded = True
        form_data = session_data.get("form_data") or {}
        voucher_data = session_data.get("voucher_folder_data") or {}
        form_id = form_data.get("form_id") or ""
        matter_uniq_code = voucher_data.get("matter_uniq_code") or ""
        if not form_id:
            raise ValueError("会话中缺少 form_id，AI需重新决策")

        draft = get_draft_info(str(form_id), token)
        return build_card(
            draft=draft,
            form_id=str(form_id),
            matter_uniq_code=str(matter_uniq_code),
        )
    except Exception as exc:
        return error_card(append_session_debug(str(exc), session_data, session_loaded))
