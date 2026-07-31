import json
try:
    from .main_scripts import get_config, get_current_variables, get_session, delete_session, update_session
except ImportError:
    from main_scripts import get_config, get_current_variables, get_session, delete_session, update_session


def run() -> str:
    """单据终止子流程。

    入参:

    出参:
    - str: 返回单据终止结果
    """
    # 终止流程时，清空当前会话的流程变量
    delete_session()

    payload = {
        "result": "好的，已为您终止当前的报销单填报。",
        "renderName": "",
        "rootComponent": "base-warp",
        "prop": "mode:single"
    }
    return f"<res-card>{json.dumps(payload, ensure_ascii=False)}</res-card>"

