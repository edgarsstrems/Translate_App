"""Audio output languages from the Google Cloud named-voice catalog (2026-10-17).

Keep legacy en/ru/lt codes and configured voices stable. Source STT stays Latvian.
Catalog: https://docs.cloud.google.com/text-to-speech/docs/list-voices-and-types
"""

# code: (display name, Cloud locale, catalog-backed default voice)
OUTPUT_LANGUAGES = {
    'af': ('Afrikaans', 'af-ZA', 'af-ZA-Standard-A'),
    'am': ('Amharic', 'am-ET', 'am-ET-Standard-A'),
    'ar': ('Arabic', 'ar-XA', 'ar-XA-Standard-A'),
    'bn': ('Bangla', 'bn-IN', 'bn-IN-Standard-A'),
    'eu': ('Basque', 'eu-ES', 'eu-ES-Standard-B'),
    'bg': ('Bulgarian', 'bg-BG', 'bg-BG-Standard-B'),
    'yue': ('Cantonese (Hong Kong)', 'yue-HK', 'yue-HK-Standard-A'),
    'ca': ('Catalan', 'ca-ES', 'ca-ES-Standard-B'),
    'zh-CN': ('Chinese (Mandarin, Simplified)', 'cmn-CN', 'cmn-CN-Standard-A'),
    'zh-TW': ('Chinese (Mandarin, Traditional)', 'cmn-TW', 'cmn-TW-Standard-A'),
    'hr': ('Croatian', 'hr-HR', 'hr-HR-Chirp3-HD-Achernar'),
    'cs': ('Czech', 'cs-CZ', 'cs-CZ-Standard-B'),
    'da': ('Danish', 'da-DK', 'da-DK-Standard-F'),
    'nl': ('Dutch', 'nl-NL', 'nl-NL-Standard-F'),
    'nl-BE': ('Dutch (Belgium)', 'nl-BE', 'nl-BE-Standard-C'),
    'en': ('English', 'en-US', 'en-US-Standard-A'),
    'en-AU': ('English (Australia)', 'en-AU', 'en-AU-Standard-A'),
    'en-IN': ('English (India)', 'en-IN', 'en-IN-Standard-A'),
    'en-GB': ('English (UK)', 'en-GB', 'en-GB-Standard-A'),
    'et': ('Estonian', 'et-EE', 'et-EE-Standard-A'),
    'fil': ('Filipino', 'fil-PH', 'fil-PH-Standard-A'),
    'fi': ('Finnish', 'fi-FI', 'fi-FI-Standard-B'),
    'fr': ('French', 'fr-FR', 'fr-FR-Standard-F'),
    'fr-CA': ('French (Canada)', 'fr-CA', 'fr-CA-Standard-A'),
    'gl': ('Galician', 'gl-ES', 'gl-ES-Standard-B'),
    'de': ('German', 'de-DE', 'de-DE-Standard-G'),
    'el': ('Greek', 'el-GR', 'el-GR-Standard-B'),
    'gu': ('Gujarati', 'gu-IN', 'gu-IN-Standard-A'),
    'he': ('Hebrew', 'he-IL', 'he-IL-Standard-A'),
    'hi': ('Hindi', 'hi-IN', 'hi-IN-Standard-A'),
    'hu': ('Hungarian', 'hu-HU', 'hu-HU-Standard-B'),
    'is': ('Icelandic', 'is-IS', 'is-IS-Standard-B'),
    'id': ('Indonesian', 'id-ID', 'id-ID-Standard-A'),
    'it': ('Italian', 'it-IT', 'it-IT-Standard-E'),
    'ja': ('Japanese', 'ja-JP', 'ja-JP-Standard-A'),
    'kn': ('Kannada', 'kn-IN', 'kn-IN-Standard-A'),
    'ko': ('Korean', 'ko-KR', 'ko-KR-Standard-A'),
    'lv': ('Latvian', 'lv-LV', 'lv-LV-Standard-B'),
    'lt': ('Lithuanian', 'lt-LT', 'lt-LT-Standard-B'),
    'ms': ('Malay', 'ms-MY', 'ms-MY-Standard-A'),
    'ml': ('Malayalam', 'ml-IN', 'ml-IN-Standard-A'),
    'mr': ('Marathi', 'mr-IN', 'mr-IN-Standard-A'),
    'nb': ('Norwegian Bokmål', 'nb-NO', 'nb-NO-Standard-F'),
    'pl': ('Polish', 'pl-PL', 'pl-PL-Standard-F'),
    'pt-BR': ('Portuguese (Brazil)', 'pt-BR', 'pt-BR-Standard-A'),
    'pt': ('Portuguese (Portugal)', 'pt-PT', 'pt-PT-Standard-E'),
    'pa': ('Punjabi', 'pa-IN', 'pa-IN-Standard-A'),
    'ro': ('Romanian', 'ro-RO', 'ro-RO-Standard-B'),
    'ru': ('Russian', 'ru-RU', 'ru-RU-Standard-A'),
    'sr': ('Serbian', 'sr-RS', 'sr-RS-Standard-B'),
    'sk': ('Slovak', 'sk-SK', 'sk-SK-Standard-B'),
    'sl': ('Slovenian', 'sl-SI', 'sl-SI-Chirp3-HD-Achernar'),
    'es': ('Spanish', 'es-ES', 'es-ES-Standard-E'),
    'es-US': ('Spanish (US)', 'es-US', 'es-US-Standard-A'),
    'sw': ('Swahili', 'sw-KE', 'sw-KE-Chirp3-HD-Achernar'),
    'sv': ('Swedish', 'sv-SE', 'sv-SE-Standard-A'),
    'ta': ('Tamil', 'ta-IN', 'ta-IN-Standard-A'),
    'te': ('Telugu', 'te-IN', 'te-IN-Standard-A'),
    'th': ('Thai', 'th-TH', 'th-TH-Standard-A'),
    'tr': ('Turkish', 'tr-TR', 'tr-TR-Standard-A'),
    'uk': ('Ukrainian', 'uk-UA', 'uk-UA-Standard-B'),
    'ur': ('Urdu', 'ur-IN', 'ur-IN-Standard-A'),
    'vi': ('Vietnamese', 'vi-VN', 'vi-VN-Standard-A'),
}

LANGUAGE_NAMES = {code: item[0] for code, item in OUTPUT_LANGUAGES.items()}

def default_voice(config, language):
    legacy = {"en": config.english_voice, "ru": config.russian_voice, "lt": config.lithuanian_voice}
    return legacy.get(language, OUTPUT_LANGUAGES[language][2])

def web_language_code(language):
    """Regional speech choices share translation codes, except Chinese scripts."""
    if language in {"zh-CN", "zh-TW"}:
        return language
    return language.split("-")[0]
