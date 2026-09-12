import errno
import json
import sqlite3
import pytest
from database_readiness import DatabaseFailure, classify, validate
from initialization_manager import InitializationManager, InitializationFailure


def database(tmp_path):
    path = tmp_path / 'memory-bread.db'
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE schema_migrations(version TEXT); CREATE TABLE captures(id INTEGER); CREATE TABLE timelines(id INTEGER); CREATE TABLE creation_skills(id INTEGER); INSERT INTO captures VALUES (42); INSERT INTO schema_migrations VALUES ("001_init");')
    return path


def test_committed_main_probe_is_clean_and_preserves_user_rows(tmp_path):
    path = database(tmp_path)
    validate(path, ['001_init'])
    validate(path, ['001_init'])
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT * FROM captures').fetchall() == [(42,)]
        assert conn.execute('SELECT * FROM initialization_write_probe').fetchall() == []


def test_missing_migration_is_detected_even_when_base_tables_exist(tmp_path):
    with pytest.raises(DatabaseFailure) as caught:
        validate(database(tmp_path), ['001_init', '002_new'])
    assert caught.value.code == 'DATABASE_MIGRATION_INCOMPLETE'
    assert caught.value.evidence == {'missing_migration_count': 1}


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / 'missing.db'
    with pytest.raises(DatabaseFailure, match='尚未创建'):
        validate(path)
    assert not path.exists()


def test_read_only_database_cannot_pass_temp_write_probe(monkeypatch, tmp_path):
    path = database(tmp_path)
    original = sqlite3.connect
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: original(path.as_uri() + '?mode=ro', uri=True))
    with pytest.raises(DatabaseFailure) as caught:
        validate(path)
    assert caught.value.code == 'DATABASE_READ_ONLY'


def test_actual_write_lock_is_classified(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as writer:
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('INSERT INTO captures VALUES (43)')
        with pytest.raises(DatabaseFailure) as caught:
            validate(path)
        assert caught.value.code == 'DATABASE_BUSY'
        writer.rollback()
    validate(path)


@pytest.mark.parametrize('error, code', [
    (OSError(errno.ENOSPC, 'private path'), 'DATABASE_DISK_FULL'),
    (PermissionError(errno.EACCES, 'private path'), 'DATABASE_ACCESS_DENIED'),
    (OSError(errno.EROFS, 'private path'), 'DATABASE_READ_ONLY'),
    (sqlite3.DatabaseError('database disk image is malformed'), 'DATABASE_CORRUPT'),
    (sqlite3.OperationalError('disk I/O error'), 'DATABASE_IO_ERROR'),
    (sqlite3.OperationalError('unknown sensitive content'), 'DATABASE_INITIALIZATION_FAILED'),
])
def test_failure_evidence_never_retains_raw_exception(error, code):
    failure = classify(error)
    assert failure.code == code
    assert str(error) not in json.dumps(failure.evidence)


def test_deterministic_failure_never_restarts_core(monkeypatch, tmp_path):
    manager = InitializationManager(tmp_path)
    monkeypatch.setattr(manager, '_core_healthy', lambda: True)
    monkeypatch.setattr(manager, '_request_core_repair_and_wait', lambda *a: pytest.fail('must not restart'))
    path = tmp_path / 'memory-bread.db'
    path.write_bytes(b'preserve this damaged database')
    with pytest.raises(InitializationFailure) as caught:
        manager._stage_database('normal', manager._new_state('normal'))
    assert caught.value.code == 'DATABASE_CORRUPT'
    assert path.read_bytes() == b'preserve this damaged database'


def test_busy_retry_recovers_without_restart(monkeypatch, tmp_path):
    manager = InitializationManager(tmp_path)
    state = manager._new_state('normal')
    manager._save_state(state)
    monkeypatch.setattr(manager, '_core_healthy', lambda: True)
    monkeypatch.setattr(manager, '_request_core_repair_and_wait', lambda *a: pytest.fail('must not restart'))
    calls = []
    def check(path):
        calls.append(path)
        if len(calls) < 3:
            raise InitializationFailure('DATABASE_BUSY', 'busy')
    monkeypatch.setattr(manager, '_validate_database', check)
    monkeypatch.setattr('initialization_manager.time.sleep', lambda _: None)
    manager._stage_database('normal', state)
    assert len(calls) == 3
    assert manager._load_state('normal')['recovery']['status'] == 'succeeded'


def test_slow_migration_timeout_never_restarts(monkeypatch, tmp_path):
    manager = InitializationManager(tmp_path)
    monkeypatch.setattr(manager, '_core_healthy', lambda: False)
    monkeypatch.setattr(manager, '_wait_for_core_health', lambda _: False)
    monkeypatch.setattr(manager, '_database_startup_status', lambda: {'phase': 'migrating'})
    monkeypatch.setattr('initialization_manager.DATABASE_STARTUP_WAIT_SECONDS', 0)
    monkeypatch.setattr(manager, '_request_core_repair_and_wait', lambda *a: pytest.fail('must not restart'))
    with pytest.raises(InitializationFailure) as caught:
        manager._ensure_normal_core_ready(manager._new_state('normal'))
    assert caught.value.code == 'DATABASE_MIGRATION_TIMEOUT'


def test_core_path_mismatch_never_creates_database(tmp_path):
    manager = InitializationManager(tmp_path)
    manager._core_database_metadata = {'path': str(tmp_path / 'other.db'), 'required_migrations': ['001_init']}
    with pytest.raises(InitializationFailure) as caught:
        manager._validate_database(tmp_path / 'memory-bread.db')
    assert caught.value.code == 'DATABASE_PATH_MISMATCH'
    assert not (tmp_path / 'memory-bread.db').exists()


def test_migration_completes_while_waiting(monkeypatch, tmp_path):
    manager = InitializationManager(tmp_path)
    manager._save_state(manager._new_state('normal'))
    states = iter([{'phase': 'migrating'}, {'phase': 'ready'}])
    monkeypatch.setattr(manager, '_database_startup_status', lambda: next(states))
    monkeypatch.setattr(manager, '_core_healthy', lambda: False)
    monkeypatch.setattr('initialization_manager.time.sleep', lambda _: None)
    manager._wait_for_database_startup()


def test_failed_migration_preserves_safe_version_evidence(monkeypatch, tmp_path):
    manager = InitializationManager(tmp_path)
    monkeypatch.setattr(manager, '_database_startup_status', lambda: {
        'phase': 'failed', 'error_code': 'DATABASE_MIGRATION_FAILED', 'migration': '002_new'})
    with pytest.raises(InitializationFailure) as caught:
        manager._wait_for_database_startup()
    assert caught.value.evidence == {'migration': '002_new'}


def test_reused_pid_is_not_considered_current_startup(monkeypatch, tmp_path):
    import time
    manager = InitializationManager(tmp_path)
    path = tmp_path / 'state' / 'database-startup.json'
    path.parent.mkdir()
    started = time.time() - 30
    path.write_text(json.dumps({'pid': 12, 'started_at': started, 'phase': 'migrating'}))
    class ReusedProcess:
        def create_time(self):
            return started + 10
    monkeypatch.setattr('initialization_manager.psutil.Process', lambda _: ReusedProcess())
    assert manager._database_startup_status() is None


def test_database_failure_report_contains_safe_evidence_and_duration(monkeypatch, tmp_path):
    import time
    manager = InitializationManager(tmp_path)
    from tests.test_initialization_manager import _stub_successful_stages, _wait_for_terminal
    _stub_successful_stages(monkeypatch, manager)
    def fail(*args):
        error = InitializationFailure('DATABASE_IO_ERROR', '记忆库读写失败')
        error.evidence = {'exception_type': 'OperationalError', 'sqlite_errorcode': 10}
        raise error
    monkeypatch.setattr(manager, '_stage_database', fail)
    manager.start('normal')
    state = _wait_for_terminal(manager)
    report = manager.get_report_bundle()
    assert state['error_code'] == 'DATABASE_IO_ERROR'
    assert 'sqlite_errorcode=10' in report['summary']
    check = next(item for item in report['checks'] if item['id'] == 'database')
    assert check['duration_ms'] is not None
    assert str(tmp_path) not in json.dumps(report)


def test_passive_status_check_does_not_require_write_lock(tmp_path):
    path = database(tmp_path)
    with sqlite3.connect(path) as writer:
        writer.execute('BEGIN IMMEDIATE')
        validate(path, ['001_init'], check_write=False)
        writer.rollback()
    with sqlite3.connect(path) as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='initialization_write_probe'").fetchall()


def test_lightweight_probe_skips_page_scan_but_keeps_schema_checks(tmp_path, monkeypatch):
    path = database(tmp_path)
    connect = sqlite3.connect
    scans = []
    def observed_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(lambda sql: scans.append(sql) if 'quick_check' in sql.lower() else None)
        return conn
    monkeypatch.setattr(sqlite3, 'connect', observed_connect)
    validate(path, ['001_init'], check_write=False, check_integrity=False)
    assert not scans
    with pytest.raises(DatabaseFailure) as caught:
        validate(path, ['missing_migration'], check_write=False, check_integrity=False)
    assert caught.value.code == 'DATABASE_MIGRATION_INCOMPLETE'
    validate(path)
    assert len(scans) == 1


def test_lightweight_probe_still_rejects_corrupt_database(tmp_path):
    path = tmp_path / 'broken.db'
    path.write_bytes(b'not a sqlite database' * 100)
    with pytest.raises(DatabaseFailure) as caught:
        validate(path, check_write=False, check_integrity=False)
    assert caught.value.code == 'DATABASE_CORRUPT'
