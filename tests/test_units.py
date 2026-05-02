from knife.utils.units import format_rate, format_size, parse_rate, parse_size


def test_parse_size_bytes():
    assert parse_size("1024") == 1024
    assert parse_size("1KB") == 1024
    assert parse_size("1k") == 1024
    assert parse_size("1MB") == 1024 ** 2
    assert parse_size("1.5G") == int(1.5 * 1024 ** 3)
    assert parse_size("2 GiB") == 2 * 1024 ** 3


def test_parse_rate():
    # 1 MB/s = 1 MiB/s in our convention
    assert parse_rate("1MB/s") == 1024 ** 2
    assert parse_rate("1MB") == 1024 ** 2
    # bits-per-second normalises to bytes
    # 8 Mbps = 1 MB/s
    assert parse_rate("8Mbps") == 1024 ** 2


def test_format_size():
    assert format_size(0) == "0.0 B"
    assert format_size(1024) == "1.0 KB"
    assert format_size(1024 ** 2) == "1.0 MB"


def test_format_rate():
    assert format_rate(1024).endswith("/s")
