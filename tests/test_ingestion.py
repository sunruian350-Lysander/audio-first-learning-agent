"""Source positions refer to the imported material, not normalized offsets."""

from audio_learning.ingestion import text_chunks


def test_leading_and_trailing_blank_lines_do_not_shift_source_positions():
    chunks = text_chunks("\n\n# 第一章\n\n第一句。\n第二句。\n\n# 第二章\n\n最后一句。\n\n")
    assert [chunk.locator for chunk in chunks] == ["原文 · 行 5–6", "原文 · 行 10–10"]
    assert [chunk.text for chunk in chunks] == ["第一句。\n第二句。", "最后一句。"]


def test_windows_and_legacy_mac_line_endings_keep_the_same_positions():
    source = "\n# 标题\n\n正文。\n"
    for ending in ("\n", "\r\n", "\r"):
        chunk = text_chunks(source.replace("\n", ending))[0]
        assert chunk.locator == "原文 · 行 4–4"
        assert chunk.text == "正文。"


def test_dense_lines_are_bounded_including_newline_separators():
    source = "\n".join("例。" for _ in range(1000))
    chunks = text_chunks(source)
    assert len(chunks) > 1
    assert all(len(chunk.text) <= 1800 for chunk in chunks)
    assert chunks[0].locator == "原文 · 行 1–600"
    assert chunks[1].locator == "原文 · 行 601–1000"


def test_long_line_fragments_keep_original_line_number():
    chunks = text_chunks("\n# 标题\n" + "甲" * 4000)
    assert [len(chunk.text) for chunk in chunks] == [1800, 1800, 400]
    assert all(chunk.locator == "原文 · 行 3–3" for chunk in chunks)
