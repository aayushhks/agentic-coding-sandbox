import struct

from fleet.docker import _demux


def test_a_multiplexed_log_stream_is_joined_back_into_text() -> None:
    def frame(stream: int, text: bytes) -> bytes:
        return struct.pack(">BxxxI", stream, len(text)) + text

    raw = frame(1, b"out one\n") + frame(2, b"err\n") + frame(1, b"out two\n")
    assert _demux(raw) == "out one\nerr\nout two\n"
    # a truncated trailing frame is dropped rather than misread
    assert _demux(raw + b"\x01\x00\x00") == "out one\nerr\nout two\n"
    # or split by stream: 1 is stdout, 2 is stderr
    assert (_demux(raw, (1,)), _demux(raw, (2,))) == ("out one\nout two\n", "err\n")
