"""共享的本地模型解码约束，无服务初始化或模型加载副作用。"""

from typing import Any, Dict


def decoding_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """保持 grammar 紧凑；集合与文本上限继续由业务校验器执行。

    展开有界的嵌套重复会超过 llama.cpp grammar 的复杂度上限，可能静默关闭
    约束。无界 grammar 循环保留相同字段类型，避免这种展开；0/1 项限制不膨胀。
    """
    def compact(value: Any) -> Any:
        if isinstance(value, list):
            return [compact(item) for item in value]
        if isinstance(value, dict):
            return {
                key: compact(item) for key, item in value.items()
                if key != "maxLength" and not (key == "maxItems" and item > 1)
            }
        return value
    return compact(schema)
