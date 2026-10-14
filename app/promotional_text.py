"""Choose complete dramatic passages without exposing paid chapters."""

import re


KEYWORDS = re.compile(r"систем\w*|ранг\w*|уров\w*|смерт\w*|кров\w*|двер\w*|таймер\w*", re.I)


def complete_excerpt(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    result = ""
    for sentence in sentences:
        candidate = (result + " " + sentence).strip()
        if len(candidate) > limit:
            break
        result = candidate
    if result:
        return result
    # An exceptionally long sentence still ends at a word boundary.
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"


def dramatic_excerpt(text: str, limit: int = 300) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return "Новый мир. Неизвестная Система. Один шанс изменить судьбу."
    russian = [p for p in paragraphs if re.search(r"[А-Яа-яЁё]", p)
               and len(re.findall(r"[A-Za-z]{3,}", p)) < 3]
    if russian:
        paragraphs = russian
    def score(item):
        index, paragraph = item
        visible = complete_excerpt(paragraph, limit)
        danger = len(re.findall(r"смерт\w*|кров\w*|двер\w*|таймер\w*", visible, re.I))
        return (min(3, len(KEYWORDS.findall(visible))) * 2 + danger * 4
                + (6 if "«" in visible or "—" in visible else 0)
                + (3 if "?" in visible or "!" in visible else 0)
                - (6 if len(visible.split()) < 4 else 0), index)
    _, chosen = max(enumerate(paragraphs), key=score)
    return complete_excerpt(chosen, limit)
