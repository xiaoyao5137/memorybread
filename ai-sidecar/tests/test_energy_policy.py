import sqlite3
from types import SimpleNamespace

from energy_policy import EnergyPolicy


def _init_preferences(db_path: str, enabled: str = "true") -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE user_preferences (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO user_preferences (key, value) VALUES (?, ?)",
        ("performance.energy_saving_mode", enabled),
    )
    conn.commit()
    conn.close()


def _battery(percent: float, plugged: bool):
    return SimpleNamespace(percent=percent, power_plugged=plugged)


def test_charging_profile_matches_model_parallelism(tmp_path):
    db_path = str(tmp_path / "memory-bread.db")
    _init_preferences(db_path)
    policy = EnergyPolicy(
        db_path,
        battery_provider=lambda: _battery(72, True),
    )

    profile = policy.current_profile(
        base_timeline_interval_secs=30,
        base_timeline_batch_size=20,
    )

    assert profile.mode == "charging"
    assert profile.allow_background_extraction is True
    assert profile.allow_diary is True
    assert profile.timeline_interval_secs == 30
    assert profile.timeline_batch_size == 20
    assert profile.bake_interval_secs == 30
    assert profile.bake_limit == 10
    assert profile.bake_concurrency == 3


def test_charging_profile_allows_explicit_model_parallelism(tmp_path):
    db_path = str(tmp_path / "memory-bread.db")
    _init_preferences(db_path)
    policy = EnergyPolicy(
        db_path,
        battery_provider=lambda: _battery(72, True),
        model_parallelism=2,
    )

    assert policy.current_profile().bake_concurrency == 2


def test_battery_profile_reduces_frequency_batch_and_bake_concurrency(tmp_path):
    db_path = str(tmp_path / "memory-bread.db")
    _init_preferences(db_path)
    policy = EnergyPolicy(
        db_path,
        battery_provider=lambda: _battery(65, False),
    )

    profile = policy.current_profile(
        base_timeline_interval_secs=30,
        base_timeline_batch_size=20,
    )

    assert profile.mode == "battery"
    assert profile.allow_background_extraction is True
    assert profile.allow_diary is False
    assert profile.timeline_interval_secs == 120
    assert profile.timeline_batch_size == 4
    assert profile.bake_interval_secs == 1800
    assert profile.bake_limit == 1
    assert profile.bake_concurrency == 1


def test_critical_battery_profile_pauses_both_background_stages(tmp_path):
    db_path = str(tmp_path / "memory-bread.db")
    _init_preferences(db_path)
    policy = EnergyPolicy(
        db_path,
        battery_provider=lambda: _battery(20, False),
    )

    profile = policy.current_profile()

    assert profile.mode == "critical_battery"
    assert profile.allow_background_extraction is False
    assert profile.allow_diary is False
    assert profile.timeline_batch_size == 0
    assert profile.bake_limit == 0
    assert profile.bake_concurrency == 0


def test_disabling_energy_saving_restores_unrestricted_behavior_on_battery(tmp_path):
    db_path = str(tmp_path / "memory-bread.db")
    _init_preferences(db_path, enabled="false")
    policy = EnergyPolicy(
        db_path,
        battery_provider=lambda: _battery(10, False),
    )

    profile = policy.current_profile(
        base_timeline_interval_secs=45,
        base_timeline_batch_size=12,
    )

    assert profile.mode == "unrestricted"
    assert profile.allow_background_extraction is True
    assert profile.allow_diary is True
    assert profile.timeline_interval_secs == 45
    assert profile.timeline_batch_size == 12
    assert profile.bake_concurrency == 3


def test_missing_battery_sensor_is_treated_as_external_power(tmp_path):
    db_path = str(tmp_path / "memory-bread.db")
    _init_preferences(db_path)
    policy = EnergyPolicy(db_path, battery_provider=lambda: None)

    profile = policy.current_profile()

    assert profile.mode == "charging"
    assert profile.on_external_power is True
