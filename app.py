from pathlib import Path
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import uuid
from datetime import datetime
from hermes_state import SessionDB
from run_agent import AIAgent
from gateway.session_context import set_current_session_id, set_custom_variables, get_session_env
import traceback
import os
from hermes_logging import setup_logging
import logging
import asyncio
import time
import json
import tempfile
import fcntl
from pathlib import Path
# 时区设置
os.environ['TZ'] = 'Asia/Shanghai'
time.tzset()

setup_logging()
logger = logging.getLogger(__name__)

# 主服务
app = FastAPI(title="Hermes API Service")

# 初始化数据库，全局统一实例
db = SessionDB()  

logger.info(f"当前时区设置为: {time.tzname}")

# ============ 请求/响应模型 ============

class Variables(BaseModel):
    token: str
    user_id: str
    agency_id: str
    data: Dict[str, Any] = {}

class HermesRequest(BaseModel):
    model: str = "qwen3.5-27b"
    input: str
    session_id: str = ""
    store: bool = True
    variables: Variables

# ============ 对话历史管理 ============

class ConversationManager:
    """对话历史管理器 - 使用数据库存储消息"""
    
    def __init__(self, db: SessionDB):
        self.db = db
    
    def get_history(self, session_id: str) -> List[Dict]:
        """从数据库获取会话的对话历史（OpenAI对话格式）"""
        # 使用 get_messages_as_conversation 获取 OpenAI 格式的对话历史
        conversation = self.db.get_messages_as_conversation(session_id)
        logger.info(
            "Retrieved %d messages for session %s",
            len(conversation),
            session_id,
        )
        return conversation

# 初始化对话管理器
conversation_manager = ConversationManager(db)


# ============ 会话状态持久化 ============

def get_hermes_home() -> str:
    """获取 Hermes 根目录，如果未定义则默认当前目录下的 .hermes"""
    # 若项目中已有此函数，可直接导入；此处提供一个兼容实现
    return os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))

def get_session_status_dir() -> Path:
    """获取会话状态目录"""
    return Path(get_hermes_home()) / "status"

def update_session_variables(session_id: str, **kwargs) -> dict:
    """
    更新会话变量到状态文件，加排他锁并原子替换。
    文件路径为: SESSION_DIR / f"{session_id}.json"
    返回合并后的完整数据。
    """
    status_dir = get_session_status_dir()
    status_dir.mkdir(parents=True, exist_ok=True)
    session_file = status_dir / f"{session_id}.json"

    # 使用 'a+' 模式确保文件存在
    with open(session_file, 'a+', encoding='utf-8') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)   # 排他锁

        f.seek(0)
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = {}   # 若文件为空或损坏，视为空会话

        # 合并新字段（支持嵌套字典更新）
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


# ============ API 接口 ============

def redact_message_credentials(value: Any) -> Any:
    """仅脱敏消息元数据中明确的凭证字段，不修改正常消息正文。"""
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if str(key).lower() in {"token", "authorization", "api_key"}
                else redact_message_credentials(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_message_credentials(item) for item in value]
    return value


@app.post("/api/hermes/responses")
async def hermes_responses(
    request: HermesRequest,
    authorization: Optional[str] = Header(None),
    auth_source: Optional[str] = Header(None)
):
    """
    Hermes 对话接口
    支持多轮对话和会话管理
    """
    try:
        # 1. 验证认证
        if not authorization:
            raise HTTPException(status_code=401, detail="Authorization header required")
        
        token = authorization.replace("Bearer ", "")
        if token != request.variables.token:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        # 2. 获取请求的session_id
        request_session_id = request.session_id
        logger.info("Request session id: %s", request_session_id)
        
        # 3. 启动agent实例
        agent = AIAgent(
            model="deepseek-v4-flash",  # 使用自定义端点名称
            base_url="https://chat.bosssoft.com.cn/v1", 
            save_trajectories=True,
            quiet_mode=True,
            session_id = request_session_id, # 如果为空，agent会创建
            session_db=db,
        )
        agent_session_id = agent.session_id
        logger.info("Agent session id: %s", agent_session_id)
        # set_current_session_id(agent_session_id)
        # 4. 获取对话历史（基于agent_session_id）
        history = conversation_manager.get_history(agent_session_id)
        
        logger.info("session id: [%s], histoty length = [%s]", agent_session_id, len(history))
        
        variables_dict = {
            "token": request.variables.token,
            "user_id": request.variables.user_id,
            "agency_id": request.variables.agency_id,
            "data": request.variables.data,
            # "auth_source": auth_source or "sso"
        }

        # 5. 设置环境变量，todo @zhaofu 改成存入status文件
        # set_custom_variables(variables_dict)

        update_session_variables(agent_session_id, **variables_dict)
        # 6. 并发使用 run_conversation
        result = await asyncio.to_thread(
            agent.run_conversation,
            request.input,
            conversation_history=history if history else None,
        )
        
        # 7. 提取响应内容和更新后的历史
        if isinstance(result, dict):
            response_content = result.get("final_response", str(result))
        else:
            response_content = str(result)
        
        # 8. 构建响应
        response_id = f"resp_{uuid.uuid4().hex[:16]}"
        
        # 获取更新后的历史长度
        updated_history = conversation_manager.get_history(agent_session_id)
        
        return {
            "id": response_id,
            "object": "chat.completion",
            "created": int(datetime.now().timestamp()),
            "model": request.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response_content
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": len(request.input.split()),
                "completion_tokens": len(response_content.split()),
                "total_tokens": len(request.input.split()) + len(response_content.split())
            },
            "session_id": agent_session_id,
            "history_length": len(updated_history)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error: {e}")
        logger.exception("Hermes response failed")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/hermes/session/end")
async def end_session(session_id: str, end_reason: str = "user_exit"):
    """结束会话"""
    try:
        # 检查会话是否存在
        session = db.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        
        db.end_session(session_id, end_reason=end_reason)
        return {
            "status": "success",
            "session_id": session_id,
            "ended_at": datetime.now().isoformat()
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/hermes/session/reopen")
async def reopen_session(session_id: str):
    """重新打开会话"""
    try:
        # 检查会话是否存在
        session = db.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        
        db.reopen_session(session_id)
        return {
            "status": "success",
            "session_id": session_id
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hermes/session/{session_id}")
async def get_session(session_id: str):
    """获取会话信息"""
    try:
        session = db.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        
        # 获取原始消息（包含所有元数据）
        messages = db.get_messages(session_id)
        conversation = db.get_messages_as_conversation(session_id)
        safe_conversation = redact_message_credentials(conversation)
        
        return {
            "session_id": session_id,
            "is_active": session.get('ended_at') is None,
            "created_at": session.get('created_at'),
            "ended_at": session.get('ended_at'),
            "end_reason": session.get('end_reason'),
            "source": session.get('source'),
            "model": session.get('model'),
            "user_id": session.get('user_id'),
            "message_count": len(messages),
            "history_length": len(conversation),
            "conversation": safe_conversation[-6:]  # 返回最近3轮对话
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hermes/session/{session_id}/history")
async def get_session_history(session_id: str, limit: int = 10):
    """获取会话对话历史"""
    try:
        session = db.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        
        # 获取OpenAI格式的对话历史
        conversation = db.get_messages_as_conversation(session_id)
        
        # 获取原始消息（包含元数据）
        messages = db.get_messages(session_id)
        
        return {
            "session_id": session_id,
            "total": len(conversation),
            "history": conversation[-limit:] if limit else conversation,
            "raw_messages": messages[-limit:] if limit else messages  # 包含元数据
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/hermes/session/{session_id}/messages")
async def get_session_messages(session_id: str):
    """获取会话的原始消息（包含所有元数据）"""
    try:
        session = db.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        
        # 获取原始消息
        messages = db.get_messages(session_id)
        safe_messages = redact_message_credentials(messages)
        
        return {
            "session_id": session_id,
            "total": len(messages),
            "messages": safe_messages
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy", "service": "hermes-api"}


# ============ 启动服务器 ============

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=36060,
        log_level="info"
    )
