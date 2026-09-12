# 脑暴完成后文档生成失败修复验收

日期：2026-09-05

## 故障与根因

最新失败记录为本机创作历史 131，模式为 brainstorm，消费简报 revision 11，正文为空。运行日志显示正式生成请求多次收到模型接口 403 权限拒绝；路由回退、子节点重试及节点跳过后最终失败。前端隐私过滤拦截了供应商原始错误，显示“生成失败，请稍后重试”。

前端使用有效模型选择规则，旧模型 ID 回退为本地模式；Core 的 Agent 入口却对 local 请求调用旧偏好注入函数，从 creation.models 中取出启用的历史外部模型与凭据。脑暴请求未经过这段偏好注入，因此脑暴正常而正式生成失败。修复不删除用户偏好，不修改原会话或简报。

## 修复

- Agent 入口不再从旧偏好注入模型、密钥和地址，且丢弃请求中残留的这些字段。local 使用 Sidecar 当前注册的本地模型，external 保留 model.request 品牌网关流程。
- 401/403 在路由和节点执行中立即上抛；流式与非流式专业节点均不因“尚无输出”重试永久 HTTP 错误。
- 权限拒绝映射为 MODEL_ACCESS_DENIED 和可操作的中文提示；其他模型请求错误也不返回供应商原文。
- 真实本地验收发现仅使用 /no_think 文本仍可能耗尽输出预算。对已有 disable_thinking 参数改用模型模板中的关闭思考前缀，保留其他调用原有行为。依据：[Qwen 官方 chat template](https://huggingface.co/Qwen/Qwen3.5-4B/blob/main/chat_template.jinja)。
- 全部正文节点失败后，不再将空文档标记为 run.completed；已有非空成果的节点容错仍保留。

## 验证

- Python 创作模块与脑暴记忆回归：354 passed，1 skipped。跳过项为默认关闭、需 RUN_LIVE_BRAINSTORM_MEMORY=1 的历史记忆在线评估。
- Core 创作相关单元/API/新增模型边界回归：66 passed。新增集成测试覆盖 local/external × direct/brainstorm × 有无请求凭据，均保留启用的旧云模型偏好，断言转发配置为空且 SSE 正常完成。
- 前端脑暴与 Agent Loop 回归：47 passed。
- 6 个修改的 Python 文件在 Python 3.9.25 下完成语法及类型联合检查；git diff --check 通过。
- 后台管理器已自动加载修复后的 Core 与 Creation Service，复查进程启动时间晚于相应源码变更。
- 真实 HTTP 验收：POST /api/creation/agent/run，local + brainstorm，经 Core → Sidecar → 本地推理生成 89 字符正文，13 秒收到 run.completed，failed_steps 为空；日志确认调用本地推理接口。验收使用独立短文请求，不覆盖历史 131。

## 范围

未重跑或覆盖用户原始长文，也未进行 DMG 打包。原脑暴简报保留，可在原会话再次生成。短文验收证明本地执行链路与非空产出已恢复，不代表原长文内容质量已经验收。
