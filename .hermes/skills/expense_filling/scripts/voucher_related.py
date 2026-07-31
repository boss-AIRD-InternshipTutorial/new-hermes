"""
凭证关联匹配子流程（voucher_related）

由收单智能体点击“去报销”按钮时触发，本流程调用“关联票据及附件凭证夹列表”接口，

如果无关联凭证则直接调用"经济事项确认"子流程，否则返回关联凭证列表卡片，供用户选择并继续执行报销流程；

依赖接口:
    POST /wallets/agent/invocation/queryRelationVoucherFolders   查询关联票据及附件凭证夹列表

参数:
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
import economic_matter_confirm as em
from typing import List, Dict, Any

try:
    # 包内调用时，直接使用相对导入拿到状态更新函数。
    from .main_scripts import (
        get_config,
        get_current_variables,
        update_session,
        delete_session,
    )
except ImportError:  # pragma: no cover - 兼容直接运行脚本的场景
    # 兼容把当前脚本单独跑起来的场景。
    from main_scripts import (
        get_config,
        get_current_variables,
        update_session,
        delete_session,
    )


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


# 查询关联票据及附件凭证夹列表
def query_rel_voucher_folders(
    base_url: str, token: str, invoice_id_list: List[str], attachment_id_list: List[str]
) -> Dict[str, Any]:
    url = f"{base_url}/wallets/agent/invocation/queryRelationVoucherFolders"
    data = json.dumps(
        {"invoiceIdList": invoice_id_list, "attachmentIdList": attachment_id_list}
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


def run(
    invoice_id_list: List[str] = None,
    attachment_id_list: List[str] = None,
) -> str:
    """凭证关联匹配子流程

    入参:
    - invoice_id_list (List[str]): 票据ID列表
    - attachment_id_list (List[str]): 附件ID列表
    出参:
    - str: 返回匹配结果卡片字符串
    """
    base_url = get_config()["base_url"]
    token = get_current_variables()["token"]
    if not base_url:
        return {"code": "E0001", "message": "缺少参数：接口访问地址（base_url）"}

    if not token:
        return {"code": "E0001", "message": "缺少参数：接口访问令牌（token）"}

    try:
        # 进入新的凭证夹匹配流程前，先清空上一轮遗留的全部流程变量。
        delete_session()
        update_session(
            voucher_folder_data={
                "invoice_id_list": invoice_id_list,
                "attachment_id_list": attachment_id_list,
            },
        )
        # invoice_id_list = invoice_id_list if invoice_id_list else []
        # attachment_id_list = attachment_id_list if attachment_id_list else []
        data = get_current_variables()["data"]
        vouchers = json.loads(data)
        invoice_id_list1 = []
        attachment_id_list1 = []
        for voucher in vouchers:
            id = voucher["id"]
            if "附件ID" in voucher["data"].keys():
                attachment_id_list1.append(id)
            else:
                invoice_id_list1.append(id)
        # 调用接口“智能匹配关联的凭证夹和凭证”接口
        result = query_rel_voucher_folders(
            base_url, token, invoice_id_list1, attachment_id_list1
        )
        if "code" in result:
            return error_card(result)
        folders = result["voucherFolders"]
        vouchers = result["vouchers"]
        bills = vouchers.get("bills", [])
      
        attachments = vouchers.get("attachments", [])

        data = {
            "answer": f"好的，我来帮您处理。检索到您票夹中有相关的待报销凭证，您可以与上传的凭证一同报销:"
        }
        card_data = {
            "result": "",
            "renderName": "MsgReservation",
            "rootComponent": "base-warp",
            "prop": "mode:sing;itemStyle:large",
            "data": data,  # 具体格式待定
        }
        size = len(folders)
        if size == 0:  # 返回零散凭证
            # 且零散凭证与输入的凭证相同（无其他关联凭证），直接转经济事项确认
            if len(invoice_id_list1) == len(bills) and len(attachments) == len(
                attachment_id_list1
            ):
                return em.run(
                    invoice_id_list=invoice_id_list,
                    attachment_id_list=attachment_id_list,
                )

        card_data["prop"] = "mode:multiple;itemStyle:large"
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
        for bill in bills:
            bill_id = bill["billId"]
            if bill_id in invoice_id_list1:
                continue
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
            primaryList.append(
                {
                    "id": bill_id,
                    "title": title,
                    "subTitle": subtitle,
                    "value": formated_amount,
                    "tags": tags,
                    "data": {"票据ID": bill["certInstId"]},
                    "selected": selected,
                    "action": "route",
                    "extra": {
                        "path": "/pages/wallet/invoice-detail/index",
                        "params": {"invoiceId": bill_id},
                    },
                }
            )
            selected = False
        for attachment in attachments:
            attachment_id = attachment["attachmentId"]
            if attachment_id in attachment_id_list1:
                continue
            ftype = attachment.get("fileExtType", "")
            tag_name = f"/{ftype}"
            tags = [{"type": "image", "name": tag_name, "width": 30}]
            title = f'{ftype} {attachment["attachmentName"]}'
            subtitle = f'{attachment["summary"]}'

            primaryList.append(
                {
                    "id": attachment_id,
                    "title": title,
                    "subTitle": subtitle,
                    "tags": tags,
                    "data": {"附件ID": attachment["certInstId"]},
                    "selected": selected,
                    "action": "route",
                    "extra": {
                        "path": "/pages/wallet/attachment-manager/components/attachment-detail",
                        "params": {
                            "recId": attachment_id,
                            "certInstId": attachment["certInstId"],
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

        secondaryAnswer = "已上传凭证:\n"
        for invoice in bills:
            bill_id = invoice["billId"]
            if bill_id in invoice_id_list1:
                amount = invoice.get("billAmount", 0)
                formated_amount = f"¥{amount:,.2f}"
                summary = invoice.get("summary", "")
                if not summary:
                    summary = ""
                secondaryAnswer += f'{invoice["invoiceClassName"]} {invoice["invoiceSpeciesName"]} {invoice.get("businessDate","")} | {summary}  {formated_amount}\n'
        for attachment in attachments:
            attachment_id = attachment["attachmentId"]
            if attachment_id in attachment_id_list1:
                #ftype = attachment.get("fileExtType", "")
                secondaryAnswer += f'{attachment["attachmentTypeName"]} | {attachment["attachmentName"]}\n'

        data["secondaryAnswer"] = secondaryAnswer
        data["buttons"] = [
            {
                "label": "去报销",
                "action": "sendMsg",
                "type": "primary",
                "extra": {"content": "按以下凭证ID去报销：", "chatMode": "C2A"},
            },
        ]

        content = json.dumps(card_data, ensure_ascii=False)
        return f"<res-card>{content}</res-card>"
    except Exception as ex:
        return error_card({"code": "E9999", "message": f"未知异常{ex}"})
