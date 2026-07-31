"""
voucher_folder_integrity_script.py — 凭证完整性校验模块

功能概述：
    根据会话ID和用户token，从会话中读取凭证夹、凭证列表、经济事项等上下文，
    必要时创建报销单，再从业务系统获取最新单据暂存数据，
    调用单据稽核接口查询凭证完整性结果，结合凭证配置判断缺失项，
    并组装为前端可渲染的 Web Card XML 字符串返回。

主入口：
    run(session_id, token, ...) -> dict
        成功：{"code": "0", "data": "<res-card>...</res-card>"}
        系统异常：{"code": "E0001", "data": "<res-card>...</res-card>"}
        接口返回结果异常：{"code": "E0002", "data": "<res-card>...</res-card>"}
        子流程入参异常：{"errorCode": "E0003", "message": "错误说明"}

错误码说明：
    E0001 - 系统异常，接口调用失败或子流程运行报错，返回系统异常卡片
    E0002 - 接口返回结果异常，查询结果为空或业务结果异常，返回接口结果异常卡片
    E0003 - 子流程入参异常，AI决策入参有误，返回包含错误信息的JSON

调用示例：
    from scripts.voucher_folder_integrity_script import run
    result = run(session_id="xxx", token="Bearer xxx")
"""

import json
from typing import Any, Optional, List

import requests

try:
    from .main_scripts import get_config, get_current_variables, get_session, update_session
    from . import auto_fill
    from . import certificate_relation_check
except ImportError:
    from main_scripts import get_config, get_current_variables, get_session, update_session
    import auto_fill
    import certificate_relation_check


BASE_URL = get_config()['base_url']
CERTIFICATE_TYPE_BY_MATTER_PATH = "/business/reimburse/economicMatter/getCertificateTypeInfoByMatter"
CREATE_REIMBURSE_PATH = "/business/reimburse/base/create"
GET_DRAFT_INFO_PATH = "/business/reimburse/base/getDraftInfoById"
AUDIT_REPORT_PATH = "/business/reimburse/base/getAuditReport"
CERTIFICATE_CONFIG_PATH = "/business/reimburse/feeaction/getCertificateConfig"
DRAFT_PATH = "/business/reimburse/base/draft"
REQUIRED_CERTIFICATE_TYPES = {1, 3}


class VoucherIntegrityChecker:
    def __init__(self, token: str, matter_uniq_code: str) -> None:
        """保存当前流程所需的鉴权信息和经济事项编码。"""
        self.token = token
        self.matter_uniq_code = matter_uniq_code
        self.headers = {
            "Authorization": token,
            "Content-Type": "application/json",
        }

    def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """统一封装 POST 请求和错误处理。"""
        try:
            response = requests.post(
                f"{BASE_URL}{path}",
                headers=self.headers,
                timeout=30,
                **kwargs,
            )
        except requests.Timeout as exc:
            raise RuntimeError("接口调用超时") from exc
        if not response.ok:
            if response.status_code in (401, 403, 404):
                raise RuntimeError(f"接口调用失败或无权限: path={path}, status={response.status_code}")
            raise RuntimeError(f"接口调用失败: path={path}, status={response.status_code}, body={response.text}")
        result = response.json()
        if result in ({}, [], None, ""):
            raise LookupError("接口返回结果为空")
        return result

    def get_certificate_type(self) -> dict[str, Any]:
        """根据经济事项编码获取对应报销单类型。"""
        return self.post(
            CERTIFICATE_TYPE_BY_MATTER_PATH,
            params={"matterUniqCode": self.matter_uniq_code},
        )
        
    def create_reimburse(self, reimbursement_type_code: str) -> dict[str, Any]:
        """创建报销单，拿到后续填单和稽核所需的 form_id。"""
        return self.post(
            CREATE_REIMBURSE_PATH,
            json={
                "reimbursementTypeCode": reimbursement_type_code,
                "economicMatterTypeId": self.matter_uniq_code,
            },
        )

    def save_draft(self, reimburse_body: dict[str, Any]) -> None:
        """创建单据后先暂存一次，让后续 getDraftInfoById 能读取到草稿。"""
        try:
            response = requests.post(
                f"{BASE_URL}{DRAFT_PATH}",
                headers=self.headers,
                json=reimburse_body,
                timeout=30,
            )
        except requests.Timeout as exc:
            raise RuntimeError("暂存单据接口调用超时") from exc
        if not response.ok:
            raise RuntimeError(f"暂存单据失败: status={response.status_code}, body={response.text}")

    def get_draft_info(self, form_id: str) -> dict[str, Any]:
        """通过单据 ID 获取最新暂存单内容。"""
        return self.post(GET_DRAFT_INFO_PATH, params={"id": form_id})

    def get_certificate_config(self, certificate_type: dict[str, Any]) -> dict[str, Any]:
        """获取当前报销单类型下的凭证配置。"""
        return self.post(
            CERTIFICATE_CONFIG_PATH,
            params={
                "matterUniqCode": self.matter_uniq_code,
                "certificateCode": certificate_type.get("certCode"),
                "reimbursementTypeCode": certificate_type.get("reimbursementTypeCode"),
            },
        )

    def get_audit_report(self, reimburse_body: dict[str, Any]) -> dict[str, Any]:
        """把最新草稿单传给稽核接口，获取完整稽核结果。"""
        return self.post(AUDIT_REPORT_PATH, json=reimburse_body)

    def get_result_code(self, item: dict[str, Any]) -> str:
        """从稽核结果节点里提取统一的结果码。"""
        result = item.get("result")
        if isinstance(result, dict):
            return result.get("code") or ""
        return item.get("resultCode") or ""

    def read_hit_certificates(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        """读取某条稽核规则命中的凭证列表，并保留规则 requiredType。"""
        hit_standard = item.get("hitStandard")
        if isinstance(hit_standard, str):
            try:
                hit_standard = json.loads(hit_standard)
            except json.JSONDecodeError:
                return []
        if not isinstance(hit_standard, dict):
            return []
        certificates: list[dict[str, Any]] = []
        for cert in hit_standard.get("certificates") or []:
            if not isinstance(cert, dict):
                continue
            certificates.append({
                "certCode": cert.get("certCode"),
                "certName": cert.get("certName"),
                "requiredType": hit_standard.get("requiredType"),
            })
        return certificates

    def is_required_certificate(self, required_type: Any) -> bool:
        """判断凭证规则是否属于必填类。"""
        return required_type in REQUIRED_CERTIFICATE_TYPES

    def certificate_keys(self, certificate: dict[str, Any]) -> list[str]:
        """返回凭证可用于匹配的 key，兼容 certCode 和 certName 不一致的情况。"""
        keys: list[str] = []
        for key in (certificate.get("certCode"), certificate.get("certName")):
            if key and key not in keys:
                keys.append(key)
        return keys

    def build_audit_map(self, audit_report_response: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """把稽核报告里的凭证完整性结果整理成 certCode -> 结果 的映射。"""
        audit_report = audit_report_response.get("auditReport", audit_report_response)
        audit_map: dict[str, dict[str, Any]] = {}

        for item in audit_report.get("economicMatterAuditResults") or []:
            if "凭证完整性" in str(item.get("inquiryContent") or ""):
                self.add_audit_item(audit_map, item)

        for fee_action in audit_report.get("feeActionAuditResults") or []:
            for inquiry in fee_action.get("feeActionInquiryAuditResults") or []:
                if "凭证完整性" not in str(inquiry.get("inquiryContent") or ""):
                    continue
                for audit_result in inquiry.get("auditResultList") or []:
                    for item in audit_result.get("resultList") or []:
                        if "凭证完整性" in str(item.get("inquiryContent") or ""):
                            self.add_audit_item(audit_map, item)

        return audit_map

    def add_audit_item(self, audit_map: dict[str, dict[str, Any]], item: dict[str, Any]) -> None:
        """把单条稽核规则命中的凭证写入映射，reject 优先保留。"""
        code = self.get_result_code(item)
        result = "pass" if code in ("pass", "1") else "reject" if code in ("reject", "-1") else code

        for certificate in self.read_hit_certificates(item):
            key = certificate.get("certCode") or certificate.get("certName")
            if not key:
                continue
            if key in audit_map and audit_map[key]["result"] == "reject":
                continue
            audit_map[key] = {
                "certCode": certificate.get("certCode"),
                "certName": certificate.get("certName"),
                "requiredType": certificate.get("requiredType"),
                "result": result,
                "passed": result == "pass",
            }

    def read_config_certificates(self, config: dict[str, Any], only_required: bool = False) -> list[dict[str, Any]]:
        """从凭证配置里收集凭证信息，only_required=True 时只收集 requiredType=1。"""
        certificates: list[dict[str, Any]] = []

        for group in config.get("matterCertificate") or []:
            for cert in group.get("certificateList") or []:
                if only_required and not self.is_required_certificate(cert.get("requiredType")):
                    continue
                certificates.append({
                    "certCode": cert.get("certCode"),
                    "certName": cert.get("certName"),
                    "isMainCert": group.get("isMainCert", 0),
                    "requiredType": cert.get("requiredType"),
                })

        for fee_action in config.get("feeCertificate") or []:
            for cert in fee_action.get("feeCertRel") or []:
                if only_required and not self.is_required_certificate(cert.get("requiredType")):
                    continue
                certificates.append({
                    "certCode": cert.get("certCode"),
                    "certName": cert.get("certName"),
                    "isMainCert": 0,
                    "requiredType": cert.get("requiredType"),
                })

        return certificates

    def compare_integrity(self, audit_report: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
        """以稽核 result 为准，结合配置补充主凭证和必填凭证缺失信息。"""
        audit_map = self.build_audit_map(audit_report)
        config_map: dict[str, dict[str, Any]] = {}
        for cert in self.read_config_certificates(config):
            for key in self.certificate_keys(cert):
                config_map[key] = cert
        result: list[dict[str, Any]] = []
        handled_keys: set[str] = set()

        for key, audit_result in audit_map.items():
            config_cert = config_map.get(key, {})
            required_type = audit_result.get("requiredType") or config_cert.get("requiredType")
            if not self.is_required_certificate(required_type):
                continue
            result.append({
                **config_cert,
                **audit_result,
                "isMainCert": config_cert.get("isMainCert", 0),
                "requiredType": required_type,
            })
            handled_keys.update(self.certificate_keys(audit_result))

        for cert in self.read_config_certificates(config, only_required=True):
            cert_keys = self.certificate_keys(cert)
            if any(key in handled_keys for key in cert_keys):
                continue
            result.append({**cert, "result": "reject", "passed": False})

        return result

    def get_integrity_status(self, integrity_result: list[dict[str, Any]]) -> str:
        """把最终凭证结果归一成 pass / reject / pending。"""
        missing = [item for item in integrity_result if not item.get("passed")]
        if not missing:
            return "pass"
        if any(item.get("isMainCert") == 1 for item in missing):
            return "reject"
        return "pending"

    def build_card(
            self,
            integrity_result: list[dict[str, Any]],
            certificate_type: dict[str, Any],
    ) -> dict[str, Any]:
        """按是否缺失主凭证，组装给前端的完整卡片。"""
        missing = [item for item in integrity_result if not item.get("passed")]
        missing_names = "、".join(item.get("certName") or item.get("certCode") or "" for item in missing)
        has_missing_main = any(item.get("isMainCert") == 1 for item in missing)

        card = {
            "result": "凭证完整性校验",
            "renderName": "MsgReservation",
            "rootComponent": "base-warp",
            "prop": "mode:single",
            "data": {
                "answer": f"已确认：本次为【{certificate_type.get('certName', '')}】\n正在进行凭证完整性校验...",
                "primaryList": [
                    {
                        "id": str(index + 1),
                        "title": item.get("certName") or item.get("certCode"),
                        "statusText": "已匹配" if item.get("passed") else "缺失",
                        "statusType": "success" if item.get("passed") else "error",
                    }
                    for index, item in enumerate(integrity_result)
                ],
                "secondaryAnswer": (
                    f"根据差旅报销制度，报销时必传{missing_names}，请补充："
                    if missing
                    else "凭证完整性校验通过。"
                ),
                "buttons": [
                    {"label": "补充凭证", "action": "add_voucher","type": "primary",
                     "sendMsgConfig":{ "content":"您补充了凭证，需要重新进行凭证完整性校验", "fieldList":[{"name":"data","parserRule":{}}]},
                     "extra": {}},
                    {"label": "从票夹选择", "action": "select_voucher", "type": "primary", "extra": {}},
                ],
            }
        }

        if missing and not has_missing_main:
            card["rootData"] = {
                "suggestionButtons": [
                    {"label": "暂不传，跳过选择", "action": "sendMsg", "extra": {"content": "暂不传，跳过选择","chatMode":"C2S2A"}}
                ]
            }
        card_str = json.dumps(card, ensure_ascii=False)
        return f"<res-card>{card_str}</res-card>"


    def execute(
            self,
            user_id: str,
            voucher_folder_id: str,
            invoice_id_list: list[str],
            attachment_id_list: list[str],
            budget_project_code: str,
            form_id: str,
    ) -> dict[str, Any]:
        """完整执行凭证完整性流程：创建单据、取草稿、查配置、查稽核、回写 session、返回卡片。"""
        certificate_type = self.get_certificate_type()
        reimbursement_type_code = certificate_type.get("reimbursementTypeCode")
        if not reimbursement_type_code:
            raise LookupError(f"经济事项未返回 reimbursementTypeCode，请确保经济事项为日常/项目差旅")

        if not form_id:
            reimburse_body = self.create_reimburse(reimbursement_type_code)
            form_id = reimburse_body.get("id")
            if not form_id:
                raise LookupError("创建报销单成功，但返回结果中没有 id")
            self.save_draft(reimburse_body)

        update_session(form_data={"form_id": form_id})

        auto_fill_result = auto_fill.fill_and_save(
            form_id=form_id,
            matter_uniq_code=self.matter_uniq_code,
            agent_user_id=user_id,
            invoice_id_list=invoice_id_list,
            attachment_id_list=attachment_id_list,
            reimbursement_type_code=reimbursement_type_code,
            budget_project_id=budget_project_code,
            voucher_folder_id=voucher_folder_id,
            base_url=BASE_URL,
            token=self.token,
        )
        if not auto_fill_result.get("success"):
            raise RuntimeError(auto_fill_result.get("error") or "用户需确保选择的经济事项为报销单类型")

        update_session(form_data={"form_id": form_id})

        latest_reimburse_body = self.get_draft_info(form_id)
        certificate_config = self.get_certificate_config(certificate_type)
        audit_report = self.get_audit_report(latest_reimburse_body)
        integrity_result = self.compare_integrity(audit_report, certificate_config)
        integrity_status = self.get_integrity_status(integrity_result)

        update_session(
            voucher_folder_data={
                "voucher_folder_id": voucher_folder_id,
                "invoice_id_list": invoice_id_list,
                "attachment_id_list": attachment_id_list,
                "matter_uniq_code": self.matter_uniq_code,
                "voucher_folder_integrity": integrity_status,
            },
            form_data={
                "form_id": form_id,
                "reimbursement_type_code": reimbursement_type_code,
                "budget_project_code": budget_project_code if budget_project_code else "",
            }
        )


        if integrity_status == 'pass':
            return certificate_relation_check.run()

        return self.build_card(integrity_result, certificate_type)




def merge_list(old_value: Any, new_value: Any) -> list[str]:
    """把 session 里的 list 和本次入参 list 合并去重。"""
    old_list = old_value if isinstance(old_value, list) else ([] if old_value is None else [old_value])
    new_list = new_value if isinstance(new_value, list) else ([] if new_value is None else [new_value])
    result: list[str] = []
    for item in old_list + new_list:
        if item not in result:
            result.append(item)
    return result


def extract_supplement_certificate_ids(raw_data: Any) -> tuple[list[str], list[str]]:
    """仅识别补充凭证回传格式，其他流程的 data 直接忽略。"""
    if not raw_data:
        return [], []
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except json.JSONDecodeError:
            return [], []
    if not isinstance(raw_data, list):
        return [], []

    invoice_ids: list[str] = []
    attachment_ids: list[str] = []
    for response_item in raw_data:
        if not isinstance(response_item, dict):
            continue
        response = response_item.get("Response")
        if not isinstance(response, dict) or response.get("Error"):
            continue
        response_data = response.get("Data")
        if not isinstance(response_data, dict):
            continue
        certificate_items = response_data.get("invoiceAttachmentList")
        if not isinstance(certificate_items, list):
            continue
        for item in certificate_items:
            if not isinstance(item, dict):
                continue
            invoice = item.get("invoiceInfoDto")
            if isinstance(invoice, dict) and invoice.get("certificateId"):
                certificate_id = str(invoice["certificateId"])
                if certificate_id not in invoice_ids:
                    invoice_ids.append(certificate_id)
            attachment = item.get("attachmentPageVO")
            if isinstance(attachment, dict) and attachment.get("certificateId"):
                certificate_id = str(attachment["certificateId"])
                if certificate_id not in attachment_ids:
                    attachment_ids.append(certificate_id)
    return invoice_ids, attachment_ids


def error_card(result: str) -> str:
    """按前端约定组装异常卡片。"""
    card = {
        "result": result,
        "renderName": "",
        "rootComponent": "base-warp",
        "prop": "mode:single",
    }
    return f"<res-card>\n{json.dumps(card, ensure_ascii=False)}\n</res-card>"


def system_error() -> str:
    """E0001：系统异常，返回固定系统异常卡片。"""
    return error_card("系统异常，请联系管理员")


def interface_result_error(message: str) -> str:
    """E0002：接口返回结果异常，把异常结果告知用户。"""
    return error_card(message)


def input_error(message: str) -> str:
    """E0003：子流程入参异常，返回异常卡片。"""
    return error_card(message)


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
        voucher_folder_id: Optional[str] = None,
        invoice_id_list: Optional[List[str]] = None,
        attachment_id_list: Optional[List[str]] = None,
        matter_uniq_code: Optional[str] = None,
        budget_project_code: Optional[str] = None,
) -> str:
    """skill 入口：从 session 取上下文并执行凭证完整性流程。"""
    session_data: Any = None
    session_loaded = False
    current_variables = get_current_variables()
    token = current_variables.get("token") or current_variables.get("x_agent_token", "")
    try:
        if not token:
            raise ValueError("缺少 token，AI需重新决策")

        session_data = get_session()
        session_loaded = True
        voucher_data = session_data.get("voucher_folder_data") or {}
        form_data = session_data.get("form_data") or {}
        user_id = str(current_variables.get("user_id") or "")
        data_invoice_ids, data_attachment_ids = extract_supplement_certificate_ids(
            current_variables.get("data")
        )
        final_voucher_folder_id = voucher_folder_id or voucher_data.get("voucher_folder_id","")
        final_invoice_id_list = merge_list(
            merge_list(voucher_data.get("invoice_id_list"), invoice_id_list),
            data_invoice_ids,
        )
        final_attachment_id_list = merge_list(
            merge_list(voucher_data.get("attachment_id_list"), attachment_id_list),
            data_attachment_ids,
        )
        final_matter_uniq_code = matter_uniq_code or voucher_data.get("matter_uniq_code","")
        final_budget_project_code = budget_project_code or form_data.get("budget_project_code","")
        form_id = form_data.get("form_id", "")

        if not final_matter_uniq_code:
            raise ValueError(f"缺少 经济事项 matter_uniq_code，AI需重新决策")

        checker = VoucherIntegrityChecker(token=token, matter_uniq_code=final_matter_uniq_code)
        card = checker.execute(
            user_id=user_id,
            voucher_folder_id=final_voucher_folder_id,
            invoice_id_list=final_invoice_id_list,
            attachment_id_list=final_attachment_id_list,
            budget_project_code=final_budget_project_code,
            form_id=form_id,
        )
        return card
    except ValueError as exc:
        return input_error(append_session_debug(str(exc), session_data, session_loaded))
    except RuntimeError as exc:
        return interface_result_error(append_session_debug(str(exc), session_data, session_loaded))
    except LookupError as exc:
        return interface_result_error(append_session_debug(str(exc), session_data, session_loaded))
    except Exception as e:
        return interface_result_error(append_session_debug(str(e), session_data, session_loaded))
