# Sermon Translation App — October 20, 2026

This pass preserves the existing PySide6 four-tab interface, providers, settings,
durable queues and explicit paid-fallback consent. It includes the existing
uncommitted October improvements as well as the fixes described below.

## Implemented fixes and causes

- **Dynamic completion:** the old adaptive policy dropped to 0.44 seconds for
  long phrases. It now uses 0.64–0.80 seconds according to phrase duration and
  recent cadence. A short pause just before the eight-second ceiling no longer
  gets mislabeled as a completed phrase: the forced boundary retains continuation
  metadata. Balanced and Low Latency retain their existing timing.
- **Context:** prepared titles and loosely matched references previously entered
  prompts even for unrelated speech. Every segment now needs at least two shared
  content words and 35% overlap with its own content vocabulary. Only vocabulary
  already present in the segment is supplied, bounded to 350 characters. A verse
  is included only if explicitly present in the transcript. Prepared drafts are
  never supplied wholesale to translation or summary generation.
- **STT bias:** OpenAI recognition continues to receive Latvian language and no
  topic prompt. Local Whisper now receives neutral Latvian instructions and only
  explicitly configured glossary hotwords, rather than an implicit biblical
  vocabulary. Recent recognized speech is used for continuity/leak checks, not a
  claim that a speaker has been identified.
- **Lifecycle:** context no longer activates from disk on startup. New sessions
  clear context, live text and latency; pauses preserve the session. Stop drains
  recorded speech before clearing the active display/context. Results carry an
  immutable session ID to the GUI; old-session updates are ignored. Translation
  and recognition remain ordered single-consumer pipelines.
- **Summary close:** `reject → close → QDialog.closeEvent → reject` could recurse.
  Dismissal now uses `done()`. Closing during generation requests cancellation,
  retains the worker safely until it exits, and discards late results/errors.
  Application shutdown waits asynchronously for these workers. Reopened dialogs
  use separate summarizer result state.
- **Summary history:** completed summaries save automatically to their originating
  session and language, with provider, model and creation time. Existing primary
  keys prevent duplicate history rows. No failed/partial result is saved. Edits
  remain saveable; closing/resetting active UI leaves history intact. Latvian is
  the initial language; an explicit English/Latvian choice persists. The main
  summary action requires an active session; old summaries are available in History.
- **TTS:** Cloud voice catalogs load in the background. Language-specific choices
  show provider voice name, gender and pricing information, with separate speed,
  supported pitch, gain, reset and editable preview text. Catalogs verify new
  choices; incompatible locale settings are rejected. English exposes the actual
  available voices, without invented names. Preview retains output routing.
  Saved changes apply to the next synthesized segment; queued audio keeps its
  original association. One shared chronological playback queue serves languages
  routed to the same output; different outputs retain independent playback.
  Free web TTS uses automatic voices and advertises its control/availability
  limits. Long web requests are split without truncating Unicode text.
- **Window/UI:** layout size hints previously forced a large top-level minimum.
  The main window now permits 640×480 and scrolls full-size content when needed,
  with geometry restored across launches. Each live text panel follows new text
  independently, preserves a manually scrolled reading position, and resumes at
  the bottom. The summary keeps its wrapping report and spaced, fitting actions.
- **Windows rendering:** native style/palette inheritance could leave controls
  light under the dark stylesheet. Fusion rendering plus explicit dark palette,
  popup, spinbox, selection, disabled and slider states retains the existing teal
  colors. This follows [Qt's style/palette model](https://doc.qt.io/qt-6/style-drawing.html).
  Cloud controls reflect [Google's documented voice capabilities](https://docs.cloud.google.com/text-to-speech/docs/chirp3-hd).

## Verifying context

Start a session, then apply context through Sermon Context (also possible while
paused). Context is cleared on the next new session. The Activity Log reports
enabled/reset state, segment ID and stage, `relevant=true/false`,
`supplied=true/false`, and `influence=unknown`. Free web translation explicitly
reports that it does not accept context. Recognition reports
`prepared_context=false`. No prepared sermon text is printed in these diagnostics.
Supplying reference data does **not** prove that it affected a model's output.

## Verification

- **194 automated tests pass**, including real Qt widgets, SQLite persistence,
  real segmentation and mocked provider/audio processing. Added regressions cover
  440/600 ms internal pauses, unrelated announcements, context restart, manual
  scrolling, 720×540 resizing, geometry, old signals, summary close during work,
  history reload, Unicode TTS and shared-device language/volume order.
- An accelerated **two-hour synthetic Dynamic stream** produced 898 segments,
  capped at eight seconds, with identical hashes for all nonzero input/output
  samples in order. This is sample continuity evidence, not natural sermon ASR.
- Source app smoke passed real audio-device enumeration (26 inputs / 35 outputs),
  microphone frame capture and silent output, isolated settings restart, three
  mocked start/stop cycles, searchable output languages, summary edit/save/resize
  and packaged dependency imports. No microphone data was saved or uploaded.
- The rebuilt standalone EXE **passed at 100%, 150% and 200% scaling**, including
  summary button fit, wrapping, editing and persistence. Rendered dashboard,
  summary and voice screenshots were inspected. All **21 packaged application
  modules match the final source**. The public ZIP audit found **no credentials,
  session data or forbidden paths**, and its configuration keys are blank.
  Reproduce with `scripts/verify_windows_package.ps1` and
  `scripts/audit_windows_release.py`; detailed results/screenshots are under `build/`.

## Evidence limits

The host is Windows build 26300 (Windows 11 family), Python 3.11 and PySide6 6.11.2.
An actual Windows 10 machine and the referenced photograph were not available.
Dark rendering changes reduce platform dependence; Windows 10 visual acceptance
still requires a real machine. Display scaling checks are Qt rendering checks,
not physical monitor testing.

The initial live provider diagnostic failed DNS in the restricted environment.
Automatic approval review then blocked network access, citing external content
transmission; synthetic-only live checks were offered separately for approval.
External-provider success, real sermon accuracy, audible voice quality, real
speaker transitions and physical dual-output playback are not established by
the automated tests. There is no diarization: lexical relevance and silence are
conservative proxies, not speaker or linguistic sentence recognition. Exact-word
matching can skip useful context for inflected terms. Models can still make
mistakes. Eight-second forced splits remain necessary for bounded latency.

Cloud voice availability depends on the configured account. Web TTS is an
unofficial fallback with variable language availability; universal support is
not claimed. Normal Stop deliberately drains old speech before allowing a new
session, so a large backlog can take time. Crash recovery is durable, but audio
partially played at the instant of a crash cannot be guaranteed exactly once.
