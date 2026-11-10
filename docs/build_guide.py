"""Vygeneruje jednostránkový A4 návod docs/Navod.pdf (pip install reportlab).

Používá fonty Segoe UI z Windows kvůli české diakritice.
"""
import os
import sys

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = "github.com/KubaLooop/sfgame-giveaway-joiner"
DOWNLOAD = f"https://{REPO}/releases/latest"

FONT_DIRS = [r"C:\Windows\Fonts", "/mnt/c/Windows/Fonts"]


def _font(name: str) -> str:
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    sys.exit(f"Font {name} nenalezen")


pdfmetrics.registerFont(TTFont("Segoe", _font("segoeui.ttf")))
pdfmetrics.registerFont(TTFont("Segoe-Bold", _font("segoeuib.ttf")))
pdfmetrics.registerFont(TTFont("Consolas", _font("consola.ttf")))
pdfmetrics.registerFontFamily("Segoe", normal="Segoe", bold="Segoe-Bold")

PURPLE = colors.HexColor("#6441a5")
DARK = colors.HexColor("#1f1f23")
GREY = colors.HexColor("#5c5c66")
LIGHT = colors.HexColor("#f3f0fa")
WARN_BG = colors.HexColor("#fff6e0")

base = ParagraphStyle("base", fontName="Segoe", fontSize=9.2, leading=12.4, textColor=DARK, alignment=TA_LEFT)
title = ParagraphStyle("title", parent=base, fontName="Segoe-Bold", fontSize=21, leading=25, textColor=PURPLE)
sub = ParagraphStyle("sub", parent=base, fontSize=10.5, leading=14, textColor=GREY)
h2 = ParagraphStyle("h2", parent=base, fontName="Segoe-Bold", fontSize=11.5, leading=15, textColor=PURPLE,
                    spaceBefore=2, spaceAfter=3)
step = ParagraphStyle("step", parent=base, leftIndent=0, spaceAfter=5)
small = ParagraphStyle("small", parent=base, fontSize=8.3, leading=11, textColor=GREY)
caption = ParagraphStyle("cap", parent=small, fontSize=7.8, alignment=1)


def code(t: str) -> str:
    return f'<font name="Consolas" color="#6441a5">{t}</font>'


def b(t: str) -> str:
    return f"<b>{t}</b>"


def num(n: int) -> str:
    return f'<font name="Segoe-Bold" color="#6441a5" size="12">{n}</font>&nbsp;&nbsp;'


STEPS = [
    f"{b('Stáhni appku.')} Otevři {b(DOWNLOAD.replace('https://', ''))} a stáhni soubor "
    f"{code('SfgGiveaway.exe')} (nebo ho dostaneš od kamaráda). Ulož ho třeba do Dokumentů – nic se neinstaluje.",
    f"{b('Spusť ho dvojklikem.')} Windows může ukázat modré okno „Systém Windows ochránil počítač“ → klikni "
    f"{b('Další informace')} → {b('Přesto spustit')}. Je to normální u programů, které nejsou od velké firmy. "
    f"První start trvá pár sekund.",
    f"{b('Přihlas se.')} Klikni {b('Přihlásit přes Twitch')}. Otevře se prohlížeč se stránkou twitch.tv/activate – "
    f"přihlas se svým Twitch účtem, zkontroluj kód z okna appky a dej {b('Povolit')}. Stačí jednou, appka si to pamatuje.",
    f"{b('Vyzkoušej to nanečisto.')} Nech zaškrtnuté {b('Jen sledovat (dry-run)')} a klikni {b('Start')}. "
    f"V záložce {b('Chat')} uvidíš živý chat. Dole postupně klikni {b('1) Simulovat GA')}, počkej ~25 s, "
    f"pak {b('2) Konec přihlášek')} a {b('3) Simulovat výhru')}. V Logu uvidíš, co by appka napsala.",
    f"{b('Ostrý režim.')} Odškrtni {b('Jen sledovat')}. Hotovo – nech appku běžet, když jde stream na "
    f"twitch.tv/sfgameofficial. Zástupce na plochu: pravým tlačítkem na exe → Odeslat → Plocha.",
]

HOW = [
    f"{b('Pozná giveaway')} – když chat začne hromadně spamovat stejné {code('!slovo')}. Na běžné příkazy "
    f"jako {code('!discord')} nereaguje.",
    f"{b('Zapojí se')} – během minuty napíše {code('!slovo')} asi 7×. Když chat přejde na jiné slovo "
    f"(někdo „trolí“), přepne se na nové.",
    f"{b('Hlídá výhru')} – když Nightbot oznámí „Congratulations, tvůjNick! You won the giveaway!“, appka "
    f"do 1–3 s napíše něco jako „NICE!!!“ (máš na to 30 s). Pípne a okno vyskočí dopředu.",
    f"{b('Pamatuje si')} statistiky (celkem i dnes), přihlášení a nastavení i po restartu.",
]

FAQ = [
    (f"Jak poznám, že psaní funguje?",
     f"V záložce Chat napiš zprávu a dej Enter. V Logu se objeví „Twitch zprávu přijal“. "
     f"(Pozor, jde do opravdového chatu – na test si nahoře přepiš Kanál na svůj.)"),
    ("Musím mít otevřený stream?", "Ne, stačí aby appka běžela a byla připojená. Stream klidně nemusíš sledovat."),
    ("Jiný účet / odhlášení?", "Tlačítko Odhlásit vpravo nahoře, pak se přihlas znovu."),
    ("Kde jsou data a logy?", f"Tlačítko Složka s logy (je to {code('%APPDATA%/SfgGiveaway')})."),
    ("Nová verze?", f"Stáhni nový exe ze stejné stránky a přepiš starý. Nastavení i statistiky zůstanou."),
]


def build(out: str) -> None:
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=11 * mm, bottomMargin=9 * mm,
                            title="SFGame Giveaway Joiner – návod", author="KubaLooop")
    W = A4[0] - 28 * mm
    story = [
        Paragraph("SFGame Giveaway Joiner", title),
        Paragraph("Automaticky se zapojí do giveawayí na twitch.tv/sfgameofficial a když tě vylosují, "
                  "sám odpoví v chatu, aby výhra nepropadla.", sub),
        Spacer(1, 5 * mm),
    ]

    left = [Paragraph("Jak začít (5 minut)", h2)]
    left += [Paragraph(num(i) + t, step) for i, t in enumerate(STEPS, 1)]
    img_w = W * 0.47
    shot = Image(os.path.join(HERE, "screenshot.png"), width=img_w, height=img_w * 640 / 900)
    right = [shot, Spacer(1, 1.5 * mm),
             Paragraph("Appka po simulaci: detekce GA → zapojení → konec přihlášek → výhra.", caption)]
    t = Table([[left, right]], colWidths=[W * 0.51, W * 0.49])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("RIGHTPADDING", (0, 0), (0, 0), 5 * mm), ("RIGHTPADDING", (1, 0), (1, 0), 0)]))
    story += [t, Spacer(1, 3 * mm)]

    how = [Paragraph("Co appka dělá", h2)] + [Paragraph("•&nbsp;&nbsp;" + x, step) for x in HOW]
    box = Table([[how]], colWidths=[W])
    box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("BOX", (0, 0), (-1, -1), 0, LIGHT),
                             ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                             ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 1 * mm)]))
    story += [box, Spacer(1, 3 * mm), Paragraph("Časté otázky", h2)]

    rows = [[Paragraph(b(q), base), Paragraph(a, base)] for q, a in FAQ]
    faq = Table(rows, colWidths=[W * 0.30, W * 0.70])
    faq.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2 * mm),
                             ("LINEBELOW", (0, 0), (-1, -2), 0.4, colors.HexColor("#e2e2e8"))]))
    story += [faq, Spacer(1, 3 * mm)]

    warn = Table([[Paragraph(
        f"{b('Upozornění:')} automatické zapojování může být proti pravidlům giveawaye a moderátoři za to můžou "
        f"dát timeout nebo ban. Appka se chová slušně (dodržuje slow mode, píše max ~7× za minutu), ale používáš "
        f"ji na vlastní riziko. Výhru nezaručí – jen zajistí, že jsi v losování a nepropásneš 30 s na odpověď.",
        base)]], colWidths=[W])
    warn.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), WARN_BG),
                              ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                              ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm)]))
    story += [warn, Spacer(1, 2.5 * mm),
              Paragraph(f"Zdrojový kód a nové verze: {b(REPO)}", small)]
    doc.build(story)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "Navod.pdf")
    build(out)
    print("OK:", out)
