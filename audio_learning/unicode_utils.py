"""Normalize malformed Unicode while retaining real mathematical characters.

PDF font extraction and JSON APIs may produce UTF-16 surrogate code units as
Python characters. Valid surrogate pairs represent one supplementary character;
a lone surrogate is not a valid Unicode scalar value and cannot be encoded to
UTF-8. The replacement character marks genuinely unreadable glyphs instead of
inventing mathematical symbols.
"""


def normalize_unicode(text: str) -> str:
    """Repair split supplementary characters and replace isolated surrogates.

    Legitimate BMP mathematics (∫, ∑, √) and supplementary math letters (𝑥)
    are left intact. This operation is idempotent.
    """
    if not any("\ud800" <= char <= "\udfff" for char in text):
        return text
    return text.encode("utf-16-le", errors="surrogatepass").decode(
        "utf-16-le", errors="replace"
    )


def normalize_json_strings(value):
    """Repair strings after JSON decoding, including escaped surrogate values."""
    if isinstance(value, str):
        return normalize_unicode(value)
    if isinstance(value, list):
        return [normalize_json_strings(item) for item in value]
    if isinstance(value, dict):
        return {normalize_unicode(key) if isinstance(key, str) else key:
                normalize_json_strings(item) for key, item in value.items()}
    return value
