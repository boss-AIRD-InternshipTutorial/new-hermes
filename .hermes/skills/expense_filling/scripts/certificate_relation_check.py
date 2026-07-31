"""关联票据校验子流程。

在凭证完整性校验之后读取最新暂存单：
- 存在未关联发票/附件时，返回“去关联”卡片。
- 不存在未关联发票/附件时，进入智能填单子流程。
"""

import json
from typing import Any, Optional

import requests

try:
    from . import auto_fill
    from .main_scripts import get_config, get_current_variables, get_session, update_session
except ImportError:
    import auto_fill
    from main_scripts import get_config, get_current_variables, get_session, update_session


BASE_URL = get_config().get("base_url", "").rstrip("/")
GET_DRAFT_INFO_PATH = "/business/reimburse/base/getDraftInfoById"
DRAFT_PATH = "/business/reimburse/base/draft"


class CertificateRelationChecker:
    def __init__(self, token: str) -> None:
        self.headers = {
            "Authorization": token,
            "Content-Type": "application/json",
        }

    def get_draft_info(self, form_id: str) -> dict[str, Any]:
        """获取最新暂存单数据。"""
        try:
            response = requests.post(
                f"{BASE_URL}{GET_DRAFT_INFO_PATH}",
                headers=self.headers,
                params={"id": form_id},
                timeout=120,
            )
        except requests.Timeout as exc:
            raise RuntimeError("获取暂存单接口调用超时") from exc

        if not response.ok:
            if response.status_code in (401, 403, 404):
                raise RuntimeError(
                    f"接口调用失败或无权限: "
                    f"path={GET_DRAFT_INFO_PATH}, status={response.status_code}"
                )
            raise RuntimeError(
                f"接口调用失败: path={GET_DRAFT_INFO_PATH}, "
                f"status={response.status_code}, body={response.text}"
            )

        result = response.json()
        if result in ({}, [], None, ""):
            raise LookupError(f"暂存单接口返回结果为空，id：{form_id}")
        if not isinstance(result, dict):
            raise LookupError(f"暂存单接口返回格式错误,id：{form_id}")
        return result

    def save_draft(self, draft_body: dict[str, Any]) -> None:
        """保存前端回传的完整草稿数据。"""
        try:
            response = requests.post(
                f"{BASE_URL}{DRAFT_PATH}",
                headers=self.headers,
                json=draft_body,
                timeout=60,
            )
        except requests.Timeout as exc:
            raise RuntimeError("暂存单据接口调用超时") from exc
        if not response.ok:
            raise RuntimeError(
                f"暂存单据失败: status={response.status_code}, body={response.text}"
            )

    def read_unrelated_certificates(self, draft: dict[str, Any]) -> list[Any]:
        """读取未关联凭证列表，null 按空列表处理。"""
        if "reimburseCertificateInfo" not in draft:
            raise LookupError("暂存单缺少 reimburseCertificateInfo")

        certificate_info = draft.get("reimburseCertificateInfo")
        if not isinstance(certificate_info, dict):
            raise LookupError("暂存单 reimburseCertificateInfo 格式错误")
        if "unRelateCertificate" not in certificate_info:
            raise LookupError("暂存单缺少 unRelateCertificate")

        unrelated = certificate_info.get("unRelateCertificate")
        if unrelated is None:
            return []
        if not isinstance(unrelated, list):
            raise LookupError("暂存单 unRelateCertificate 格式错误")
        return unrelated

    def build_route_params(
        self,
        draft: dict[str, Any],
        form_id: str,
        matter_uniq_code: str,
    ) -> dict[str, str]:
        """组装与必填项校验一致的单据页跳转参数。"""
        basic_info = draft.get("basicInfo")
        basic_info = basic_info if isinstance(basic_info, dict) else {}
        return {
            "draftId": str(draft.get("id") or form_id),
            "certCode": str(basic_info.get("reimbursementTypeCode") or ""),
            "certName": str(basic_info.get("reimbursementTypeName") or ""),
            "certType": "reimb",
            "economicMatterTypeId": str(
                basic_info.get("economicMatterTypeId") or matter_uniq_code or ""
            ),
            "schema_key":"_fj"
        }

    def build_card(
        self,
        unrelated_count: int,
        route_params: dict[str, str],
    ) -> str:
        """组装未关联凭证提示卡片。"""
        card = {
            "result": "",
            "renderName": "MsgReservation",
            "rootComponent": "base-warp",
            "prop": "mode:single",
            "data": {
                "answer": (
                    f"✅凭证完整性校验通过。存在{unrelated_count}个未关联的"
                    "发票/附件，需手动关联后才能智能填单。"
                ),
                "buttons": [
                    {
                        "type": "primary",
                        "label": "去关联",
                        "showType": "button",
                        "action": "releate_fj",
                        "sendMsgConfig": {"fieldList": [{"name": "data", "parserRule": {}}]},
                        "extra": {
                            
                            "params": route_params,
                        },
                    }
                ],
            },
        }
        return f"<res-card>{json.dumps(card, ensure_ascii=False)}</res-card>"

    def execute(
        self,
        form_id: str,
        matter_uniq_code: str = "",
        supplement_data: dict[str, Any] | None = None,
    ) -> str:
        """必要时先保存前端回传数据，再根据最新草稿分流。"""
        if supplement_data:
            self.save_draft(supplement_data)
        draft = self.get_draft_info(form_id)
        unrelated = self.read_unrelated_certificates(draft)

        if unrelated:
            update_session(
                form_data={
                    "form_id": form_id,
                    "certificate_relation_check": "reject",
                }
            )
            route_params = self.build_route_params(draft, form_id, matter_uniq_code)
            return self.build_card(len(unrelated), route_params)

        update_session(
            form_data={
                "form_id": form_id,
                "certificate_relation_check": "pass",
            }
        )
        return auto_fill.run(use_current_draft=True)


def error_card(result: str) -> str:
    """组装异常卡片。"""
    card = {
        "result": result,
        "renderName": "",
        "rootComponent": "base-warp",
        "prop": "mode:single",
    }
    return f"<res-card>\n{json.dumps(card, ensure_ascii=False)}\n</res-card>"


def system_error(message: str) -> str:
    return error_card(message)


def interface_result_error(message: str) -> str:
    return error_card(message)


def input_error(message: str) -> str:
    return error_card(message)


def parse_supplement_data(raw_data: Any) -> dict[str, Any]:
    """前端 data 可能是字典或 JSON 字符串，统一转换为草稿字典。"""
    if not raw_data:
        return {}
    if isinstance(raw_data, dict):
        return raw_data
    if not isinstance(raw_data, str) or not raw_data.strip():
        return {}
    try:
        data = json.loads(raw_data)
    except json.JSONDecodeError as exc:
        raise ValueError("data 不是合法 JSON") from exc
    return data if isinstance(data, dict) else {}


def is_current_draft_data(data: dict[str, Any], form_id: str) -> bool:
    """判断 data 是否为当前单据的完整草稿，避免误暂存其他流程上下文。"""
    return (
        isinstance(data, dict)
        and str(data.get("id") or "") == str(form_id)
        and "version" in data
        and isinstance(data.get("basicInfo"), dict)
        and isinstance(data.get("reimburseCertificateInfo"), dict)
    )


def append_session_debug(message: str, session_data: Any, session_loaded: bool) -> str:
    """在异常文案中附加会话读取状态，方便排查 session 是否为空或读失败。"""
    if not session_loaded:
        return f"{message}\nsession获取状态：未成功获取"
    try:
        session_text = json.dumps(session_data, ensure_ascii=False, default=str)
    except Exception:
        session_text = repr(session_data)
    return f"{message}\nsession获取状态：已获取\nsession值：{session_text}"


def run(
) -> str:
    """skill 入口：从当前变量和 session 读取上下文并执行校验。"""
    session_data: Any = None
    session_loaded = False
    try:
        current_variables = get_current_variables()
        token = current_variables.get("token", "")
        if not token:
            raise ValueError("缺少 token，AI需重新决策")

        session_data = get_session()
        session_loaded = True
        form_data = session_data.get("form_data") or {}
        voucher_data = session_data.get("voucher_folder_data") or {}
        final_form_id =  form_data.get("form_id", "")
        final_matter_uniq_code = voucher_data.get("matter_uniq_code", "")

        if not final_form_id:
            raise ValueError(f"{session_data}缺少 form_id，AI需重新决策")

        checker = CertificateRelationChecker(token)
        supplement_data = parse_supplement_data(current_variables.get("data"))
        if not is_current_draft_data(supplement_data, str(final_form_id)):
            supplement_data = None
        return checker.execute(
            form_id=str(final_form_id),
            matter_uniq_code=str(final_matter_uniq_code or ""),
            supplement_data=supplement_data,
        )
    except ValueError as exc:
        return input_error(append_session_debug(str(exc), session_data, session_loaded))
    except (RuntimeError, LookupError) as exc:
        return interface_result_error(append_session_debug(str(exc), session_data, session_loaded))
    except Exception as e:
        return system_error(append_session_debug(str(e), session_data, session_loaded))
