"""
bill_audit.py — 单据稽核报告查询模块

功能概述：
    根据会话ID和用户token，从业务系统获取单据暂存数据，
    调用稽核接口查询稽核结果，解析后组装为前端可渲染的 Web Card XML 字符串返回。

主入口：
    run() -> str
        成功："<res-card>...</res-card>"
        系统异常："<res-card>...</res-card>"
        接口返回结果异常："<res-card>...</res-card>"
        子流程入参异常："<res-card>...</res-card>"

错误码说明：
    E0001 - 系统异常，接口调用失败或子流程运行报错，返回系统异常卡片
    E0002 - 接口返回结果异常，查询结果为空或业务结果异常，返回接口结果异常卡片
    E0003 - 子流程入参异常，AI决策入参有误，返回异常卡片

调用示例：
    from scripts.bill_audit import run
    result = run()
"""

import json
import traceback

import httpx

try:
    from .main_scripts import get_config, get_current_variables, get_session
except ImportError:
    from main_scripts import get_config, get_current_variables, get_session

__all__ = ["run"]


# HTTP 请求超时时间（秒）
HTTP_TIMEOUT = 120

# 单据名称，用于 buttons 跳转参数 certName
CERT_NAME = "日常差旅报销单"
# 单据详情页路由路径，用于 buttons 跳转参数 path
REIMBURSE_DETAIL_PATH = "plugin://rs-pre-approal/pre-page"

# 稽核结果统计字段映射：(statusType, statusText, auditResultCountDTO源字段)
STATUS_COUNT_MAPPING = [
    ("default", "全部", "allCount"),
    ("success", "通过", "passCount"),
    ("warning", "警告", "warnCount"),
    ("error", "不通过", "rejectCount"),
]

# ================================  自定义异常 ================================================


class BillAuditError(Exception):
    """稽核业务异常，携带错误码和用户友好提示"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


# --------------------------------  业务系统API相关定义START ------------------------------------
getDraftInfoById = "/business/reimburse/base/getDraftInfoById"

getAuditReport = "/business/reimburse/base/getAuditReport"


def _get_base_url() -> str:
    """
    从公共配置动态获取业务系统域名

    :return: 业务系统 base_url，如 http://43.138.207.132:28080/saas-industry106
    """
    return get_config().get("base_url", "")


def _get_getDraftInfoById_api_url() -> str:
    """
    获取 getDraftInfoById 完整接口地址

    :return: http://172.18.163.52:30000/saas-industry/business/reimburse/base/getDraftInfoById
    """
    return _get_base_url().rstrip("/") + getDraftInfoById


def _get_getAuditReport_api_url() -> str:
    """
    获取 getAuditReport 完整接口地址

    :return: http://172.18.163.52:30000/saas-industry/business/reimburse/base/getAuditReport
    """
    return _get_base_url().rstrip("/") + getAuditReport


# --------------------------------  业务系统API相关定义END ------------------------------------


def _get_session_data(session_data: dict | None = None) -> str:
    """
    从当前会话中获取表单ID

    :return: form_id
    :raises BillAuditError: E0003 form_id 取不到
    """
    if session_data is None:
        session_data = get_session()
    form_id = (session_data.get("form_data") or {}).get("form_id")
    if not form_id:
        raise BillAuditError("E0003", "查无结果，请检查输入参数的值是否正确")
    return form_id


def _get_form_draft_data(form_id: str, token: str) -> dict:
    """
    根据表单ID和token获取表单草稿数据

    :param form_id: 表单ID
    :param token: 用户token
    :return: draft_from_data 表单草稿数据
    :raises BillAuditError: E0001 参数无效/鉴权失败；E0003 结果为空
    :raises httpx.TimeoutException: 超时，由 run 统一处理为 E0002
    """
    url = _get_getDraftInfoById_api_url()
    headers = {"Authorization": token, "Content-Type": "application/json"}
    params = {"id": form_id}

    with httpx.Client(trust_env=False) as client:
        response = client.post(
            url, headers=headers, params=params, timeout=HTTP_TIMEOUT
        )

    if response.status_code == 401:
        raise BillAuditError("E0001", "登录已过期，请重新登录后再试")
    if response.status_code == 404:
        raise BillAuditError("E0001", "单据不存在，请确认单据ID是否正确")
    if response.status_code >= 400:
        raise BillAuditError(
            "E0001", f"请求参数无效（HTTP {response.status_code}）：{response.text}"
        )

    draft_from_data = response.json()
    if not draft_from_data:
        raise BillAuditError("E0003", "查无结果，请检查输入参数的值是否正确")
    return draft_from_data


def _do_query_audit_report(draft_from_data: dict, token: str) -> dict:
    """
    根据表单草稿数据和token查询审计报告

    :param draft_from_data: 表单草稿数据
    :param token: 用户token
    :return: form_audit_report 审计报告数据
    :raises BillAuditError: E0001 鉴权失败；E0003 结果为空
    :raises httpx.TimeoutException: 超时，由 run 统一处理为 E0002
    """
    url = _get_getAuditReport_api_url()
    headers = {"Authorization": token, "Content-Type": "application/json"}

    with httpx.Client(trust_env=False) as client:
        response = client.post(
            url, headers=headers, json=draft_from_data, timeout=HTTP_TIMEOUT
        )

    if response.status_code == 401:
        raise BillAuditError("E0001", "登录已过期，请重新登录后再试")
    if response.status_code >= 400:
        raise BillAuditError(
            "E0001", f"请求参数无效（HTTP {response.status_code}）：{response.text}"
        )

    form_audit_report = response.json()
    if not form_audit_report:
        raise BillAuditError("E0003", "查无结果，请检查输入参数的值是否正确")
    return form_audit_report


def _get_simple_report(form_audit_report: dict) -> tuple:
    """
    从稽核报告中提取四个核心字段

    :param form_audit_report: 稽核报告完整数据
    :return: (invoiceAuditResultDTOList, feeActionAuditResults, economicMatterAuditResults, auditResultCountDTO)
    """
    audit_report = form_audit_report.get("auditReport", {})
    invoice_audit_result_dto_list = audit_report.get("invoiceAuditResultDTOList", [])
    fee_action_audit_results = audit_report.get("feeActionAuditResults", [])
    economic_matter_audit_results = audit_report.get("economicMatterAuditResults", [])
    audit_result_count_dto = audit_report.get("auditResultCountDTO", {})

    return (
        invoice_audit_result_dto_list,
        fee_action_audit_results,
        economic_matter_audit_results,
        audit_result_count_dto,
    )


def _analysis_report_data(
    form_id, cert_code, cert_name, economic_matter_type_id, form_audit_report: dict
) -> dict:
    """
    将稽核报告解析组装为 web_card 的 data 部分

    :param form_audit_report: 完整稽核报告对象
    :return: web_card data 数据
    """
    (
        invoice_audit_result_dto_list,
        fee_action_audit_results,
        economic_matter_audit_results,
        audit_result_count_dto,
    ) = _get_simple_report(form_audit_report)

    # 步骤1：解析 audit_result_count_dto，生成 status
    status_data = [
        {
            "statusType": status_type,
            "statusText": status_text,
            "count": audit_result_count_dto.get(source_field, 0),
        }
        for status_type, status_text, source_field in STATUS_COUNT_MAPPING
    ]
    status = {
        "title": "稽核结果",
        "data": status_data,
    }

    error_count = audit_result_count_dto.get("rejectCount", 0)
    if error_count > 0:
        answer = f"单据已填写完成并自动为您稽核，{error_count}条稽核点预检查未通过，请处理后提交"
        primary_list = _create_primary_list(
            invoice_audit_result_dto_list,
            fee_action_audit_results,
            economic_matter_audit_results,
        )
        button_label = "去处理拦截项"

    else:
        answer = "单据已填写完成并稽核通过，确认无误可以提交审核啦～"
        primary_list = []
        button_label = "查看单据并提交"

    buttons = [
        {
            "type": "primary",
            "label": button_label,
            "action": "route",
            "showType": "link",
            "extra": {
                "path": REIMBURSE_DETAIL_PATH,
                "params": {
                    "draftId": form_id,
                    "certCode": cert_code,
                    "certName": cert_name,
                    "certType": "reimb",
                    "economicMatterTypeId": economic_matter_type_id,
                },
            },
        }
    ]

    return {
        "answer": answer,
        "status": status,
        "primaryList": primary_list,
        "buttons": buttons,
    }


def _build_economic_matter_items(economic_matter_audit_results: list) -> list:
    """
    生成事项稽核（h1）的不通过子项列表，无错误时返回 []

    :param economic_matter_audit_results: 经济事项稽核结果列表
    :return: [header, item, ...] 或 []
    """
    grouped_items: dict[str, dict] = {}
    for item in economic_matter_audit_results:
        if item.get("result", {}).get("code") != "reject":
            continue
        title = item.get("inquiryContent", "")
        desc = _read_missing_certificate_desc(item)
        grouped_item = grouped_items.setdefault(
            title,
            {
                "title": title,
                "statusType": "error",
                "descParts": [],
            },
        )
        _append_desc_part(grouped_item["descParts"], desc)

    if not grouped_items:
        return []
    items = []
    for idx, grouped_item in enumerate(grouped_items.values()):
        items.append(
            {
                "id": str(100 + idx),
                "title": grouped_item["title"],
                "statusType": grouped_item["statusType"],
                "desc": _join_desc_parts(grouped_item["descParts"]),
            }
        )
    return [{"id": "h1", "title": "事项稽核", "isHeader": True}] + items


def _append_desc_part(desc_parts: list[str], desc: str) -> None:
    """合并同类事项稽核描述，避免重复渲染多张卡片。"""
    if not desc:
        return
    part = desc[2:] if desc.startswith("缺少") else desc
    if part and part not in desc_parts:
        desc_parts.append(part)


def _join_desc_parts(desc_parts: list[str]) -> str:
    """同一事项稽核项的描述统一展示在一行。"""
    if not desc_parts:
        return ""
    return "\n".join(f"缺少{part}" for part in desc_parts)


def _loads_json_object(value) -> dict:
    """接口部分字段是 JSON 字符串，解析失败时兜底为空 dict。"""
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return result if isinstance(result, dict) else {}


def _read_missing_certificate_desc(item: dict) -> str:
    """事项凭证完整性拦截时，优先展示缺少的凭证名称。"""
    content = _loads_json_object(item.get("content"))
    cert_name = content.get("name") or content.get("certName")
    if cert_name:
        return f"缺少{cert_name}"

    hit_standard = _loads_json_object(item.get("hitStandard"))
    certificates = hit_standard.get("certificates")
    if isinstance(certificates, list):
        names = [
            cert.get("certName") or cert.get("name")
            for cert in certificates
            if isinstance(cert, dict) and (cert.get("certName") or cert.get("name"))
        ]
        if names:
            return "缺少" + "、".join(str(name) for name in names)

    return item.get("explain", "")


def _build_fee_action_items(fee_action_audit_results: list) -> list:
    """
    生成费用稽核（h2）的不通过子项列表，无错误时返回 []

    :param fee_action_audit_results: 费用行为稽核结果列表
    :return: [header, item, ...] 或 []
    """
    items = []
    idx = 0
    for fee_action in fee_action_audit_results:
        fee_type_name = fee_action.get("feeActionTypeUniqueName", "")
        for detail in fee_action.get("feeActionDetailAuditResultList", []):
            for audit_result in detail.get("auditResultList", []):
                for result in audit_result.get("resultList", []):
                    if result.get("result", {}).get("code") != "reject":
                        continue
                    row_index = result.get("rowIndex", 0)
                    inquiry_content = result.get("inquiryContent", "")
                    explain = result.get("explain", "")
                    items.append(
                        {
                            "id": str(200 + idx),
                            "title": f"{fee_type_name}-{inquiry_content}",
                            "statusType": "error",
                            "desc": f"[第{row_index + 1}行]{explain}",
                        }
                    )
                    idx += 1

    if not items:
        return []
    return [{"id": "h2", "title": "费用稽核", "isHeader": True}] + items


def _collect_failed_check_rules(check_rule_results: list, failed: list) -> None:
    """
    递归遍历 checkRuleResults 树，收集所有 result=="0" 的节点

    :param check_rule_results: checkRuleResults 节点列表
    :param failed: 收集结果（原地追加）
    """
    for rule in check_rule_results or []:
        if rule.get("result") == "-1":
            failed.append(
                {
                    "title": rule.get("ruleItemCode", ""),
                    "desc": rule.get("ruleItemDesc", ""),
                }
            )
        _collect_failed_check_rules(rule.get("children") or [], failed)


def _build_invoice_items(invoice_audit_result_dto_list: list) -> list:
    """
    生成凭证稽核（h3）的不通过子项列表，无错误时返回 []

    :param invoice_audit_result_dto_list: 发票稽核结果列表
    :return: [header, item, ...] 或 []
    """
    items = []
    idx = 0
    for invoice in invoice_audit_result_dto_list:
        if invoice.get("resultCode") != "-1":
            continue
        failed_rules = []
        _collect_failed_check_rules(invoice.get("checkRuleResults") or [], failed_rules)
        for rule in failed_rules:
            items.append(
                {
                    "id": str(300 + idx),
                    "title": rule["title"],
                    "statusType": "error",
                    "desc": rule["desc"],
                }
            )
            idx += 1

    if not items:
        return []
    return [{"id": "h3", "title": "凭证稽核", "isHeader": True}] + items


def _create_primary_list(
    invoice_audit_result_dto_list: list,
    fee_action_audit_results: list,
    economic_matter_audit_results: list,
) -> list:
    """
    生成 primaryList，汇总各维度稽核不通过项，兜底返回 []

    :param invoice_audit_result_dto_list: 发票稽核结果列表
    :param fee_action_audit_results: 费用行为稽核结果列表
    :param economic_matter_audit_results: 经济事项稽核结果列表
    :return: primaryList 列表
    """
    economic_items = _build_economic_matter_items(economic_matter_audit_results)
    fee_items = _build_fee_action_items(fee_action_audit_results)
    invoice_items = _build_invoice_items(invoice_audit_result_dto_list)

    return economic_items + fee_items + invoice_items


def _analysis_audit_report(
    form_id, cert_code, cert_name, economic_matter_type_id, form_audit_report: dict
) -> dict:
    """
    解析审计报告，提取web_card所需数据

    :param form_audit_report: 审计报告数据
    :return: web_card 卡片数据对象
    """
    # 步骤3：解析组装 data 部分
    data = _analysis_report_data(form_id, cert_code, cert_name, economic_matter_type_id, form_audit_report)

    # 步骤2 & 4：组装固定结构，返回完整 web_card
    web_card = {
        "renderName": "MsgReservation",
        "rootComponent": "base-warp",
        "prop": "mode:sing;itemStyle:large",
        "data": data,
    }
    return web_card


def _build_web_card(web_card: dict) -> str:
    """
    根据web_card数据构建XML字符串

    :param web_card: 卡片数据对象
    :return: <res-card>{...}</res-card> 格式的字符串
    """
    content = json.dumps(web_card, ensure_ascii=False, indent=2)
    return f"<res-card>{content}</res-card>"


def error_card(result: str) -> str:
    card_data = {
        "result": result,
        "renderName": "",
        "rootComponent": "base-warp",
        "prop": "mode:single",
    }
    content = json.dumps(card_data, ensure_ascii=False, indent=2)
    return f"<res-card>{content}</res-card>"


def system_error() -> str:
    """E0001：系统异常，返回固定系统异常卡片。"""
    return error_card("系统异常，请联系管理员")


def interface_result_error(message: str) -> str:
    """E0002：接口返回结果异常，把异常结果告知用户。"""
    return error_card(message)


def input_error(message: str) -> str:
    """E0003：子流程入参异常，返回异常卡片。"""
    return error_card(message)


def append_session_debug(message: str, session_data, session_loaded: bool) -> str:
    """在异常文案中附加会话读取状态，区分空会话和读取失败。"""
    if not session_loaded:
        return f"{message}\nsession获取状态：未成功获取"
    try:
        session_text = json.dumps(session_data, ensure_ascii=False, default=str)
    except Exception:
        session_text = repr(session_data)
    return f"{message}\nsession获取状态：已获取\nsession值：{session_text}"


def run() -> str:
    """
    获取审计报告主入口，集中处理异常并返回统一结构

    :return: "<res-card>...</res-card>"
    """
    session_data = None
    session_loaded = False
    try:
        token = get_current_variables().get("token", "")
        if not token:
            raise ValueError("缺少 token，AI需重新决策")

        session_data = get_session()
        session_loaded = True
        form_id = _get_session_data(session_data)
        draft_from_data = _get_form_draft_data(form_id, token)
        form_audit_report = _do_query_audit_report(draft_from_data, token)
        basic_info = draft_from_data.get("basicInfo", {})
        cert_code = basic_info.get("reimbursementTypeCode", "")
        cert_name = basic_info.get("reimbursementTypeName", "")
        economic_matter_type_id = basic_info.get("economicMatterTypeId", "")
        web_card = _analysis_audit_report(
            form_id, cert_code, cert_name, economic_matter_type_id, form_audit_report
        )
        result = _build_web_card(web_card)
        return result

    except ValueError as exc:
        return input_error(append_session_debug(str(exc), session_data, session_loaded))

    except BillAuditError as exc:
        return interface_result_error(append_session_debug(exc.message, session_data, session_loaded))

    except httpx.TimeoutException:
        return interface_result_error(
            append_session_debug("接口调用超时，请稍后重试", session_data, session_loaded)
        )

    except Exception as exc:
        traceback.print_exc()
        return interface_result_error(append_session_debug(str(exc), session_data, session_loaded))
