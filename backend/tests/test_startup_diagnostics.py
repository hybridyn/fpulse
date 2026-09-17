import pytest

from fpulse.main import _assert_writable_data_dir


def test_assert_writable_data_dir_reports_actionable_failure(monkeypatch, tmp_path):
    target = tmp_path / "blocked"

    def boom(*args, **kwargs):
        raise PermissionError("denied for test")

    monkeypatch.setattr("os.makedirs", boom)

    with pytest.raises(RuntimeError) as exc:
        _assert_writable_data_dir(str(target))

    message = str(exc.value)
    assert "F-Pulse cannot use FPULSE_DATA_DIR" in message
    assert "denied for test" in message
    assert "python -m fpulse doctor" in message


def test_assert_writable_data_dir_accepts_writable_path(tmp_path):
    target = tmp_path / "data"

    _assert_writable_data_dir(str(target))

    assert target.is_dir()
