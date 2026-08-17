"""
模型管理器 - 管理本地 AI 模型的下载、配置和切换

支持的模型类型：
1. 文本推理模型（LLM）- 用于时间线提炼与 bake 提炼
2. 向量模型（Embedding）- 用于语义搜索
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from enum import Enum

try:
    from runtime_process_guard import enforce_runtime_guards, shutdown_all_managed_serves
except ImportError:  # 测试环境或独立调用时守卫不可用不阻断主流程
    def enforce_runtime_guards(reason: str = ""):
        return {"killed_runners": [], "killed_serves": []}

    def shutdown_all_managed_serves(reason: str = ""):
        return []

logger = logging.getLogger(__name__)

MIN_MACOS_MAJOR_FOR_OLLAMA = 12  # DMG 内嵌 Ollama 支持 macOS 12+
OLLAMA_API_BASE = "http://localhost:11434"
OLLAMA_MACOS_DOWNLOAD_URL = "https://ollama.com/download/mac"
# Ollama 安装/运行状态探测结果缓存窗口，避免模型页一次打开重复触发子进程与 HTTP 探测
SETUP_STATUS_CACHE_TTL_S = 5.0
MODEL_ID_ALIASES = {
    "qwen3.5-4b": "mbem-v1-local",
    "qwen2.5-3b": "mbem-v1-local",
    "qwen2.5-7b": "mbem-v1-local",
    "qwen3.5-7b": "mbem-v1-local",
    "deepseek-r1-7b": "mbem-v1-local",
    "gemma4-e4b": "mbem-v1-local",
    "llama3.2-3b": "mbem-v1-local",
    "gemma2-2b": "mbem-v1-local",
    "bge-m3": "bge-small-zh",
    "bge-small": "bge-small-zh",
    "text-embedding-3-small": "bge-small-zh",
}


class ModelType(str, Enum):
    """模型类型"""
    LLM = "llm"           # 文本推理模型
    EMBEDDING = "embedding"  # 向量模型


class ModelStatus(str, Enum):
    """模型状态"""
    NOT_INSTALLED = "not_installed"  # 未安装
    DOWNLOADING = "downloading"      # 下载中
    INSTALLED = "installed"          # 已安装
    ACTIVE = "active"                # 激活中


@dataclass
class ModelInfo:
    """模型信息"""
    id: str                    # 模型 ID
    name: str                  # 显示名称
    type: ModelType            # 模型类型
    provider: str              # 提供商（ollama/huggingface/openai）
    model_id: str              # 实际模型标识
    size_gb: float             # 模型大小（GB）
    description: str           # 描述
    status: ModelStatus = ModelStatus.NOT_INSTALLED
    is_default: bool = False   # 是否为默认模型
    requires_api_key: bool = False  # 是否需要 API Key


# 预定义的模型列表
AVAILABLE_MODELS = {
    # ========== 文本推理模型（LLM） ==========
    "mbem-v1-local": ModelInfo(
        id="mbem-v1-local",
        name="MBEM v1.0",
        type=ModelType.LLM,
        provider="ollama",
        model_id="qwen3.5:4b",
        size_gb=2.3,
        description="MemoryBread Extract Model Local 1.0，本地提炼模型 v1",
        is_default=True
    ),

    # ========== 向量模型（Embedding） ==========
    "bge-small-zh": ModelInfo(
        id="bge-small-zh",
        name="MBEMB V1.0",
        type=ModelType.EMBEDDING,
        provider="ollama",
        model_id="qllama/bge-small-zh-v1.5:q4_k_m",
        size_gb=0.05,
        description="MemoryBread向量模型",
        is_default=True
    ),
}


class ModelManager:
    """模型管理器"""

    def __init__(self, config_path: Optional[Path] = None):
        if config_path is None:
            config_path = Path.home() / ".memory-bread" / "model_config.json"

        self.config_path = config_path
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

        # 加载配置
        self.config = self._load_config()

        # 下载进度跟踪
        self._download_progress: Dict[str, int] = {}
        self._download_errors: Dict[str, str] = {}
        self._download_lock = threading.Lock()

        # Ollama 升级状态
        self._upgrade_status: Dict[str, str] = {}  # {'status': 'upgrading'/'success'/'error', 'message': '...'}
        self._upgrade_lock = threading.Lock()

        # Ollama 安装状态探测缓存
        self._setup_status_cache: Optional[Dict] = None
        self._setup_status_cache_at = 0.0
        self._setup_status_cache_lock = threading.Lock()

    def _load_config(self) -> Dict:
        """加载模型配置"""
        if self.config_path.exists():
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            active_llm = MODEL_ID_ALIASES.get(config.get('active_llm'), config.get('active_llm', 'mbem-v1-local'))
            active_embedding = MODEL_ID_ALIASES.get(config.get('active_embedding'), config.get('active_embedding', 'bge-small-zh'))
            config['active_llm'] = active_llm if active_llm in AVAILABLE_MODELS else 'mbem-v1-local'
            config['active_embedding'] = active_embedding if active_embedding in AVAILABLE_MODELS else 'bge-small-zh'
            # 推理并发由供电状态自动控制，不再读取或向用户暴露持久化配置。
            config.pop('llm_max_concurrency', None)
            return config

        # 默认配置
        return {
            "active_llm": "mbem-v1-local",
            "active_embedding": "bge-small-zh",
            "active_image": None,
            "api_keys": {}
        }

    def _save_config(self):
        """保存模型配置"""
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)

    def _parse_macos_major_version(self) -> Optional[int]:
        # 优先使用 platform.mac_ver()，但在 PyInstaller 环境下可能返回 10.16
        version = (platform.mac_ver()[0] or "").strip()

        # 如果 platform.mac_ver() 返回空或无效值，使用 sw_vers 命令
        if not version or version.startswith('10.16'):
            try:
                result = subprocess.run(
                    ['sw_vers', '-productVersion'],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if result.returncode == 0:
                    version = result.stdout.strip()
            except Exception:
                pass

        if not version:
            return None
        try:
            return int(version.split('.')[0])
        except Exception:
            return None

    def _resolve_ollama_command(self) -> Optional[str]:
        # MemoryBread 首次初始化安装的是无菜单栏的托管 CLI 运行时，不依赖 Homebrew。
        # 优先复用应用数据目录中的最新已校验版本。
        managed_root = Path.home() / ".memory-bread" / "initialization" / "runtime" / "ollama"
        if managed_root.exists():
            managed_candidates = sorted(
                [
                    *managed_root.glob("v*/runtime/bin/ollama"),
                    *managed_root.glob("v*/runtime/ollama"),
                ],
                reverse=True,
            )
            for candidate in managed_candidates:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)

        # 检查 DMG 打包版本的内嵌 Ollama
        dmg_managed_root = Path.home() / "Library" / "Application Support" / "com.memory-bread.app" / "runtime" / ".memory-bread" / "initialization" / "runtime" / "ollama"
        if dmg_managed_root.exists():
            dmg_candidates = sorted(
                [
                    *dmg_managed_root.glob("v*/runtime/bin/ollama"),
                    *dmg_managed_root.glob("v*/runtime/ollama"),
                ],
                reverse=True,
            )
            for candidate in dmg_candidates:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)

        cmd = shutil.which('ollama')
        if cmd:
            return cmd

        arch = platform.machine().lower()
        candidates = []
        if arch == 'arm64':
            candidates.append('/opt/homebrew/bin/ollama')
            candidates.append('/usr/local/bin/ollama')
        elif arch == 'x86_64':
            candidates.append('/usr/local/bin/ollama')
            candidates.append('/opt/homebrew/bin/ollama')
        else:
            candidates.append('/opt/homebrew/bin/ollama')
            candidates.append('/usr/local/bin/ollama')
        candidates.append('/Applications/Ollama.app/Contents/Resources/ollama')

        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def _is_ollama_running(self, base_url: str = OLLAMA_API_BASE) -> bool:
        url = f"{base_url.rstrip('/')}/api/tags"
        try:
            import urllib.request
            import socket
            req = urllib.request.Request(url, method='GET')
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status == 200
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError):
            return False

    def get_ollama_setup_status(self, force: bool = False) -> Dict:
        """获取 Ollama 安装/运行状态（带短 TTL 缓存）。

        安装、启动、升级等动作之后需要最新结果时传 force=True 绕过缓存。
        """
        now = time.time()
        with self._setup_status_cache_lock:
            if (
                not force
                and self._setup_status_cache is not None
                and now - self._setup_status_cache_at < SETUP_STATUS_CACHE_TTL_S
            ):
                return self._setup_status_cache

        detail = self._probe_ollama_setup_status()
        with self._setup_status_cache_lock:
            self._setup_status_cache = detail
            self._setup_status_cache_at = time.time()
        return detail

    def _probe_ollama_setup_status(self) -> Dict:
        system = platform.system()
        is_macos = system == 'Darwin'
        arch = platform.machine().lower()

        # 使用统一的版本检测逻辑（支持 PyInstaller 环境）
        macos_version = ''
        if is_macos:
            version_from_platform = (platform.mac_ver()[0] or '').strip()
            if version_from_platform and not version_from_platform.startswith('10.16'):
                macos_version = version_from_platform
            else:
                # PyInstaller 环境下 platform.mac_ver() 可能返回 10.16，使用 sw_vers
                try:
                    result = subprocess.run(
                        ['sw_vers', '-productVersion'],
                        capture_output=True,
                        text=True,
                        timeout=2
                    )
                    if result.returncode == 0:
                        macos_version = result.stdout.strip()
                except Exception:
                    pass

        major = self._parse_macos_major_version() if is_macos else None
        version_compatible = True if not is_macos else (major is not None and major >= MIN_MACOS_MAJOR_FOR_OLLAMA)

        ollama_path = self._resolve_ollama_command()
        # Ollama.app 可以正常运行但尚未把 CLI 链接到 PATH；此时 API 可用也应视为已安装。
        running = self._is_ollama_running()
        installed = bool(ollama_path) or running
        brew_path = shutil.which('brew')
        brew_available = bool(brew_path)

        # 获取 Ollama 版本
        ollama_version = None
        if installed:
            try:
                # --version 正常时立即返回，收紧超时避免 CLI 卡住拖慢状态探测
                result = subprocess.run([ollama_path, '--version'], capture_output=True, text=True, timeout=3)
                if result.returncode == 0:
                    # 输出格式: "ollama version is 0.1.23"
                    version_line = result.stdout.strip()
                    if 'version' in version_line:
                        ollama_version = version_line.split()[-1]
            except Exception:
                pass

        if not is_macos:
            message = '当前系统非 macOS，不支持自动安装 Ollama。'
        elif not version_compatible:
            message = f'当前 macOS {macos_version or "unknown"}，建议升级到 {MIN_MACOS_MAJOR_FOR_OLLAMA}+ 后再安装 Ollama。'
        elif installed and running:
            message = 'Ollama 已安装且服务运行中。'
        elif installed:
            message = 'Ollama 已安装，但服务未启动。'
        elif brew_available:
            message = 'Ollama 未安装，可自动执行 brew install ollama。'
        else:
            message = '未检测到 Ollama 和 Homebrew，请先安装 Homebrew。'

        if arch == 'arm64':
            recommended = 'brew install ollama (Homebrew: /opt/homebrew/bin)'
        elif arch == 'x86_64':
            recommended = 'brew install ollama (Homebrew: /usr/local/bin)'
        else:
            recommended = 'brew install ollama'

        can_auto_install = is_macos and version_compatible and (installed or brew_available)

        return {
            'platform': system.lower(),
            'is_macos': is_macos,
            'system_version': macos_version,
            'arch': arch,
            'version_compatible': version_compatible,
            'ollama_path': ollama_path,
            'ollama_installed': installed,
            'ollama_running': running,
            'ollama_version': ollama_version,
            'brew_available': brew_available,
            'brew_path': brew_path,
            'recommended_install_method': recommended,
            'official_download_url': OLLAMA_MACOS_DOWNLOAD_URL,
            'minimum_macos_major': MIN_MACOS_MAJOR_FOR_OLLAMA,
            'can_auto_install': can_auto_install,
            'message': message,
        }

    def install_ollama_auto(self) -> Dict:
        status = self.get_ollama_setup_status()
        if not status['is_macos']:
            return {'status': 'error', 'stage': 'detect', 'message': status['message'], 'detail': status}
        if not status['version_compatible']:
            return {'status': 'error', 'stage': 'detect', 'message': status['message'], 'detail': status}
        if status['ollama_installed']:
            return {'status': 'ok', 'stage': 'install', 'message': 'Ollama 已安装，无需重复安装。', 'detail': status}
        if not status['brew_available']:
            return {
                'status': 'error',
                'stage': 'manual_install',
                'message': '未检测到 Homebrew，请从官方安装页下载 Ollama。',
                'official_download_url': OLLAMA_MACOS_DOWNLOAD_URL,
                'detail': status,
            }

        try:
            result = subprocess.run(
                [status['brew_path'], 'install', 'ollama'],
                capture_output=True,
                text=True,
                timeout=1800,
            )
        except subprocess.TimeoutExpired:
            return {'status': 'error', 'stage': 'install', 'message': '安装 Ollama 超时，请稍后重试。', 'detail': status}
        except Exception as exc:
            return {'status': 'error', 'stage': 'install', 'message': f'安装 Ollama 失败: {exc}', 'detail': status}

        refreshed = self.get_ollama_setup_status(force=True)
        if result.returncode == 0 and refreshed['ollama_installed']:
            return {'status': 'ok', 'stage': 'install', 'message': 'Ollama 安装完成。', 'detail': refreshed}

        err = (result.stderr or result.stdout or '').strip()[-500:]
        return {
            'status': 'error',
            'stage': 'install',
            'message': f"Ollama 安装失败{('：' + err) if err else ''}",
            'detail': refreshed,
        }

    def start_ollama_service(self) -> Dict:
        status = self.get_ollama_setup_status()
        if not status['ollama_installed']:
            return {'status': 'error', 'stage': 'start', 'message': 'Ollama 未安装，无法启动服务。', 'detail': status}
        if status['ollama_running']:
            return {'status': 'ok', 'stage': 'verify', 'message': 'Ollama 服务已在运行。', 'detail': status}

        cmd = status['ollama_path'] or self._resolve_ollama_command()
        if not cmd:
            return {'status': 'error', 'stage': 'start', 'message': '未找到 ollama 可执行文件。', 'detail': status}

        # 启动前清扫：终止历史遗留的孤儿 llama-server，保证新 serve 是唯一的宿主。
        try:
            enforce_runtime_guards(reason="before ollama serve start")
        except Exception as exc:
            logger.warning("启动前进程守卫巡检失败（忽略）: %s", exc)

        serve_env = os.environ.copy()
        # 全局最多驻留 1 个模型 = 最多 1 个 llama-server 子进程。
        serve_env['OLLAMA_MAX_LOADED_MODELS'] = '1'
        try:
            subprocess.Popen(
                [cmd, 'serve'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=serve_env,
            )
        except Exception as exc:
            return {'status': 'error', 'stage': 'start', 'message': f'启动 Ollama 服务失败: {exc}', 'detail': status}

        deadline = time.time() + 8
        while time.time() < deadline:
            if self._is_ollama_running():
                refreshed = self.get_ollama_setup_status(force=True)
                return {'status': 'ok', 'stage': 'verify', 'message': 'Ollama 服务已启动。', 'detail': refreshed}
            time.sleep(0.5)

        refreshed = self.get_ollama_setup_status(force=True)
        return {'status': 'error', 'stage': 'verify', 'message': 'Ollama 服务启动超时，请手动执行 ollama serve。', 'detail': refreshed}

    def upgrade_ollama(self) -> Dict:
        """启动 Ollama 升级任务"""
        status = self.get_ollama_setup_status()
        if not status['is_macos']:
            return {'status': 'error', 'message': '当前系统非 macOS，不支持自动升级。'}
        if not status['brew_available']:
            return {'status': 'error', 'message': '未检测到 Homebrew，无法自动升级。'}

        with self._upgrade_lock:
            if self._upgrade_status.get('status') == 'upgrading':
                return {'status': 'upgrading', 'message': self._upgrade_status.get('message', '升级中...')}
            self._upgrade_status = {'status': 'upgrading', 'message': '准备升级...'}

        threading.Thread(target=self._upgrade_ollama_task, args=(status,), daemon=True).start()
        return {'status': 'upgrading', 'message': '升级任务已启动'}

    def _upgrade_ollama_task(self, status: Dict):
        """后台升级任务"""
        try:
            logger.info("开始升级 Ollama...")

            # 清理手动安装的 ollama 目录
            with self._upgrade_lock:
                self._upgrade_status = {'status': 'upgrading', 'message': '清理旧版本...'}

            import shutil
            opt_dir = '/opt/homebrew/opt/ollama'
            if os.path.exists(opt_dir) and not os.path.islink(opt_dir):
                logger.info(f"删除手动安装的目录: {opt_dir}")
                shutil.rmtree(opt_dir)

            # unlink 可能的旧链接
            subprocess.run([status['brew_path'], 'unlink', 'ollama'], capture_output=True, timeout=30)

            with self._upgrade_lock:
                self._upgrade_status = {'status': 'upgrading', 'message': '正在执行 brew install ollama...'}

            result = subprocess.run(
                [status['brew_path'], 'install', 'ollama'],
                capture_output=True,
                text=True,
                timeout=600,
            )

            if result.returncode == 0:
                # 确保链接创建
                with self._upgrade_lock:
                    self._upgrade_status = {'status': 'upgrading', 'message': '创建符号链接...'}
                subprocess.run([status['brew_path'], 'link', 'ollama'], capture_output=True, timeout=30)

                # 重启 Ollama 服务（后台启动）
                with self._upgrade_lock:
                    self._upgrade_status = {'status': 'upgrading', 'message': '重启 Ollama 服务...'}
                # 禁用 pkill -9：宿主被 SIGKILL 后 llama-server 子进程会变孤儿驻留内存。
                # 改用守卫模块按进程树优雅终止，确保重启后全局最多 1 个 llama-server。
                try:
                    shutdown_all_managed_serves(reason="ollama upgrade restart")
                except Exception as exc:
                    logger.warning("升级前关闭托管 Ollama 失败，回退 pkill: %s", exc)
                    subprocess.run(['pkill', 'ollama'], capture_output=True)
                time.sleep(1)
                # 后台启动 ollama serve
                serve_env = os.environ.copy()
                serve_env['OLLAMA_MAX_LOADED_MODELS'] = '1'
                subprocess.Popen(
                    ['ollama', 'serve'],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=serve_env,
                )
                time.sleep(2)

                refreshed = self.get_ollama_setup_status(force=True)
                with self._upgrade_lock:
                    self._upgrade_status = {
                        'status': 'success',
                        'message': f"升级完成，当前版本: {refreshed.get('ollama_version', 'unknown')}",
                        'version': refreshed.get('ollama_version'),
                    }
            else:
                output = (result.stderr or result.stdout or '').lower()
                if 'already locked' in output or 'process has already locked' in output:
                    with self._upgrade_lock:
                        self._upgrade_status = {'status': 'error', 'message': '有其他 Homebrew 进程正在运行，请稍后重试'}
                else:
                    err = (result.stderr or result.stdout or '').strip()[-300:]
                    with self._upgrade_lock:
                        self._upgrade_status = {'status': 'error', 'message': f'升级失败: {err}'}
        except subprocess.TimeoutExpired:
            with self._upgrade_lock:
                self._upgrade_status = {'status': 'error', 'message': '升级超时（超过10分钟），请检查网络或手动执行 brew install ollama'}
        except Exception as e:
            with self._upgrade_lock:
                self._upgrade_status = {'status': 'error', 'message': f'升级失败: {str(e)}'}

    def get_upgrade_status(self) -> Dict:
        """获取升级状态"""
        with self._upgrade_lock:
            return dict(self._upgrade_status) if self._upgrade_status else {'status': 'idle'}

    def list_models(self, model_type: Optional[ModelType] = None) -> List[Dict]:
        """
        列出所有可用模型

        Args:
            model_type: 筛选模型类型，None 表示全部

        Returns:
            模型信息列表
        """
        models = []

        for model_id, model_info in AVAILABLE_MODELS.items():
            if model_type and model_info.type != model_type:
                continue

            # 检查模型状态
            status = self._check_model_status(model_info)
            model_dict = asdict(model_info)
            model_dict['status'] = status.value
            model_dict['type'] = model_info.type.value

            # 标记是否为当前激活的模型
            if model_info.type == ModelType.LLM:
                model_dict['is_active'] = (model_id == self.config.get('active_llm'))
            elif model_info.type == ModelType.EMBEDDING:
                model_dict['is_active'] = (model_id == self.config.get('active_embedding'))

            models.append(model_dict)

        return models

    def _check_model_status(self, model_info: ModelInfo) -> ModelStatus:
        """检查模型状态"""
        if model_info.provider == "ollama":
            return self._check_ollama_model(model_info.model_id)
        elif model_info.provider == "huggingface":
            return self._check_huggingface_model(model_info.model_id)
        elif model_info.provider == "openai":
            # API 模型不需要下载
            api_key = self.config.get('api_keys', {}).get('openai')
            return ModelStatus.INSTALLED if api_key else ModelStatus.NOT_INSTALLED

        return ModelStatus.NOT_INSTALLED

    def _check_ollama_model(self, model_id: str) -> ModelStatus:
        """检查 Ollama 模型是否已安装"""
        cmd = self._resolve_ollama_command()
        if not cmd:
            return ModelStatus.NOT_INSTALLED

        try:
            result = subprocess.run(
                [cmd, 'list'],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0 and model_id in result.stdout:
                return ModelStatus.INSTALLED
            return ModelStatus.NOT_INSTALLED
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ModelStatus.NOT_INSTALLED

    def _check_huggingface_model(self, model_id: str) -> ModelStatus:
        """检查 HuggingFace 模型是否已下载"""
        # 转换模型 ID 为缓存路径
        cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
        model_dir = cache_dir / f"models--{model_id.replace('/', '--')}"

        if model_dir.exists():
            # 检查是否有 snapshots 目录且不为空
            snapshots_dir = model_dir / "snapshots"
            if snapshots_dir.exists() and list(snapshots_dir.iterdir()):
                return ModelStatus.INSTALLED

        return ModelStatus.NOT_INSTALLED

    def download_model(self, model_id: str) -> Dict:
        """
        下载模型

        Args:
            model_id: 模型 ID

        Returns:
            下载任务信息
        """
        model_id = MODEL_ID_ALIASES.get(model_id, model_id)
        if model_id not in AVAILABLE_MODELS:
            raise ValueError(f"未知的模型 ID: {model_id}")

        model_info = AVAILABLE_MODELS[model_id]

        if model_info.provider == "ollama":
            return self._download_ollama_model(model_info)
        elif model_info.provider == "huggingface":
            return self._download_huggingface_model(model_info)
        elif model_info.provider == "openai":
            return {"status": "success", "message": "API 模型无需下载，请配置 API Key"}

        raise ValueError(f"不支持的模型提供商: {model_info.provider}")

    def _download_ollama_model(self, model_info: ModelInfo) -> Dict:
        """下载 Ollama 模型"""
        try:
            with self._download_lock:
                self._download_progress[model_info.id] = 0
                # 重试时清理上一次下载错误。
                self._download_errors.pop(model_info.id, None)

            def download_thread():
                url = "http://localhost:11434/api/pull"
                data = json.dumps({"name": model_info.model_id}).encode('utf-8')

                try:
                    req = urllib.request.Request(
                        url,
                        data=data,
                        headers={'Content-Type': 'application/json'}
                    )
                    with urllib.request.urlopen(req, timeout=3600) as resp:
                        for line in resp:
                            try:
                                obj = json.loads(line.decode('utf-8'))

                                # 检查错误
                                if 'error' in obj:
                                    error_msg = obj['error']
                                    logger.error(f"下载失败: {error_msg}")
                                    with self._download_lock:
                                        self._download_progress.pop(model_info.id, None)
                                        # 保存错误信息
                                        if "newer version" in error_msg:
                                            self._download_errors[model_info.id] = "需要更新 Ollama 版本"
                                        else:
                                            self._download_errors[model_info.id] = "下载失败"
                                        logger.info(f"已保存错误信息: {model_info.id} -> {self._download_errors[model_info.id]}")
                                    break

                                status = obj.get('status', '')

                                # 计算进度
                                if 'total' in obj and 'completed' in obj:
                                    total = obj['total']
                                    completed = obj['completed']
                                    if total > 0:
                                        progress = int(completed * 100 / total)
                                        with self._download_lock:
                                            self._download_progress[model_info.id] = progress

                                # 检查是否完成
                                if status == 'success':
                                    with self._download_lock:
                                        self._download_progress[model_info.id] = 100
                                    break
                            except json.JSONDecodeError:
                                pass

                    # 下载完成后清理
                    with self._download_lock:
                        if model_info.id in self._download_progress:
                            if self._download_progress[model_info.id] >= 100:
                                self._download_progress.pop(model_info.id, None)

                except Exception as e:
                    logger.error(f"下载失败: {e}")
                    with self._download_lock:
                        self._download_progress.pop(model_info.id, None)
                        self._download_errors[model_info.id] = "下载失败，请检查网络和本地运行环境"

            thread = threading.Thread(target=download_thread, daemon=True)
            thread.start()

            logger.info(f"开始下载 Ollama 模型: {model_info.model_id}")
            return {
                "status": "downloading",
                "message": f"正在后台下载 {model_info.name}，请稍候..."
            }

        except Exception as e:
            return {
                "status": "error",
                "message": f"下载失败: {str(e)}"
            }

    def _download_huggingface_model(self, model_info: ModelInfo) -> Dict:
        """下载 HuggingFace 模型"""
        # HuggingFace 模型会在首次使用时自动下载
        return {
            "status": "pending",
            "message": f"{model_info.name} 将在首次使用时自动下载"
        }

    def set_active_model(self, model_id: str) -> Dict:
        """
        设置激活的模型

        Args:
            model_id: 模型 ID

        Returns:
            操作结果
        """
        model_id = MODEL_ID_ALIASES.get(model_id, model_id)
        if model_id not in AVAILABLE_MODELS:
            raise ValueError(f"未知的模型 ID: {model_id}")

        model_info = AVAILABLE_MODELS[model_id]

        # 检查模型是否已安装
        status = self._check_model_status(model_info)
        if status == ModelStatus.NOT_INSTALLED:
            return {
                "status": "error",
                "message": f"{model_info.name} 尚未安装，请先下载"
            }

        # 设置激活模型
        if model_info.type == ModelType.LLM:
            self.config['active_llm'] = model_id
        elif model_info.type == ModelType.EMBEDDING:
            self.config['active_embedding'] = model_id

        self._save_config()

        logger.info(f"已切换到模型: {model_info.name}")
        return {
            "status": "success",
            "message": f"已切换到 {model_info.name}"
        }

    def set_api_key(self, provider: str, api_key: str) -> Dict:
        """
        设置 API Key

        Args:
            provider: 提供商（openai 等）
            api_key: API Key

        Returns:
            操作结果
        """
        if 'api_keys' not in self.config:
            self.config['api_keys'] = {}

        self.config['api_keys'][provider] = api_key
        self._save_config()

        logger.info(f"已设置 {provider} API Key")
        return {
            "status": "success",
            "message": f"已设置 {provider} API Key"
        }

    def get_active_models(self) -> Dict:
        """获取当前激活的模型"""
        return {
            "llm": self.config.get('active_llm'),
            "embedding": self.config.get('active_embedding'),
            "image": self.config.get('active_image'),
        }

    def get_all_status(self) -> Dict[str, dict]:
        """返回所有模型的运行时状态 {model_id: {status, download_progress, is_active, error}}"""
        result = {}
        active_llm = MODEL_ID_ALIASES.get(self.config.get('active_llm'), self.config.get('active_llm'))
        active_emb = self.config.get('active_embedding')
        active_image = self.config.get('active_image')
        from model_registry import AVAILABLE_MODELS as REGISTRY_MODELS
        all_models = list(AVAILABLE_MODELS.items()) + [
            (meta.id, meta) for meta in REGISTRY_MODELS if meta.id not in AVAILABLE_MODELS
        ]

        # 一次性拉取 Ollama 已装模型清单，避免每个 Ollama 模型都单独请求一次
        ollama_installed_names = None
        if any(info.provider == 'ollama' for _, info in all_models):
            ollama_installed_names = self._fetch_ollama_installed_names()

        for model_id, info in all_models:
            error_msg = None
            # 检查是否正在下载
            with self._download_lock:
                if model_id in self._download_progress:
                    progress = self._download_progress[model_id]
                    if progress >= 100:
                        # 下载完成，清理进度
                        self._download_progress.pop(model_id, None)
                        status = 'installed'
                    else:
                        status = 'downloading'
                elif model_id in self._download_errors:
                    # 有下载错误
                    error_msg = self._download_errors[model_id]
                    status = 'error'
                    progress = 0
                elif self._is_installed(model_id, info, ollama_installed_names):
                    status = 'installed'
                    progress = 100
                else:
                    status = 'not_installed'
                    progress = 0

            is_active = (model_id == active_llm or model_id == active_emb or model_id == active_image)
            if is_active and status == 'installed':
                status = 'active'

            result[model_id] = {
                'status': status,
                'download_progress': progress,
                'is_active': is_active,
            }
            if error_msg:
                result[model_id]['error'] = error_msg

        return result

    def set_config_field(self, model_id: str, field_key: str, value: str) -> None:
        """保存模型的某个配置字段（如 api_key、base_url）"""
        model_id = MODEL_ID_ALIASES.get(model_id, model_id)
        if 'model_configs' not in self.config:
            self.config['model_configs'] = {}
        if model_id not in self.config['model_configs']:
            self.config['model_configs'][model_id] = {}
        self.config['model_configs'][model_id][field_key] = value
        # 同时更新 api_keys 以保持向后兼容
        if field_key == 'api_key':
            if model_id in AVAILABLE_MODELS:
                provider = AVAILABLE_MODELS[model_id].provider
            else:
                from model_registry import get_model as registry_get_model
                meta = registry_get_model(model_id)
                provider = meta.provider if meta else model_id
            if 'api_keys' not in self.config:
                self.config['api_keys'] = {}
            self.config['api_keys'][provider] = value
        self._save_config()

    def validate_api_key(self, model_id: str) -> tuple:
        """
        验证模型的 API Key 是否有效。
        Returns: (ok: bool, message: str)
        """
        model_id = MODEL_ID_ALIASES.get(model_id, model_id)
        info = AVAILABLE_MODELS.get(model_id)
        if info is None:
            from model_registry import get_model as registry_get_model
            info = registry_get_model(model_id)
        if info is None:
            return False, f"未知模型 {model_id}"

        cfg = self.config.get('model_configs', {}).get(model_id, {})
        api_key = cfg.get('api_key') or self.config.get('api_keys', {}).get(info.provider, '')

        if not api_key:
            return False, "未配置 API Key"

        try:
            import urllib.request, json as _json
            provider = info.provider

            if provider == 'openai':
                base_url = cfg.get('base_url', 'https://api.openai.com/v1')
                req = urllib.request.Request(
                    f"{base_url}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                urllib.request.urlopen(req, timeout=5)
                return True, "API Key 有效"

            elif provider == 'anthropic':
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages",
                    data=_json.dumps({"model": "claude-sonnet-4-20250514", "max_tokens": 1,
                                      "messages": [{"role": "user", "content": "hi"}]}).encode(),
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
                return True, "API Key 有效"

            elif provider == 'deepseek':
                req = urllib.request.Request(
                    "https://api.deepseek.com/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                urllib.request.urlopen(req, timeout=5)
                return True, "API Key 有效"

            elif provider in ('tongyi', 'kimi', 'doubao'):
                # 这些 provider 暂时只做格式校验
                if len(api_key) > 10:
                    return True, "API Key 格式正确（未做在线验证）"
                return False, "API Key 格式不正确"

            else:
                return True, "已保存（未做在线验证）"

        except Exception as e:
            err = str(e)
            if '401' in err or 'Unauthorized' in err:
                return False, "API Key 无效或已过期"
            if '403' in err:
                return False, "API Key 权限不足"
            return False, f"验证失败: {err}"

    def _ollama_names_for_model(self, model_id: str) -> list[str]:
        model_id = MODEL_ID_ALIASES.get(model_id, model_id)
        # 优先使用 AVAILABLE_MODELS 中的 model_id
        if model_id in AVAILABLE_MODELS:
            info = AVAILABLE_MODELS[model_id]
            if info.model_id:
                return [info.model_id]

        # 回退到旧的命名规则
        if model_id.startswith('qwen2.5-'):
            return [model_id.replace('qwen2.5-', 'qwen2.5:')]
        if model_id.startswith('qwen3.5-'):
            return [model_id.replace('qwen3.5-', 'qwen3.5:')]
        if model_id.startswith('llama3.2-'):
            return [model_id.replace('llama3.2-', 'llama3.2:')]
        if model_id.startswith('gemma2-'):
            return [model_id.replace('gemma2-', 'gemma2:')]
        if model_id.startswith('gemma4-'):
            return [model_id.replace('gemma4-', 'gemma4:')]
        if model_id.startswith('deepseek-r1-'):
            return [model_id.replace('deepseek-r1-', 'deepseek-r1:')]
        return [model_id]

    def _fetch_ollama_installed_names(self) -> set:
        """拉取 Ollama 已安装模型名集合，服务不可达时返回空集合"""
        try:
            resp = urllib.request.urlopen(f"{OLLAMA_API_BASE}/api/tags", timeout=2)
            data = json.loads(resp.read())
            return {m['name'] for m in data.get('models', [])}
        except Exception:
            return set()

    def _is_installed(self, model_id: str, info, ollama_installed_names: Optional[set] = None) -> bool:
        """检查模型是否已安装；可传入预取的 Ollama 模型清单避免重复请求"""
        model_id = MODEL_ID_ALIASES.get(model_id, model_id)
        if info.provider == 'ollama':
            names = ollama_installed_names
            if names is None:
                names = self._fetch_ollama_installed_names()
            aliases = self._ollama_names_for_model(model_id)
            return any(alias == name or name.startswith(f"{alias}:") for alias in aliases for name in names)
        elif info.provider == 'huggingface':
            hf_dir = Path.home() / '.cache' / 'huggingface' / 'hub'
            return any(hf_dir.glob(f"*{model_id}*"))
        elif info.requires_api_key:
            # 商业模型：有 api_key 配置即视为"已安装"
            cfg = self.config.get('model_configs', {}).get(model_id, {})
            return bool(cfg.get('api_key') or
                        self.config.get('api_keys', {}).get(info.provider, ''))
        return False

    def activate_model(self, model_id: str) -> bool:
        """激活指定模型，供 API server 调用"""
        model_id = MODEL_ID_ALIASES.get(model_id, model_id)
        from model_registry import get_model as registry_get_model
        meta = registry_get_model(model_id)
        if not meta:
            logger.error(f"activate_model: 未知模型 {model_id}")
            return False
        if not self._is_installed(model_id, meta):
            logger.error(f"activate_model: 模型未安装 {model_id}")
            return False
        if meta.category == 'llm':
            self.config['active_llm'] = model_id
        elif meta.category == 'embedding':
            self.config['active_embedding'] = model_id
        elif meta.category == 'image':
            self.config['active_image'] = model_id
        else:
            logger.error(f"activate_model: 不支持激活类型 {meta.category}")
            return False
        self._save_config()
        logger.info(f"已切换激活模型: {model_id}")
        return True

    def delete_model(self, model_id: str) -> bool:
        """删除（卸载）指定 Ollama 模型"""
        model_id = MODEL_ID_ALIASES.get(model_id, model_id)
        from model_registry import get_model as registry_get_model
        meta = registry_get_model(model_id)
        if not meta or meta.provider != 'ollama':
            logger.error(f"delete_model: 不支持删除 {model_id}")
            return False
        aliases = self._ollama_names_for_model(model_id)
        ollama_tag = aliases[0] if aliases else model_id
        cmd = self._resolve_ollama_command()
        if not cmd:
            logger.error("delete_model: 未找到 ollama 可执行文件")
            return False
        try:
            result = subprocess.run(
                [cmd, 'rm', ollama_tag],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                logger.info(f"已删除模型: {ollama_tag}")
                return True
            logger.error(f"删除模型失败: {result.stderr}")
            return False
        except Exception as e:
            logger.error(f"删除模型异常: {e}")
            return False
