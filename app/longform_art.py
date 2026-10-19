"""Deterministic widescreen fantasy illustration, generated entirely in memory."""
import math
import random
from PIL import Image, ImageDraw, ImageFilter

SIZE = (1280, 720)


def render_longform_art(series_id):
    """Own procedural artwork: no network, cached image files or provider delays."""
    theme = 'garden' if series_id == 3 else 'railgun' if series_id == 5 else 'system'
    accent = {'garden': (70, 248, 135), 'railgun': (33, 206, 255), 'system': (255, 43, 83)}[theme]
    image = Image.new('RGBA', SIZE)
    d = ImageDraw.Draw(image)
    for y in range(720):
        glow = math.exp(-((y-310)/280)**2)
        d.line((0, y, 1280, y), fill=(int(8+30*glow), int(7+16*glow), int(20+37*glow), 255))
    halo = Image.new('RGBA', SIZE)
    h = ImageDraw.Draw(halo)
    h.ellipse((700, 30, 1340, 620), fill=(*accent, 210))
    image = Image.alpha_composite(image, halo.filter(ImageFilter.GaussianBlur(75)))
    d = ImageDraw.Draw(image)
    rng = random.Random(5900+int(series_id or 0))
    # Layered distant towers and angular mountains give a strong art silhouette.
    d.polygon([(620, 550), (790, 310), (930, 520), (1130, 230), (1280, 440), (1280, 720)], fill='#10142b')
    for x in range(750, 1280, 58):
        top = rng.randint(310, 465)
        d.rectangle((x, top, x+36, 590), fill='#090d21')
        d.polygon([(x-3, top), (x+18, top-50), (x+40, top)], fill='#11182b')
        for y in range(top+22, 560, 32):
            d.rectangle((x+12, y, x+18, y+13), fill=(*accent, 180))
    for radius in (168, 186, 213):
        d.ellipse((1030-radius, 300-radius, 1030+radius, 300+radius), outline=(*accent, 200), width=4)
    for i in range(24):
        angle = i*math.tau/24
        x, y = 1030+204*math.cos(angle), 300+204*math.sin(angle)
        d.line((x-7, y-9, x+8, y+10), fill='#ffe46b', width=3)
    if theme == 'garden':
        # Glowing leaves, golden seed rewards and a cultivation vessel.
        d.polygon([(878, 470), (1184, 470), (1140, 543), (920, 543)], fill='#181730', outline='#ffe36c', width=5)
        d.line((1030, 465, 1034, 206), fill='#b7ffbc', width=12)
        for x, y, side in ((1032, 400, -1), (1032, 347, 1), (1032, 286, -1), (1032, 232, 1)):
            d.polygon([(x, y), (x+side*140, y-76), (x+side*119, y-8)],
                      fill='#39de82', outline='#e8ffb2', width=4)
            d.line((x, y, x+side*123, y-54), fill='#f7ffd2', width=3)
        for x, y in ((880, 226), (1180, 275), (1134, 145), (948, 126)):
            d.ellipse((x-19, y-19, x+19, y+19), fill='#ffd326', outline='white', width=3)
            d.line((x-8, y, x+8, y), fill='#9a4e08', width=3)
    elif theme == 'railgun':
        # A heavy barrel/coil assembly and a white-hot magical lightning discharge.
        d.polygon([(817, 387), (1101, 229), (1190, 272), (889, 444)], fill='#192d4b', outline='#92e9ff', width=5)
        for x, y in ((876, 376), (935, 343), (994, 310), (1053, 277)):
            d.line((x-21, y-35, x+27, y+40), fill='#12bffc', width=15)
            d.line((x-20, y-34, x+26, y+39), fill='white', width=4)
        for shift in (-35, 0, 32):
            points = [(1120, 265), (1160, 226+shift), (1150, 206+shift), (1210, 164+shift), (1200, 148+shift), (1279, 103+shift)]
            d.line(points, fill='#0a9eff', width=16)
            d.line(points, fill='white', width=4)
        d.polygon([(856, 408), (915, 431), (947, 515), (896, 524)], fill='#0b1129', outline='#6bdfff', width=3)
    else:
        # Hooded zero-rank protagonist and a luminous broken System warning panel.
        d.polygon([(893, 533), (928, 340), (1011, 290), (1101, 342), (1170, 537)], fill='#0b1023', outline='#78c6ff', width=4)
        d.polygon([(955, 306), (950, 233), (1014, 188), (1076, 237), (1070, 310)], fill='#101425', outline='#c7d9ff', width=4)
        d.line((980, 274, 1046, 271), fill=accent, width=6)
        d.rounded_rectangle((1102, 184, 1250, 347), radius=12, fill='#291020', outline=accent, width=5)
        d.polygon([(1176, 208), (1234, 306), (1118, 306)], fill='#ffcb37', outline='white', width=3)
        d.line((1176, 239, 1176, 272), fill='#40101d', width=8)
        d.ellipse((1171, 284, 1181, 294), fill='#40101d')
    for _ in range(65):
        x, y = rng.randrange(770, 1280), rng.randrange(25, 540)
        d.ellipse((x, y, x+3, y+3), fill='#fff3a1')
    # Protect the left-hand headline while preserving a bright right art focal point.
    shade = Image.new('RGBA', SIZE)
    sd = ImageDraw.Draw(shade)
    for x in range(980):
        alpha = int(220*(1-x/980)**.5)
        sd.line((x, 0, x, 720), fill=(2, 3, 10, alpha))
    sd.rectangle((0, 542, 1280, 720), fill=(3, 6, 16, 245))
    return Image.alpha_composite(image, shade).convert('RGB')
