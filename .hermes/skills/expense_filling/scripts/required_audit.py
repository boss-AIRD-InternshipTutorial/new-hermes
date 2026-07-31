"""
required_audit_script.py — 单据必填项校验模块

功能概述：
    根据会话ID和用户token，从会话中读取单据ID和报销类型编码，
    从业务系统获取单据暂存数据和单据schema，校验静态必填项，
    并组装为前端可渲染的 Web Card XML 字符串返回。

主入口：
    run() -> str
        成功："<res-card>...</res-card>"
        系统异常："<res-card>...</res-card>"
        接口返回结果异常："<res-card>...</res-card>"
        子流程入参异常："<res-card>...</res-card>"

错误码说明：
    E0001 - 系统异常，接口调用失败或子流程运行报错，返回系统异常卡片
    E0002 - 接口返回结果异常，查询结果为空或业务结果异常，返回接口结果异常卡片
    E0003 - 子流程入参异常，AI决策入参有误，返回包含错误信息的JSON

调用示例：
    from scripts.required_audit import run
    result = run()
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

import requests


CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.append(str(CURRENT_DIR))

try:
    from .main_scripts import get_session, update_session, get_current_session_id, get_current_variables, get_config
except ImportError:
    from main_scripts import get_session, update_session, get_current_session_id, get_current_variables, get_config


BASE_URL = get_config()["base_url"]
GET_DRAFT_INFO_PATH = "/business/reimburse/base/getDraftInfoById"
SCHEMA_BY_TYPE_CODE_PATH = "/business/reimburse/schema/getConfigByTypeCode"
DRAFT_PATH = "/business/reimburse/base/draft"
FEE_ICON_MAP = {"01001": "✈️", "01002": "🏨", "02101": "🍴", "02102": "🚗", "otherFee": "📌"}
FEE_NAME_MAP = {"01001": "城市间交通费", "01002": "住宿费", "02101": "用餐补助", "02102": "交通补助", "otherFee": "其他费用"}


class RequiredFieldChecker:
    def __init__(self, token: str = "") -> None:
        """保存请求鉴权信息，供后续接口调用使用。"""
        self.headers = {"Authorization": token, "Content-Type": "application/json"}
        self.reimburse_config: dict[str, Any] = {}
        self.reference_array_fields: set[str] = set()

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """统一封装 GET/POST 请求和错误处理。"""
        try:
            response = requests.request(method, f"{BASE_URL}{path}", headers=self.headers, timeout=30, **kwargs)
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

    def get_draft_info(self, form_id: str) -> dict[str, Any]:
        """通过单据 form_id 获取最新暂存单数据。"""
        return self.request("POST", GET_DRAFT_INFO_PATH, params={"id": form_id})

    def save_draft(self, draft_body: dict[str, Any]) -> None:
        """调用暂存接口保存合并后的完整草稿。"""
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
            raise RuntimeError(f"暂存单据失败: status={response.status_code}, body={response.text}")

    def get_schema_response(self, reimbursement_type_code: str, matter_uniq_code: str = "", form_id: str = "") -> dict[str, Any]:
        """通过报销类型编码获取 schema 配置结果。"""
        params = {"reimbursementTypeCode": reimbursement_type_code, "clientType": 1}
        if matter_uniq_code:
            params["economicMatterTypeId"] = matter_uniq_code
        if form_id:
            params["id"] = form_id
        return self.request(
            "GET",
            SCHEMA_BY_TYPE_CODE_PATH,
            params=params,
        )

    def get_schema(self, schema_response: dict[str, Any]) -> dict[str, Any]:
        """解析 jsonSchema，并用小程序组件定义原值替换其中的 $ref。"""
        self.reimburse_config = (
            schema_response.get("reimburseConfig")
            if isinstance(schema_response.get("reimburseConfig"), dict)
            else {}
        )
        self.reference_array_fields = set()
        json_schema = schema_response.get("jsonSchema")
        if isinstance(json_schema, str):
            schema = json.loads(json_schema)
        elif isinstance(json_schema, dict):
            schema = json_schema
        else:
            schema = schema_response

        component_infos = schema_response.get("schemaComponentInfos")
        if not isinstance(component_infos, dict):
            return schema

        components: dict[str, dict[str, Any]] = {}
        for name, component in component_infos.items():
            if isinstance(component, str):
                component = json.loads(component)
            if isinstance(component, dict):
                components[str(name)] = component
        return self.replace_schema_references(schema, components)

    def replace_schema_references(
        self,
        node: Any,
        components: dict[str, dict[str, Any]],
        field_name: str = "",
    ) -> Any:
        """把 $ref 节点完整替换为 schemaComponentInfos 中的对应组件。"""
        if isinstance(node, list):
            return [self.replace_schema_references(item, components, field_name) for item in node]
        if not isinstance(node, dict):
            return node

        reference = node.get("$ref")
        if isinstance(reference, str):
            component = components.get(reference.rsplit("/", 1)[-1])
            if component is not None:
                if field_name:
                    self.reference_array_fields.add(field_name)
                return component

        return {
            key: self.replace_schema_references(value, components, key)
            for key, value in node.items()
        }

    def get_form_data(self, form_field_info: dict[str, Any]) -> dict[str, Any]:
        """兼容 form 包裹和直接平铺两种草稿数据格式。"""
        return form_field_info.get("form") if isinstance(form_field_info.get("form"), dict) else form_field_info

    def is_full_draft_data(self, data: dict[str, Any]) -> bool:
        """判断前端是否回传了完整草稿，完整草稿可直接暂存。"""
        return (
            isinstance(data, dict)
            and isinstance(data.get("basicInfo"), dict)
            and isinstance(data.get("feeInfoMap"), dict)
            and "id" in data
            and "version" in data
        )

    def get_form_sections(self, schema: dict[str, Any]) -> dict[str, Any]:
        """取出 schema 中 form 下的一级字段定义。"""
        properties = schema.get("properties", {})
        form = properties.get("form")
        return form.get("properties", {}) if isinstance(form, dict) else properties

    def get_fee_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """取出费用明细 feeInfoMap 对应的 schema 定义。"""
        fee_map = self.find_schema_property(schema, "feeInfoMap")
        return fee_map.get("properties", {}) if isinstance(fee_map, dict) else {}

    def find_schema_property(self, node: dict[str, Any], target_key: str) -> dict[str, Any]:
        """递归查找指定 schema 属性，兼容 app 端多层容器结构。"""
        if not isinstance(node, dict):
            return {}
        properties = node.get("properties", {})
        if target_key in properties and isinstance(properties[target_key], dict):
            return properties[target_key]
        for child in properties.values():
            result = self.find_schema_property(child, target_key)
            if result:
                return result
        return {}

    def is_empty(self, value: Any) -> bool:
        """判断值是否为空。"""
        return value is None or value == "" or value == [] or value == {}

    def normalize(self, value: Any) -> Any:
        """把 None 统一转成空字符串，方便前端回显。"""
        if value is None:
            return ""
        if isinstance(value, dict):
            return {key: self.normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.normalize(item) for item in value]
        return value

    def is_required(self, node: dict[str, Any]) -> bool:
        """判断 schema 节点是否是静态必填字段。"""
        if node.get("required") is True:
            return True
        validator = node.get("x-validator")
        if isinstance(validator, dict):
            return validator.get("required") is True
        if isinstance(validator, list):
            return any(isinstance(item, dict) and item.get("required") is True for item in validator)
        return False

    def has_reaction(self, node: dict[str, Any]) -> bool:
        """判断字段是否带联动逻辑，带联动的不做静态必填校验。"""
        return not self.is_empty(node.get("x-reactions")) or not self.is_empty(node.get("x-reaction"))

    def resolve_dynamic_bool(self, value: Any) -> bool | None:
        """解析组件级布尔值及简单的 dynamicConfigMap 布尔表达式。"""
        if isinstance(value, bool):
            return value
        if not isinstance(value, str):
            return None
        text = value.strip()
        if text.lower() in ("true", "false"):
            return text.lower() == "true"
        match = re.fullmatch(r"\{\{\s*(!?)\$dynamicConfigMap\.([A-Za-z_]\w*)\s*\}\}", text)
        if not match:
            return None
        result = bool(self.reimburse_config.get(match.group(2)))
        return not result if match.group(1) else result

    def is_reference_array_required(self, node: dict[str, Any]) -> bool:
        """判断引用组件数组当前是否要求至少存在一行数据。"""
        if self.resolve_dynamic_bool(node.get("x-visible")) is False:
            return False
        candidates = [node.get("required")]
        validator = node.get("x-validator")
        if isinstance(validator, dict):
            candidates.append(validator.get("required"))
        elif isinstance(validator, list):
            candidates.extend(
                item.get("required")
                for item in validator
                if isinstance(item, dict)
            )
        return any(self.resolve_dynamic_bool(value) is True for value in candidates)

    def title_of(self, key: str, node: dict[str, Any]) -> str:
        """从 schema 中拿字段展示名，拿不到就回退到 key。"""
        return (
            node.get("title")
            or node.get("x-decorator-props", {}).get("label")
            or node.get("x-component-props", {}).get("title")
            or node.get("x-component-props", {}).get("header", {}).get("title")
            or FEE_NAME_MAP.get(key)
            or key
        )

    def split_names(self, field_name: str) -> list[str]:
        """把 [a,b] 和 {value:a,text:b} 这种字段拆成真实字段名。"""
        if field_name.startswith("[") and field_name.endswith("]"):
            return [item.strip() for item in field_name[1:-1].split(",") if item.strip()]
        if field_name.startswith("{") and field_name.endswith("}"):
            names = re.findall(r"(?:value|text)\s*:\s*([^,}]+)", field_name)
            return [item.strip() for item in names if item.strip()]
        return [field_name]

    def read_value(self, data: dict[str, Any], field_name: str) -> Any:
        """按字段名从 dict 里读取值，支持点路径。"""
        if field_name in data:
            return data.get(field_name)
        current: Any = data
        for key in field_name.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    def field_missing(self, data: dict[str, Any], field_name: str) -> bool:
        """判断某个字段是否缺失。"""
        return any(self.is_empty(self.read_value(data, name)) for name in self.split_names(field_name))

    def field_value(self, data: dict[str, Any], field_name: str) -> Any:
        """读取字段值并做回显格式化。"""
        names = self.split_names(field_name)
        if len(names) == 1:
            return self.normalize(self.read_value(data, names[0]))
        return {name: self.normalize(self.read_value(data, name)) for name in names}

    def collect_required_fields(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        """递归收集当前节点下所有静态必填的子字段。"""
        fields: list[dict[str, Any]] = []
        for key, child in node.get("properties", {}).items():
            if not isinstance(child, dict) or child.get("x-slot"):
                continue
            if child.get("properties"):
                fields.extend(self.collect_required_fields(child))
                continue
            if self.is_required(child) and not self.has_reaction(child):
                fields.append({"name": child.get("name") or key, "title": self.title_of(key, child)})
        return fields

    def add_value(self, target: dict[str, Any], field_name: str, value: Any) -> None:
        """把缺失字段写入目标 dict，支持点路径嵌套。"""
        if "." not in field_name:
            target[field_name] = self.normalize(value)
            return
        current = target
        parts = field_name.split(".")
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = self.normalize(value)

    def add_row_value(self, target: dict[str, Any], row: dict[str, Any], field_name: str) -> None:
        """把数组行里的缺失字段写入目标 dict，组合字段会拆成多个独立 key。"""
        for name in self.split_names(field_name):
            target[name] = self.normalize(self.read_value(row, name))

    def add_fee_row_context(
        self,
        target: dict[str, Any],
        row: dict[str, Any],
        fee_code: str,
        title: str,
        is_fee_action: bool,
    ) -> None:
        """给费用行补充暂存接口需要的隐藏类型字段。"""
        if not is_fee_action:
            return
        if row.get("id"):
            target["id"] = row.get("id")
        if row.get("recId"):
            target["recId"] = row.get("recId")
        target["feeActionTypeCode"] = row.get("feeActionTypeCode") or fee_code
        target["feeActionTypeUniqueCode"] = row.get("feeActionTypeUniqueCode") or fee_code
        target["feeActionTypeUniqueName"] = row.get("feeActionTypeUniqueName") or title

    def merge_field(self, target: dict[str, Any], field_name: str, value: Any) -> None:
        """把一个字段合并进目标对象，支持点路径。"""
        if "." not in field_name:
            target[field_name] = value
            return
        current = target
        parts = field_name.split(".")
        for part in parts[:-1]:
            next_value = current.get(part)
            if not isinstance(next_value, dict):
                next_value = {}
                current[part] = next_value
            current = next_value
        current[parts[-1]] = value

    def merge_dict_fields(self, target: dict[str, Any], patch: dict[str, Any]) -> None:
        """把补充字段合并进对象，保留对象原有字段。"""
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict) and "." not in key:
                self.merge_dict_fields(target[key], value)
                continue
            self.merge_field(target, key, value)

    def find_fee_row(self, rows: list[Any], patch_row: dict[str, Any], index: int) -> dict[str, Any]:
        """费用明细行优先按 id 匹配，否则按行号匹配，确保保留原行字段。"""
        patch_id = patch_row.get("id") or patch_row.get("recId")
        if patch_id:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if str(row.get("id") or row.get("recId") or "") == str(patch_id):
                    return row
        if index < len(rows) and isinstance(rows[index], dict):
            return rows[index]
        new_row: dict[str, Any] = {}
        rows.append(new_row)
        return new_row

    def merge_fee_rows(self, form_data: dict[str, Any], fee_code: str, patch_rows: Any) -> None:
        """合并 feeInfoMap 下的费用行，保留每一行原有 id、金额、凭证等字段。"""
        fee_info_map = form_data.setdefault("feeInfoMap", {})
        if not isinstance(fee_info_map, dict):
            fee_info_map = {}
            form_data["feeInfoMap"] = fee_info_map

        rows = fee_info_map.setdefault(fee_code, [])
        if not isinstance(rows, list):
            rows = []
            fee_info_map[fee_code] = rows

        if isinstance(patch_rows, dict):
            patch_rows = [patch_rows]
        if not isinstance(patch_rows, list):
            return

        for index, patch_row in enumerate(patch_rows):
            if not isinstance(patch_row, dict):
                continue
            if fee_code == "otherFee" and not patch_row.get("feeActionTypeCode"):
                patch_row["feeActionTypeCode"] = patch_row.get("feeActionTypeUniqueCode") or "otherFee"
            elif fee_code != "otherFee":
                patch_row.setdefault("feeActionTypeCode", fee_code)
                patch_row.setdefault("feeActionTypeUniqueCode", fee_code)
            target_row = self.find_fee_row(rows, patch_row, index)
            self.merge_dict_fields(target_row, patch_row)

    def apply_supplement_data(self, draft_body: dict[str, Any], supplement_data: dict[str, Any]) -> None:
        """把用户补充字段合并回完整草稿，保留 basicInfo / feeInfoMap 父级结构。"""
        if not supplement_data:
            return

        form_data = self.get_form_data(draft_body)
        if not isinstance(form_data, dict):
            return

        fee_info_map = form_data.get("feeInfoMap")
        fee_info_map = fee_info_map if isinstance(fee_info_map, dict) else {}

        if isinstance(supplement_data.get("feeInfoMap"), dict):
            for fee_code, patch_rows in supplement_data["feeInfoMap"].items():
                self.merge_fee_rows(form_data, fee_code, patch_rows)

        if isinstance(supplement_data.get("basicInfo"), dict):
            basic_info = form_data.setdefault("basicInfo", {})
            if not isinstance(basic_info, dict):
                basic_info = {}
                form_data["basicInfo"] = basic_info
            self.merge_dict_fields(basic_info, supplement_data["basicInfo"])

        # 前端已按草稿层级回传的字段，必须合并回原层级，不能默认塞进 basicInfo。
        for key, value in supplement_data.items():
            if key in ("feeInfoMap", "basicInfo"):
                continue
            if isinstance(value, dict):
                target = form_data.setdefault(key, {})
                if not isinstance(target, dict):
                    target = {}
                    form_data[key] = target
                self.merge_dict_fields(target, value)
                continue
            if key in form_data and not (key in fee_info_map or (isinstance(value, list) and key[:1].isdigit()) or key == "otherFee"):
                self.merge_field(form_data, key, value)
                continue

        for key, value in supplement_data.items():
            if key in ("feeInfoMap", "basicInfo"):
                continue
            if isinstance(value, dict):
                continue
            if key in form_data and not (key in fee_info_map or (isinstance(value, list) and key[:1].isdigit()) or key == "otherFee"):
                continue
            if key in fee_info_map or (isinstance(value, list) and key[:1].isdigit()) or key == "otherFee":
                self.merge_fee_rows(form_data, key, value)
                continue

            basic_info = form_data.setdefault("basicInfo", {})
            if isinstance(basic_info, dict):
                self.merge_field(basic_info, key, value)
            else:
                self.merge_field(form_data, key, value)

    def has_child_data(self, node: dict[str, Any], data: dict[str, Any]) -> bool:
        """判断父节点下是否已有任意子字段数据。"""
        for key, child in node.get("properties", {}).items():
            if not isinstance(child, dict):
                continue
            field_name = child.get("name") or key
            if any(name in data for name in self.split_names(field_name)):
                return True
        return False

    def is_transparent_container(self, node: dict[str, Any], key: str) -> bool:
        """判断 schema 节点是否只是 app/pc 端布局容器，不对应真实表单数据。"""
        if node.get("type") == "void":
            return True
        field_name = node.get("name") or key
        return field_name.startswith("_")

    def check_node(
        self,
        node: dict[str, Any],
        key: str,
        data: dict[str, Any],
        form_data: dict[str, Any],
        missing: dict[str, Any],
        titles: list[str],
        only_required_parent_children: bool,
        fee_action_node_ids: set[int] | None = None,
    ) -> None:
        """递归检查一个 schema 节点，把缺失内容写进 missing。表单数据与 schmea 做查询，返回缺失字段信息"""
        if not isinstance(node, dict) or node.get("x-slot"):
            return

        field_name = node.get("name") or key
        title = self.title_of(key, node)

        if node.get("type") == "array":
            is_fee_action = id(node) in (fee_action_node_ids or set())
            rows = self.read_value(data, field_name)
            if field_name in self.reference_array_fields:
                if self.is_reference_array_required(node) and (not isinstance(rows, list) or not rows):
                    missing[field_name] = []
                    if title not in titles:
                        titles.append(title)
                return
            row_schema = node.get("items") if isinstance(node.get("items"), dict) else node
            row_fields = self.collect_required_fields(row_schema)
            if not row_fields:
                if self.is_required(node) and not self.has_reaction(node) and self.field_missing(data, field_name):
                    self.add_value(missing, field_name, self.field_value(data, field_name))
                    if title not in titles:
                        titles.append(title)
                return

            if not isinstance(rows, list) or not rows:
                # feeInfoMap 只定义费用行结构；没有实际行不代表行内字段缺失。
                if is_fee_action:
                    return
                row_data: dict[str, Any] = {}
                self.add_fee_row_context(row_data, {}, field_name, title, is_fee_action)
                for field in row_fields:
                    self.add_row_value(row_data, {}, field["name"])
                missing[field_name] = [row_data]
                if title not in titles:
                    titles.append(title)
                return

            for row in rows:
                if not isinstance(row, dict):
                    continue
                missed = [field for field in row_fields if self.field_missing(row, field["name"])]
                if not missed:
                    continue
                row_data = {} if only_required_parent_children else dict(row)
                self.add_fee_row_context(row_data, row, field_name, title, is_fee_action)
                for field in row_fields:
                    self.add_row_value(row_data, row, field["name"])
                missing.setdefault(field_name, []).append(self.normalize(row_data))
                if title not in titles:
                    titles.append(title)
            return

        if node.get("properties"):
            next_data = self.read_value(data, field_name)
            if not isinstance(next_data, dict):
                if self.is_transparent_container(node, key):
                    next_data = data
                elif field_name not in data and not self.has_child_data(node, data):
                    return
                else:
                    next_data = data
            for child_key, child in node.get("properties", {}).items():
                self.check_node(
                    child,
                    child_key,
                    next_data,
                    form_data,
                    missing,
                    titles,
                    only_required_parent_children,
                    fee_action_node_ids,
                )
            return

        if self.is_required(node) and not self.has_reaction(node) and self.field_missing(data, field_name):
            self.add_value(missing, field_name, self.field_value(data, field_name))
            if title not in titles:
                titles.append(title)

    def check_required(
        self,
        schema: dict[str, Any],
        form_field_info: dict[str, Any],
        only_required_parent_children: bool = True,
    ) -> tuple[dict[str, Any], list[str]]:
        """根据 schema 和草稿数据，得到缺失字段结构和缺失标题列表。"""
        form_data = self.get_form_data(form_field_info)
        missing: dict[str, Any] = {}
        titles: list[str] = []
        fee_action_node_ids = {
            id(node)
            for node in self.get_fee_schema(schema).values()
            if isinstance(node, dict)
        }
        for key, node in self.get_form_sections(schema).items():
            self.check_node(
                node,
                key,
                form_data,
                form_data,
                missing,
                titles,
                only_required_parent_children,
                fee_action_node_ids,
            )
        return missing, titles

    def money_text(self, value: Any) -> str:
        """把金额转成前端显示的 ¥xx.xx 文本。"""
        try:
            return f"¥{float(value or 0):.2f}"
        except (TypeError, ValueError):
            return "¥0.00"

    def fee_amount(self, rows: Any) -> float:
        """汇总某个费用 code 下所有行的报销金额。"""
        rows = rows if isinstance(rows, list) else []
        total = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                total += float(row.get("reimbursementTotalAmount") or 0)
            except (TypeError, ValueError):
                pass
        return total

    def build_primary_list(self, schema: dict[str, Any], form_data: dict[str, Any]) -> list[dict[str, Any]]:
        """根据 feeInfoMap 组装卡片顶部的费用汇总列表。"""
        fee_schema = self.get_fee_schema(schema)
        fee_info_map = form_data.get("feeInfoMap") if isinstance(form_data.get("feeInfoMap"), dict) else {}
        result = []
        for fee_code, rows in fee_info_map.items():
            title = self.title_of(fee_code, fee_schema.get(fee_code, {}))
            result.append({"title": f"{FEE_ICON_MAP.get(fee_code, '📌')} {title}", "value": self.money_text(self.fee_amount(rows))})
        return result

    def build_header(self, form_data: dict[str, Any]) -> dict[str, Any]:
        """组装卡片头部标题和副标题。"""
        basic_info = form_data.get("basicInfo") if isinstance(form_data.get("basicInfo"), dict) else form_data
        title = basic_info.get("reimbursementTypeName") or basic_info.get("certName") or "日常差旅报销"
        traveler = basic_info.get("applicant") or basic_info.get("agentName") or ""
        route = basic_info.get("travelRoute") or ""
        date_text = basic_info.get("travelDate") or ""
        trip_days = basic_info.get("tripDays")
        sub_title = " | ".join(str(item) for item in (traveler, route, date_text) if item)
        if trip_days not in (None, ""):
            sub_title = f"{sub_title} ({trip_days}天)" if sub_title else f"{trip_days}天"
        return {"title": title, "subTitle": sub_title}

    def build_card(
        self,
        schema: dict[str, Any],
        form_field_info: dict[str, Any],
        missing: dict[str, Any],
        titles: list[str],
        form_id: str,
        matter_uniq_code: str = "",
    ) -> dict[str, Any]:
        """根据是否有缺失字段，组装两种不同的卡片结构。"""
        form_data = self.get_form_data(form_field_info)
        primary_list = self.build_primary_list(schema, form_data)
        fee_info_map = form_data.get("feeInfoMap") if isinstance(form_data.get("feeInfoMap"), dict) else {}
        total = sum(self.fee_amount(rows) for rows in fee_info_map.values())
        count = len(titles)

        basic_info = form_data.get("basicInfo") if isinstance(form_data.get("basicInfo"), dict) else {}
        route_params = {
            "draftId": form_id,
            "certCode": basic_info.get("reimbursementTypeCode") or "",
            "certName": basic_info.get("reimbursementTypeName") or "",
            "certType": "reimb",
            "economicMatterTypeId": matter_uniq_code or basic_info.get("economicMatterTypeId") or "",
        }
        card = {
            "result": f"✅ 已根据凭证自动填单。有{count}项信息需要您手动补充：" if count else "",
            "renderName": "msg-voucher-recognition-card",
            "rootComponent": "base-warp",
            "prop": "mode:single",
            "data": {
                "answer": "" if count else "已为您自动填写报销单，请确认以下信息：",
                "header": self.build_header(form_data),
                "primaryList": primary_list,
                "summary": {"label": "报销总额：", "value": self.money_text(total)},
            },
        }

        if not count:
            card["data"]["footer"] = {
                "leftButton": {
                    "type": "primary",
                    "label": "查看单据 >",
                    "showType": "link",
                    "action": "route",
                    "extra": {"path": "plugin://rs-pre-approal/pre-page", "params": route_params},
                },
                "rightButton": {
                    "type": "primary",
                    "showType": "button",
                    "label": "下一步",
                    "action": "sendMsg",
                    "extra": {"data": "sendMsg", "content": "下一步"},
                },
            }
            card_str = json.dumps(card, ensure_ascii=False)
            return f"<res-card>{card_str}</res-card>"

        card["data"].update(
            {
                "warningBox": {
                    "title": "以下信息需您补充：",
                    "content": "、".join(titles),
                    "data": {
                        "schema": json.dumps({}, ensure_ascii=False),
                        "formData": json.dumps({key: "" for key in missing}, ensure_ascii=False),
                    },
                },
                "footer": {
                    "leftButton": {
                        "type": "primary",
                        "label": "跳转单据填写 >",
                        "showType": "link",
                        "action": "route",
                        "extra": {"path": "plugin://rs-pre-approal/pre-page", "params": route_params}
                        },
                    "rightButton": {
                        "type": "primary",
                        "showType": "button",
                        "label": "去补充",
                        "action": "fill_missing",
                        "sendMsgConfig": {"fieldList": [{"name": "data", "parserRule": {}}]},
                        "extra": {},
                    },
                },
            }
        )
        card_str = json.dumps(card, ensure_ascii=False)
        return f" <res-card>{card_str}</res-card>"

    def execute(
        self,
        form_id: str = "",
        reimbursement_type_code: str = "",
        supplement_data: dict[str, Any] | None = None,
        matter_uniq_code: str = "",
    ) -> str:
        """拉取 schema 和暂存单，并返回最终卡片。"""
        if not form_id or not reimbursement_type_code:
            raise ValueError("缺少 form_id 或 reimbursement_type_code，AI需重新决策")
        schema_response = self.get_schema_response(reimbursement_type_code, matter_uniq_code, form_id)
        form_field_info = self.get_draft_info(form_id)
        if supplement_data:
            if self.is_full_draft_data(supplement_data):
                form_field_info = supplement_data
            else:
                self.apply_supplement_data(form_field_info, supplement_data)
            self.save_draft(form_field_info)
            form_field_info = self.get_draft_info(form_id)
        schema = self.get_schema(schema_response)
        if not schema or not form_field_info:
            raise LookupError("schema 或单据暂存数据为空")
        missing, titles = self.check_required(schema, form_field_info)
        update_session(
            form_data={
                "form_id": form_id,
                "reimbursement_type_code": reimbursement_type_code,
                "required_field_check": not bool(missing),
            }
        )
        return self.build_card(schema, form_field_info, missing, titles, form_id, matter_uniq_code)


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
    """在异常文案中附加会话读取状态，区分空会话和读取失败。"""
    if not session_loaded:
        return f"{message}\nsession获取状态：未成功获取"
    try:
        session_text = json.dumps(session_data, ensure_ascii=False, default=str)
    except Exception:
        session_text = repr(session_data)
    return f"{message}\nsession获取状态：已获取\nsession值：{session_text}"


def parse_supplement_data(raw_data: Any) -> dict[str, Any]:
    """前端 data 是 dict 或 JSON string，统一转成 dict。"""
    if not raw_data:
        return {}
    if isinstance(raw_data, dict):
        return raw_data
    if not isinstance(raw_data, str):
        return {}
    if not raw_data.strip():
        return {}
    try:
        data = json.loads(raw_data)
    except json.JSONDecodeError as exc:
        raise ValueError("data 不是合法 JSON") from exc
    return data if isinstance(data, dict) else {}


def run() -> str:
    """skill 入口：从 session 读取上下文并执行必填项校验。"""
    session_data: Any = None
    session_loaded = False
    try:
        current_variables = get_current_variables()
        token = current_variables.get("token", "")
        if not token:
            raise ValueError("缺少 token，AI需重新决策")

        session_data = get_session()
        session_loaded = True
        form_session = session_data.get("form_data") or {}
        final_form_id = form_session.get("form_id")
        final_reimbursement_type_code = form_session.get("reimbursement_type_code")
        voucher_data = session_data.get("voucher_folder_data") or {}
        matter_uniq_code = voucher_data.get("matter_uniq_code","")
        # 前端补充后传入的 data 本身就是 formData/完整草稿，没有 formData 包装层。
        supplement_data = parse_supplement_data(current_variables.get("data"))

        checker = RequiredFieldChecker(token=token)
        card = checker.execute(
            form_id=final_form_id,
            reimbursement_type_code=final_reimbursement_type_code,
            supplement_data=supplement_data,
            matter_uniq_code=matter_uniq_code,
        )
        return card
    except ValueError as exc:
        return input_error(append_session_debug(str(exc), session_data, session_loaded))
    except RuntimeError as exc:
        return interface_result_error(append_session_debug(str(exc), session_data, session_loaded))
    except LookupError as exc:
        return interface_result_error(append_session_debug(str(exc), session_data, session_loaded))
    except Exception as exc:
        return interface_result_error(append_session_debug(str(exc), session_data, session_loaded))
