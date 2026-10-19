"""Sermon Context Management: Parsing, indexing, and fast lightweight retrieval.

Reference material only; never replaces actual spoken microphone transcription.
"""
from __future__ import annotations

import json
import re
import threading
from functools import wraps
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Optional

from .config import app_data_dir, atomic_write_text

COMMON_LV_STOPWORDS = {
    "un", "ka", "ir", "par", "ar", "no", "to", "tas", "tā", "kā", "arī", "bet",
    "ja", "lai", "vai", "es", "mēs", "jūs", "viņš", "viņa", "viņi", "viņas",
    "man", "mums", "jums", "viņam", "viņai", "kad", "kur", "kas", "ko", "kam",
    "pēc", "pie", "uz", "zem", "būt", "bija", "būs", "nav", "tikai", "vēl",
    "jau", "tāpēc", "tādēļ", "tomēr", "taču", "kaut", "gan", "līdz", "pret",
}


def synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call

BIBLE_BOOK_PATTERNS = [
    r"\b\d?\s*(?:Mozus|Samuēla|Ķēniņu|Laiku|Korintiešiem|Tesaloniķiešiem|Timotejam|Pētera|Jāņa)\s+\d+:\d+(?:-\d+)?\b",
    r"\b(?:Jozuas|Soģu|Rutes|Ezras|Nehemijas|Esteres|Ījaba|Psalmi|Salamana pamācības|Mācītāja|Augstā dziesma)\s+\d+:\d+(?:-\d+)?\b",
    r"\b(?:Jesajas|Jeremijas|Raudu dziesmas|Ecēhiēla|Daniēla|Hosejas|Joēla|Amosa|Obadijas|Jonas|Mihas|Nahuma|Habakuka|Cefanjas|Hagaja|Zaharijas|Maleahija)\s+\d+:\d+(?:-\d+)?\b",
    r"\b(?:Mateja|Marka|Lūkas|Jāņa|Apustuļu darbi|Romiešiem|Galatiešiem|Efeziešiem|Filipiešiem|Kolosiešiem|Titam|Filemonam|Ebrejiem|Jēkaba|Jūdas|Atklāsmes)\s+\d+:\d+(?:-\d+)?\b",
    r"\b(?:Mat|Mk|Lk|Jņ|Rom|Kor|Gal|Ef|Fil|Kol|Tim|Tit|Ebr|Jēk|Pēt|Atkl)\.?\s*\d+:\d+(?:-\d+)?\b",
]


@dataclass
class SermonSection:
    index: int
    heading: str
    text: str
    keywords: set[str]


class SermonContextManager:
    """Manages pastor's prepared sermon text for fast reference retrieval."""

    def __init__(self, storage_path: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self.storage_path = storage_path or (app_data_dir() / "sermon_context.json")
        self.raw_text: str = ""
        self.title: str = ""
        self.sections: List[SermonSection] = []
        self.key_terms: List[str] = []
        self.bible_references: List[str] = []
        self.is_active: bool = False
        # Saved material can be explicitly loaded as a template, never activated
        # automatically after restart/crash. Active context belongs to one session.

    @synchronized
    def load(self) -> None:
        if not self.storage_path.exists():
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                text = data.get("text", "")
                self.title = data.get("title", "")
                self.set_text(text, save=False)
        except Exception:
            pass

    @synchronized
    def save(self) -> None:
        data = {
            "title": self.title,
            "text": self.raw_text,
            "word_count": len(self.raw_text.split()),
            "character_count": len(self.raw_text),
            "bible_references": self.bible_references,
            "key_terms": self.key_terms,
        }
        atomic_write_text(self.storage_path, json.dumps(data, indent=2, ensure_ascii=False))

    @synchronized
    def clear(self) -> None:
        self.raw_text = ""
        self.title = ""
        self.sections.clear()
        self.key_terms.clear()
        self.bible_references.clear()
        self.is_active = False
        if self.storage_path.exists():
            try:
                self.storage_path.unlink()
            except Exception:
                pass

    @synchronized
    def set_text(self, text: str, title: str = "", save: bool = True) -> None:
        self.raw_text = text.strip()
        if title:
            self.title = title.strip()
        self.is_active = bool(self.raw_text)
        if not self.is_active:
            self.clear()
            return
        self._process_text()
        if save:
            self.save()

    def _extract_bible_references(self, text: str) -> List[str]:
        refs = []
        for pat in BIBLE_BOOK_PATTERNS:
            for match in re.finditer(pat, text, flags=re.IGNORECASE):
                ref = match.group(0).strip()
                if ref not in refs:
                    refs.append(ref)
        return refs

    def _extract_keywords(self, text: str) -> set[str]:
        words = re.findall(r"\b[A-Za-zĀČĒĢĪĶĻŅŠŪŽāčēģīķļņšūž]{4,}\b", text.casefold())
        return {w for w in words if w not in COMMON_LV_STOPWORDS}

    def _process_text(self) -> None:
        # Extract title if not explicitly set
        lines = [line.strip() for line in self.raw_text.splitlines() if line.strip()]
        if not self.title and lines:
            first_line = lines[0]
            if len(first_line) <= 100:
                self.title = first_line

        self.bible_references = self._extract_bible_references(self.raw_text)

        # Extract high-frequency topical terms
        kw = self._extract_keywords(self.raw_text)
        # Also extract capitalized Proper Nouns (e.g., personas, locations)
        proper_nouns = re.findall(r"\b[A-ZĀČĒĢĪĶĻŅŠŪŽ][a-zāčēģīķļņšūž]{3,}\b", self.raw_text)
        term_freq: Dict[str, int] = {}
        for pn in proper_nouns:
            term_freq[pn] = term_freq.get(pn, 0) + 1
        sorted_terms = sorted(term_freq.items(), key=lambda x: x[1], reverse=True)
        self.key_terms = [t[0] for t in sorted_terms[:25] if t[1] >= 2]

        # Divide into sections (by headings or paragraphs ~150-300 words)
        paragraphs = re.split(r"\n\s*\n+", self.raw_text)
        self.sections.clear()
        sec_idx = 0
        current_chunk: List[str] = []
        current_words = 0

        for p in paragraphs:
            p_clean = p.strip()
            if not p_clean:
                continue
            words_in_p = len(p_clean.split())
            if current_words + words_in_p > 220 and current_chunk:
                sec_text = "\n\n".join(current_chunk)
                heading = current_chunk[0].splitlines()[0][:80]
                self.sections.append(
                    SermonSection(
                        index=sec_idx,
                        heading=heading,
                        text=sec_text,
                        keywords=self._extract_keywords(sec_text),
                    )
                )
                sec_idx += 1
                current_chunk = [p_clean]
                current_words = words_in_p
            else:
                current_chunk.append(p_clean)
                current_words += words_in_p

        if current_chunk:
            sec_text = "\n\n".join(current_chunk)
            heading = current_chunk[0].splitlines()[0][:80]
            self.sections.append(
                SermonSection(
                    index=sec_idx,
                    heading=heading,
                    text=sec_text,
                    keywords=self._extract_keywords(sec_text),
                )
            )

    def get_relevant_context(self, spoken_text: str, max_chars: int = 500) -> str:
        """Find the most relevant section matching the currently spoken transcript."""
        if not self.is_active or not self.sections or not spoken_text.strip():
            return ""

        spoken_kw = self._extract_keywords(spoken_text)
        if not spoken_kw:
            return ""

        best_score = 0
        best_section: Optional[SermonSection] = None
        for sec in self.sections:
            overlap = len(spoken_kw.intersection(sec.keywords))
            if overlap > best_score:
                best_score = overlap
                best_section = sec

        if not best_section or best_score < 2 or best_score / len(spoken_kw) < .35:
            return ""

        # Return a compact snippet from the matching section
        text = best_section.text.strip()
        if len(text) > max_chars:
            # Find best sentence within section
            sentences = re.split(r"(?<=[.!?])\s+", text)
            matching_sents = []
            for s in sentences:
                s_kw = self._extract_keywords(s)
                if s_kw.intersection(spoken_kw):
                    matching_sents.append(s)
            if matching_sents:
                snippet = " ".join(matching_sents)
                return snippet[:max_chars].strip()
            return text[:max_chars].strip() + "..."
        return text

    @synchronized
    def get_compact_prompt_hint(self, spoken_text: str) -> str:
        """Supply only terms evidenced in this segment, never unspoken verses/drafts.

        Conservative lexical matching is not diarization or proof of influence.
        Fresh evidence is required on every segment, including speaker transitions.
        """
        if not self.get_relevant_context(spoken_text, max_chars=350):
            return ""
        spoken_kw = self._extract_keywords(spoken_text)
        terms = sorted(spoken_kw.intersection(self._extract_keywords(self.raw_text)))
        hint = "Reference vocabulary also present in this segment: " + ", ".join(terms)
        explicit_refs = [ref for ref in self._extract_bible_references(spoken_text)
                         if ref.casefold() in self.raw_text.casefold()]
        if explicit_refs:
            hint = 'References explicitly spoken: ' + ', '.join(explicit_refs) + '\n' + hint
        return hint[:350]

    def get_stats(self) -> dict:
        word_count = len(self.raw_text.split()) if self.raw_text else 0
        char_count = len(self.raw_text) if self.raw_text else 0
        return {
            "active": self.is_active,
            "title": self.title,
            "word_count": word_count,
            "char_count": char_count,
            "sections_count": len(self.sections),
            "bible_references_count": len(self.bible_references),
            "key_terms_count": len(self.key_terms),
        }
