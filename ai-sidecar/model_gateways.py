"""First-party model gateways; endpoints are resolved locally, never from user URLs."""
import json
import re
import sys
from pathlib import Path

CATALOG = (Path(sys._MEIPASS) / "model_gateways/providers.json") if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent / "shared/model-gateways/providers.json"
PROVIDERS = json.loads(CATALOG.read_text())
PROVIDER_MAP = {p["id"]: p for p in PROVIDERS}
REGIONS = {
    "cn-beijing": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "ap-southeast-1": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "us-east-1": None,
    "ap-northeast-1": None,
}


def resolve_gateway(provider, region="cn-beijing", workspace_id=""):
    if not isinstance(provider, str) or provider not in PROVIDER_MAP:
        raise ValueError("不支持的模型平台")
    if provider != "aliyun":
        return PROVIDER_MAP[provider]["base_url"]
    if not isinstance(region, str) or region not in REGIONS:
        raise ValueError("不支持的百炼地域")
    if not isinstance(workspace_id, str) or (workspace_id and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", workspace_id)):
        raise ValueError("百炼业务空间 ID 格式无效")
    if workspace_id:
        return f"https://{workspace_id}.{region}.maas.aliyuncs.com/compatible-mode/v1"
    if not REGIONS[region]:
        raise ValueError("该地域需要填写百炼业务空间 ID")
    return REGIONS[region]
