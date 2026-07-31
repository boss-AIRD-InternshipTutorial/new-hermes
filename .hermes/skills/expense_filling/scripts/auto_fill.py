"""
自动填单子流程（auto-fill）

供 main_scripts.py import 使用。负责调用智能填单接口完成报销单自动填充，并在填单成功后暂存草稿。

对外暴露：
    run()              完整填单流程（加载会话 → 凭证夹并集 → 填单暂存 → 审计卡片）
    fill_and_save()    一键填单暂存（纯业务参数，内部自动拼装 body，供同事直接复用）
    call_smart_fill()  单独调用智能填单接口（含重试），供同事直接复用
    call_draft()       单独调用暂存接口，供同事直接复用

参数获取（遵循项目规范）:
    base_url  → get_config()["base_url"]
    token     → get_current_variables()["token"]
    user_id   → get_current_variables()["user_id"]
    会话变量  → get_session() / update_session()

会话状态结构:
    voucher_folder_data:
        voucher_folder_id       str    凭证夹ID（有值时自动拉取夹内票据/附件ID做并集）
        invoice_id_list         list   票据 certificateId 列表
        attachment_id_list      list   附件 certificateId 列表
        matter_uniq_code        str    经济事项唯一码
    form_data:
        is_auto_fill_form       bool   是否已自动填单
        budget_project_id       str    预算项目ID（填单成功后回写）
        form_id                 str    业务系统内单据ID
        reimbursement_type_code str    报销类型编码（如 0150010010046）
        project_code            str    预算项目编码（有值时触发预算项目明细查询）
        dept_id                 str    经办人部门ID（未传时自动通过 queryUserInfoByUserId 获取）
        auth_type_list          list   授权类型列表（未传时自动通过 getConfigByTypeCode 获取）
        reim_user_id_list       list   报销人用户ID列表（未传时自动用 [agent_user_id]）
        reim_dept_id_list       list   报销人部门ID列表（未传时自动用 [dept_id]）
        business_unique_code    str    报销单号（未传时自动从草稿 reimbursementNo 取值）

流程:
    1. 加载会话状态、解析报销人、合并入参
    2. 凭证夹并集：voucher_folder_id 有值时分别调票据/附件接口拉取 certificateId，
       与传入的 invoice_id_list / attachment_id_list 去重合并
    3. ID 前置转换：调用 queryInvoiceAttachmentInfoList，把前端误传的
       invoiceId / recId 转换为 certificateId；查询失败时不阻断主流程
    4. getUnRelateCertificate — 凭证关联查询
    5. getDraftInfoById — 获取草稿完整数据
    6. 如果 project_code 有值 → getProjectAndDetail 查询预算项目明细
    7. _assemble_fill_body — 以草稿为底拼装请求体
    8. call_smart_fill；若前置转换临时失败并导致填单失败，则再次调用
       queryInvoiceAttachmentInfoList 转成 certificateId，重新关联并再填单一次
    9. call_draft
    10. required_audit.RequiredFieldChecker — 单据必填项校验，返回校验结果卡片
    11. 更新会话状态 / 返回结果

依赖接口:
    POST /business/reimburse/certificate/getUnRelateCertificate        凭证关联查询
    POST /business/reimburse/base/getDraftInfoById                     获取草稿
    POST /business/budget/project/getProjectAndDetail                  预算项目明细查询
    POST /business/reimburse/base/smartFill                            智能填单
    POST /business/reimburse/base/draft                                暂存草稿
    GET  /business/reimburse/user/queryUserInfoByUserId                用户信息
    GET  /business/reimburse/schema/getConfigByTypeCode                报销类型配置
    GET   /user-extension/authUser/listByUserNameAndUnitId                            人员查询
    POST /wallets/user/voucher/folder/queryInvoiceListNew             凭证夹内票据查询
    POST /wallets/user/voucher/folder/attachment/list                 凭证夹内附件查询
    POST /wallets/agent/invocation/queryInvoiceAttachmentInfoList     invoiceId/recId 转 certificateId
"""

import json
from typing import Optional

import requests

try:
    from .main_scripts import get_config, get_current_variables, get_session, update_session
except ImportError:
    from main_scripts import get_config, get_current_variables, get_session, update_session

# ==================== 接口地址 ====================
_API_BASE = "/business/reimburse"
_BUDGET_API_BASE = "/business/budget"
_WALLET_API_BASE = "/wallets/user/voucher/folder"
SMART_FILL_API = f"{_API_BASE}/base/smartFill"
DRAFT_API = f"{_API_BASE}/base/draft"
GET_DRAFT_API = f"{_API_BASE}/base/getDraftInfoById"
GET_UNRELATE_CERT_API = f"{_API_BASE}/certificate/getUnRelateCertificate"
GET_PROJECT_API = f"{_BUDGET_API_BASE}/project/getProjectAndDetail"
USER_INFO_API = f"{_API_BASE}/user/queryUserInfoByUserId"
SCHEMA_CONFIG_API = f"{_API_BASE}/schema/getConfigByTypeCode"
PERSON_QUERY_API = "/user-extension/authUser/listByUserNameAndUnitId"
QUERY_FOLDER_INVOICE_API = f"{_WALLET_API_BASE}/queryInvoiceListNew"
QUERY_FOLDER_ATTACH_API = f"{_WALLET_API_BASE}/attachment/list"
QUERY_CERTIFICATE_INFO_API = "/wallets/agent/invocation/queryInvoiceAttachmentInfoList"

# ==================== 经济事项 → 报销类型 兜底映射 ====================
MATTER_TO_REIMB_TYPE = {
    "01001001": "0150010010018",
    "01001020": "0150010010046",
    "020010154": "0150010010037",
}



# ==================== 异常返回工具 ====================

def _system_error_card(detail: str = "") -> str:
    """E0001 系统异常卡片，detail 用于区分异常来源方便排查"""
    payload = {
        "result": "系统异常，请联系管理员123",
        "renderName": "",
        "rootComponent": "base-warp",
        "prop": "mode:single",
    }
    if detail:
        payload["detail"] = detail
    return f"<res-card>\n{json.dumps(payload, ensure_ascii=False)}\n</res-card>"


def _api_result_error_card(message: str) -> str:
    """E0002 接口返回结果异常卡片"""
    payload = {
        "result": message,
        "renderName": "",
        "rootComponent": "base-warp",
        "prop": "mode:single",
    }
    return f"<res-card>\n{json.dumps(payload, ensure_ascii=False)}\n</res-card>"


def _param_error(message: str) -> dict:
    """E0003 子流程入参异常，返回 dict 供 AI 重新决策"""
    return {"errorCode": "E0003", "message": message}


def _person_selection_card(persons: list, person_name: str) -> str:
    """
    人员多选卡片：同名人员超过 1 条时，返回选择卡片让用户确认。

    卡片格式参照 budget_project 的 <res-card> 模式，内含 personList 供前端渲染选择列表。
    """
    person_list = []
    for p in persons:
        item = {
            "userId": str(p.get("userId", "")),
            "userName": p.get("userName", ""),
        }
        dept = p.get("deptName", "")
        job = p.get("jobName", "")
        subtitle = " | ".join(filter(None, [dept, job]))
        if subtitle:
            item["subTitle"] = subtitle
        person_list.append(item)

    card_data = {
        "renderName": "person_selection_card",
        "rootComponent": "base-warp",
        "prop": "mode:single",
        "data": {
            "answer": f"找到多位 [{person_name}]，请选择报销人：",
            "personList": person_list,
        },
    }
    content = json.dumps(card_data, ensure_ascii=False)
    return f"<res-card>{content}</res-card>"


# ==================== 对外 API：填单 / 暂存 ====================

def call_smart_fill(body: dict, base_url: str, token: str,
                    invoice_id_list: list = None,
                    attachment_id_list: list = None) -> Optional[dict]:
    """
    调用智能填单接口。

    正常请求失败后，如果调用方传入了票据/附件 ID，则尝试把前端误传的
    invoiceId / recId 查询并转换为 certificateId，重新查询凭证关联数据、
    更新 body 后再进行一轮填单。未传 ID 时保持原有三参数调用行为。

    :param body: 完整的 smartFill 请求体
    :param base_url: 业务系统基座地址
    :param token: JWT 令牌
    :param invoice_id_list: 票据 certificateId 或前端误传的 invoiceId
    :param attachment_id_list: 附件 certificateId 或前端误传的 recId
    :return: 填单结果 dict（已解包 data 字段），失败返回 None
    """
    headers = {"Authorization": token, "Content-Type": "application/json"}

    def _request(fill_body):
        result = None
        for i in range(2):
            try:
                response = requests.post(
                    f"{base_url}{SMART_FILL_API}",
                    headers=headers, json=fill_body, timeout=150)
                response.raise_for_status()
                result = response.json()
                break
            except requests.RequestException:
                if i == 1:
                    return None
        if not isinstance(result, dict):
            return None
        return result if not isinstance(result.get("data"), dict) else result["data"]

    fill_result = _request(body)
    if fill_result is not None:
        return fill_result

    repair_result = _repair_certificate_ids(
        invoice_id_list, attachment_id_list, base_url, token)
    if repair_result is None:
        return None
    repaired_invoice_ids, repaired_attachment_ids, changed = repair_result
    if not changed:
        return None

    repaired_certs = _query_unrelate_certificates(
        repaired_invoice_ids, repaired_attachment_ids, base_url, token)
    if repaired_certs is None:
        return None

    repaired_body = dict(body)
    cert_info = repaired_body.get("reimburseCertificateInfo", {})
    cert_info = dict(cert_info) if isinstance(cert_info, dict) else {}
    cert_info["unRelateCertificate"] = repaired_certs
    repaired_body["reimburseCertificateInfo"] = cert_info
    return _request(repaired_body)


def call_draft(body: dict, base_url: str, token: str,
               error_details: Optional[list[str]] = None) -> bool:
    """
    调用暂存接口。

    :param body: 完整的草稿请求体（smartFill 返回结果）
    :param base_url: 业务系统基座地址
    :param token: JWT 令牌
    :param error_details: 可选的错误详情接收列表
    :return: True 成功，False 失败
    """
    headers = {"Authorization": token, "Content-Type": "application/json"}

    try:
        response = requests.post(f"{base_url}{DRAFT_API}",
                                 headers=headers, json=body, timeout=200)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        if error_details is not None:
            response = getattr(exc, "response", None)
            if response is not None:
                error_details.append(
                    f"status={response.status_code}, body={response.text}"
                )
            else:
                error_details.append(str(exc))
        return False


# ==================== 内部：body 拼装 ====================

def _assemble_fill_body(draft_data: dict, form_id: str, matter_uniq_code: str,
                        agent_user_id: str, agent_user_name: str,
                        reimbursement_type_code: str, voucher_folder_id: str,
                        unrelate_certs: list,
                        budget_project_id: str = None,
                        project_detail: dict = None) -> dict:
    """以草稿为底版拼装 smartFill/draft 请求体，fill_and_save 和 run 共用。"""
    body = dict(draft_data)
    body["fillModel"] = "0"
    body["id"] = form_id
    body.setdefault("version", 0)
    if agent_user_id:
        body["agentUserId"] = agent_user_id
    if voucher_folder_id:
        body["certificateFolderId"] = voucher_folder_id

    # basicInfo
    basic = body.get("basicInfo", {})
    if not isinstance(basic, dict):
        basic = {}
    basic["economicMatterTypeId"] = matter_uniq_code
    basic["reimbursementTypeCode"] = reimbursement_type_code
    if not basic.get("applyUsers"):
        basic["applyUsers"] = [{
            "feeActionPersonCategory": "417001",
            "feeActionPersonUserId": agent_user_id,
            "feeActionPersonUserName": agent_user_name,
        }]
    body["basicInfo"] = basic

    # projectInfo
    if project_detail:
        body["projectInfo"] = [project_detail]
    elif budget_project_id and not body.get("projectInfo"):
        body["projectInfo"] = [{"projectId": budget_project_id}]

    # certificateInfo
    if unrelate_certs:
        cert_info = body.get("reimburseCertificateInfo", {})
        if not isinstance(cert_info, dict):
            cert_info = {}
        cert_info["unRelateCertificate"] = unrelate_certs
        body["reimburseCertificateInfo"] = cert_info

    return body


# ==================== 对外 API：一键填单暂存（无需组装 body） ====================

def fill_and_save(form_id: str,
                  matter_uniq_code: str,
                  agent_user_id: str,
                  agent_user_name: str = "",
                  invoice_id_list: list = None,
                  attachment_id_list: list = None,
                  reimbursement_type_code: str = None,
                  budget_project_id: str = None,
                  voucher_folder_id: str = "",
                  base_url: str = None,
                  token: str = None):
    """
    一键智能填单 + 暂存。调用方无需了解 body 结构、草稿加载、凭证关联等细节。

    内部自动完成:
        1. 参数自动补全（base_url / token / reimbursement_type_code）
        2. 凭证关联查询 (getUnRelateCertificate)
        3. 草稿获取并作为 body 底版 (getDraftInfoById)
        4. body 拼装（basicInfo / projectInfo / certificateInfo）
        5. call_smart_fill → 智能填单
        6. call_draft → 暂存草稿

    :param form_id:           业务系统单据ID（必须）
    :param matter_uniq_code:  经济事项唯一码（必须）
    :param agent_user_id:     报销人用户ID（必须）
    :param agent_user_name:   报销人姓名
    :param invoice_id_list:   票据 certificateId 列表
    :param attachment_id_list: 附件 certificateId 列表
    :param reimbursement_type_code: 报销类型编码，None 则从 MATTER_TO_REIMB_TYPE 查兜底
    :param budget_project_id: 预算项目ID（先在 body.projectInfo 插一个占位）
    :param voucher_folder_id: 凭证夹ID
    :param base_url:          业务系统基座，None 则从 get_config() 获取
    :param token:             JWT 令牌，None 则从 get_current_variables() 获取

    :return: {"success": True, "fill_result": {...}} 或
             {"success": False, "error": "..."}
    """
    # 复制一份，避免凭证夹合并、ID 修复时修改调用方传入的原始 list。
    invoice_id_list = list(invoice_id_list or [])
    attachment_id_list = list(attachment_id_list or [])

    # ---- 参数自动补全 ----
    if base_url is None:
        base_url = get_config().get("base_url", "")
    if token is None:
        try:
            token = get_current_variables().get("token", "")
        except Exception:
            token = ""
    if not reimbursement_type_code:
        reimbursement_type_code = MATTER_TO_REIMB_TYPE.get(matter_uniq_code, "")

    # ---- 前置校验 ----
    if not token:
        return {"success": False, "error": "token不能为空"}
    if not form_id:
        return {"success": False, "error": "缺少 form_id"}
    if not matter_uniq_code:
        return {"success": False, "error": "缺少 matter_uniq_code"}
    if not agent_user_id:
        return {"success": False, "error": "缺少 agent_user_id"}
    if not reimbursement_type_code:
        return {"success": False, "error": f"未配置经济事项 {matter_uniq_code} 的报销类型"}

    headers = {"Authorization": token, "Content-Type": "application/json"}


    # ---- 凭证夹 → 并集（夹内票据/附件分别拉取 certificateId 合并） ----
    if voucher_folder_id:
        folder_result = _query_voucher_folder_items(voucher_folder_id, base_url, token)
        if folder_result is not None:
            folder_inv_ids, folder_att_ids = folder_result
            for cid in folder_inv_ids:
                if cid and cid not in invoice_id_list:
                    invoice_id_list.append(cid)
            for cid in folder_att_ids:
                if cid and cid not in attachment_id_list:
                    attachment_id_list.append(cid)

    # ---- Step 1: ID 前置转换 ----
    # 查询命中的 invoiceId/recId 替换成 certificateId；未命中或接口失败时
    # 保留原 ID 继续执行，call_smart_fill 中仍有失败后的二次兜底。
    preflight_result = _repair_certificate_ids(
        invoice_id_list, attachment_id_list, base_url, token)
    if preflight_result is not None:
        repaired_invoice_ids, repaired_attachment_ids, changed = preflight_result
        if changed:
            invoice_id_list = repaired_invoice_ids
            attachment_id_list = repaired_attachment_ids

    # ---- Step 2: 凭证关联 ----
    unrelate_certs = _query_unrelate_certificates(
        invoice_id_list, attachment_id_list, base_url, token)
    if unrelate_certs is None:
        repair_result = _repair_certificate_ids(
            invoice_id_list, attachment_id_list, base_url, token)
        if repair_result is not None:
            repaired_invoice_ids, repaired_attachment_ids, changed = repair_result
            if changed:
                invoice_id_list = repaired_invoice_ids
                attachment_id_list = repaired_attachment_ids
                unrelate_certs = _query_unrelate_certificates(
                    invoice_id_list, attachment_id_list, base_url, token)
    if unrelate_certs is None:
        return {"success": False, "error": "凭证关联查询失败"}

    # ---- Step 3: 获取草稿作为 body 底版 ----
    try:
        r = requests.post(f"{base_url}{GET_DRAFT_API}?id={form_id}",
                          headers=headers, timeout=30)
        r.raise_for_status()
        draft_data = r.json()
    except requests.RequestException:
        return {"success": False, "error": "获取草稿失败"}

    # ---- Step 4: 拼装 body + 填单暂存 ----
    body = _assemble_fill_body(
        draft_data=draft_data, form_id=form_id, matter_uniq_code=matter_uniq_code,
        agent_user_id=agent_user_id, agent_user_name=agent_user_name,
        reimbursement_type_code=reimbursement_type_code,
        voucher_folder_id=voucher_folder_id, unrelate_certs=unrelate_certs,
        budget_project_id=budget_project_id,
    )

    fill = call_smart_fill(
        body, base_url, token,
        invoice_id_list=invoice_id_list,
        attachment_id_list=attachment_id_list,
    )
    if fill is None:
        return {"success": False, "error": "智能填单失败"}

    draft_errors: list[str] = []
    if not call_draft(fill, base_url, token, error_details=draft_errors):
        detail = f": {draft_errors[0]}" if draft_errors else ""
        return {"success": False, "error": f"暂存单据失败{detail}"}

    return {"success": True, "fill_result": fill}


# ==================== 完整填单流程 ====================

def run(token: str = None, base_url: str = None,
        agent_user_id: str = None, agent_user_name: str = "",
        invoice_id_list: list = None, attachment_id_list: list = None,
        matter_uniq_code: str = None,
        use_current_draft: bool = False):
    """
    自动填单子流程入口。

    所有参数均可选（None 表示自动解析）：
        token             - JWT 令牌，None 则从 get_current_variables() 获取
        base_url          - 业务系统基座，None 则从 get_config() 获取
        agent_user_id     - 报销人ID，None 则从 get_current_variables() 获取
        agent_user_name   - 报销人姓名，无ID时通过查询接口解析
        invoice_id_list   - 票据 certificateId，传入后增量合并到会话
        attachment_id_list- 附件 certificateId，传入后增量合并到会话
        matter_uniq_code  - 经济事项唯一码，传入后覆盖会话
        use_current_draft - True 时使用已填写的最新草稿进入必填项校验，
                            不再重复查询凭证、智能填单和暂存

    返回: "<res-card>..."（成功，校验结果卡片）或 异常卡片 / E0003 错误码
    """
    # ==================== 自动解析未传入的参数 ====================
    if base_url is None:
        base_url = get_config().get("base_url", "")

    if token is None or agent_user_id is None:
        try:
            cv = get_current_variables()
        except Exception:
            cv = {}
        if token is None:
            token = cv.get("token", "")
        if agent_user_id is None:
            agent_user_id = str(cv.get("user_id", "") or "")

    # ==================== 加载会话状态 ====================
    session = get_session()
    vfd = session.get("voucher_folder_data", {}) or {}
    fd = session.get("form_data", {}) or {}

    # ==================== 报销人解析 ====================
    if not agent_user_id:
        if agent_user_name:
            persons = _query_persons(agent_user_name, base_url, token)
            if persons is None:
                return _system_error_card("人员查询接口调用失败（listByUserNameAndUnitId）")
            if len(persons) == 0:
                return _api_result_error_card(f"未检索到 [{agent_user_name}] 的相关人员，请提供报销人的完整姓名")
            if len(persons) == 1:
                agent_user_id = str(persons[0]["userId"])
                agent_user_name = persons[0].get("userName", agent_user_name)
            else:
                return _person_selection_card(persons, agent_user_name)
        else:
            return _param_error("缺少报销人ID或姓名")

    # ==================== 增量合并入参到会话状态 ====================
    vfd_up = {}
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

    # ==================== 取会话最新值 ====================
    invoice_id_list = vfd.get("invoice_id_list", []) or []
    attachment_id_list = vfd.get("attachment_id_list", []) or []
    matter_uniq_code = vfd.get("matter_uniq_code", "")
    form_id = fd.get("form_id", "")
    voucher_folder_id = vfd.get("voucher_folder_id", "")
    budget_project_id = fd.get("budget_project_id", "")
    reimbursement_type_code = fd.get("reimbursement_type_code", "") or MATTER_TO_REIMB_TYPE.get(matter_uniq_code, "")

    # ---- 预算项目查询相关字段（上游子流程写入 form_data，缺失时自动补全） ----
    project_code = fd.get("budget_project_code", "")
    dept_id = fd.get("dept_id", "")
    auth_type_list = fd.get("auth_type_list", []) or []
    reim_user_id_list = fd.get("reim_user_id_list", []) or []
    reim_dept_id_list = fd.get("reim_dept_id_list", []) or []
    business_unique_code = fd.get("business_unique_code", "")

    # ==================== 前置校验 ====================
    if not token:
        return _param_error("token不能为空")
    if not matter_uniq_code:
        return _param_error("缺少经济事项")
    if not form_id:
        return _param_error("会话中无单据ID")
    if not reimbursement_type_code:
        return _param_error(f"未配置经济事项 {matter_uniq_code} 的报销类型")

    # ==================== 凭证夹 → 并集（夹内票据/附件分别拉取 certificateId 合并） ====================
    if voucher_folder_id and not use_current_draft:
        folder_result = _query_voucher_folder_items(voucher_folder_id, base_url, token)
        if folder_result is not None:
            folder_inv_ids, folder_att_ids = folder_result
            for cid in folder_inv_ids:
                if cid and cid not in invoice_id_list:
                    invoice_id_list.append(cid)
            for cid in folder_att_ids:
                if cid and cid not in attachment_id_list:
                    attachment_id_list.append(cid)

    # ==================== Step 0: ID 前置转换 + 凭证关联查询 ====================
    unrelate_certs = []
    if not use_current_draft:
        preflight_result = _repair_certificate_ids(
            invoice_id_list, attachment_id_list, base_url, token)
        if preflight_result is not None:
            repaired_invoice_ids, repaired_attachment_ids, changed = preflight_result
            if changed:
                invoice_id_list = repaired_invoice_ids
                attachment_id_list = repaired_attachment_ids
                # 会话中这两个字段约定存 certificateId，修正后立即回写，
                # 避免后续轮次继续拿 invoiceId/recId 重复转换。
                update_session(voucher_folder_data={
                    "invoice_id_list": invoice_id_list,
                    "attachment_id_list": attachment_id_list,
                })

        unrelate_certs = _query_unrelate_certificates(
            invoice_id_list, attachment_id_list, base_url, token)
        if unrelate_certs is None:
            # 前置查询可能只是临时失败；关联失败时再转换一次作为兜底。
            repair_result = _repair_certificate_ids(
                invoice_id_list, attachment_id_list, base_url, token)
            if repair_result is not None:
                repaired_invoice_ids, repaired_attachment_ids, changed = repair_result
                if changed:
                    invoice_id_list = repaired_invoice_ids
                    attachment_id_list = repaired_attachment_ids
                    update_session(voucher_folder_data={
                        "invoice_id_list": invoice_id_list,
                        "attachment_id_list": attachment_id_list,
                    })
                    unrelate_certs = _query_unrelate_certificates(
                        invoice_id_list, attachment_id_list, base_url, token)
        if unrelate_certs is None:
            return _api_result_error_card("凭证关联查询失败")

    # ==================== Step 1: 获取草稿完整数据 ====================
    headers = {"Authorization": token, "Content-Type": "application/json"}

    try:
        r = requests.post(f"{base_url}{GET_DRAFT_API}?id={form_id}",
                          headers=headers, timeout=30)
        r.raise_for_status()
        draft_data = r.json()
    except requests.RequestException:
        return _api_result_error_card("获取草稿失败")

    # 关联校验进入此分支前，凭证完整性流程已经完成过
    # fill_and_save。此时草稿中也没有未关联凭证，重复调用
    # smartFill 会对同一版本的草稿再次填单和暂存。
    if use_current_draft:
        project_info = draft_data.get("projectInfo") or []
        session_update = {
            "is_auto_fill_form": True,
            "form_id": form_id,
            "required_field_check": True,
        }
        if project_info and isinstance(project_info[0], dict):
            session_update["budget_project_id"] = str(
                project_info[0].get("projectId") or ""
            )
        update_session(form_data=session_update)

        try:
            from .required_audit import run as audit_run
        except ImportError:
            from required_audit import run as audit_run
        return audit_run()

    # businessUniqueCode 未传时，从草稿的 reimbursementNo 自动获取
    if not business_unique_code:
        business_unique_code = draft_data.get("reimbursementNo", "")

    # ==================== Step 2: 预算项目明细查询 ====================
    project_detail = None
    if project_code:
        if not dept_id:
            user_info = _query_user_info(agent_user_id, base_url, token)
            if user_info is None:
                return _system_error_card("用户信息查询接口调用失败（queryUserInfoByUserId）")
            dept_id = user_info.get("deptId", "")
        if not reim_user_id_list:
            reim_user_id_list = [int(agent_user_id)] if agent_user_id.isdigit() else []
        if not reim_dept_id_list:
            reim_dept_id_list = [int(dept_id)] if dept_id and dept_id.isdigit() else []
        if not auth_type_list:
            auth_type_list = _query_project_select_source_types(
                base_url, token, reimbursement_type_code) or []

        project_detail = _query_project_detail(
            base_url=base_url, token=token,
            project_code_or_name=project_code,
            user_id=int(agent_user_id) if agent_user_id.isdigit() else agent_user_id,
            dept_id=dept_id,
            auth_type_list=auth_type_list,
            reim_user_id_list=reim_user_id_list,
            reim_dept_id_list=reim_dept_id_list,
            business_type=reimbursement_type_code,
            business_unique_code=business_unique_code,
            matter_uniq_code=matter_uniq_code,
        )
        if project_detail is None:
            return _api_result_error_card(f"预算项目查询失败: {project_code}")

    # ==================== Step 3: 拼装 body + 填单暂存 ====================
    body = _assemble_fill_body(
        draft_data=draft_data, form_id=form_id, matter_uniq_code=matter_uniq_code,
        agent_user_id=agent_user_id, agent_user_name=agent_user_name,
        reimbursement_type_code=reimbursement_type_code,
        voucher_folder_id=voucher_folder_id, unrelate_certs=unrelate_certs,
        budget_project_id=budget_project_id, project_detail=project_detail,
    )

    fill = call_smart_fill(
        body, base_url, token,
        invoice_id_list=invoice_id_list,
        attachment_id_list=attachment_id_list,
    )
    if fill is None:
        return _api_result_error_card("智能填单失败")

    draft_errors: list[str] = []
    if not call_draft(fill, base_url, token, error_details=draft_errors):
        detail = f": {draft_errors[0]}" if draft_errors else ""
        return _api_result_error_card(f"暂存单据失败{detail}")

    # ==================== 更新会话状态 ====================
    pi = fill.get("projectInfo", [])
    up = {"is_auto_fill_form": True, "form_id": form_id, "required_field_check": True}
    if pi:
        up["budget_project_id"] = str(pi[0].get("projectId", ""))
    update_session(form_data=up)

    # ==================== Step 5: 必填项校验 ====================
    try:
        from .required_audit import run as audit_run
    except ImportError:
        from required_audit import run as audit_run
    return audit_run()


# ==================== 内部辅助函数 ====================



def _query_user_info(user_id: str, base_url: str, token: str) -> Optional[dict]:
    """GET /business/reimburse/user/queryUserInfoByUserId?userId=xxx"""
    try:
        r = requests.get(
            f"{base_url}{USER_INFO_API}?userId={user_id}",
            headers={"Authorization": token}, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.RequestException:
        return None


def _query_project_select_source_types(base_url: str, token: str,
                                       reimbursement_type_code: str) -> Optional[list]:
    """GET /business/reimburse/schema/getConfigByTypeCode → reimburseConfig.projectSelectSourceTypes"""
    try:
        r = requests.get(
            f"{base_url}{SCHEMA_CONFIG_API}"
            f"?reimbursementTypeCode={reimbursement_type_code}&clientType=0",
            headers={"Authorization": token}, timeout=15)
        r.raise_for_status()
        config = (r.json().get("reimburseConfig") or {})
        return config.get("projectSelectSourceTypes")
    except requests.RequestException:
        return None


def _query_project_detail(base_url: str, token: str,
                          project_code_or_name: str,
                          user_id: int, dept_id: str,
                          auth_type_list: list, reim_user_id_list: list,
                          reim_dept_id_list: list,
                          business_type: str, business_unique_code: str,
                          matter_uniq_code: str) -> Optional[dict]:
    """POST /business/budget/project/getProjectAndDetail"""
    headers = {"Authorization": token, "Content-Type": "application/json"}
    body = {
        "isLeaf": 1,
        "projectClass": "1",
        "userId": user_id,
        "deptId": dept_id,
        "authTypeList": auth_type_list,
        "reimUserIdList": reim_user_id_list,
        "reimDeptIdList": reim_dept_id_list,
        "businessType": business_type,
        "businessUniqueCode": business_unique_code,
        "matterUniqCode": matter_uniq_code,
        "projectTypeCode": "",
        "projectCodeOrName": project_code_or_name,
    }
    try:
        r = requests.post(
            f"{base_url}{GET_PROJECT_API}?page=1&size=10",
            headers=headers, json=body, timeout=30)
        r.raise_for_status()
        content = (r.json().get("content") or [])
        return content[0] if content else {}
    except requests.RequestException:
        return None


def _query_unrelate_certificates(invoice_id_list: list, attachment_id_list: list,
                                 base_url: str, token: str) -> Optional[list]:
    """POST /business/reimburse/certificate/getUnRelateCertificate"""
    headers = {"Authorization": token, "Content-Type": "application/json"}
    body = {
        "certIdList": invoice_id_list or [],
        "attachmentIdList": attachment_id_list or [],
    }
    if not body["certIdList"] and not body["attachmentIdList"]:
        return []
    try:
        r = requests.post(f"{base_url}{GET_UNRELATE_CERT_API}",
                          headers=headers, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except requests.RequestException:
        return None


def _repair_certificate_ids(invoice_id_list: list, attachment_id_list: list,
                            base_url: str, token: str) -> Optional[tuple]:
    """
    将前端误传的 invoiceId / recId 转换为填单需要的 certificateId。

    该函数只在凭证关联或智能填单已经失败后调用：
    - invoice_id_list 作为 invoiceIdList 查询；
    - attachment_id_list 作为 attachmentIdList 查询；
    - 查询命中的源 ID 替换为 certificateId；
    - 未命中的值原样保留，因为它可能本来就是合法的 certificateId。

    :return: (票据 certificateId 列表, 附件 certificateId 列表, 是否发生替换)，
             接口调用或返回结构异常时返回 None。
    """
    invoice_ids = list(invoice_id_list or [])
    attachment_ids = list(attachment_id_list or [])
    if not invoice_ids and not attachment_ids:
        return invoice_ids, attachment_ids, False

    headers = {"Authorization": token, "Content-Type": "application/json"}
    body = {
        "invoiceIdList": invoice_ids,
        "attachmentIdList": attachment_ids,
    }
    try:
        response = requests.post(
            f"{base_url}{QUERY_CERTIFICATE_INFO_API}",
            headers=headers, json=body, timeout=30)
        response.raise_for_status()
        payload = response.json()
        response_body = payload.get("Response", {})
        if not isinstance(response_body, dict) or response_body.get("Error"):
            return None
        data_list = response_body.get("Data")
        if not isinstance(data_list, list):
            return None
    except (requests.RequestException, TypeError, ValueError, AttributeError):
        return None

    invoice_map = {}
    attachment_map = {}
    for item in data_list:
        if not isinstance(item, dict):
            continue
        invoice = item.get("invoiceInfoDto")
        if isinstance(invoice, dict):
            source_id = invoice.get("invoiceId")
            certificate_id = invoice.get("certificateId")
            if source_id not in (None, "") and certificate_id not in (None, ""):
                invoice_map[str(source_id)] = str(certificate_id)
        attachment = item.get("attachmentPageVO")
        if isinstance(attachment, dict):
            source_id = attachment.get("recId")
            certificate_id = attachment.get("certificateId")
            if source_id not in (None, "") and certificate_id not in (None, ""):
                attachment_map[str(source_id)] = str(certificate_id)

    changed = False

    def _replace_and_dedupe(values, value_map):
        nonlocal changed
        result = []
        seen = set()
        for value in values:
            replacement = value_map.get(str(value), value)
            if str(replacement) != str(value):
                changed = True
            marker = str(replacement)
            if marker not in seen:
                seen.add(marker)
                result.append(replacement)
        return result

    repaired_invoice_ids = _replace_and_dedupe(invoice_ids, invoice_map)
    repaired_attachment_ids = _replace_and_dedupe(attachment_ids, attachment_map)
    return repaired_invoice_ids, repaired_attachment_ids, changed


def _query_persons(name: str, base_url: str, token: str, agencyId: str = "") -> Optional[list]:
    """GET /authUser/listByUserNameAndUnitId?userName=xxx"""
    try:
        r = requests.get(f"{base_url}{PERSON_QUERY_API}",
                         headers={"Authorization": token},
                         params={"userName": name, **( {"agencyId": agencyId} if agencyId else {})},
                         timeout=15)
        r.raise_for_status()
        d = r.json()
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            inner = d.get("data", [])
            return inner if isinstance(inner, list) else []
        return []
    except requests.RequestException:
        return None


def _query_voucher_folder_items(voucher_folder_id: str,
                                base_url: str, token: str) -> Optional[tuple]:
    """
    分别调票据接口和附件接口，拉取凭证夹内所有 certificateId，按类型拆分。

    - POST /wallets/user/voucher/folder/queryInvoiceListNew  → 票据
    - POST /wallets/user/voucher/folder/attachment/list      → 附件

    :return: (invoice_ids: list, attachment_ids: list) 二元组，失败返回 None
    """
    headers = {"Authorization": token, "Content-Type": "application/json"}


    def _extract_ids(api_path, body):
        try:
            r = requests.post(f"{base_url}{api_path}",
                              headers=headers, json=body, timeout=30)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, list):
                return []
            return [item["certificateId"] for item in data
                    if isinstance(item, dict) and item.get("certificateId")]
        except requests.RequestException:
            return None

    inv_body = {
        "queryKeyWord": None,
        "issueStartDate": None,
        "issueEndDate": None,
        "importStartDate": None,
        "importEndDate": None,
        "voucherFolderId": voucher_folder_id,
    }
    att_body = {
        "queryKeyWord": None,
        "attachmentType": None,
        "attachmentTypeList": [],
        "beginDate": None,
        "endDate": None,
        "voucherFolderId": voucher_folder_id,
    }

    inv_ids = _extract_ids(QUERY_FOLDER_INVOICE_API, inv_body)
    att_ids = _extract_ids(QUERY_FOLDER_ATTACH_API, att_body)

    if inv_ids is None and att_ids is None:
        return None

    return inv_ids or [], att_ids or []
