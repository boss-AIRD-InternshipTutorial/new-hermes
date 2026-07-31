import configparser
import fcntl
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from hermes_constants import get_hermes_home
from hermes_logging import setup_logging
import logging

setup_logging()
logger = logging.getLogger(__name__)


SESSION_STATUS_DIR = Path(get_hermes_home()) / "status"  # 会话变量存储路径

def get_current_session_id() -> str:
    """Hermes平台通用方法：获取会话ID"""
    session_id = ""
    
    # 尝试从gateway获取
    try:
        from gateway.session_context import get_session_env

        session_id = get_session_env("HERMES_SESSION_ID", "")
        if session_id:
            logger.info(
                "get_current_session_id HERMES_SESSION_ID pid=%s session_id=%s", 
                os.getpid(),
                session_id
            )
            return session_id
    except ImportError:
        pass

    return session_id

def get_session_status_file_path() -> Path:
    """获取指定会话的会话文件存储路径。

    入参:

    出参:
    - Path: 对应的 JSON 文件路径对象
    """
    # 每个会话单独落一个 json 文件，便于按会话隔离变量。
    session_id = get_current_session_id()
    return SESSION_STATUS_DIR / f"{session_id}.json"


def get_current_variables() -> dict:
    session_file = get_session_status_file_path()
    data = {}
    if not session_file.exists():
        return data
    with open(session_file, "r", encoding="utf-8") as f:
        data = json.loads(f.read())
    if isinstance(data, dict):
        variables = data.get("variables")
        logger.info(
                "get_current_variables = %s", 
                variables
            )
        if isinstance(variables, dict):
            return variables
        # 否则把 dict 当作裸 cv 直接返回
    return data


def get_session() -> dict[str, Any]:
    """读取会话的完整流程变量，并加共享锁

    入参:

    出参:
    - dict[str, Any]: 完整的会话字典
    """
    session_file = get_session_status_file_path()
    if not session_file.exists():
        return {}

    # 打开文件并加共享锁（允许其他读，阻塞写）
    with open(session_file, 'r', encoding='utf-8') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)  # 共享锁
        try:
            data = json.load(f)
            return data
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)  # 解锁（可选，with块结束自动释放）


def delete_session() -> dict[str, Any]:
    """删除会话的完整流程变量。

    入参:

    出参:
    - dict[str, Any]: 删除后重新生成的空会话
    """
    session_file = get_session_status_file_path()
    if session_file.exists():
        try:
            # 打开文件并加排他锁，防止在重命名时与其他读写操作（如 update_session）发生并发冲突
            with open(session_file, 'a+', encoding='utf-8') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                new_name = f"{session_file.stem}-delete{session_file.suffix}"
                new_path = session_file.with_name(new_name)
                session_file.rename(new_path)
        except FileNotFoundError:
            # 防止在 exists() 检查和 open() 之间文件被意外删除
            pass

    return None


def update_session(**kwargs) -> dict[str, Any]:
    """更新会话变量，加排他锁并原子替换，保证并发安全"""
    session_file = get_session_status_file_path()
    # 使用 'a+' 模式确保文件存在（如果不存在则创建），且可读写
    with open(session_file, 'a+', encoding='utf-8') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)   # 排他锁

        f.seek(0)  # 移动到文件开头读取内容
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = {}   # 若文件为空或损坏，视为空会话

        # 合并新字段
        for key, value in kwargs.items():
            if isinstance(value, dict) and key in data and isinstance(data[key], dict):
                data[key].update(value)
            else:
                data[key] = value

        # 原子替换写入（临时文件 + os.replace）
        temp_fd, temp_path = tempfile.mkstemp(
            dir=session_file.parent,
            prefix=session_file.name + '.tmp',
            suffix='.json'
        )
        try:
            with os.fdopen(temp_fd, 'w', encoding='utf-8') as tf:
                json.dump(data, tf, ensure_ascii=False, indent=2)
            os.replace(temp_path, session_file)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise
        # 锁在 with 结束时释放
    return data




#################################### 获取参数 START ####################################

def get_config():
    """
    获取配置项：读取 config.ini 文件并返回所有键值对的字典（JSON）

    返回示例：{"base_url": "http://43.138.207.132:28080/saas-industry106"}
    """
    config = configparser.ConfigParser()

    # 获取当前脚本所在目录
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # 构造 config.ini 的绝对路径
    config_path = os.path.join(current_dir, '..', 'references', 'config.ini')

    # 读取配置文件
    config.read(config_path, encoding='utf-8')

    result = {}
    for section in config.sections():
        for key, value in config.items(section):
            result[key] = value

    return result

#################################### 获取参数 END ####################################



#################################### 兜底回复函数 START ####################################

def unanswered(answer: str) -> str:
    """兜底回复函数

    触发节点:
    - 用户意图无法被任何流程覆盖时，需要用拟人化口吻去和用户交互

    业务规则（当未匹配到其他流程、需要进入本流程时，再继续考虑以下情况）：
    - 如果用户提出要修改单据内的信息，则告诉用户不支持对话修改单据信息、请在单据详情页修改

    入参:
    - answer (str): 用于回复用户的文字

    出参:
    - str: 返回通用回复卡片字符串
    """
    payload = {
        "result": answer,
        "renderName": "",
        "rootComponent": "base-warp",
        "prop": "mode:single"
    }

    return f"<res-card>{json.dumps(payload, ensure_ascii=False)}</res-card>"

#################################### 兜底回复函数 END ####################################
