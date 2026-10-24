"""Conservative response checks; these detect delivery failures, not accuracy.

Never infer intended speech or rewrite text from a prepared sermon. Short shared
words, proper names and quotations are deliberately allowed.
"""
from __future__ import annotations

import re
import unicodedata


class InvalidTranslationOutput(ValueError):
    """A provider response must be retried or retained, never delivered as speech."""


_LATVIAN_MARKERS = frozenset({
    'mēs', 'jūs', 'viņš', 'viņa', 'viņi', 'šajā', 'šis', 'šī', 'šorīt',
    'mūsu', 'jūsu', 'tavs', 'tavu', 'savu', 'manu', 'kāpēc', 'tāpēc',
    'ķermenis', 'ķermeni', 'jūtas', 'rūpēties', 'nevaram', 'esam',
})


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", unicodedata.normalize('NFC', text).casefold())


def translation_issue(source: str, translated: str, target_language: str) -> str | None:
    """Return only high-confidence format/language failures, without source text."""
    if not isinstance(translated, str) or not translated.strip():
        return 'empty'
    stripped = translated.strip()
    if stripped.startswith(('{', '```')) or re.match(r'^\[\s*["{\[]', stripped):
        return 'malformed_response'
    if target_language == 'lv':
        return None
    original, output = _words(source), _words(translated)
    if original == output and (len(original) >= 6 or
                              (len(original) >= 4 and len(set(original) & _LATVIAN_MARKERS) >= 2)):
        return 'untranslated_source'
    # A long copied Latvian span dominating the answer is a delivery failure.
    # Do not reject a short name, amen, or a quotation within translated prose.
    if len(output) >= 8 and len(set(output) & _LATVIAN_MARKERS) >= 3:
        width = max(6, int(len(output) * .8))
        for start in range(len(output) - width + 1):
            span = output[start:start + width]
            if any(original[i:i + width] == span for i in range(len(original) - width + 1)):
                return 'mostly_untranslated_source'
    return None


def validate_translations(source: str, translations: dict[str, str], languages: list[str]) -> dict[str, str]:
    for language in languages:
        issue = translation_issue(source, translations.get(language, ''), language)
        if issue:
            raise InvalidTranslationOutput(f'language={language} reason={issue}')
    return translations
