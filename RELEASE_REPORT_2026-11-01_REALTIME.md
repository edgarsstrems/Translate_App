# Persistent Realtime transcription — November 1, 2026

The transcription picker retains every existing choice and adds
`gpt-transcribe` and `gpt-live-transcribe`. A fresh picker defaults to
`gpt-4o-mini-transcribe`; an explicitly saved model survives restart. Private
environment files, credentials and installed settings are preserved. Changing
the idle picker selects the pipeline for the next session, using the existing
session lifecycle and four-tab interface.

## Official API contract checked

| Model | Application transport | Price per submitted audio minute |
| --- | --- | --- |
| gpt-4o-mini-transcribe | Existing file pipeline; default | About $0.003, token-billed |
| gpt-transcribe | POST `/v1/audio/transcriptions` | $0.0045 |
| gpt-live-transcribe | Persistent WebSocket `/v1/realtime?intent=transcription` | $0.017 |

Duration rates are from the official [file model](https://developers.openai.com/api/docs/models/gpt-transcribe)
and [live model](https://developers.openai.com/api/docs/models/gpt-live-transcribe)
pages. The mini estimate and token rates are documented in
[pricing](https://developers.openai.com/api/docs/pricing). At 60 submitted minutes,
the corresponding estimates are $0.18, $0.27 and $1.02. Replays, transport padding
and continuous silence increase submitted duration; local estimates are not bills.

File requests use JSON responses, `languages=["lv"]`, bounded `prompt` and literal
`keywords`. They do not send the older singular language, temperature, logprob or
word-timestamp parameters to gpt-transcribe. The app sends documented PCM16 WAV.
The [file API reference](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)
lists flac, mp3, mp4, mpeg, mpga, m4a, ogg, wav and webm. The
[speech-to-text guide](https://developers.openai.com/api/docs/guides/speech-to-text)
sets a 25 MB upload limit. File SSE streams the response to a completed upload;
it does not continuously accept microphone samples. The new file model can also
transcribe committed Realtime turns, but this app deliberately uses its file API.

The [Realtime transcription guide](https://developers.openai.com/api/docs/guides/realtime-transcription)
specifies a Bearer-authenticated transcription socket, without a URL model or
Beta header. A GA `session.update` selects transcription, 24 kHz PCM input,
gpt-live-transcribe, Latvian language hints and medium delay. Both noise reduction
and turn detection are null. This model requires client turns; server/semantic
VAD is unsupported. The app appends base64 mono PCM16 LE and commits at its own
boundaries. It matches committed item IDs and consumes completed transcripts in
capture order. Delta previews never become source speech. The model offers no
word timestamps, confidence or speaker labels; local ranges are labeled explicitly.
Delay supports minimal, low, medium, high and xhigh. The app uses medium.

The [session guide](https://developers.openai.com/api/docs/guides/realtime-conversations)
documents the 60-minute session limit. The transport rotates after 55 minutes at
a drained boundary and also honors an earlier server expiration.

## Focused implementation

- Capture still copies callbacks into the bounded queue and commits original
  float32 PCM in the separate journal worker. Segmentation advertises only
  committed ranges. Provider and UI work never enters the callback/journal worker.
- The opt-in transport reads 500 ms original windows, applies the existing
  acoustic admission/gain rules and statefully converts 16 kHz to 24 kHz PCM16.
  Integer interpolation weights make the derivative independent of packet
  partitioning. Original audio remains immutable. Transport padding for a tiny
  final tail never becomes speech evidence.
- Live boundaries use the current analysis frame rather than retrospectively
  selecting an already-streamed pause. Dynamic, Balanced and Low Latency remain
  available; file-model boundary behavior is preserved. A custom fractional hard
  limit can be rounded by less than one 20 ms analysis frame in Live mode.
- `realtime.sqlite` retains turn ownership, generations, exact packet bytes/hashes,
  socket-write receipts, raw events and pending work. A socket write is not proof
  of server delivery. One commit acknowledgement is outstanding at a time;
  up to two finalized-inference turns can be in flight. Backpressure stays on disk.
- A lost connection replays unresolved original turns at most once, respecting
  the configured recovery limit. Final rows never replay. Late generations,
  duplicate event IDs and repeated finals cannot generate duplicate source.
  Genuine repeated wording in distinct turns remains eligible. Conflicting
  unconsumed finals are retained for review. Account/model rejection opens a
  circuit for the session; later capture is retained without further requests.
- Existing source/translation/synthesis/playback ordering, acoustic validation,
  context isolation, quiet-speech support, short-window guards, physical-output
  serialization and latency definition remain in place. Only validated finals
  enter Gemini/TTS. No paid file-model fallback is automatic.
- Diagnostic exports include original WAV, exact Realtime packet WAVs, events
  and attempt metadata. Pending/review work blocks retention. Eligible drained
  transport audio expires before the atomic original-audio expiry; an interruption
  still leaves the originals for recovery. Metadata and session history remain.
- Indexed original-range lookup avoids repeatedly scanning preceding capture
  packets during a long Realtime service. Integrity includes transport cost estimates.

## Verification

Automated checks use mock providers and block external sockets and Google token
refresh. The targeted suite covers real SDK multipart serialization, continuous
pre-commit packets, out-of-order finals, final-before-ack, genuine repeated deltas,
stale generations, replay limits, quota rejection, tiny/near-floor hallucination
holds, supported quiet speech, rotation, Pause/resume, Stop tails, picker persistence,
raw packet export, restart persistence and retention.

- 382 final unit/regression tests passed with external sockets and Google token refresh
  blocked (103.433 seconds). Sandbox runs encountered an existing language
  settings roundtrip failure with Windows access denied during atomic replacement;
  the targeted 12-test group and full suite passed outside the sandbox.
- Source Qt/audio smoke passed, including native WebSocket imports and the
  16-to-24 kHz resampler, without provider calls or saved-setting changes.
- A 180-second actual callback/journal/Realtime/ordered-output simulation passed,
  preserving the original sample hash, injecting a possibly-submitted-packet
  disconnect, and draining all queues with no duplicate source commits.
- Three offline recorded-audio cases passed: unchanged original PCM, exact
  transport duration, real acoustic evidence, supported quiet speech eligible,
  and near-floor mock wording held. Quiet windows received at most 4x gain;
  unsupported windows received neither gain nor context. Mock words are not WER.
- Balanced and Low Latency each passed 180-second callback/journal/Realtime
  simulations with forced disconnect, exact original hash, ordered source/output
  and zero pending or review items (33 and 49 source rows respectively).
- The final tiny-tail test preserves a genuine 50 ms word, omits all reference
  hints, and accounts separately for original samples and transport padding.
  The 17-test Realtime suite passed after this fix.
- The optimized two-hour Dynamic simulation passed: 115,200,000 original samples,
  matching original SHA-256, 1,108 ordered source/output batches, four connections
  covering an injected disconnect and two planned rotations, and all pending and
  review counts zero. Wall time was 556.781 seconds; provider/VAD/output were
  mocked and paid requests were zero. Replayed/padded transport duration was
  explicitly counted (7,208.499958 seconds potentially submitted).

- Windows EXE rebuilt after the final application edit. Packaged smoke checks
  passed at 100%, 150% and 200% Qt scaling, including native Realtime imports,
  fullscreen, navigation/scrolling, original four tabs, diagnostics and fonts.
  Narrow 200% navigation and 150% fullscreen screenshots were visually inspected.
- All 33 packaged application modules match the final source. The public ZIP is
  generated with the existing explicit allowlist and a blank-key environment
  template; final promotion requires the source/privacy audit to pass.

Local evidence is under ignored `build/realtime-2026-11-01/`, including
`regression-final-382.log`, `recorded-offline.json`,
`optimized-long/stream-7200.json`, `stream-balanced.json`,
`stream-low_latency.json`, `package-smoke-1.json`, `package-smoke-1.5.json`,
`package-smoke-2.json` and the final release audit. An older ZIP rollback is
retained locally. Final artifacts use the standard `dist-app/ChurchTranslator/`
and `dist-app/ChurchTranslator-Windows.zip` layout. Package/source checks do not
establish successful paid-provider inference on this exhausted-credit account.

## Authorized real-audio comparison

The user authorized sermon recordings and at most $0.03. The comparison used a
ten-second recording, a quiet derivative and a 2.5-second near-floor recording.
Three gpt-transcribe file requests were rejected with HTTP 429
`credit_balance_exhausted`. Realtime session configuration was likewise rejected;
no Realtime audio packets were submitted and no transcripts were returned.
Testing was stopped. The file requests reserved a conservative total $0.0016875
before sending; rejected requests do not establish billable usage and account
billing was not inspected. No Gemini, translation or TTS calls were made.

Actual recognition accuracy and provider latency could therefore not be compared.
The reusable comparison script now stops immediately on an account/model
rejection. It saves private hypotheses and checked-reference scoring locally,
reserves the estimated budget before every audio request/append, disables SDK
retries and buys no new mini baseline. No private audio, transcript, reference,
credentials or databases are committed or included in the public package.

Architecturally, file latency includes capture-to-boundary plus the upload/request;
Realtime can show draft text during capture but still waits for the same validated
turn boundary before translation. Neither a lower audible delay nor improved WER
is claimed. Synthetic queue/rotation tests do not establish natural-service
accuracy, audible quality, calibrated uncertainty, Windows 10 compatibility,
microphone/driver reliability or successful paid-provider acceptance.
