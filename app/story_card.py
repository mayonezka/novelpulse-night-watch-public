"""Temporary 9:16 share card for a reader's NovelPulse RPG profile."""

from __future__ import annotations

import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


WIDTH, HEIGHT = 720, 1280
RANK_TITLES = {
    'F': 'Новичок без класса',
    'E': 'Кандидат Системы',
    'D': 'Пробуждённый',
    'C': 'Боец Разлома',
    'B': 'Охотник за артефактами',
    'A': 'Мастер Подземелий',
    'S': 'Владыка Системы',
}


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Choose an installed Cyrillic font on Windows or Linux."""
    if bold:
        candidates = (
            r'C:\Windows\Fonts\arialbd.ttf', r'C:\Windows\Fonts\segoeuib.ttf',
            r'C:\Windows\Fonts\tahomabd.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf',
        )
    else:
        candidates = (
            r'C:\Windows\Fonts\arial.ttf', r'C:\Windows\Fonts\segoeui.ttf',
            r'C:\Windows\Fonts\tahoma.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf',
        )
    for candidate in candidates:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow versions before the size argument was added.
        return ImageFont.load_default()


def _centered(draw: ImageDraw.ImageDraw, text: str, y: int, font, fill, *, stroke_width=0,
              stroke_fill=(0, 0, 0)) -> None:
    bounds = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    x = (WIDTH - (bounds[2] - bounds[0])) // 2
    draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke_width,
              stroke_fill=stroke_fill)


def _draw_background() -> Image.Image:
    image = Image.new('RGB', (WIDTH, HEIGHT))
    pixels = image.load()
    for y in range(HEIGHT):
        t = y / max(1, HEIGHT - 1)
        color = (int(7 + 7 * t), int(13 + 7 * t), int(29 + 14 * t))
        for x in range(WIDTH):
            glow = max(0, 1 - abs(x - WIDTH * .5) / (WIDTH * .8)) * max(0, 1 - abs(y - 500) / 850)
            pixels[x, y] = (min(255, int(color[0] + 5 * glow)),
                            min(255, int(color[1] + 16 * glow)),
                            min(255, int(color[2] + 34 * glow)))
    return image


def render_player_story_card(user_row: dict, series_title: str = '') -> Path:
    """Render a shareable 720x1280 card outside the repository's data directory.

    The caller owns the returned temporary file and must delete it after delivery.
    """
    rank = str(user_row.get('player_rank', 'F')).upper()
    if rank not in RANK_TITLES:
        rank = 'F'
    nickname = ('@' + str(user_row['username'])) if user_row.get('username') else 'Читатель NovelPulse'
    nickname = nickname[:32]
    tg_id = int(user_row.get('tg_id', 0))

    image = _draw_background().convert('RGBA')
    glow = Image.new('RGBA', (WIDTH, HEIGHT), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cyan = (54, 224, 255, 150)
    gold = (255, 194, 73, 180)
    crimson = (255, 73, 107, 155)
    # Layered rune-like framing and a softly glowing central status seal.
    gd.rounded_rectangle((28, 32, WIDTH - 28, HEIGHT - 32), radius=34, outline=cyan, width=5)
    gd.rounded_rectangle((42, 46, WIDTH - 42, HEIGHT - 46), radius=28, outline=gold, width=2)
    gd.ellipse((149, 284, 571, 706), outline=cyan, width=9)
    gd.ellipse((166, 301, 554, 689), outline=gold, width=3)
    for x, y, direction in ((64, 170, 1), (656, 170, -1), (64, 1090, 1), (656, 1090, -1)):
        gd.line((x, y, x + 34 * direction, y - 20, x + 60 * direction, y + 8,
                 x + 86 * direction, y - 24), fill=crimson, width=4)
    image = Image.alpha_composite(image, glow.filter(ImageFilter.GaussianBlur(16)))
    image = Image.alpha_composite(image, glow)
    draw = ImageDraw.Draw(image)

    # Gold energy glyph drawn as a vector so the card remains legible without emoji fonts.
    draw.polygon(((347, 70), (382, 70), (358, 110), (385, 110), (330, 166),
                  (344, 124), (322, 124)), fill=(255, 199, 77, 255))
    _centered(draw, 'СИСТЕМА NOVELPULSE', 172, _font(30, bold=True), (236, 246, 255, 255))
    _centered(draw, 'КАРТОЧКА ИГРОКА', 216, _font(21, bold=True), (92, 222, 255, 255))

    # Rank shield badge.
    draw.polygon(((360, 337), (478, 386), (466, 535), (360, 608),
                  (254, 535), (242, 386)), fill=(16, 30, 55, 238), outline=(255, 199, 77, 255), width=5)
    _centered(draw, f'{rank}-РАНГ', 405, _font(54, bold=True), (255, 221, 127, 255),
              stroke_width=2, stroke_fill=(72, 37, 8, 255))
    _centered(draw, RANK_TITLES[rank], 478, _font(23, bold=True), (235, 244, 255, 255))

    draw.rounded_rectangle((77, 735, WIDTH - 77, 991), radius=28,
                           fill=(9, 20, 39, 232), outline=(72, 197, 235, 230), width=2)
    stats = (
        ('ИГРОК', nickname),
        ('ПРОЧИТАНО ГЛАВ', str(max(0, int(user_row.get('chapters_read_count', 0))))),
        ('СТРИК', f"{max(0, int(user_row.get('streak_days', 0)))}/3 ДН."),
        ('ОПЫТ СИСТЕМЫ', f"{max(0, int(user_row.get('player_xp', 0)))} XP"),
    )
    y = 762
    label_font, value_font = _font(17, bold=True), _font(22, bold=True)
    for label, value in stats:
        draw.text((111, y), label, font=label_font, fill=(119, 190, 214, 255))
        bounds = draw.textbbox((0, 0), value, font=value_font)
        draw.text((WIDTH - 111 - (bounds[2] - bounds[0]), y - 4), value,
                  font=value_font, fill=(249, 250, 255, 255))
        y += 55

    if series_title:
        _centered(draw, str(series_title)[:45], 1025, _font(19, bold=True), (255, 208, 103, 255))
    draw.rounded_rectangle((64, 1082, WIDTH - 64, 1180), radius=28,
                           fill=(20, 47, 73, 250), outline=(75, 223, 255, 255), width=3)
    # Search icon is drawn instead of relying on an emoji font.
    draw.ellipse((91, 1109, 126, 1144), outline=(93, 229, 255, 255), width=5)
    draw.line((120, 1138, 137, 1155), fill=(93, 229, 255, 255), width=6)
    draw.text((151, 1100), 'ПОИСК В TELEGRAM', font=_font(17, bold=True), fill=(130, 219, 241, 255))
    draw.text((151, 1125), '@NovelPulseBot', font=_font(31, bold=True), fill=(255, 255, 255, 255))
    _centered(draw, f't.me/NovelPulseBot?start=ref_{tg_id}', 1201,
              _font(17, bold=True), (255, 205, 96, 255))

    handle = tempfile.NamedTemporaryFile(prefix='novelpulse-story-', suffix='.png', delete=False)
    path = Path(handle.name)
    handle.close()
    try:
        image.convert('RGB').save(path, format='PNG', optimize=True)
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise
