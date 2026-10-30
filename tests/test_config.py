import stat
import sys
from datetime import time

import pytest

from study_coach.config import Blackout, Config, load_config, save_config


def test_round_trip_and_hhmm_format(tmp_path):
    path = tmp_path / "config.yaml"
    cfg = Config(calendar_feeds=["https://example.com/secret.ics"])
    cfg.schedule.blackouts.append(Blackout(days=["tue"], start=time(17), end=time(21), label="work"))
    save_config(cfg, path)

    assert "'17:00'" in path.read_text()
    loaded = load_config(path)
    assert loaded == cfg


@pytest.mark.skipif(sys.platform == "win32", reason="Windows uses ACLs, not Unix file modes")
def test_config_file_is_private(tmp_path):
    path = tmp_path / "nested" / "config.yaml"
    save_config(Config(), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_missing_file_gives_defaults(tmp_path):
    assert load_config(tmp_path / "nope.yaml") == Config()
