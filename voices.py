"""Kokoro voice catalog shown to people: readable names, language and quality grade."""

# Grades come from Kokoro's VOICES.md (hexgrad/Kokoro-82M). Spanish, Portuguese
# and some other voices have no published grade.
_GRADES = {
    "af_heart": "A", "af_alloy": "C", "af_aoede": "C+", "af_bella": "A-", "af_jessica": "D",
    "af_kore": "C+", "af_nicole": "B-", "af_nova": "C", "af_river": "D", "af_sarah": "C+",
    "af_sky": "C-", "am_adam": "F+", "am_echo": "D", "am_eric": "D", "am_fenrir": "C+",
    "am_liam": "D", "am_michael": "C+", "am_onyx": "D", "am_puck": "C+", "am_santa": "D-",
    "bf_alice": "D", "bf_emma": "B-", "bf_isabella": "C", "bf_lily": "D", "bm_daniel": "D",
    "bm_fable": "C", "bm_george": "C", "bm_lewis": "D+", "jf_alpha": "C+", "jf_gongitsune": "C",
    "jf_nezumi": "C-", "jf_tebukuro": "C", "jm_kumo": "C-", "zf_xiaobei": "D", "zf_xiaoni": "D",
    "zf_xiaoxiao": "D", "zf_xiaoyi": "D", "zm_yunjian": "D", "zm_yunxi": "D", "zm_yunxia": "D",
    "zm_yunyang": "D", "ef_dora": None, "em_alex": None, "em_santa": None, "ff_siwis": "B-",
    "hf_alpha": "C", "hf_beta": "C", "hm_omega": "C", "hm_psi": "C", "if_sara": "C",
    "im_nicola": "C", "pf_dora": None, "pm_alex": None, "pm_santa": None,
}

# Kokoro language code -> label, ISO prefixes used by book metadata.
LANGUAGES = {
    "a": ("English (US)", ("en", "en-us")),
    "b": ("English (UK)", ("en-gb",)),
    "e": ("Spanish", ("es",)),
    "f": ("French", ("fr",)),
    "h": ("Hindi", ("hi",)),
    "i": ("Italian", ("it",)),
    "j": ("Japanese", ("ja",)),
    "p": ("Portuguese (BR)", ("pt",)),
    "z": ("Chinese", ("zh",)),
}

_GRADE_ORDER = ["A", "A-", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F+", "F"]


def voice_ids():
    return list(_GRADES)


def describe(voice_id):
    language, gender = voice_id[0], voice_id[1]
    name = voice_id.split("_", 1)[1].replace("_", " ").title()
    return {
        "id": voice_id,
        "name": name,
        "language": language,
        "language_label": LANGUAGES[language][0],
        "gender": "female" if gender == "f" else "male",
        "grade": _GRADES[voice_id],
    }


def voices_for(language):
    """Voices for one language, best graded first; ungraded voices keep catalog order."""
    voices = [describe(voice) for voice in _GRADES if voice[0] == language]
    rank = {grade: index for index, grade in enumerate(_GRADE_ORDER)}
    return sorted(voices, key=lambda voice: rank.get(voice["grade"], len(rank)))


def default_voice(language):
    voices = voices_for(language)
    return voices[0]["id"] if voices else "af_heart"


def language_from_tag(tag):
    """Map a metadata tag such as 'en-GB' or 'es' to a Kokoro language code."""
    tag = (tag or "").strip().lower().replace("_", "-")
    if not tag:
        return None
    for code, (_, prefixes) in LANGUAGES.items():
        if tag in prefixes:
            return code
    for code, (_, prefixes) in LANGUAGES.items():
        if tag.split("-")[0] in prefixes:
            return code
    return None
