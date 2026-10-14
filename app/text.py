"""Lossless paragraph-aware splitting within Telegram's UTF-16 budget."""


def split_text(text: str, limit: int = 4000) -> list[str]:
    if limit < 2:
        raise ValueError("Text limit must be at least two UTF-16 units")
    chunks = []
    while text:
        size, end = 0, 0
        for character in text:
            width = 2 if ord(character) > 0xFFFF else 1
            if size + width > limit:
                break
            size += width
            end += 1
        if end < len(text):
            # Prefer paragraphs, then lines, then words; don't emit tiny pages.
            for separator in ("\n\n", "\n", " "):
                boundary = text.rfind(separator, end // 2, end)
                if boundary >= 0:
                    end = boundary + len(separator)
                    break
        chunks.append(text[:end])
        text = text[end:]
    return chunks
