"""Conservative document-body gate, shared in behavior with the bake gate.

A long navigation tree is not evidence of a loaded document. Require a loading
failure or multiple UI signals before rejecting prose-poor content; outlines,
tables and code documents without those signals remain valid.
"""
import re

_FAILURE_MARKERS = ('you need to enable javascript to run this app', '文档加载失败', '页面加载失败')
_NAVIGATION_MARKERS = ('知识库', '目录', '首页', '导航')
_UI_MARKERS = ('0 bytes/s', '全部暂停', '进行中', '收藏', '管理员', '分享', '编辑', '设置')
_EDITOR_MARKERS = ('关闭提示', '只读模式', '离线阅读模式', '默认字体', '文档内容为空')


def is_document_shell(text: str) -> bool:
    lowered = (text or '').lower()
    # Only completed, reasonably sized prose sentences count. A flattened AX
    # navigation tree may be thousands of characters long without any sentences.
    sentences = re.findall(r'[^。！？!?；;\n.]*[。！？!?；;.]', lowered)
    prose_chars = sum(
        len(sentence) for sentence in sentences
        if 32 <= len(sentence) <= 400
        and not any(marker in sentence for marker in _FAILURE_MARKERS + _EDITOR_MARKERS)
        and sum(marker in sentence for marker in _NAVIGATION_MARKERS) < 2
    )
    if prose_chars >= 200:
        return False
    failure = any(marker in lowered for marker in _FAILURE_MARKERS)
    navigation = sum(marker in lowered for marker in _NAVIGATION_MARKERS)
    controls = sum(marker in lowered for marker in _UI_MARKERS)
    return failure or (navigation >= 2 and controls >= 3)
