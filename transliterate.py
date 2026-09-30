"""
O'zbek tilidagi matnni lotin <-> kirill orasida o'giradigan modul.

Qoidaga asoslangan (tashqi kutubxonasiz), shu sabab Railway'da qo'shimcha
pip paketi kerak emas va ishonchli ishlaydi. Ikkala yo'nalishda ham
quyidagilarga tegilmaydi:
- URL'lar (https://..., t.me/..., www...)
- @mention'lar va #hashtag'lar
- Telefon raqamlari va boshqa raqamlar
- HTML teglari (<b>, <a href="...">, ...) va HTML entity'lar (&amp; va h.k.)
- Maqsad tilida allaqachon yozilgan matn (masalan lotin->kirill yo'nalishida
  allaqachon kirillcha bo'lgan qism, va aksincha)
"""

import re

# ============================== LOTIN -> KIRILL ==============================

# O'zbek tilida o' / g' uchun ishlatiladigan turli apostrof belgilari
# (odamlar turli klaviaturada turlicha yozadi; iPhone/Android klaviaturasi
# oddiy apostrofni ’ yoki ‘ kabi "aylanma" belgiga avtokorrektsiya qiladi —
# shu variantlar ham albatta shu ro'yxatda bo'lishi kerak)
_APOSTROPHES = "ʻʼ'`´ʹ’‘"
# Tez (O(1)) tekshirish uchun set — satr ichida qidirishdan tezroq
_APOSTROPHES_SET = frozenset(_APOSTROPHES)

_LATIN_TO_CYR_SINGLE = {
    "a": "а", "b": "б", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "ҳ",
    "i": "и", "j": "ж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о",
    "p": "п", "q": "қ", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в",
    "x": "х", "y": "й", "z": "з",
}

# Ikki harfli birikmalar — dict sifatida, har bir harf uchun ro'yxatni
# ketma-ket aylanish o'rniga bitta O(1) qidiruv qilish uchun
_LAT_DIGRAPH_MAP = {
    "yo": "ё", "yu": "ю", "ya": "я", "ye": "е",
    "sh": "ш", "ch": "ч", "ts": "ц",
}

_OG_LOWER = frozenset(("o", "g"))
_OG_TO_CYR = {"o": "ў", "g": "ғ"}

# Lotin so'z (harflar va ular orasidagi apostrof, masalan "bo'ylab").
# Oxirgi qismdagi yakka apostrof ham ushlanadi — masalan "bog'", "tog'",
# yoki yolg'iz "G'" kabi so'zlar apostrof bilan tugasa ham to'g'ri ishlansin.
_LAT_WORD_PATTERN = re.compile(
    r"[A-Za-z]+(?:[" + re.escape(_APOSTROPHES) + r"][A-Za-z]+)*"
    r"[" + re.escape(_APOSTROPHES) + r"]?"
)


def _cased(source_char: str, replacement: str) -> str:
    """Bitta harf manbaning katta/kichikligiga qarab moslashtiriladi."""
    return replacement.upper() if source_char.isupper() else replacement


def _cased_multi(source_char: str, replacement: str) -> str:
    """Ko'p harfli almashtirish — faqat birinchi harfi katta/kichik bo'ladi
    (manba bitta harf, natija bir necha harf bo'lgani uchun to'liq katta
    holatni saqlab bo'lmaydi — bu tabiiy cheklov)."""
    if source_char.isupper():
        return replacement[0].upper() + replacement[1:]
    return replacement


def _latin_to_cyr_word(word: str) -> str:
    out = []
    i = 0
    n = len(word)
    while i < n:
        c = word[i]
        lc = c.lower()  # bir marta hisoblab, qayta ishlatamiz

        # o' / g'  ->  ў / ғ
        if lc in _OG_LOWER and i + 1 < n and word[i + 1] in _APOSTROPHES_SET:
            out.append(_cased(c, _OG_TO_CYR[lc]))
            i += 2
            continue

        # sh, ch, ts, yo, yu, ya, ye — bitta O(1) dict qidiruvi
        if i + 1 < n:
            cyr2 = _LAT_DIGRAPH_MAP.get(lc + word[i + 1].lower())
            if cyr2 is not None:
                out.append(_cased(c, cyr2))
                i += 2
                continue

        # so'z boshidagi "e" -> "э" (masalan "elon" -> "элон")
        if lc == "e" and i == 0:
            out.append(_cased(c, "э"))
            i += 1
            continue

        cyr1 = _LATIN_TO_CYR_SINGLE.get(lc)
        if cyr1 is not None:
            out.append(_cased(c, cyr1))
            i += 1
            continue

        # o'/g' bilan band bo'lmagan, ammo so'z ichida qolgan apostrof
        # (masalan "ta'sir", "san'at") -> tutuq belgisi
        if c in _APOSTROPHES_SET:
            out.append("ъ")
            i += 1
            continue

        out.append(c)
        i += 1

    return "".join(out)


# ============================== KIRILL -> LOTIN ==============================

_CYR_TO_LAT_SINGLE = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ж": "j", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x",
    "э": "e", "ы": "i",
}

_CYR_TO_LAT_MULTI = {
    "ц": "ts", "ч": "ch", "ш": "sh",
    "ё": "yo", "ю": "yu", "я": "ya",
    "ў": "o'", "қ": "q", "ғ": "g'", "ҳ": "h",
}

# ъ -> apostrof, ь -> tushirib qoldiriladi (standart o'zbek lotin
# orfografiyasida yumshoq belgiga mos harf yo'q)
_CYR_SPECIAL = {"ъ": "'", "ь": ""}

# "е" harfi qaysi holatda "ye", qaysi holatda faqat "e" bo'lishini aniqlash
# uchun undan oldingi unli harflar ro'yxati (so'z boshida ham "ye" bo'ladi)
_CYR_VOWELS = set("аоуиўэяюёе")

_CYR_WORD_PATTERN = re.compile(r"[Ѐ-ӿ]+")


def _cyr_to_lat_word(word: str) -> str:
    out = []
    n = len(word)
    for i, c in enumerate(word):
        lower = c.lower()

        if lower == "е":
            prev = word[i - 1].lower() if i > 0 else None
            if i == 0 or (prev is not None and prev in _CYR_VOWELS):
                out.append(_cased_multi(c, "ye"))
            else:
                out.append(_cased(c, "e"))
            continue

        if lower in _CYR_TO_LAT_MULTI:
            out.append(_cased_multi(c, _CYR_TO_LAT_MULTI[lower]))
            continue

        if lower in _CYR_SPECIAL:
            out.append(_CYR_SPECIAL[lower])
            continue

        if lower in _CYR_TO_LAT_SINGLE:
            out.append(_cased(c, _CYR_TO_LAT_SINGLE[lower]))
            continue

        out.append(c)

    return "".join(out)


# ============================== UMUMIY QISM ==============================

# Texnik elementlar — bularga tegmaymiz (ikkala yo'nalishda ham)
_SKIP_PATTERN = re.compile(
    r"(https?://\S+|t\.me/\S+|www\.\S+|"
    r"@[A-Za-z0-9_]+|#[\w']+|"
    r"&[a-zA-Z]+;|&#\d+;|"
    r"\+?\d[\d\-\s()]{3,}\d)"
)

_TAG_SPLIT = re.compile(r"(<[^>]+>)")


def _apply_word_transform(text: str, word_pattern: "re.Pattern[str]", word_fn) -> str:
    if not text:
        return text

    def repl(match: "re.Match[str]") -> str:
        return word_fn(match.group(0))

    parts = []
    last = 0
    for m in _SKIP_PATTERN.finditer(text):
        before = text[last:m.start()]
        parts.append(word_pattern.sub(repl, before))
        parts.append(m.group(0))  # texnik element — o'zgarishsiz
        last = m.end()
    parts.append(word_pattern.sub(repl, text[last:]))
    return "".join(parts)


def _apply_html_transform(html: str, word_pattern: "re.Pattern[str]", word_fn) -> str:
    if not html:
        return html
    parts = _TAG_SPLIT.split(html)
    for i, part in enumerate(parts):
        if part.startswith("<") and part.endswith(">"):
            continue  # HTML tegi — o'zgarishsiz qoladi
        parts[i] = _apply_word_transform(part, word_pattern, word_fn)
    return "".join(parts)


def latin_to_cyrillic_html(html: str) -> str:
    """Lotin harflarni kirillga o'giradi (HTML teglariga tegmaydi)."""
    return _apply_html_transform(html, _LAT_WORD_PATTERN, _latin_to_cyr_word)


def cyrillic_to_latin_html(html: str) -> str:
    """Kirill harflarni lotinga o'giradi (HTML teglariga tegmaydi)."""
    return _apply_html_transform(html, _CYR_WORD_PATTERN, _cyr_to_lat_word)


# Rejimlar: bot.py shu kodlar orqali kanal sozlamasiga qarab tanlaydi
TRANSLIT_MODES = {
    "l2c": ("Lotin → Kirill", latin_to_cyrillic_html),
    "c2l": ("Kirill → Lotin", cyrillic_to_latin_html),
    "off": ("O'chirilgan", lambda html: html),
}
DEFAULT_TRANSLIT_MODE = "l2c"


def transliterate(html: str, mode: str) -> str:
    """Berilgan rejimga (l2c / c2l / off) qarab HTML matnni o'giradi."""
    _, fn = TRANSLIT_MODES.get(mode, TRANSLIT_MODES[DEFAULT_TRANSLIT_MODE])
    return fn(html)
