"""Video codec choice (fork): the ffmpeg command for H.265 (default) and H.264."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.utils.ffmpeg import encode_command
from app.workers.timelapse_service import EncodingSettings


def _cmd(**kw: str) -> list[str]:
    return encode_command(["-i", "in"], Path("out.mp4"), crf=23, preset="medium", pixel_format="yuv420p10le", **kw)


def test_default_is_h265_as_before() -> None:
    cmd = _cmd()
    assert cmd[cmd.index("-c:v") + 1] == "libx265"
    assert "hvc1" in cmd and "yuv420p10le" in cmd


def test_h264_plays_everywhere() -> None:
    cmd = _cmd(codec="h264")
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-crf") + 1] == "23" and cmd[cmd.index("-preset") + 1] == "medium"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"  # 8-bit 4:2:0 whatever the HEVC setting
    assert "avc1" in cmd and "libx265" not in cmd and cmd[-1] == "out.mp4"


def test_encoding_settings_carry_the_codec() -> None:
    base = dict(frame_rate=24, crf=23, preset="medium", pixel_format="yuv420p", ffmpeg_timeout=600)
    assert EncodingSettings.from_scheduler_settings(SimpleNamespace(**base)).codec == "h265"
    assert EncodingSettings.from_scheduler_settings(SimpleNamespace(**base, video_codec="h264")).codec == "h264"
