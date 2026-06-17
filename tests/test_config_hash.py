from hl_bot.config_hash import hash_config, normalize_config


def test_hash_is_stable_across_key_order():
    a = {"x": 1, "y": 2, "z": {" nested_a": 1, "nested_b": 2}}
    b = {"z": {"nested_b": 2, " nested_a": 1}, "y": 2, "x": 1}
    assert hash_config(a) == hash_config(b)


def test_hash_differs_when_values_differ():
    assert hash_config({"x": 1}) != hash_config({"x": 2})


def test_float_rounding_stability():
    a = {"threshold": 0.1 + 0.2}
    b = {"threshold": 0.30000000000000004}
    assert hash_config(a) == hash_config(b)


def test_nested_list_ordering():
    a = {"levels": [3, 1, 2]}
    b = {"levels": [1, 3, 2]}
    assert hash_config(a) != hash_config(b)


def test_normalize_config_returns_sorted_dict():
    raw = {"b": 2, "a": {"d": 4, "c": 3}}
    normalized = normalize_config(raw)
    assert list(normalized.keys()) == ["a", "b"]
    assert list(normalized["a"].keys()) == ["c", "d"]


def test_hash_length():
    assert len(hash_config({})) == 16
