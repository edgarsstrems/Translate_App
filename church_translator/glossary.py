from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Glossary:
    source_replacements: dict[str, str] = field(default_factory=dict)
    translation_terms: dict[str, dict[str, str]] = field(default_factory=dict)

    def apply_source_replacements(self, text: str) -> str:
        corrected = text
        for source, replacement in self.source_replacements.items():
            corrected = corrected.replace(source, replacement)
        return corrected

    def prompt_hints(self, target_language: str) -> str:
        lines = []
        for latvian, translations in self.translation_terms.items():
            translated = translations.get(target_language)
            if translated:
                lines.append(f"- {latvian} => {translated}")
        if not lines:
            return ""
        return "Use these glossary translations consistently:\n" + "\n".join(lines)


def load_glossary(project_root: Path) -> Glossary:
    path = project_root / "glossary.json"
    if not path.exists():
        return Glossary()
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return Glossary(
        source_replacements=dict(raw.get("source_replacements", {})),
        translation_terms=dict(raw.get("translation_terms", {})),
    )
