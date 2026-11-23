import pytest

from bitsai.matrix.mentions import extract_question

BOT = "@bitsdev:example.org"
NAME = "Bits AI (dev)"


@pytest.mark.parametrize(
    "content",
    [
        {"body": "Bits AI (dev): why is checkout slow?", "m.mentions": {"user_ids": [BOT]}},
        {
            "body": "Bits AI (dev): why is checkout slow?",
            "formatted_body": f'<a href="https://matrix.to/#/{BOT}">Bits AI (dev)</a>: '
            "why is checkout slow?",
        },
        {"body": "bitsdev: why is checkout slow?"},
        {"body": "@bitsdev, why is checkout slow?"},
        {"body": f"{BOT} why is checkout slow?"},
        {"body": "BITSDEV why is checkout slow?"},
    ],
)
def test_addressed_forms(content):
    assert extract_question(content, BOT, NAME) == "why is checkout slow?"


def test_mention_mid_sentence_keeps_text():
    q = extract_question({"body": "hey team, what does Bits AI (dev) think?",
                          "m.mentions": {"user_ids": [BOT]}}, BOT, NAME)
    assert q == "hey team, what does Bits AI (dev) think?"


@pytest.mark.parametrize(
    "content",
    [
        {"body": "why is checkout slow?"},
        {"body": "bitsdevelopment is a word"},
        {"body": "ask bitsdev later"},
        {"body": "x", "m.mentions": {"user_ids": ["@someone:else"]}},
        {"body": "x", "formatted_body": '<a href="https://matrix.to/#/@other:x">o</a>'},
    ],
)
def test_not_addressed(content):
    assert extract_question(content, BOT, NAME) is None


def test_bare_mention_returns_empty_question():
    assert extract_question({"body": "bitsdev:"}, BOT, NAME) == ""
