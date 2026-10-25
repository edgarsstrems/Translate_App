"""Sermon Context Management: Parsing, indexing, and fast lightweight retrieval.

Reference material only; never replaces actual spoken microphone transcription.
"""
from __future__ import annotations

import json
import re
import threading
import unicodedata
import time
import math
from collections import Counter
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
    "savu", "savs", "savā", "sava", "mūsu", "viņu", "sevi", "esam", "nedz",
    "citiem", "daudz", "darīt", "pietiekami", "šajā", "bieži", "ļoti", "nevaram",
    "kāpēc", "vienmēr", "katrs", "katram", "visu", "visiem", "daži", "tāpat",
}


def synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call

BIBLE_BOOK_PATTERNS = [
    r"\b(?:[123]\.?[ \t]*)?(?:Mozus|Samuēla|Ķēniņu|Laiku|Korintiešiem|Tesaloniķiešiem|Timotejam|Pētera|Jāņa)\s+\d+:\d+(?:[-–]\d+)?\b",
    r"\b(?:Jozuas|Soģu|Rutes|Ezras|Nehemijas|Esteres|Ījaba|Psalmi|Salamana pamācības|Mācītāja|Augstā dziesma)\s+\d+:\d+(?:-\d+)?\b",
    r"\b(?:Jesajas|Jeremijas|Raudu dziesmas|Ecēhiēla|Daniēla|Hosejas|Joēla|Amosa|Obadijas|Jonas|Mihas|Nahuma|Habakuka|Cefanjas|Hagaja|Zaharijas|Maleahija)\s+\d+:\d+(?:-\d+)?\b",
    r"\b(?:Mateja|Marka|Lūkas|Jāņa|Apustuļu darbi|Romiešiem|Galatiešiem|Efeziešiem|Filipiešiem|Kolosiešiem|Titam|Filemonam|Ebrejiem|Jēkaba|Jūdas|Atklāsmes)\s+\d+:\d+(?:-\d+)?\b",
    r"\b(?:[123]\.?[ \t]*)?(?:Mt|Mat|Mk|Lk|Jņ|Rom|Kor|Gal|Ef|Fil|Kol|Tim|Tit|Ebr|Jēk|Pēt|Atkl)\.?\s*\d+:\d+(?:[-–]\d+)?\b",
    r"\b(?:Psalms?|Psalmi|Ps)\.?\s*\d+:\d+(?:[-–]\d+)?\b",
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
        self.processing_ms = 0.0
        self.topic_terms: List[str] = []
        self.headings: List[str] = []
        self._section_keys: list[set[str]] = []
        self._weights: dict[str, float] = {}
        self._document_keys: set[str] = set()
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
                self.set_text(text, title=self.title, save=False)
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
        self.topic_terms.clear()
        self.headings.clear()
        self._section_keys.clear()
        self._weights.clear()
        self._document_keys.clear()
        self.is_active = False
        if self.storage_path.exists():
            try:
                self.storage_path.unlink()
            except Exception:
                pass

    @synchronized
    def set_text(self, text: str, title: str = "", save: bool = True) -> None:
        started = time.perf_counter()
        self.raw_text = unicodedata.normalize('NFC', text).strip()
        self.title = title.strip()
        self.is_active = bool(self.raw_text)
        if not self.is_active:
            self.clear()
            return
        self._process_text()
        self._index_sections()
        self.processing_ms = (time.perf_counter() - started) * 1000
        if save:
            self.save()

    @staticmethod
    def read_docx(path: Path) -> tuple[str, str]:
        """Read body paragraphs and tables in document order, retaining headings.

        Parsing is local and happens before Apply/Start. No provider is called.
        """
        from docx import Document
        from docx.oxml.ns import qn
        document = Document(path)
        blocks, title = [], ''
        for block in document.element.body:
            if block.tag == qn('w:p'):
                text = ''.join(
                    node.text or '' for node in block.iter(qn('w:t')))
                style = block.find('w:pPr/w:pStyle', block.nsmap)
                if not title and style is not None and style.get(qn('w:val'), '').lower() in {'title', 'heading1'}:
                    title = text.strip()
                if text.strip():
                    blocks.append(text.strip())
            elif block.tag == qn('w:tbl'):
                for row in block.findall('w:tr', block.nsmap):
                    cells = [' '.join(node.text or '' for node in cell.iter(qn('w:t')))
                             for cell in row.findall('w:tc', row.nsmap)]
                    if any(c.strip() for c in cells):
                        blocks.append(' | '.join(cells))
        text = unicodedata.normalize('NFC', '\n\n'.join(blocks))
        if not text.strip():
            raise ValueError('The DOCX contains no readable sermon text.')
        return text, unicodedata.normalize('NFC', title)

    # Small transparent concept lexicon, supplemented by inflections and IDF.
    # This is local topic similarity, not a calibrated semantic model.
    _CONCEPTS = (
        ('ķermen', 'vesel', 'mies', 'organism'),
        ('uztur', 'ēdien', 'ēšan', 'pārtik', 'barīb', 'vitamīn'),
        ('mieg', 'atpūt', 'atspirg', 'gulē', 'nogur'),
        ('raiz', 'trauks', 'stres', 'satrauk', 'rūpest'),
        ('lūg', 'pielūg', 'aizlūg'),
        ('ticīb', 'paļāv', 'uztic'),
        ('kustīb', 'sport', 'vingrin', 'fizisk'),
    )

    def _features(self, text: str) -> set[str]:
        keys = {self._term_key(w) for w in self._extract_keywords(text)}
        for index, stems in enumerate(self._CONCEPTS):
            if any(word.startswith(stem) for word in keys for stem in stems):
                keys.add(f'@topic{index}')
        return keys

    def _index_sections(self) -> None:
        counts = Counter(re.findall(r'\b[^\W\d_]{4,}\b', self.raw_text.casefold()))
        self.topic_terms = [word for word, _ in counts.most_common()
                            if word not in COMMON_LV_STOPWORDS][:24]
        self.headings = [s.heading for s in self.sections]
        self._section_keys = [self._features(section.text) for section in self.sections]
        self._document_keys = {self._term_key(w) for w in self._extract_keywords(self.raw_text)}
        frequency = Counter(key for keys in self._section_keys for key in keys)
        self._weights = {key: 1 + math.log((1 + len(self.sections)) / (1 + count))
                         for key, count in frequency.items()}

    def _select_section(self, source: str, recent: str = '') -> tuple[int, float]:
        current = self._features(source)
        history = self._features(recent)
        best, best_score = -1, 0.0
        for section, keys in zip(self.sections, self._section_keys):
            overlap = current & keys
            # A single accidental proper noun must not activate a whole topic.
            if len(overlap) < 2:
                continue
            weight = sum(self._weights.get(k, 1) for k in overlap)
            coverage = weight / max(1, sum(self._weights.get(k, 1) for k in current))
            if coverage < .3:
                continue
            score = coverage + .15 * len(history & keys) / max(1, len(history))
            if score > best_score:
                best, best_score = section.index, score
        return best, best_score

    @synchronized
    def recognition_guidance(self, recent_source: str = '') -> dict:
        """Bounded vocabulary, never draft paragraphs or guessed spoken words.

        STT has no current transcript yet: global terms bootstrap the first chunk;
        one recent recognized source narrows later hints. Unrelated speech suppresses
        prepared hints rather than forcing the speaker back onto the draft.
        """
        started = time.perf_counter()
        section, score = self._select_section(recent_source) if recent_source else (-1, 0.)
        active = self.is_active and bool(self.raw_text)
        reason = 'inactive' if not active else ('bootstrap_vocabulary' if not recent_source else
                  ('recent_section' if section >= 0 else 'recent_topic_mismatch'))
        vocabulary = []
        if active and (not recent_source or section >= 0):
            text = self.sections[section].text if section >= 0 else self.raw_text
            counts = Counter(re.findall(r'\b[^\W\d_]{4,}\b', text))
            vocabulary = [word for word, _ in counts.most_common()
                          if word.casefold() not in COMMON_LV_STOPWORDS][:32]
        return {'vocabulary': ', '.join(vocabulary)[:600], 'loaded': bool(self.raw_text),
                'active': active, 'section': section, 'score': round(score, 3), 'reason': reason,
                'processing_ms': round((time.perf_counter() - started) * 1000, 3)}

    @synchronized
    def translation_guidance(self, source: str, recent: str = '') -> dict:
        section, score = self._select_section(source, recent)
        if not self.is_active or section < 0:
            return {}
        # Overall topic is a vocabulary list, never sentences to be translated.
        keys = self._features(source)
        terms = [w for w in self.topic_terms if self._features(w) & keys]
        section_words = sorted(w for w in self.sections[section].keywords if self._features(w) & keys)
        return {'overall_topic_terms': ', '.join(terms)[:180],
                'section_vocabulary': ', '.join(section_words)[:220]}

    def _extract_bible_references(self, text: str) -> List[str]:
        refs = []
        for pat in BIBLE_BOOK_PATTERNS:
            for match in re.finditer(pat, text, flags=re.IGNORECASE):
                ref = match.group(0).strip()
                if ref not in refs:
                    refs.append(ref)
        return refs

    def _extract_keywords(self, text: str) -> set[str]:
        words = re.findall(r"\b[A-Za-zĀČĒĢĪĶĻŅŠŪŽāčēģīķļņšūž]{4,}\b", unicodedata.normalize('NFC', text).casefold())
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
        self.key_terms = [t[0] for t in sorted_terms[:25] if t[0].casefold() not in COMMON_LV_STOPWORDS]

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
            is_heading = (words_in_p <= 12 and len(p_clean) <= 100 and
                          not re.search(r'[.!?;:]$', p_clean) and '|' not in p_clean)
            if current_chunk and (current_words + words_in_p > 220 or is_heading):
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

        spoken_kw = {self._term_key(w) for w in self._extract_keywords(spoken_text)}
        if not spoken_kw:
            return ""

        best_score = 0
        best_section: Optional[SermonSection] = None
        for sec in self.sections:
            overlap = len(spoken_kw.intersection(self._term_key(w) for w in sec.keywords))
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
                s_kw = {self._term_key(w) for w in self._extract_keywords(s)}
                if s_kw.intersection(spoken_kw):
                    matching_sents.append(s)
            if matching_sents:
                snippet = " ".join(matching_sents)
                return snippet[:max_chars].strip()
            return text[:max_chars].strip() + "..."
        return text

    @staticmethod
    def _term_key(word: str) -> str:
        """Conservative Latvian inflections; this does not guess a topic."""
        for suffix in ('iem', 'ām', 'ēm', 'us', 'as', 'es', 'is', 'am', 'em', 'ā', 'ē', 'a', 'e', 'i', 'u', 's'):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                return word[:-len(suffix)]
        return word

    @synchronized
    def retrieval_diagnostics(self, spoken_text: str) -> dict:
        words = {self._term_key(w) for w in self._extract_keywords(spoken_text)}
        scored = [(len(words.intersection(self._term_key(w) for w in sec.keywords)), sec.index)
                  for sec in self.sections] if self.is_active and words else []
        overlap, index = max(scored, key=lambda item: item[0], default=(0, -1))
        coverage = overlap / len(words) if words else 0.0
        selected, score = self._select_section(spoken_text) if self.is_active else (-1, 0.)
        matched = selected >= 0
        return {'loaded': bool(self.raw_text), 'active': self.is_active,
                'section': selected, 'overlap': overlap, 'topic_score': round(score, 3),
                'coverage': round(coverage, 3), 'matched': matched,
                'reason': 'matched_terms' if matched else ('no_relevant_match' if self.is_active else 'inactive')}

    @synchronized
    def get_compact_prompt_hint(self, spoken_text: str) -> str:
        """Supply only terms evidenced in this segment, never unspoken verses/drafts.

        Conservative lexical matching is not diarization or proof of influence.
        Fresh evidence is required on every segment, including speaker transitions.
        """
        if not self.retrieval_diagnostics(spoken_text)['matched']:
            return ""
        spoken_kw = self._extract_keywords(spoken_text)
        document_keys = self._document_keys
        terms = sorted(w for w in spoken_kw if self._term_key(w) in document_keys)
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
