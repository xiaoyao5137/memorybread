"""Recover launcher identity drift without adopting unrelated listeners."""
from types import SimpleNamespace

import psutil
import pytest

from initialization_manager import InitializationManager


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    manager = InitializationManager(base_dir=tmp_path)
    executable = manager._runtime_root('normal') / 'v-test/runtime/ollama'
    environment = {
        'OLLAMA_HOST': '127.0.0.1:11434',
        'OLLAMA_MODELS': str(manager._models_root('normal')),
    }
    connection = SimpleNamespace(
        status=psutil.CONN_LISTEN,
        laddr=SimpleNamespace(ip='127.0.0.1', port=11434),
    )
    process = SimpleNamespace(
        pid=123,
        info={'exe': str(executable)},
        cmdline=lambda: [str(executable), 'serve'],
        environ=lambda: environment,
        net_connections=lambda kind: [connection],
        is_running=lambda: True,
        create_time=lambda: 123.5,
    )
    monkeypatch.setattr(manager, '_managed_ollama_executable', lambda mode: executable)
    monkeypatch.setattr(psutil, 'process_iter', lambda attrs: [process])
    monkeypatch.setattr(psutil, 'Process', lambda pid: process)
    return manager, executable, environment, connection, process


@pytest.mark.parametrize('stale', [False, True])
def test_initialization_recovers_missing_or_stale_identity(runtime, monkeypatch, stale):
    manager, executable, _, _, _ = runtime
    if stale:
        manager._write_process_marker('normal', 'ollama', 123, executable, {
            'create_time': 1.0, 'port': 11434,
            'models_root': str(manager._models_root('normal')),
        })
    assert not manager._managed_ollama_marker_valid('normal')
    monkeypatch.setattr(manager, '_ollama_healthy', lambda url: True)
    monkeypatch.setattr(manager, '_ollama_gui_running', lambda: False)
    monkeypatch.setattr(manager, '_start_ollama', lambda *args: pytest.fail('healthy runtime restarted'))
    skipped, _ = manager._stage_inference_engine('normal', manager._new_state('normal'))
    assert skipped
    assert manager._managed_ollama_marker_valid('normal')


@pytest.mark.parametrize('mismatch', ['exe', 'models', 'host', 'port', 'address', 'not_listening', 'command', 'dead', 'denied'])
def test_identity_recovery_rejects_unverified_process(runtime, mismatch):
    manager, _, environment, connection, process = runtime
    if mismatch == 'exe':
        process.info['exe'] = '/usr/local/bin/ollama'
    elif mismatch == 'models':
        environment['OLLAMA_MODELS'] = '/tmp/other-models'
    elif mismatch == 'host':
        environment['OLLAMA_HOST'] = '127.0.0.1:11435'
    elif mismatch == 'port':
        connection.laddr.port = 11435
    elif mismatch == 'address':
        connection.laddr.ip = '0.0.0.0'
    elif mismatch == 'not_listening':
        connection.status = psutil.CONN_ESTABLISHED
    elif mismatch == 'command':
        process.cmdline = lambda: [process.info['exe'], 'run']
    elif mismatch == 'dead':
        process.is_running = lambda: False
    else:
        def denied():
            raise psutil.AccessDenied(123)
        process.environ = denied
    assert not manager._managed_ollama_process_owned('normal')
    assert not (manager._workspace_root('normal') / 'processes/ollama.json').exists()


def test_identity_recovery_supports_psutil_before_6(runtime):
    manager, _, _, _, process = runtime
    process.connections = process.net_connections
    del process.net_connections
    assert manager._managed_ollama_process_owned('normal')


def test_identity_recovery_does_not_adopt_sandbox_or_race_launcher(runtime, monkeypatch):
    manager, _, _, _, _ = runtime
    monkeypatch.setattr(psutil, 'process_iter', lambda *args: pytest.fail('unexpected discovery'))
    assert not manager._managed_ollama_process_owned('sandbox')
    monkeypatch.setattr(manager, '_external_backend_start_in_progress', lambda: True)
    assert not manager._managed_ollama_process_owned('normal')
