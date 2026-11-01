"""Conservative acoustic/text plausibility checks, never accuracy scores."""
import math
import re


def unsupported_previous_echo(text, previous, evidence):
    """Uncalibrated hold, never proof of silence or general deduplication."""
    words = lambda value: re.findall(r'[^\W\d_]+', (value or '').casefold())
    current, prior = words(text), words(previous)
    return bool(len(current) >= 4 and len(prior) >= len(current)
        and prior[-len(current):] == current
        and evidence.get('vad_available')
        and not evidence.get('probable_speech_sample_ranges')
        and float(evidence.get('probable_speech_seconds', 0)) == 0
        and float(evidence.get('rms_dbfs', 0)) < -60
        and float(evidence.get('peak_dbfs', 0)) < -40)


def short_audio_warning(samples, text, sample_rate=16000):
    """Flag implausibly dense text over a very short original window.

    Padding is not speech evidence. A short real word remains allowed; the
    heuristic cannot establish whether a word was actually spoken.
    """
    duration = samples / sample_rate
    words = re.findall(r'[^\W\d_]+', text, re.UNICODE)
    syllables = sum(len(re.findall(r'[aāeēiīouūy]+', word.casefold())) for word in words)
    suspicious = bool(duration < .35 and words and
        ((syllables >= 3 and syllables > duration * 12 + 1) or len(words) > max(2, duration * 10)))
    return {'original_seconds': round(duration, 6), 'word_count': len(words),
            'vowel_group_count': syllables, 'possible_short_audio_hallucination': suspicious,
            'proof_of_hallucination': False, 'action': 'retain_for_review' if suspicious else 'preserve_wording'}


def supported_token_spans(text, tokens):
    """Return low-score spans only if the actual tokens exactly cover the text."""
    if not isinstance(tokens, (list, tuple)):
        return []
    parts = []
    for token in tokens:
        value = token.get('token') if isinstance(token, dict) else getattr(token, 'token', None)
        score = token.get('logprob') if isinstance(token, dict) else getattr(token, 'logprob', None)
        if not isinstance(value, str) or not isinstance(score, (int, float)) or not math.isfinite(score):
            return []
        parts.append((value, score))
    if ''.join(value for value, _ in parts) != text:
        return []
    spans, cursor = [], 0
    for value, score in parts:
        left, right = cursor, cursor + len(value)
        cursor = right
        if score < -1 and value.strip():
            spans.append({'start': left, 'end': right, 'reason': 'uncalibrated_token_score'})
    return spans
