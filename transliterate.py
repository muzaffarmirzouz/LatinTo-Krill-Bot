"""
O'zbek tilidagi matnni lotin <-> kirill orasida o'giradigan modul.

Qoidaga asoslangan (tashqi kutubxonasiz). Ikkala yo'nalishda ham
quyidagilarga tegilmaydi:
- URL'lar (https://..., t.me/..., www...), email va domenlar (site.uz)
- @mention'lar va #hashtag'lar
- Telefon raqamlari va boshqa raqamlar
- HTML teglari (<b>, <a href="...">, ...) va HTML entity'lar (&amp; va h.k.)
- <code> va <pre> ichidagi matn
- Mashhur brend nomlari (YouTube, Instagram, TikTok, ...)
- Maqsad tilida allaqachon yozilgan matn
"""

import re

# ============================== LOTIN -> KIRILL ==============================

_APOSTROPHES = "ʻʼ'`´ʹ’‘"
_APOSTROPHES_SET = frozenset(_APOSTROPHES)

_LATIN_TO_CYR_SINGLE = {
    "a": "а", "b": "б", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "ҳ",
    "i": "и", "j": "ж", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о",
    "p": "п", "q": "қ", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в",
    "x": "х", "y": "й", "z": "з",
}

_LAT_DIGRAPH_MAP = {
    "yo": "ё", "yu": "ю", "ya": "я", "ye": "е",
    "sh": "ш", "ch": "ч", "ts": "ц",
}

_OG_LOWER = frozenset(("o", "g"))
_OG_TO_CYR = {"o": "ў", "g": "ғ"}

_A = re.escape(_APOSTROPHES)
# Apostrof so'z ichida yoki faqat o/g dan keyin so'z oxirida ("bog'", "tog'")
# so'zga kiradi. Tirnoq sifatida ishlatilgan apostrof ('salom') kirmaydi.
_LAT_WORD_PATTERN = re.compile(
    r"[A-Za-z]+(?:[" + _A + r"][A-Za-z]+)*(?:(?<=[oOgG])[" + _A + r"])?"
)


def _cased(source_char: str, replacement: str) -> str:
    return replacement.upper() if source_char.isupper() else replacement


def _latin_to_cyr_word(word: str) -> str:
    out = []
    i = 0
    n = len(word)
    while i < n:
        c = word[i]
        lc = c.lower()

        # y + o' -> й + ў  ("yo'q" -> "йўқ", "yo'l" -> "йўл"), "yo" digrafi emas
        if (lc == "y" and i + 2 < n and word[i + 1].lower() == "o"
                and word[i + 2] in _APOSTROPHES_SET):
            out.append(_cased(c, "й"))
            i += 1
            continue

        # o' / g'  ->  ў / ғ
        if lc in _OG_LOWER and i + 1 < n and word[i + 1] in _APOSTROPHES_SET:
            out.append(_cased(c, _OG_TO_CYR[lc]))
            i += 2
            continue

        # sh, ch, ts, yo, yu, ya, ye
        if i + 1 < n:
            cyr2 = _LAT_DIGRAPH_MAP.get(lc + word[i + 1].lower())
            if cyr2 is not None:
                out.append(_cased(c, cyr2))
                i += 2
                continue

        # so'z boshidagi "e" -> "э"
        if lc == "e" and i == 0:
            out.append(_cased(c, "э"))
            i += 1
            continue

        cyr1 = _LATIN_TO_CYR_SINGLE.get(lc)
        if cyr1 is not None:
            out.append(_cased(c, cyr1))
            i += 1
            continue

        # so'z ichida qolgan apostrof ("ta'sir") -> tutuq belgisi
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
    "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh",
    "ё": "yo", "ю": "yu", "я": "ya",
    "ў": "o'", "қ": "q", "ғ": "g'", "ҳ": "h",
}

_CYR_SPECIAL = {"ъ": "'", "ь": ""}

_CYR_VOWELS = set("аоуиўэяюёеы")

_CYR_WORD_PATTERN = re.compile(r"[Ѐ-ӿ]+")


def _cased_multi(word: str, i: int, replacement: str) -> str:
    """Ko'p harfli almashtirish. Qo'shni harf ham katta bo'lsa (ШАХАР,
    ЁҚ) — hammasi katta ('SHAHAR', 'YOQ'), aks holda faqat birinchisi."""
    c = word[i]
    if not c.isupper():
        return replacement
    n = len(word)
    neighbor_upper = (i + 1 < n and word[i + 1].isupper()) or (
        i > 0 and word[i - 1].isupper()
    )
    if neighbor_upper:
        return replacement.upper()
    return replacement[0].upper() + replacement[1:]


def _cyr_to_lat_word(word: str) -> str:
    out = []
    for i, c in enumerate(word):
        lower = c.lower()

        if lower == "е":
            prev = word[i - 1].lower() if i > 0 else None
            if i == 0 or (prev is not None and prev in _CYR_VOWELS):
                out.append(_cased_multi(word, i, "ye"))
            else:
                out.append(_cased(c, "e"))
            continue

        if lower in _CYR_TO_LAT_MULTI:
            out.append(_cased_multi(word, i, _CYR_TO_LAT_MULTI[lower]))
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

_SKIP_PATTERN = re.compile(
    r"("
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"                      # email
    r"|https?://\S+|t\.me/\S+|www\.\S+"                  # URL
    r"|(?<![\w.])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:com|uz|ru|org|net|me|io|info|tv|kz|by)\b(?:/\S*)?"  # domen
    r"|(?i:\b(?:youtube|instagram|tiktok|telegram|facebook|whatsapp|google|payme|click)\b)"  # brendlar
    r"|@[A-Za-z0-9_]+|#[\w']+"
    r"|&[a-zA-Z]+;|&#\d+;"
    r"|\+?\d[\d\-\s()]{3,}\d"
    r")"
)

_TAG_SPLIT = re.compile(r"(<[^>]+>)")
_CODE_OPEN = re.compile(r"<(code|pre)(\s[^>]*)?>", re.I)
_CODE_CLOSE = re.compile(r"</(code|pre)\s*>", re.I)


def _apply_word_transform(text: str, word_pattern: "re.Pattern[str]", word_fn) -> str:
    if not text:
        return text

    def repl(match: "re.Match[str]") -> str:
        return word_fn(match.group(0))

    parts = []
    last = 0
    for m in _SKIP_PATTERN.finditer(text):
        parts.append(word_pattern.sub(repl, text[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(word_pattern.sub(repl, text[last:]))
    return "".join(parts)


def _apply_html_transform(html: str, word_pattern: "re.Pattern[str]", word_fn) -> str:
    if not html:
        return html
    parts = _TAG_SPLIT.split(html)
    code_depth = 0
    for i, part in enumerate(parts):
        if part.startswith("<") and part.endswith(">"):
            if _CODE_OPEN.match(part):
                code_depth += 1
            elif _CODE_CLOSE.match(part):
                code_depth = max(0, code_depth - 1)
            continue
        if code_depth > 0:
            continue
        parts[i] = _apply_word_transform(part, word_pattern, word_fn)
    return "".join(parts)


def latin_to_cyrillic_html(html: str) -> str:
    return _apply_html_transform(html, _LAT_WORD_PATTERN, _latin_to_cyr_word)


def cyrillic_to_latin_html(html: str) -> str:
    return _apply_html_transform(html, _CYR_WORD_PATTERN, _cyr_to_lat_word)


# ============================== YANGI ALIFBO ==============================
# Yangi o'zbek lotin alifbosi: sh -> ş, ch -> ç, o' -> ö, g' -> ğ

_NEW_ALPHABET_OG = {"o": "ö", "g": "ğ"}


def _latin_word_to_new(word: str) -> str:
    """Eski lotin yozuvdagi so'zni yangi alifboga o'giradi (Sh -> Ş, O' -> Ö ...)."""
    out = []
    i = 0
    n = len(word)
    while i < n:
        c = word[i]
        lc = c.lower()
        if lc in _NEW_ALPHABET_OG and i + 1 < n and word[i + 1] in _APOSTROPHES_SET:
            out.append(_cased(c, _NEW_ALPHABET_OG[lc]))
            i += 2
            continue
        if i + 1 < n:
            pair = lc + word[i + 1].lower()
            if pair == "sh":
                out.append(_cased(c, "ş"))
                i += 2
                continue
            if pair == "ch":
                out.append(_cased(c, "ç"))
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _cyr_word_to_new(word: str) -> str:
    return _latin_word_to_new(_cyr_to_lat_word(word))


def new_alphabet_html(html: str) -> str:
    """Lotin (eski yozuv) VA kirill matnni yangi alifboga o'giradi.
    HTML teglari, URL, @mention va hokazolarga tegilmaydi."""
    step1 = _apply_html_transform(html, _CYR_WORD_PATTERN, _cyr_word_to_new)
    return _apply_html_transform(step1, _LAT_WORD_PATTERN, _latin_word_to_new)


TRANSLIT_MODES = {
    "l2c": ("Lotin → Kirill", latin_to_cyrillic_html),
    "c2l": ("Kirill → Lotin", cyrillic_to_latin_html),
    "new": ("Yangi alifbo (ş ç ö ğ)", new_alphabet_html),
    "off": ("O'chirilgan", lambda html: html),
}
# Yangi kanallar uchun standart: post matniga tegmaslik
DEFAULT_TRANSLIT_MODE = "off"


def transliterate(html: str, mode: str) -> str:
    _, fn = TRANSLIT_MODES.get(mode, TRANSLIT_MODES[DEFAULT_TRANSLIT_MODE])
    return fn(html)
