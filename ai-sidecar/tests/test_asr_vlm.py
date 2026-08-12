"""
ASR + VLM 模块测试

测试覆盖：
- AsrOutput / AsrSegment 数据类属性
- MockAsrBackend 行为
- AsrModel 编排逻辑（transcribe / 错误处理）
- AsrWorker 异步 IPC 处理（成功 / 文件不存在 / 后端不可用）
- VlmOutput / SceneType 数据类属性
- MockVlmBackend 行为
- VlmModel 编排逻辑
- VlmWorker 异步 IPC 处理
- WhisperBackend 接口（不依赖实际安装）
- MiniCpmVBackend 接口（不依赖实际安装）
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from typing import Optional

import pytest

from asr.backend  import AsrBackend, AsrOutput, AsrSegment
from asr.model    import AsrModel
from asr.whisper  import WhisperBackend
from asr.worker   import AsrWorker
from vlm.backend  import SceneType, VlmBackend, VlmOutput
from vlm.minicpm  import MiniCpmVBackend
from vlm.model    import VlmModel
from vlm.worker   import VlmWorker
from memory_bread_ipc import IpcResponse, ResponseStatus


# ── Mock 工具 ─────────────────────────────────────────────────────────────────

class MockAsrBackend(AsrBackend):
    """测试用 Mock ASR 后端"""

    def __init__(
        self,
        output:       Optional[AsrOutput] = None,
        should_raise: Optional[Exception] = None,
        available:    bool             = True,
    ) -> None:
        self._output       = output or AsrOutput(
            text="模拟转录文字",
            language="zh",
            segments=[
                AsrSegment(start_sec=0.0, end_sec=2.5, text="模拟转录文字"),
            ],
        )
        self._should_raise = should_raise
        self._available    = available
        self.call_count    = 0

    def is_available(self) -> bool:
        return self._available

    def transcribe(self, audio_path: str, language: Optional[str] = None) -> AsrOutput:
        self.call_count += 1
        if self._should_raise:
            raise self._should_raise
        return self._output

    @property
    def model_name(self) -> str:
        return "mock-whisper"


class MockVlmBackend(VlmBackend):
    """测试用 Mock VLM 后端"""

    def __init__(
        self,
        output:       Optional[VlmOutput] = None,
        should_raise: Optional[Exception] = None,
        available:    bool             = True,
    ) -> None:
        self._output = output or VlmOutput(
            description = "用户正在使用飞书编辑文档",
            scene_type  = SceneType.DOC_WRITING,
            tags        = ["飞书", "文档", "工作"],
        )
        self._should_raise = should_raise
        self._available    = available
        self.call_count    = 0

    def is_available(self) -> bool:
        return self._available

    def analyze(self, image_path: str, prompt: str = "") -> VlmOutput:
        self.call_count += 1
        if self._should_raise:
            raise self._should_raise
        return self._output

    @property
    def model_name(self) -> str:
        return "mock-vlm"


def _make_asr_request(audio_path: str, language: Optional[str] = None):
    from memory_bread_ipc import IpcRequest
    from memory_bread_ipc.message import AsrRequest
    task = AsrRequest(capture_id=1, audio_path=audio_path, language=language)
    return IpcRequest(id=str(uuid.uuid4()), ts=int(time.time() * 1000), task=task)


def _make_vlm_request(screenshot_path: str, prompt: str = "请分析这张截图"):
    from memory_bread_ipc import IpcRequest
    from memory_bread_ipc.message import VlmRequest
    task = VlmRequest(capture_id=1, screenshot_path=screenshot_path, prompt=prompt)
    return IpcRequest(id=str(uuid.uuid4()), ts=int(time.time() * 1000), task=task)


@pytest.fixture
def wav_path(tmp_path) -> str:
    """创建一个最小合法 WAV 文件（44 字节）"""
    path = tmp_path / "test.wav"
    # WAV header: RIFF + WAV + fmt + data
    header = (
        b"RIFF" + (36).to_bytes(4, "little") +    # ChunkSize
        b"WAVE" +
        b"fmt " + (16).to_bytes(4, "little") +    # Subchunk1Size
        (1).to_bytes(2, "little") +                # PCM format
        (1).to_bytes(2, "little") +                # Mono
        (16000).to_bytes(4, "little") +            # SampleRate 16kHz
        (32000).to_bytes(4, "little") +            # ByteRate
        (2).to_bytes(2, "little") +                # BlockAlign
        (16).to_bytes(2, "little") +               # BitsPerSample
        b"data" + (0).to_bytes(4, "little")        # Subchunk2Size (empty audio)
    )
    path.write_bytes(header)
    return str(path)


@pytest.fixture
def jpeg_path(tmp_path) -> str:
    """创建一个最小合法 JPEG 文件"""
    from PIL import Image
    img  = Image.new("RGB", (4, 4), color=(100, 150, 200))
    path = tmp_path / "test.jpg"
    img.save(str(path), format="JPEG")
    return str(path)


# ── AsrOutput / AsrSegment ───────────────────────────────────────────────────

class TestAsrOutput:
    def test_duration_with_segments(self):
        output = AsrOutput(
            text="文字",
            segments=[
                AsrSegment(0.0, 1.5, "文"),
                AsrSegment(1.5, 3.2, "字"),
            ],
        )
        assert output.duration == pytest.approx(3.2)

    def test_duration_empty(self):
        assert AsrOutput(text="").duration == 0.0

    def test_is_empty_true(self):
        assert AsrOutput(text="").is_empty
        assert AsrOutput(text="   ").is_empty

    def test_is_empty_false(self):
        assert not AsrOutput(text="你好").is_empty

    def test_default_language(self):
        assert AsrOutput(text="text").language == "zh"

    def test_segment_fields(self):
        seg = AsrSegment(start_sec=1.0, end_sec=2.5, text="测试")
        assert seg.start_sec == pytest.approx(1.0)
        assert seg.end_sec   == pytest.approx(2.5)
        assert seg.text      == "测试"


# ── MockAsrBackend ────────────────────────────────────────────────────────────

class TestMockAsrBackend:
    def test_returns_output(self, wav_path):
        backend = MockAsrBackend()
        output  = backend.transcribe(wav_path)
        assert output.text == "模拟转录文字"

    def test_raises_when_configured(self, wav_path):
        backend = MockAsrBackend(should_raise=RuntimeError("Whisper 崩溃"))
        with pytest.raises(RuntimeError, match="Whisper 崩溃"):
            backend.transcribe(wav_path)

    def test_unavailable(self):
        backend = MockAsrBackend(available=False)
        assert not backend.is_available()

    def test_call_count(self, wav_path):
        backend = MockAsrBackend()
        backend.transcribe(wav_path)
        backend.transcribe(wav_path)
        assert backend.call_count == 2

    def test_is_backend_subclass(self):
        assert isinstance(MockAsrBackend(), AsrBackend)


# ── AsrModel ─────────────────────────────────────────────────────────────────

class TestAsrModel:
    def test_transcribe_success(self, wav_path):
        model  = AsrModel(backend=MockAsrBackend())
        output = model.transcribe(wav_path)
        assert output.text == "模拟转录文字"

    def test_raises_when_backend_unavailable(self, wav_path):
        model = AsrModel(backend=MockAsrBackend(available=False))
        with pytest.raises(RuntimeError, match="不可用"):
            model.transcribe(wav_path)

    def test_model_name(self):
        model = AsrModel(backend=MockAsrBackend())
        assert model.model_name == "mock-whisper"

    def test_create_default_returns_model(self):
        model = AsrModel.create_default()
        assert isinstance(model, AsrModel)


# ── AsrWorker ────────────────────────────────────────────────────────────────

class TestAsrWorkerSuccess:
    async def test_ok_response(self, wav_path):
        model  = AsrModel(backend=MockAsrBackend())
        worker = AsrWorker(model=model)
        req    = _make_asr_request(wav_path)

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.OK
        assert resp.id == req.id
        assert resp.result is not None

    async def test_text_in_result(self, wav_path):
        model  = AsrModel(backend=MockAsrBackend())
        worker = AsrWorker(model=model)
        req    = _make_asr_request(wav_path)

        resp = await worker.handle(req)
        assert resp.result.text == "模拟转录文字"

    async def test_segments_in_result(self, wav_path):
        model  = AsrModel(backend=MockAsrBackend())
        worker = AsrWorker(model=model)
        req    = _make_asr_request(wav_path)

        resp = await worker.handle(req)
        assert len(resp.result.segments) == 1

    async def test_latency_recorded(self, wav_path):
        model  = AsrModel(backend=MockAsrBackend())
        worker = AsrWorker(model=model)
        resp   = await worker.handle(_make_asr_request(wav_path))
        assert resp.latency_ms >= 0

    async def test_response_id_matches(self, wav_path):
        model  = AsrModel(backend=MockAsrBackend())
        worker = AsrWorker(model=model)
        req    = _make_asr_request(wav_path)
        resp   = await worker.handle(req)
        assert resp.id == req.id


class TestAsrWorkerErrors:
    async def test_file_not_found(self, tmp_path):
        model  = AsrModel(backend=MockAsrBackend())
        worker = AsrWorker(model=model)
        req    = _make_asr_request(str(tmp_path / "ghost.wav"))

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.ERROR
        assert "FILE_NOT_FOUND" in (resp.error or "")

    async def test_backend_unavailable(self, wav_path):
        model  = AsrModel(backend=MockAsrBackend(available=False))
        worker = AsrWorker(model=model)
        req    = _make_asr_request(wav_path)

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.ERROR
        assert "ASR_FAILED" in (resp.error or "")

    async def test_runtime_error(self, wav_path):
        model  = AsrModel(backend=MockAsrBackend(
            available=True, should_raise=RuntimeError("Whisper 崩溃")
        ))
        worker = AsrWorker(model=model)
        req    = _make_asr_request(wav_path)

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.ERROR
        assert "ASR_FAILED" in (resp.error or "")

    async def test_error_has_id(self, tmp_path):
        model  = AsrModel(backend=MockAsrBackend())
        worker = AsrWorker(model=model)
        req    = _make_asr_request(str(tmp_path / "ghost.wav"))
        resp   = await worker.handle(req)
        assert resp.id == req.id


# ── VlmOutput / SceneType ────────────────────────────────────────────────────

class TestVlmOutput:
    def test_basic_fields(self):
        output = VlmOutput(
            description = "用户在使用飞书",
            scene_type  = SceneType.IM_CHAT,
            tags        = ["飞书", "IM"],
        )
        assert output.description == "用户在使用飞书"
        assert output.scene_type  == SceneType.IM_CHAT
        assert output.tags        == ["飞书", "IM"]

    def test_default_scene_type(self):
        output = VlmOutput(description="无法识别")
        assert output.scene_type == SceneType.UNKNOWN

    def test_default_tags(self):
        output = VlmOutput(description="test")
        assert output.tags == []

    def test_scene_type_values(self):
        assert SceneType.DOC_WRITING.value   == "doc_writing"
        assert SceneType.IM_CHAT.value       == "im_chat"
        assert SceneType.BROWSING.value      == "browsing"
        assert SceneType.CODING.value        == "coding"
        assert SceneType.VIDEO_MEETING.value == "video_meeting"


# ── MockVlmBackend ────────────────────────────────────────────────────────────

class TestMockVlmBackend:
    def test_returns_output(self, jpeg_path):
        backend = MockVlmBackend()
        output  = backend.analyze(jpeg_path)
        assert output.description == "用户正在使用飞书编辑文档"
        assert output.scene_type  == SceneType.DOC_WRITING

    def test_raises_when_configured(self, jpeg_path):
        backend = MockVlmBackend(should_raise=RuntimeError("VLM 崩溃"))
        with pytest.raises(RuntimeError, match="VLM 崩溃"):
            backend.analyze(jpeg_path)

    def test_call_count(self, jpeg_path):
        backend = MockVlmBackend()
        backend.analyze(jpeg_path)
        backend.analyze(jpeg_path)
        assert backend.call_count == 2

    def test_is_backend_subclass(self):
        assert isinstance(MockVlmBackend(), VlmBackend)


# ── VlmModel ──────────────────────────────────────────────────────────────────

class TestVlmModel:
    def test_analyze_success(self, jpeg_path):
        model  = VlmModel(backend=MockVlmBackend())
        output = model.analyze(jpeg_path)
        assert output.scene_type == SceneType.DOC_WRITING

    def test_raises_when_unavailable(self, jpeg_path):
        model = VlmModel(backend=MockVlmBackend(available=False))
        with pytest.raises(RuntimeError, match="不可用"):
            model.analyze(jpeg_path)

    def test_model_name(self):
        model = VlmModel(backend=MockVlmBackend())
        assert model.model_name == "mock-vlm"

    def test_create_default_returns_model(self):
        model = VlmModel.create_default()
        assert isinstance(model, VlmModel)


# ── VlmWorker ────────────────────────────────────────────────────────────────

class TestVlmWorkerSuccess:
    async def test_ok_response(self, jpeg_path):
        model  = VlmModel(backend=MockVlmBackend())
        worker = VlmWorker(model=model)
        req    = _make_vlm_request(jpeg_path)

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.OK
        assert resp.id == req.id
        assert resp.result is not None

    async def test_description_in_result(self, jpeg_path):
        model  = VlmModel(backend=MockVlmBackend())
        worker = VlmWorker(model=model)
        resp   = await worker.handle(_make_vlm_request(jpeg_path))
        assert resp.result.description == "用户正在使用飞书编辑文档"

    async def test_scene_type_in_result(self, jpeg_path):
        model  = VlmModel(backend=MockVlmBackend())
        worker = VlmWorker(model=model)
        resp   = await worker.handle(_make_vlm_request(jpeg_path))
        assert resp.result.scene_type.value == "doc_writing"

    async def test_tags_in_result(self, jpeg_path):
        model  = VlmModel(backend=MockVlmBackend())
        worker = VlmWorker(model=model)
        resp   = await worker.handle(_make_vlm_request(jpeg_path))
        assert "飞书" in resp.result.tags

    async def test_latency_recorded(self, jpeg_path):
        model  = VlmModel(backend=MockVlmBackend())
        worker = VlmWorker(model=model)
        resp   = await worker.handle(_make_vlm_request(jpeg_path))
        assert resp.latency_ms >= 0


class TestVlmWorkerErrors:
    async def test_file_not_found(self, tmp_path):
        model  = VlmModel(backend=MockVlmBackend())
        worker = VlmWorker(model=model)
        req    = _make_vlm_request(str(tmp_path / "ghost.jpg"))

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.ERROR
        assert "FILE_NOT_FOUND" in (resp.error or "")

    async def test_backend_unavailable(self, jpeg_path):
        model  = VlmModel(backend=MockVlmBackend(available=False))
        worker = VlmWorker(model=model)
        req    = _make_vlm_request(jpeg_path)

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.ERROR
        assert "VLM_FAILED" in (resp.error or "")

    async def test_runtime_error(self, jpeg_path):
        model  = VlmModel(backend=MockVlmBackend(
            available=True, should_raise=RuntimeError("模型推理失败")
        ))
        worker = VlmWorker(model=model)
        req    = _make_vlm_request(jpeg_path)

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.ERROR

    async def test_error_has_id(self, tmp_path):
        model  = VlmModel(backend=MockVlmBackend())
        worker = VlmWorker(model=model)
        req    = _make_vlm_request(str(tmp_path / "ghost.jpg"))
        resp   = await worker.handle(req)
        assert resp.id == req.id


# ── WhisperBackend 接口测试 ───────────────────────────────────────────────────

class TestWhisperBackend:
    def test_model_name(self):
        backend = WhisperBackend(model_name="base")
        assert "whisper" in backend.model_name.lower()
        assert "base"    in backend.model_name.lower()

    def test_lazy_load(self):
        backend = WhisperBackend()
        assert backend._model is None

    def test_is_available_returns_bool(self):
        backend = WhisperBackend()
        assert isinstance(backend.is_available(), bool)

    def test_run_raises_when_not_installed(self, wav_path):
        try:
            import pywhispercpp  # type: ignore  # noqa: F401
            pytest.skip("pywhispercpp 已安装，跳过此测试")
        except ImportError:
            pass

        backend = WhisperBackend()
        with pytest.raises(RuntimeError, match="pywhispercpp"):
            backend.transcribe(wav_path)


# ── MiniCpmVBackend 接口测试 ──────────────────────────────────────────────────

class TestMiniCpmVBackend:
    def test_model_name(self):
        backend = MiniCpmVBackend()
        assert "MiniCPM" in backend.model_name or "minicpm" in backend.model_name.lower()

    def test_lazy_load(self):
        backend = MiniCpmVBackend()
        assert backend._model is None

    def test_is_available_returns_bool(self):
        backend = MiniCpmVBackend()
        assert isinstance(backend.is_available(), bool)

    def test_parse_response_json(self):
        """_parse_response 正确解析 JSON 输出"""
        text = '{"description":"正在使用飞书","scene":"IM聊天","tags":["飞书","聊天"]}'
        output = MiniCpmVBackend._parse_response(text)
        assert output.description == "正在使用飞书"
        assert output.scene_type  == SceneType.IM_CHAT
        assert "飞书" in output.tags

    def test_parse_response_fallback(self):
        """JSON 解析失败时返回原始文本"""
        text   = "这不是 JSON"
        output = MiniCpmVBackend._parse_response(text)
        assert output.description == "这不是 JSON"
        assert output.scene_type  == SceneType.UNKNOWN
