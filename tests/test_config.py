"""Tests for configuration loading and CLI > config > default precedence."""

import textwrap

from ytchannel.config import Config


def test_defaults_without_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # ensure no real config is picked up
    cfg = Config.from_file(str(tmp_path / "nonexistent.toml"))
    assert cfg.output_dir == "./downloads"
    assert cfg.quality == "best"
    assert cfg.delay == 2.0
    assert cfg.audio_only is False


def test_from_file_reads_defaults_table(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / "c.toml"
    p.write_text(
        textwrap.dedent(
            """
            [defaults]
            output_dir = "~/Videos"
            quality = "1080p"
            delay = 5
            write_thumbnail = true
            """
        ),
        encoding="utf-8",
    )
    cfg = Config.from_file(str(p))
    assert cfg.output_dir == "~/Videos"
    assert cfg.quality == "1080p"
    assert cfg.delay == 5.0
    assert cfg.write_thumbnail is True


def test_merge_cli_overrides_only_set_values(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = Config.from_file(str(tmp_path / "x.toml"))
    cfg.merge_cli({"quality": "720", "delay": None, "audio_only": True})
    assert cfg.quality == "720"        # CLI override applied
    assert cfg.audio_only is True      # CLI override applied
    assert cfg.delay == 2.0            # None -> not overridden, default kept
    assert cfg.output_dir == "./downloads"
