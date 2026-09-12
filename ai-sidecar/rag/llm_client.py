"""
LLM 客户端

支持本地 Ollama 和云端 API（通义千问、文心一言等）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Any, Optional
from openai import OpenAI
from runtime_endpoints import service_base_url

logger = logging.getLogger(__name__)


class LLMClient:
    """LLM 客户端（兼容 Ollama 和 OpenAI API）"""
    
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: str = "ollama",  # Ollama 不需要真实 key
        model: str = "qwen2.5:7b",
    ):
        """
        初始化 LLM 客户端
        
        Args:
            base_url: API 基础地址
            api_key: API 密钥
            model: 模型名称
        """
        base_url = base_url or (service_base_url("ollama") + "/v1")
        self.base_url = base_url
        self.model = model
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        logger.info(f"初始化 LLMClient: {base_url}, model={model}")
    
    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: int = 2000,
        temperature: float = 0.7,
    ) -> str:
        """
        生成回答
        
        Args:
            prompt: 用户提示词
            system_prompt: 系统提示词
            max_tokens: 最大生成长度
            temperature: 温度参数
            
        Returns:
            生成的文本
        """
        try:
            messages = []
            
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            
            messages.append({"role": "user", "content": prompt})
            
            logger.debug(f"调用 LLM: {self.model}")
            
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            
            answer = response.choices[0].message.content
            logger.debug(f"LLM 返回: {len(answer)} 字符")
            
            return answer
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return f"抱歉，AI 服务暂时不可用：{str(e)}"
    
    def generate_with_context(
        self,
        query: str,
        contexts: List[Dict[str, Any]],
        user_preferences: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        基于检索上下文生成回答
        
        Args:
            query: 用户查询
            contexts: 检索到的上下文列表
            user_preferences: 用户偏好设置
            
        Returns:
            生成的回答
        """
        # 构建系统提示词
        system_prompt = self._build_system_prompt(user_preferences)
        
        # 构建用户提示词
        user_prompt = self._build_user_prompt(query, contexts)
        
        return self.generate(
            prompt=user_prompt,
            system_prompt=system_prompt,
            max_tokens=2000,
            temperature=0.7,
        )
    
    def _build_system_prompt(
        self,
        user_preferences: Optional[Dict[str, str]] = None,
    ) -> str:
        """构建系统提示词"""
        base_prompt = """你是记忆面包，一个智能的工作助手。

你的职责：
1. 根据用户的工作记录（屏幕截图、文本记录）回答问题
2. 提供准确、有用的信息
3. 如果信息不足，诚实地告知用户

回答要求：
- 简洁明了，直接回答问题
- 引用具体的工作记录作为依据
- 使用中文回答
"""
        
        # 注入用户偏好
        if user_preferences:
            tone = user_preferences.get("tone", "专业")
            style = user_preferences.get("style", "简洁")
            base_prompt += f"\n用户偏好：语气={tone}，风格={style}"
        
        return base_prompt
    
    def _build_user_prompt(
        self,
        query: str,
        contexts: List[Dict[str, Any]],
    ) -> str:
        """构建用户提示词"""
        if not contexts:
            return f"用户问题：{query}\n\n没有找到相关的工作记录。"
        
        # 构建上下文
        context_text = "相关工作记录：\n\n"
        for i, ctx in enumerate(contexts[:5], start=1):  # 最多使用 5 条
            text = ctx.get("text", "")[:200]  # 截断过长文本
            app = ctx.get("app_name", "未知应用")
            source = ctx.get("source", "unknown")
            
            context_text += f"{i}. [{app}] {text}\n"
            context_text += f"   (来源: {source})\n\n"
        
        prompt = f"""{context_text}

用户问题：{query}

请根据上述工作记录回答用户的问题。如果记录中没有相关信息，请诚实告知。
"""
        
        return prompt


# 全局单例
_llm_client = None


def get_llm_client() -> LLMClient:
    """获取全局 LLM 客户端单例"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
