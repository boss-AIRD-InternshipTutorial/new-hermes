"""
凭证夹匹配子流程（voucher_match）

供 main_scripts.py import 使用。负责调用凭证夹匹配接口完成报销凭证选择。

依赖接口:
    POST /wallets/agent/invocation/listPersonalVoucherFolders    查询个人凭证夹列表
    GET  /authUser/listByUserNameAndUnitId   根据人员姓名精确查询人员信息列表

参数:
    - agent_user_id (str): 代理报销用户ID
    - agent_user_name (str): 代理报销用户姓名
    - start_time (str): 报销发生的起始日期，格式：YYYY-MM-DD
    - end_time (str): 报销发生的结束时间，格式：YYYY-MM-DD
    - departure (str): 出差出发城市
    - destination (str): 出差目的城市
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
import re
from typing import List, Dict, Any
from openai import OpenAI
from hermes_logging import setup_logging
import logging
setup_logging()
logger = logging.getLogger(__name__)
try:
    # 包内调用时，直接使用相对导入拿到状态更新函数。
    from .main_scripts import (
        get_config,
        get_current_session_id,
        get_current_variables,
        delete_session,
    )
except ImportError:  # pragma: no cover - 兼容直接运行脚本的场景
    # 兼容把当前脚本单独跑起来的场景。
    from main_scripts import (
        get_config,
        get_current_session_id,
        get_current_variables,
        delete_session,
    )


# -------------------------- 环境变量 --------------------------
# 语言模型 llm_base_url  、llm_api_key 、 llm_model
LLM_MODEL_API_KEY = get_config()["llm_api_key"]
LLM_MODEL_BASE_URL = get_config()["llm_base_url"]
LLM_MODEL_NAME = get_config()["llm_model"]

# 初始化客户端，设置大模型 API密钥和基础URL
llm_client = OpenAI(
    api_key=LLM_MODEL_API_KEY, base_url=LLM_MODEL_BASE_URL  # API密钥  # API端点
)


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


# 根据人员姓名精确查询人员信息列表
def query_user_info_by_name(
    base_url: str, token: str, user_name: str
) -> List[Dict[str, Any]]:
    url = f"{base_url}/user-extension/authUser/listByUserNameAndUnitId?userName={user_name}"
    return api_get(url, token)


# 查询个人凭证夹列表
def list_voucher_folders(
    base_url: str,
    token: str,
    user_id: str,
    start_date: str,
    end_date: str,
    departure: str,
    destination: str,
) -> Dict[str, Any]:
    url = f"{base_url}/wallets/agent/invocation/listPersonalVoucherFolders"
    data = json.dumps(
        {
            "startDate": start_date,
            "endDate": end_date,
            "location": departure,
            "destination": destination,
            "userId": user_id,
        }
    )
    return api_post(url, token, data)


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


import time


def match_by_llm(text_content, query_key_word):
    # start_time = time.time()
    system_prompt = f"""
# Role: 语义匹配专家

## Task
基于用户输入列表（Input List）和检索关键词（Query Key Word），输出用户输入列表中与关键词语义匹配的输入的序号列表，如[1,3,4];若无匹配项，则输出空数组[]。
"""
    user_prompt = f"""#检索关键词（Query Key Word）:{query_key_word}
    # 用户输入列表（Input List）:
    {text_content} """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # print(messages)
    response = llm_client.chat.completions.create(
        model=LLM_MODEL_NAME,  # 使用语言模型
        messages=messages,
        extra_body={"enable_thinking": False},
        temperature=0.1,  # 控制输出随机性，0-1之间
        max_tokens=2048,  # 限制生成内容的最大长度
        stream=False,  # 是否使用流式传输
    )

    # times = time.time() - start_time
    # print(f"LLM 耗时：{times:.2f}秒")
    response_data = response.choices[0].message.content
    # print(response_data)
    try:
        response_data = json.loads(response_data)
    except Exception as e:
        # 正则提取 JSON 部分
        pattern = r"(\[\d+(,\d+)*\])"
        match = re.search(pattern, response_data, re.DOTALL)

        if match:
            json_str = match.group(0)
            response_data = json.loads(json_str)
        else:
            return []

    return response_data


# 使用大模型 根据query_key_word匹配事项
def match_bills(bills, query_key_word):
    idx = 1
    text_content = ""
    matched_idx_list = []

    for bill in bills:
        matched_idxs = []
        matched_idx_list.append(matched_idxs)
        remark = bill.get("billRemark", "")
        if remark:
            text_content += f"{idx}、{remark}\n"
            matched_idxs.append(idx)
            idx += 1
        summary = bill.get("summary", "")
        if summary:
            text_content += f"{idx}、{summary}\n"
            matched_idxs.append(idx)
            idx += 1

    llm_matched_idx_list = match_by_llm(text_content, query_key_word)
    matched_bills = []
    for bill, matched_idxs in zip(bills, matched_idx_list):
        for idx in llm_matched_idx_list:
            if idx in matched_idxs:
                matched_bills.append(bill)
                break
    return matched_bills


def match_attachments(attachments, query_key_word):
    idx = 1
    text_content = ""
    matched_idx_list = []

    for attachment in attachments:
        matched_idxs = []
        matched_idx_list.append(matched_idxs)
        name = attachment.get("attachmentName", "")
        if name:
            text_content += f"{idx}、{name}\n"
            matched_idxs.append(idx)
            idx += 1
        summary = attachment.get("summary", "")
        if summary:
            text_content += f"{idx}、{summary}\n"
            matched_idxs.append(idx)
            idx += 1
    llm_matched_idx_list = match_by_llm(text_content, query_key_word)
    matched_attachments = []
    for attachment, matched_idxs in zip(attachments, matched_idx_list):
        for idx in llm_matched_idx_list:
            if idx in matched_idxs:
                matched_attachments.append(attachment)
                break
    return matched_attachments


def match_folders(folders, query_key_word):
    idx = 1
    text_content = ""
    matched_idx_list = []
    for folder in folders:
        matched_idxs = []
        matched_idx_list.append(matched_idxs)
        travelInfos = folder.get("travelInfos", [])
        for travelInfo in travelInfos:
            queryKeyWord = travelInfo.get("queryKeyWord", "")
            if queryKeyWord:
                text_content += f"{idx}、{remark}\n"
                matched_idxs.append(idx)
                idx += 1
        vouchers = folder["vouchers"]
        bills = vouchers["bills"]
        for bill in bills:
            remark = bill.get("billRemark", "")
            if remark:
                text_content += f"{idx}、{remark}\n"
                matched_idxs.append(idx)
                idx += 1
            summary = bill.get("summary", "")
            if summary:
                text_content += f"{idx}、{summary}\n"
                matched_idxs.append(idx)
                idx += 1
        attachments = vouchers["attachments"]
        for attachment in attachments:
            name = attachment.get("attachmentName", "")
            if name:
                text_content += f"{idx}、{name}\n"
                matched_idxs.append(idx)
                idx += 1
            summary = attachment.get("summary", "")
            if summary:
                text_content += f"{idx}、{summary}\n"
                matched_idxs.append(idx)
                idx += 1
    llm_matched_idx_list = match_by_llm(text_content, query_key_word)
    matched_folders = []
    for folder, matched_idxs in zip(folders, matched_idx_list):
        for idx in llm_matched_idx_list:
            if idx in matched_idxs:
                matched_folders.append(folder)
                break
    return matched_folders


def run(
    agent_user_id: str = None,
    agent_user_name: str = None,
    start_date: str = None,
    end_date: str = None,
    departure: str = None,
    destination: str = None,
    query_key_word: str = None,
) -> str:
    """凭证夹匹配子流程

    入参:
    - agent_user_id (str): 代理报销用户ID
    - agent_user_name (str): 代理报销用户姓名
    - start_time (str): 报销发生的起始日期，格式：YYYY-MM-DD
    - end_time (str): 报销发生的结束时间，格式：YYYY-MM-DD
    - departure (str): 出差出发城市
    - destination (str): 出差目的城市
    出参:
    - str: 返回匹配结果卡片字符串
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

    try:
        # 进入新的凭证夹匹配流程前，先清空上一轮遗留的全部流程变量。
        delete_session()

        card_data = voucher_folder_match(
            base_url,
            token,
            agent_user_id,
            agent_user_name,
            start_date,
            end_date,
            departure,
            destination,
            query_key_word,
        )
        if "code" in card_data:
            return error_card(card_data)
        content = json.dumps(card_data, ensure_ascii=False)
        return f"<res-card>{content}</res-card>"
    except Exception as ex:
        return error_card({"code": "E9999", "message": f"未知异常{ex}"})


def accept(query_key_word):
    black_list = ["报销", "费用", "出差", "差旅", "行程", "发票", "项目差旅"]
    for item in black_list:
        if query_key_word == item:
            return False
    return True


def voucher_folder_match(
    base_url: str,
    token: str,
    agent_user_id: str = None,
    agent_user_name: str = None,
    start_date: str = None,
    end_date: str = None,
    departure: str = None,
    destination: str = None,
    query_key_word: str = None,
) -> Dict[str, Any]:

    if not agent_user_id:
        if agent_user_name:
            # 根据人员姓名精确查询人员信息列表
            user_list = query_user_info_by_name(base_url, token, agent_user_name)
            if "code" in user_list:
                return user_list
            """
            根据返回的人员数量判断：
            1. 人员数量=0：纯文本卡片，反问用户全名
            2. 人员数量=1：正常走子流程，但需携带人员ID
            3. 人员数量≥2：人员选项卡
            """
            user_size = len(user_list)
            if user_size == 0:
                return {
                    "result": f"用户{agent_user_name}查无此人，请确认输入是否有误？",
                    "renderName": "msg-system-error-card",
                    "rootComponent": "base-warp",
                    "prop": "mode:single",
                }
            elif user_size > 1:
                primaryList = []
                selected = True
                for user in user_list:
                    label = (
                        f'{user["userName"]} - {user["jobName"]} - {user["deptName"]}'
                    )
                    primaryList.append(
                        {
                            "id": user["userId"],
                            "title": label,
                            "data": user,
                            "selected": selected,
                        }
                    )
                    selected = False

                return {
                    "result": f"查询到{user_size}个同名用户，如下：",
                    "renderName": "MsgReservation",
                    "rootComponent": "base-warp",
                    "prop": "mode:single;itemStyle:large",
                    "data": {"answer": "请选择：", "primaryList": primaryList},
                }

    result = list_voucher_folders(
        base_url, token, agent_user_id, start_date, end_date, departure, destination
    )
    if "code" in result:
        return result
    if query_key_word and accept(query_key_word):  # 按“事项”语义匹配过滤
        folders = result["voucherFolders"]
        if len(folders) == 0:  # 零散凭证
            vouchers = result["vouchers"]
            bills = vouchers["bills"]
            try:
                matched_bills = match_bills(bills, query_key_word)
            except Exception as e:
                return {"code": "E1001", "message": f"LLM API调用出错:{e}"}
            vouchers["bills"] = matched_bills
            attachments = vouchers["attachments"]
            try:
                matched_attachments = match_attachments(attachments, query_key_word)
            except Exception as e:
                return {"code": "E1001", "message": f"LLM API调用出错:{e}"}
            vouchers["attachments"] = matched_attachments
        else:  # 凭证夹
            try:
                matched_folders = match_folders(folders, query_key_word)
            except Exception as e:
                return {"code": "E1001", "message": f"LLM API调用出错:{e}"}
            result["voucherFolders"] = matched_folders

    folders = result["voucherFolders"]
    if len(folders) > 10:
        folders = folders[:10]
    data = {}
    card_data = {
        "result": "",
        "renderName": "MsgReservation",
        "rootComponent": "base-warp",
        "prop": "mode:sing;itemStyle:large",
        "data": data,  # 具体格式待定
    }
    size = len(folders)

    if size == 1:
        data["answer"] = "好的，我来帮您处理。检测到您票夹中有相关的待报销事项："
        data["secondaryAnswer"] = "您可以选择："
        folder = folders[0]
        title = f'{folder["folderName"]}'

        amount = float(folder.get("totalAmount", 0))
        formated_amount = f"¥{amount:,.2f}"
        travelInfos = folder.get("travelInfos", [])
        subtitle = ""
        for travelInfo in travelInfos:
            startDate = travelInfo.get("startDate", "")
            endDate = travelInfo.get("endDate", "")
            departure = travelInfo.get("location", "")
            destination = travelInfo.get("destination", "")
            if departure and destination:
                subtitle += f"{departure}-->{destination}, "
            if startDate and endDate:
                subtitle += f"{startDate}-{endDate}\n"
            else:
                subtitle += "\n"
        subtitle += f'{folder["invoiceCount"]}张发票， {folder["attachmentCount"]}个附件，{formated_amount}'
        folder_id = folder["folderId"]
        data["primaryList"] = [
            {
                "id": folder_id,
                "title": title,
                "subTitle": subtitle,
                "data": folder,
                "action": "route",
                "extra": {
                    "path": "/pages/wallet/voucher-folder/voucher-folder-detail/index",
                    "params": {"voucherFolderId": folder_id},
                },
                "selected": True,
            }
        ]
        data["buttons"] = [
            {"label": "补充凭证", "action": "add_voucher"},
            {
                "label": "去报销",
                "action": "sendMsg",
                "type": "primary",
                "extra": {"content": "按以下凭证夹ID去报销：", "chatMode": "C2A"},
            },
        ]

        card_data["rootData"] = {
            "suggestionButtons": [
                {
                    "label": "不是这个，我要报销别的",
                    "action": "select_voucher",
                    "extra": {"type": "route", "data": {"path": "/", "params": {}}},
                }
            ]
        }
    elif size > 1:
        card_data["result"] = f"发现你有{size}个待报销事项，请选择本次要报销的："
        data["answer"] = "请选择报销事项："
        primaryList = []
        selected = True
        for folder in folders:
            title = f'{folder["folderName"]}'
            amount = folder.get("totalAmount", "0")
            formated_amount = f"¥{amount:,.2f}"
            travelInfos = folder.get("travelInfos", [])
            subtitle = ""
            for travelInfo in travelInfos:
                startDate = travelInfo.get("startDate", "")
                endDate = travelInfo.get("endDate", "")
                departure = travelInfo.get("location", "")
                destination = travelInfo.get("destination", "")
                if departure and destination:
                    subtitle += f"{departure}-->{destination}, "
                if startDate and endDate:
                    subtitle += f"{startDate}-{endDate}\n"
                else:
                    subtitle += "\n"
            subtitle += f'{folder["invoiceCount"]}张发票， {folder["attachmentCount"]}个附件，{formated_amount}'
            folder_id = folder["folderId"]
            primaryList.append(
                {
                    "id": folder_id,
                    "title": title,
                    "subTitle": subtitle,
                    "selected": selected,
                    "data": folder,
                    "action": "route",
                    "extra": {
                        "path": "/pages/wallet/voucher-folder/voucher-folder-detail/index",
                        "params": {"voucherFolderId": folder_id},
                    },
                }
            )
            selected = False
        data["primaryList"] = primaryList
        data["buttons"] = [
            {
                "label": "去报销",
                "action": "sendMsg",
                "type": "primary",
                "extra": {"content": "按以下凭证夹ID去报销：", "chatMode": "C2A"},
            }
        ]
        card_data["rootData"] = {
            "suggestionButtons": [
                {
                    "label": "以上都不是，要报销别其他事项",
                    "action": "select_voucher",
                    "extra": {"type": "route", "data": {"path": "/", "params": {}}},
                }
            ]
        }
    else:
        vouchers = result["vouchers"]
        bills = vouchers.get("bills", [])
        attachments = vouchers.get("attachments", [])
        card_data["prop"] = "mode:multiple;itemStyle:large"
        selected = True
        if bills or attachments:
            data["answer"] = "好的，我来帮您处理。检索到您票夹中有相关的待报销凭证："
            primaryList = []

            for bill in bills:
                tags = []
                tags.append(
                    {
                        "text": bill["invoiceClassNameAbbr"],
                        "type": "fill",
                        "status": "success",
                    }
                )
                tags.append(
                    {
                        "text": bill["invoiceSpeciesName"],
                        "type": "outline",
                        "status": "success",
                    }
                )
                title = f'{bill["invoiceClassName"]}'
                subtitle = f'{bill["summary"]}'
                amount = bill.get("reimbursableAmount", 0)
                formated_amount = f"¥{amount:,.2f}"
                bill_id = bill["certInstId"]
                primaryList.append(
                    {
                        "id": bill_id,
                        "title": title,
                        "subTitle": subtitle,
                        "value": formated_amount,
                        "tags": tags,
                        "data": {"票据ID": bill_id},
                        "selected": selected,
                        "action": "route",
                        "extra": {
                            "path": "/pages/wallet/invoice-detail/index",
                            "params": {"invoiceId": bill["billId"]},
                        },
                    }
                )
                selected = False
            for attachment in attachments:
                ftype = attachment.get("fileExtType", "")
                tag_name = f"/{ftype}"
                tags = [{"type": "image", "name": tag_name, "width": 30}]
                title = f'{ftype} {attachment["attachmentName"]}'
                subtitle = f'{attachment["summary"]}'
                attachment_id = attachment["certInstId"]
                primaryList.append(
                    {
                        "id": attachment_id,
                        "title": title,
                        "subTitle": subtitle,
                        "tags": tags,
                        "data": {"附件ID": attachment_id},
                        "selected": selected,
                        "action": "route",
                        "extra": {
                            "path": "/pages/wallet/attachment-manager/components/attachment-detail",
                            "params": {
                                "recId": attachment["attachmentId"],
                                "certInstId": attachment_id,
                                "certificateDefinitionCode": attachment[
                                    "certificateDefinitionCode"
                                ],
                                "attachmentName": attachment["attachmentName"],
                                "reimburseStatus": attachment["reimburseStatus"],
                                "attachmentSource": attachment["attachmentSource"],
                            },
                        },
                    }
                )
                selected = False
            data["primaryList"] = primaryList

            data["secondaryAnswer"] = "您可以选择："
            data["buttons"] = [
                {"label": "补充凭证", "action": "add_voucher"},
                {
                    "label": "去报销",
                    "action": "sendMsg",
                    "type": "primary",
                    "extra": {"content": "按以下凭证ID去报销：", "chatMode": "C2A"},
                },
            ]
            card_data["rootData"] = {
                "suggestionButtons": [
                    {
                        "label": "不是这个，我要报销别的",
                        "action": "select_voucher",
                        "extra": {"type": "route", "data": {"path": "/", "params": {}}},
                    }
                ]
            }
        else:
            return {
                "result": "",
                "renderName": "",
                "rootComponent": "base-warp",
                "prop": "mode:single",
                "data": {
                    "answer": "未在你的票夹中找到相关的凭证。\n你可以：",
                    "buttons": [
                        {"label": "上传凭证", "action": "add_voucher"},
                        {
                            "label": "从票夹选择",
                            "action": "select_voucher",
                            "extra": {
                                "type": "route",
                                "data": {"path": "/", "params": {}},
                            },
                        },
                    ],
                },
            }

    return card_data
