"""Conservative recall policy for consultation requests (Python 3.9).

Consultations recall by default; only explicit user restrictions can skip recall.
Only the user's instruction is classified, never the contents of an attachment.
"""
import re
from typing import Tuple


def decide_retrieval(instruction: str, has_material: bool) -> Tuple[bool, str]:
    text = (instruction or '').strip().lower()
    if re.search(r'(?:不要|无需|不用|禁止)(?:再)?(?:检索|搜索|查询|召回)(?:历史|记忆|资料|知识库)?|'
                 r'\b(?:do not|don\x27t) (?:search|retrieve)\b', text):
        return False, 'explicit_no_retrieval'
    return True, 'recall_by_default'
