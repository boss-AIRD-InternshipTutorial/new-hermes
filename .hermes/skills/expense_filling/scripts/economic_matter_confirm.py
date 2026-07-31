"""
经济事项确认子流程（economic_matter_confirm）

供 main_scripts.py import 使用。负责确认经济事项。

依赖接口:
    POST /wallets/agent/invocation/queryEconomicMatters   查询关联经济事项列表

参数:
    - voucher_folder_id (str): 凭证夹ID
    - invoice_id_list (List[str]): 票据ID列表
    - attachment_id_list (List[str]): 附件ID列表

返回:
    成功: <res-card>{content}</res-card>
    失败: {"code": "E0001", "message": "..."}  参数校验/前置条件不满足
          {"code": "E0002", "message": "..."}  接口调用失败
          {"code": "E9999", "message": "..."}  未知异常

"""

import json
import requests
from typing import Dict, List, Any
import budget_project
import voucher_folder_integrity

try:
    # 包内调用时，直接使用相对导入拿到状态更新函数。
    # from .session_scripts import get_session, update_session, delete_session
    # from .api import list_economic_matter
    from .main_scripts import (
        get_config,
        get_current_session_id,
        get_current_variables,
        get_session,
        update_session,
    )

except ImportError:  # pragma: no cover - 兼容直接运行脚本的场景
    # 兼容把当前脚本单独跑起来的场景。
    # from session_scripts import get_session, update_session, delete_session
    # from api import list_economic_matter
    from main_scripts import (
        get_config,
        get_current_session_id,
        get_current_variables,
        get_session,
        update_session,
    )


def error_card(result):
    message = result["message"]
    card_data = {
        "result": message,
        "renderName": "",
        "rootComponent": "base-warp",
        "prop": "mode:single",
    }
    content = json.dumps(card_data, ensure_ascii=False)
    return f"<res-card>{content}</res-card>"


def api_post(url: str, token: str, data: str):
    try:
        headers = {
            "Authorization": token,  # 只使用一个有效 token
            "Content-Type": "application/json",
        }
        response = requests.post(url, headers=headers, data=data)
        if response.status_code == 200:
            return response.json()["Response"]["Data"]
        else:
            return {"code": "E0002", "message": response.text}
    except Exception as ex:
        return {"code": "E0999", "message": str(ex)}


def api_get(url: str, token: str):
    try:
        headers = {
            "Authorization": token,
            "Content-Type": "application/json",
        }
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
        else:
            return {"code": "E0002", "message": response.text}
    except Exception as ex:
        return {"code": "E0999", "message": str(ex)}


def list_economic_matter(
    base_url: str,
    token: str,
    voucher_folder_id: str,
    invoice_id_list: List[str],
    attachment_id_list: List[str],
    certInstId=True,
) -> List[Dict[str, Any]]:
    """#查询关联经济事项列表

    入参:
    - url (str): 接口地址
    - token (str): 系统登录令牌
    - voucherFolderId integer <int64> 凭证夹ID
    - voucherIdList array[string] 凭证ID列表
    - attachmentIdList array[integer] 附件ID列表

    出参:
    - List[Dict[str, Any]]: 经济事项和凭证类型的对照表
    """
    voucher_folder_id, invoice_id_list, attachment_id_list

    if certInstId:
        data = json.dumps(
            {
                "voucherFolderId": voucher_folder_id,
                "voucherCertInstIdList": invoice_id_list,
                "attachmentCertInstIdList": attachment_id_list,
            }
        )
    else:
        data = json.dumps(
            {
                "voucherFolderId": voucher_folder_id,
                "voucherIdList": invoice_id_list,
                "attachmentIdList": attachment_id_list,
            }
        )
    url = f"{base_url}/wallets/agent/invocation/queryEconomicMatters"
    return api_post(url, token, data)


def list_all_matters(
    base_url: str,
    token: str,
) -> List[Dict[str, Any]]:
    """查询用户启用的经济事项列表"""
    url = f"{base_url}/expense-model/cert-economic-matter/listEnabledMatter"
    return api_get(url, token)


def edit_distance(s1, s2):
    m, n = len(s1), len(s2)
    dp = [[0] * (n + 1) for _ in range(m + 1)]

    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if s1[i - 1] == s2[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = min(
                    dp[i - 1][j] + 1,  # 删除
                    dp[i][j - 1] + 1,  # 插入
                    dp[i - 1][j - 1] + 1,
                )  # 替换

    return dp[m][n]


def filter_matters(matters):
    result = []
    for matter in matters:
        matterTypeName = matter.get("matterTypeName", "")
        matterType = matter.get("matterType", "")
        # 防御：接口返回None值
        if (matterTypeName and matterTypeName.endswith("报销")) or (
            matterType and matterType.endswith("bx")
        ):
            elementCertName = matter.get("elementCertName", "")
            if elementCertName:
                name = matter["name"]
                if edit_distance(elementCertName, name) > 1:
                    matter["name"] = f"{elementCertName} - {name}"
            result.append(matter)
    return result


def run(
    voucher_folder_id: str = None,
    invoice_id_list: List[str] = None,
    attachment_id_list: List[str] = None,
) -> str:
    """经济事项确认子流程。

    入参:
    - session_id (str): 会话ID，对应请求头 X-Hermes-Session-Id
    - base_url (str): 接口访问地址
    - token (str): 用户token
    - voucher_folder_id (str): 凭证夹ID
    - invoice_id_list (List[str]): 票据ID列表
    - attachment_id_list (List[str]): 附件ID列表

    出参:
    - str: 返回经济事项确认卡片；当匹配结果有且仅有1个时，自动更新状态并返回下一子流程卡片
    """
    base_url = get_config()["base_url"]
    session_id = get_current_session_id()
    token = get_current_variables()["token"]

    if not session_id:
        return {"code": "E0001", "message": "缺少参数：会话ID（session_id）"}
    if not base_url:
        return {"code": "E0001", "message": "缺少参数：接口访问地址（base_url）"}

    if not token:
        return {"code": "E0001", "message": "缺少参数：接口访问令牌（token）"}

    if not voucher_folder_id and not invoice_id_list and not attachment_id_list:
        return {
            "code": "E0001",
            "message": "缺少参数：凭证夹ID或者凭证ID列表（voucher_folder_id/invoice_id_list/attachment_id_list）",
        }
    if voucher_folder_id:
        invoice_id_list = []
        attachment_id_list = []
    try:
        # 获取当前的流程变量，避免覆盖丢失已有数据
        current_session = get_session()
        current_voucher_folder_data = current_session.get("voucher_folder_data", {})
        resolved_voucher_folder_id = (
            voucher_folder_id
            if voucher_folder_id
            else current_voucher_folder_data.get("voucher_folder_id", "")
        )
        resolved_invoice_id_list = (
            invoice_id_list
            if invoice_id_list
            else current_voucher_folder_data.get("invoice_id_list", [])
        )
        resolved_attachment_id_list = (
            attachment_id_list
            if attachment_id_list
            else current_voucher_folder_data.get("attachment_id_list", [])
        )
        update_session(
            voucher_folder_data={
                "voucher_folder_id": resolved_voucher_folder_id,
                "invoice_id_list": resolved_invoice_id_list,
                "attachment_id_list": resolved_attachment_id_list,
            },
        )
        matters = list_economic_matter(
            base_url, token, voucher_folder_id, invoice_id_list, attachment_id_list
        )

        if "code" in matters:
            return error_card(matters)
        
        if len(matters)==0:
            matters = list_economic_matter(
                base_url, token, voucher_folder_id, invoice_id_list, attachment_id_list,False
            )            
        # 智能整理的凭证夹重新关联经济事项
        if (
            voucher_folder_id
            and len(matters) == 1
            and matters[0]["name"] == "通用报销单"
        ):
            data = get_current_variables()["data"]
            folder = json.loads(data)
            vouchers = folder[0]["data"]["vouchers"]
            bills = vouchers["bills"]
            invoice_ids = [bill["billId"] for bill in bills]
            attachments = vouchers["attachments"]
            attachment_ids = [attachment["attachmentId"] for attachment in attachments]
            matters = list_economic_matter(
                base_url, token, "", invoice_ids, attachment_ids, False
            )
            if "code" in matters:
                return error_card(matters)
        matters = filter_matters(matters)

        size = len(matters)
        if size == 0:
            # 查询用户启用的经济事项列表
            matter_list = []
            matters = list_all_matters(base_url, token)
            if "code" in matters:
                return error_card(matters)
            matters = filter_matters(matters)
            for matter in matters:
                matter_list.append({"label": matter["name"], "value": matter})
            card_data = {
                "result": "",
                "renderName": "",
                "rootComponent": "base-warp",
                "prop": "mode:single",
                "data": {
                    "answer": "凭证未关联经济事项。\n你可以查看：",
                    "buttons": [
                        {
                            "label": "所有经济事项",
                            "action": "choose_economic_matter",
                            "extra": {"data": {"list": matter_list}},
                        }
                    ],
                },
            }
        elif size == 1:  # 匹配到唯一经济事项：直接确认，进入下一节点
            # 取唯一命中的经济事项，并先写回凭证夹流程变量。
            selected_economic = matters[0]
            # 直接更新经济事项，由于新版 update_session 仅更新传入字段，无需读取旧值
            update_session(
                voucher_folder_data={
                    "matter_uniq_code": selected_economic.get("code"),
                },
            )

            # 科研类需先做“预算项目选择”，非科研类则直接进入“凭证完整性稽核”。
            # rcjf=日常经费
            # xmjf=项目经费  项目经费 就是科研类
            if "xmjf" == selected_economic.get("fundingType"):
                return budget_project.run()
            else:
                # 状态更新完成后，继续调用“凭证完整性稽核子流程”函数，直接进入下一步。
                return voucher_folder_integrity.run()

        else:
            primaryList = []
            selected = True
            for matter in matters:
                primaryList.append(
                    {
                        "id": matter["id"],
                        "title": matter["name"],
                        "data": matter,
                        "selected": selected,
                    }
                )
                selected = False
            data = {"answer": "请选择本次报销类型：", "primaryList": primaryList}
            data["buttons"] = [
                {
                    "label": "去报销",
                    "action": "sendMsg",
                    "type": "primary",
                    "extra": {
                        "content": "经济事项已确认",
                        # "chatMode": "C2A",
                    },
                }
            ]
            matter_list = []
            matters = list_all_matters(base_url, token)
            if "code" in matters:
                return error_card(matters)
            matters = filter_matters(matters)
            for matter in matters:
                matter_list.append({"label": matter["name"], "value": matter})
            card_data = {
                "result": "",
                "renderName": "MsgReservation",
                "rootComponent": "base-warp",
                "prop": "mode:sing;itemStyle:large",
                "data": data,
                "rootData": {
                    "suggestionButtons": [
                        {
                            "label": "以上都不是，要报销其他类型",
                            "action": "choose_economic_matter",
                            "extra": {"data": {"list": matter_list}},
                        }
                    ]
                },
            }
        content = json.dumps(card_data, ensure_ascii=False)
        return f"<res-card>{content}</res-card>"
    except Exception as ex:
        return error_card({"code": "E9999", "message": f"未知异常{ex}"})
