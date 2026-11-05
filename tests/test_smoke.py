from wow_api_mcp import greet


def test_greet_default():
    assert greet() == "hello, world"


def test_greet_name():
    assert greet("model") == "hello, model"
