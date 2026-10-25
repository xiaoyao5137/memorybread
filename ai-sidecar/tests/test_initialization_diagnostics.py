import errno
import json
import urllib.error
from types import SimpleNamespace

import pytest

import initialization_diagnostics as diagnostics
from initialization_manager import InitializationFailure, InitializationManager


def records(base):
    path = base / 'logs' / 'initialization-diagnostics.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_journal_preserves_failure_without_exception_content_or_paths(tmp_path):
    state = {'run_id': 'failure-run', 'mode': 'normal', 'state': 'failed',
             'current_stage': 'inference_engine', 'error_code': 'RUNTIME_START_FAILED'}
    try:
        raise urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED,
            'private prompt alice@example.com token=private /Users/alice/secret'))
    except Exception as error:
        diagnostics.write_event(tmp_path, 'run_failed', state, error=error,
            evidence={'port': 11434, 'reason': 'health_timeout', 'response': 'private answer',
                      'migration': '/Users/alice/private.db'})
    result = records(tmp_path)[0]
    assert result['exception'] == {'exception_type': 'URLError',
                                   'cause_type': 'ConnectionRefusedError', 'errno': errno.ECONNREFUSED}
    assert result['diagnostic_id'] == 'failure-run'
    assert result['evidence'] == {'port': 11434, 'reason': 'health_timeout'}
    assert result['frames'][-1]['function'] == 'test_journal_preserves_failure_without_exception_content_or_paths'
    serialized = json.dumps(result)
    for secret in ('alice', 'private prompt', 'private answer', 'token=', '/Users/'):
        assert secret not in serialized


def test_journal_survives_new_writer_and_rotates_with_failure_intact(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, 'MAX_JOURNAL_BYTES', 1000)
    state = {'run_id': 'old-run'}
    diagnostics.write_event(tmp_path, 'run_failed', state)
    diagnostics.write_event(tmp_path, 'run_failed', state)
    diagnostics.write_event(tmp_path, 'run_started', {'run_id': 'new-run', 'previous_run_id': 'old-run'})
    current = records(tmp_path)
    previous = tmp_path / 'logs' / 'initialization-diagnostics.jsonl.1'
    assert previous.exists()
    assert 'old-run' in previous.read_text()
    assert current[-1]['previous_run_id'] == 'old-run'
    assert len(list(previous.parent.iterdir())) == 2


@pytest.mark.parametrize('error,expected', [
    (urllib.error.HTTPError('http://127.0.0.1:11434/api/tags', 503, 'private', {}, None),
     {'exception_type': 'HTTPError', 'http_status': 503}),
    (urllib.error.URLError(TimeoutError('private')), {'exception_type': 'URLError', 'cause_type': 'TimeoutError'}),
], ids=['http-error', 'socket-timeout'])
def test_probe_failure_captures_http_or_socket_cause(tmp_path, monkeypatch, error, expected):
    manager = InitializationManager(tmp_path)
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(manager, '_http_json', fail)
    url = manager._ollama_base_url('normal')
    assert manager._ollama_healthy(url) is False
    for key, value in expected.items():
        assert manager._runtime_probes[url][key] == value
    assert manager._runtime_probes[url]['proxy_bypassed'] is True


def test_early_engine_exit_reports_exit_code_instead_of_waiting_for_timeout(tmp_path, monkeypatch):
    manager = InitializationManager(tmp_path)
    manager._processes['ollama:normal'] = SimpleNamespace(pid=123, poll=lambda: 7)
    monkeypatch.setattr(manager, '_ollama_healthy', lambda _: False)
    monkeypatch.setattr(manager, '_port_in_use', lambda _: False)
    with pytest.raises(InitializationFailure) as caught:
        manager._wait_for_ollama('normal')
    assert caught.value.evidence['reason'] == 'process_exited'
    assert caught.value.evidence['exit_code'] == 7
    assert caught.value.evidence['probe_attempts'] == 1
    assert '意外退出' in str(caught.value)


def test_timeout_keeps_last_probe_evidence(tmp_path, monkeypatch):
    manager = InitializationManager(tmp_path)
    url = manager._ollama_base_url('normal')
    manager._runtime_probes[url] = {'exception_type': 'JSONDecodeError', 'proxy_bypassed': True}
    monkeypatch.setattr(manager, '_port_in_use', lambda _: True)
    with pytest.raises(InitializationFailure) as caught:
        manager._wait_for_ollama('normal', timeout=0)
    assert caught.value.evidence['reason'] == 'health_timeout'
    assert caught.value.evidence['exception_type'] == 'JSONDecodeError'
    assert caught.value.evidence['tcp_reachable'] is True
    assert '等待时间内没有回应' in str(caught.value)


def test_failed_run_shows_curated_message_and_journals_every_attempt(tmp_path, monkeypatch):
    manager = InitializationManager(tmp_path)
    state = manager._new_state('normal')
    state['current_stage'] = 'inference_engine'
    manager._save_state(state)
    monkeypatch.setattr(manager, '_stage_preflight', lambda *_: (True, 'ok'))
    def fail(*_):
        error = InitializationFailure('RUNTIME_START_FAILED', 'raw private server output /Users/alice')
        error.evidence = {'reason': 'process_exited', 'exit_code': 9, 'port': 11434}
        raise error
    monkeypatch.setattr(manager, '_stage_inference_engine', fail)
    monkeypatch.setattr(manager, '_prepare_stage_auto_repair', lambda *_: True)
    manager._run('normal', state['run_id'])
    public = manager.get_status()
    assert public['failure_title'] == '本地 AI 暂时无法使用'
    assert '意外退出' in public['message']
    assert '上报诊断' in public['suggestion']
    assert public['diagnostic_id'] == state['run_id']
    assert 'failure_evidence' not in public
    events = records(tmp_path)
    assert len([item for item in events if item['event'] == 'stage_attempt_failed']) == 2
    assert events[-1]['event'] == 'run_failed'
    assert events[-1]['evidence']['exit_code'] == 9
    assert 'raw private' not in json.dumps(public)
    assert 'exit_code=9' in manager.get_report_bundle()['summary']


def test_sparse_startup_logs_are_flushed_before_process_exit(tmp_path):
    import subprocess
    import sys
    import threading
    import time

    process = subprocess.Popen([sys.executable, '-u', '-c',
        'import sys; print("startup: failed to bind"); sys.stdin.read()'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    path = tmp_path / 'startup.log'
    pump = threading.Thread(target=InitializationManager._pump_bounded_log,
                            args=(process.stdout, path), daemon=True)
    pump.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if path.exists() and 'failed to bind' in path.read_text():
                break
            time.sleep(0.01)
        assert process.poll() is None
        assert 'failed to bind' in path.read_text()
    finally:
        process.terminate()
        process.wait(3)
        process.stdin.close()
        pump.join(3)
