import pytest

from ocr.backends.base import OcrBox
from ocr.layout import reading_order_text


def cell(text, x, y, width=30, height=10, scale=1):
    return OcrBox(text, 1.0, [[a * scale, b * scale] for a, b in
                            [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]])


@pytest.mark.parametrize('scale', [1, 0.001, 3])
def test_column_major_table_keeps_values_on_named_rows(scale):
    boxes = [cell('方案', 0, 0, scale=scale), cell('基线', 0, 30, scale=scale),
             cell('方案甲', 0, 60, scale=scale), cell('方案乙', 0, 90, scale=scale),
             cell('请求数', 100, 0, scale=scale), cell('500', 100, 30, scale=scale),
             cell('500', 100, 60, scale=scale), cell('500', 100, 90, scale=scale),
             cell('QPS', 200, 0, scale=scale), cell('0.2', 200, 30, scale=scale),
             cell('0.2', 200, 60, scale=scale), cell('0.2', 200, 90, scale=scale),
             cell('耗时', 300, 0, scale=scale), cell('25.04', 300, 30, scale=scale),
             cell('21.60 (+13.7%)', 300, 61, scale=scale),
             cell('11.84 (+52.7%)', 300, 89, scale=scale)]
    text = reading_order_text(boxes)
    assert text.splitlines() == ['方案 | 请求数 | QPS | 耗时', '基线 | 500 | 0.2 | 25.04',
                                 '方案甲 | 500 | 0.2 | 21.60 (+13.7%)',
                                 '方案乙 | 500 | 0.2 | 11.84 (+52.7%)']


def test_missing_or_invalid_geometry_preserves_backend_text():
    for bbox in [[], [[0, 0]], [[0, 0]] * 4, [[float('nan'), 0]] * 4]:
        assert reading_order_text([cell('第一行', 0, 0), OcrBox('第二行', 1, bbox)]) == '第一行\n第二行'


def test_adjacent_words_and_separate_paragraphs():
    boxes = [cell('第三段', 0, 80), cell('Hello', 0, 0), cell('world', 35, 1),
             cell('第二段', 0, 40), OcrBox(' ', 1)]
    assert reading_order_text(boxes) == 'Hello | world\n第二段\n第三段'


@pytest.mark.asyncio
async def test_foreground_ipc_preserves_table_rows_and_still_filters_privacy(monkeypatch):
    from types import SimpleNamespace
    from memory_bread_ipc import IpcRequest
    from ocr.backends.base import OcrOutput
    from ocr.worker import OcrWorker

    monkeypatch.setattr('ocr.worker.interactive_demand_active', lambda: False)
    boxes = [cell('方案甲', 0, 0), cell('方案乙', 0, 30),
             cell('SECRET', 100, 0), cell('11.84', 100, 30)]
    class Engine:
        def process(self, path):
            return OcrOutput(boxes)
    class Privacy:
        def detect_and_redact(self, text, actual_boxes):
            assert text == '方案甲 | SECRET\n方案乙 | 11.84'
            assert actual_boxes == boxes
            return SimpleNamespace(is_sensitive=True, sanitized_text=text.replace('SECRET', '[已过滤]'),
                                   detected_types=['test'])
    worker = OcrWorker(engine=Engine())
    worker._privacy_filter = Privacy()
    response = await worker.handle(IpcRequest(id='layout', ts=1, task={
        'type': 'ocr', 'capture_id': 0, 'screenshot_path': 'image', 'priority': 'foreground'}))
    assert response.result.text == '方案甲 | [已过滤]\n方案乙 | 11.84'
