"""
reimbursement_submit_success.py — 单据提交成功展示子流程

功能概述：
    接收前端通过 variables.data 传入的单据提交结果 JSON，
    组装单据提交成功卡片，并清空当前会话流程变量。
"""

import json
from typing import Any

try:
    from .main_scripts import delete_session, get_current_variables
except ImportError:
    from main_scripts import delete_session, get_current_variables


def res_card(payload: dict[str, Any]) -> str:
    return f"<res-card>\n{json.dumps(payload, ensure_ascii=False)}\n</res-card>"


def error_card(result: str) -> str:
    return res_card(
        {
            "result": result,
            "renderName": "",
            "rootComponent": "base-warp",
            "prop": "mode:single",
        }
    )


def system_error() -> str:
    return error_card("系统异常，请联系管理员")


def interface_result_error(message: str) -> str:
    return error_card(message)


def input_error(message: str) -> str:
    return error_card(message)


def parse_submit_result(raw_data: Any) -> dict[str, Any]:
    if isinstance(raw_data, str):
        if not raw_data.strip():
            raise ValueError("提交结果为空，AI需重新决策")
        try:
            raw_data = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise ValueError("提交结果不是合法 JSON，AI需重新决策") from exc

    if not isinstance(raw_data, dict) or not raw_data:
        raise ValueError("提交结果为空，AI需重新决策")
    if isinstance(raw_data.get("submitResponse"), dict):
        raw_data = raw_data["submitResponse"]
    return raw_data


def read_approval_node(form_resume: dict[str, Any]) -> str:
    assignee_names = form_resume.get("taskNextAssigneeNames")
    if isinstance(assignee_names, list):
        names = [str(name) for name in assignee_names if name]
        if names:
            return "、".join(names)
    return "暂无"


def build_success_card(submit_result: dict[str, Any]) -> str:
    form_resume = submit_result.get("formResume") or {}
    if not isinstance(form_resume, dict):
        form_resume = {}

    reimbursement_no = form_resume.get("reimbursementNo") or ""
    approval_node = read_approval_node(form_resume)
    process_id = form_resume.get("flowId") or ""
    cert_code = form_resume.get("reimbursementTypeCode") or ""
    cert_name = form_resume.get("reimbursementTypeName") or ""
    cert_inst_id = form_resume.get("id") or form_resume.get("recId") or ""
    task_id = form_resume.get("taskId") or ""

    payload = {
        "result": "",
        "renderName": "",
        "rootComponent": "base-warp",
        "prop": "mode:single",
        "data": {
            "answer":"\n".join(
            [
                "🎉单据已提交成功",
                f"报销单号：{reimbursement_no}" if reimbursement_no else "报销单号：",
                f"审批节点：{approval_node}",
            ]
        ),
            "buttons": [
                {
                    "type": "primary",
                    "label": "查看提交详情",
                    "showType": "button",
                    "action": "route",
                    "extra": {
                        "path": "plugin://rs-pre-approal/pre-page",
                        "params": {
                            "certCode": cert_code,
                            "certName": cert_name,
                            "certInstId": cert_inst_id,
                            "isApply": True,
                            "taskId": task_id,
                        },
                    },
                }
            ]
        },
        "rootData": {
            "suggestionButtons": [
                {
                    "label": "查询已发起报销单的进度",
                    "action": "route",
                    "extra": {
                        "params": {"busType": "reimb", "processId": process_id},
                        "path": "/pages/wallet/plugin-approval-process/index",
                    },
                },
                {
                    "label": "继续报销",
                    "action": "sendMsg",
                    "extra": {"content": "继续报销"},
                },
            ]
        },
    }
    return res_card(payload)


def run() -> str:
    try:
        current_variables = get_current_variables()
        submit_result = parse_submit_result(current_variables.get("data"))

        if submit_result.get("result") is not True or submit_result.get("saveResult") is not True:
            message = submit_result.get("message") or "单据提交失败，请重新提交"
            return interface_result_error(str(message))

        card = build_success_card(submit_result)
        delete_session()
        return card
    except ValueError as exc:
        return input_error(str(exc))
    except Exception:
        return system_error()
