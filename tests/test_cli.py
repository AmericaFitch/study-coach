import sys
from pathlib import Path

from study_coach.cli import server_command


def test_server_command_is_an_absolute_path_next_to_python(monkeypatch, tmp_path):
    # Simulate a virtualenv that isn't activated: nothing on PATH.
    monkeypatch.setenv("PATH", "")
    name = "study-coach.exe" if sys.platform == "win32" else "study-coach"
    fake_python = tmp_path / "bin" / "python"
    fake_python.parent.mkdir()
    (fake_python.parent / name).write_text("")
    monkeypatch.setattr(sys, "executable", str(fake_python))

    assert server_command() == str(fake_python.parent / name)
    assert Path(server_command()).is_absolute()
