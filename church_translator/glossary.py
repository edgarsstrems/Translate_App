from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


# Built-in phonetic & speech recognition corrections for Latvian sermon audio
BUILTIN_SERMON_REPLACEMENTS: list[tuple[re.Pattern, str]] = [
    # Fused words & acoustic slips with Lord / Kungs
    (re.compile(r"\btas\s+kungstoa\b", re.IGNORECASE), "Tas Kungs to"),
    (re.compile(r"\bkungstoa\b", re.IGNORECASE), "Kungs to"),
    (re.compile(r"\bkungsto\b", re.IGNORECASE), "Kungs to"),
    (re.compile(r"\btas\s+kungs\b", re.IGNORECASE), "Tas Kungs"),
    
    # Common speech-to-text slips
    (re.compile(r"\bvisvis\b", re.IGNORECASE), "viss"),
    (re.compile(r"\bradīsu\b", re.IGNORECASE), "radījis"),
    
    # Latvian homophone slips: "vīņu" (Whisper typo for viņu) -> "Viņu" (Him / God / Jesus)
    (re.compile(r"\bar\s+vīņu\b", re.IGNORECASE), "ar Viņu"),
    (re.compile(r"\bvīņu\b", re.IGNORECASE), "Viņu"),
    # Only replace "ar vīnu" if explicitly preceded by interpersonal/relationship/spiritual walk context
    (re.compile(r"\b(attiecīb[a-zāēīūļķņģčšž]*|personīg[a-zāēīūļķņģčšž]*|draudzīb[a-zāēīūļķņģčšž]*|staigā[a-zāēīūļķņģčšž]*|būt\s+vienot[a-zāēīūļķņģčšž]*)\s+ar\s+vīnu\b", re.IGNORECASE), r"\1 ar Viņu"),
    
    # Common religious titles and words
    (re.compile(r"\bdeus\b", re.IGNORECASE), "Dievs"),
    (re.compile(r"\bjezus\b", re.IGNORECASE), "Jēzus"),
    (re.compile(r"\bjezū\b", re.IGNORECASE), "Jēzu"),
    (re.compile(r"\bjezus\s+kristus\b", re.IGNORECASE), "Jēzus Kristus"),
    (re.compile(r"\bjezus\s+kristu\b", re.IGNORECASE), "Jēzu Kristu"),
    (re.compile(r"\bsvetais\s+gars\b", re.IGNORECASE), "Svētais Gars"),
    (re.compile(r"\bsvētais\s+gars\b", re.IGNORECASE), "Svētais Gars"),
    (re.compile(r"\bsvēta\s+gara\b", re.IGNORECASE), "Svētā Gara"),
    (re.compile(r"\bdieva\s+vards\b", re.IGNORECASE), "Dieva vārds"),
    (re.compile(r"\bbraļiem\b", re.IGNORECASE), "brāļiem"),
    (re.compile(r"\baleluja\b", re.IGNORECASE), "Aleluja"),
    (re.compile(r"\bamen\b", re.IGNORECASE), "Āmen"),
    (re.compile(r"\bamēn\b", re.IGNORECASE), "Āmen"),
    (re.compile(r"\bsvārcer\b", re.IGNORECASE), "sauc"),
]


def _match_case(matched_text: str, target: str) -> str:
    if not matched_text or not target:
        return target
    if matched_text.isupper():
        return target.upper()
    if matched_text[0].isupper():
        return target[0].upper() + target[1:]
    if matched_text[0].islower() and target[0].isupper() and not target.startswith("Tas Kungs") and not target.startswith("Dievs") and not target.startswith("Jēzus") and not target.startswith("Svētais"):
        return target[0].lower() + target[1:]
    return target


@dataclass(frozen=True)
class Glossary:
    source_replacements: dict[str, str] = field(default_factory=dict)
    translation_terms: dict[str, dict[str, str]] = field(default_factory=dict)

    def apply_source_replacements(self, text: str) -> str:
        if not text:
            return text

        corrected = text

        # 1. Apply built-in sermon acoustic and phonetic corrections
        for pattern, replacement in BUILTIN_SERMON_REPLACEMENTS:
            if "\\" in replacement or "(" in pattern.pattern and "r\"" in str(replacement):
                corrected = pattern.sub(replacement, corrected)
            else:
                def make_repl(target_word: str):
                    return lambda m: _match_case(m.group(0), target_word)
                corrected = pattern.sub(make_repl(replacement), corrected)

        # 2. Apply user-defined source replacements from glossary.json
        for source, replacement in self.source_replacements.items():
            if not source:
                continue
            # Use regex word boundary replacement if source is simple alphanumeric word
            if re.match(r"^[\w\sāēīūļķņģčšž]+$", source, flags=re.UNICODE):
                escaped = re.escape(source)
                corrected = re.sub(rf"\b{escaped}\b", replacement, corrected, flags=re.IGNORECASE)
            else:
                corrected = corrected.replace(source, replacement)

        # Fix capitalization if sentence starts with lowercase after substitution
        if corrected and corrected[0].islower():
            corrected = corrected[0].upper() + corrected[1:]

        return corrected

    def prompt_hints(self, target_language: str) -> str:
        lines = []
        for latvian, translations in self.translation_terms.items():
            translated = translations.get(target_language)
            if translated:
                lines.append(f"- {latvian} => {translated}")
        if not lines:
            return ""
        return "Glossary terms to strictly follow:\n" + "\n".join(lines)


def load_glossary(project_root: Path) -> Glossary:
    path = project_root / "glossary.json"
    if not path.exists():
        return Glossary()
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return Glossary(
            source_replacements=dict(raw.get("source_replacements", {})),
            translation_terms=dict(raw.get("translation_terms", {})),
        )
    except Exception:
        return Glossary()
