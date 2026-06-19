from papercompass.text import normalize_title, parse_year, title_tokens


def test_normalize_title_keeps_chinese_and_ascii_words() -> None:
    assert normalize_title("Rethinking: 中文 Spelling-Correction!") == "rethinking 中文 spelling correction"


def test_parse_year() -> None:
    assert parse_year("Published 2024-01-01") == 2024
    assert parse_year("") is None


def test_title_tokens_drop_stopwords() -> None:
    assert "spelling" in title_tokens("A Method for Chinese Spelling Correction")
    assert "for" not in title_tokens("A Method for Chinese Spelling Correction")
