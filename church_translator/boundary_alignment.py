"""Ownership for explicitly shared audio, never text-only repetition removal."""
from dataclasses import replace
import math
import re


def owns_timestamped_words(result, capture):
    """Keep words by audio-relative end time, including words crossing a cut.

    This is enabled only for APIs returning actual word timestamps. Text-only
    providers receive the exact logical core, so guessed word locations can
    never erase genuine repeated speech.
    """
    if not capture or not capture.get('request_overlap_samples'):
        return result, {'applied': False, 'reason': 'no_shared_audio'}
    words = getattr(result, 'words', None)
    if not words:
        return result, {'applied': False, 'reason': 'word_timestamps_unavailable', 'review_required': True}
    rate = capture['sample_rate']
    request_start = capture['request_start_sample']
    left = (capture['start_sample'] - request_start) / rate
    right = (capture['end_sample'] - request_start) / rate
    kept, removed, all_words = [], [], []
    for item in words:
        word = item if isinstance(item, dict) else item.model_dump() if hasattr(item, 'model_dump') else vars(item)
        start, end = word.get('start'), word.get('end')
        all_words.append(str(word.get('word', '')))
        duration = (capture['request_end_sample'] - request_start) / rate
        if (not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                or not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end < start or end > duration + .05):
            return result, {'applied': False, 'reason': 'invalid_word_timestamps', 'review_required': True}
        if end > left + 1 / rate and end <= right + 1 / rate:
            kept.append(str(word.get('word', '')))
        else:
            removed.append({'word': word.get('word', ''), 'start': start, 'end': end})
    tokens = lambda text: re.findall(r'\w+', text.casefold())
    if tokens(' '.join(all_words)) != tokens(result.text):
        return result, {'applied': False, 'reason': 'untimed_source_words', 'review_required': True}
    # Always retain the original response independently in the request audit.
    text = ' '.join(part.strip() for part in kept if part.strip())
    return replace(result, text=text), {'applied': True, 'method': 'audio_relative_word_end',
        'owned_seconds': [left, right], 'excluded_context_words': removed,
        'request_start_sample': request_start, 'source_capture_id': capture['capture_id']}
