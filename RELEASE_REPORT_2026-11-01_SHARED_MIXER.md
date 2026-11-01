# Independent language mixing — November 1, 2026

English and the selectable language now overlap intentionally on a shared output.
English never waits for the selectable language's sentences to finish, and vice
versa. Sentences within one language remain sequential, complete and uninterrupted.
This replaces the shared-device serialization policy in the previous TTS release
(`83f7970`); older release reports describe that earlier policy.

## Mixer and scheduling

- Routing groups matching PortAudio output selections, including the default
  selection resolved to its device index. A shared group uses one
  `MixedAudioPlayer`; separate device groups retain independent players.
- Each language has its own durable synthesis, prepared-audio and
  `playback-<language>.sqlite` queue. The cross-language admission barrier is
  removed. Backpressure conditions are also per language, so a stalled optional
  queue cannot hold the English synthesis worker. Stable receipts prevent
  repeated admission from duplicating audio.
- Two decoded chunks per lane feed a single blocking PortAudio writer. The writer
  combines 20 ms float32 PCM blocks at the device's default sample rate, with
  stereo or mono according to device capability. A lane consumes its current
  sentence fully before reading its next sentence, including filling a partial
  output block with the start of the next sentence. Different lanes fill the
  same output frames concurrently. There is no same-language crossfade/overlap.
- A missing, slow or failed lane contributes silence while other lanes continue.
  Provider, decode, database and diagnostic work never runs on the mixer writer.
  Dedicated decoder/acknowledgement workers and a reporting worker keep those
  operations separate. The first ready language can open/start the output without
  waiting for another language to initialize or synthesize.
- Load ratios and high-backlog warnings are reported per language. They never
  add the two languages' durations as if the speaker still serialized them.
  Backlog warnings label summed unacknowledged audio honestly, including the
  already-played part of the current unacknowledged chunk.
- One output stream stays open through playback and idle periods until End
  Session. Empty lanes contribute zeros at the normal hardware write cadence;
  there is no per-sentence stop/start or arbitrary pause. Previously accepted
  speech still drains while microphone capture is paused, as before. End Session
  waits for synthesis and then all language queues and the final device drain.

## PCM conversion, volume and durability

The existing bundled FFmpeg resampler prepares playback derivatives off the writer.
Mono duplicates at the same level into stereo; stereo-to-mono uses the mean.
Multichannel input uses its encoded channel layout; an unspecified layout is held
for recovery instead of guessing speaker assignments. Device format stays fixed
through a session, so a 24 kHz Cloud voice and a 16 kHz local voice do not reopen
the output between sentences. Original encoded audio is never replaced by the
converted derivative.

The mixer uses fixed **1/N headroom**, where N is the number of enabled language
lanes on that output. Two lanes therefore each receive a 0.5 mixer gain in addition
to their independent saved volume controls. This is a fixed master headroom policy,
not ducking: voices do not pump in level when another voice starts/stops. It also
means one speaking voice remains at that headroom level while its companion lane
is quiet. Out-of-range float PCM is normalized as a derivative, and a final
numerical bounds guard prevents clipping. Non-finite/corrupt audio stays in its
own durable lane; another language can continue. Words, voice, speed, punctuation,
natural pauses and quiet syllables are not edited or trimmed.

Queue rows survive decoding, submission and partial output. FIFO acknowledgement
occurs only after a conservative PortAudio clock/latency completion estimate or
a successful final backend drain. Device failure retains unacknowledged originals;
partially played items require explicit review/recovery and never auto-replay.
Recovery/export and retention already inspect every durable SQLite work queue,
including the separate playback lanes. The final pending count sums all lanes.

Capture, source segmentation, acoustic/silence protections, file transcription,
Realtime reconciliation/reconnect, translation validation, settings and existing
voices are preserved. English's default remains Iapetus at 0.95. The four-tab UI
only adds a routing explanation for intentional mixed-language playback. No saved
routing or volume preference is migrated.

## Verification

Local evidence is under ignored `build/shared-mixer/`.

- Exact PCM tests compare simultaneous English/Yoruba output against the expected
  sample sums and per-language volume gains. Full-scale inputs remain bounded.
  Sentence starts/ends are checked at sample-frame precision, including boundaries
  inside a 20 ms block and short final tails.
- Tests cover both directions of blocked decoding, a blocked reporting callback,
  failed/corrupt audio isolated to one language, sample-rate/channel conversion,
  source-byte preservation, stable receipts, failed output and explicit FIFO
  recovery, shared/separate routing and language-specific backpressure isolation.
- Two-hour deterministic service: **1,200 sentences per language**, **2,400
  completions**, **115,200,000** shared 16 kHz output frames and the same number of
  overlapping-language frames. Every sentence was delivered once and in order
  within its lane, all queues drained, and every ready-to-ready lane gap was
  **zero frames**. One stream start and final drain were used. Decoder/resampler,
  mixing and durable queues are real; the DAC/providers are simulated.
  Each sentence has a distinct encoded level; **6,000** interior PCM blocks were
  independently checked against the expected two-language sums, alongside the
  frame-accurate ordering/completion checks.
- A separate 60-sentence test uses real threads, SQLite, decoding and mixing with
  a mock blocking output. Maximum observed work between writes was **1.329 ms**
  (average **0.135 ms**), below the 200 ms avoidable scheduling target.
- Muted real-device run: eight sentences, four per language, **44,100 overlapping
  frames** on a native **44.1 kHz stereo** output, all queues drained and no reported
  underflows. Ready sentence transitions were **zero frames / zero estimated
  avoidable gap**. Reported output latency was 180.3 ms. This writes digital
  silence and uses PortAudio estimates; it is not an acoustic loopback test.
- The 180-second Realtime integration uses the production shared mixer with
  mock output/providers: 28 ordered source rows, 56 language outputs, forced
  disconnect/reconnect, exact original-sample hash and zero pending/review work.
  Both languages overlap; the final integration run recorded 356,160 overlapping
  48 kHz frames.

- **405** final unit/regression tests passed in **95.522 seconds**, with external
  sockets and Google token refresh blocked. Source Qt/audio smoke passed with the
  existing build workflow's isolated native DLL PATH, including real digital-
  silence output, settings roundtrips, all four tabs, navigation and fullscreen.
  One earlier source-smoke attempt under the inherited shell PATH encountered a
  native Windows access violation; the clean-PATH retry required no application
  change. This does not establish the exact cause of that first native failure.

- Final 180-second Dynamic file pipeline passed with 22 ordered translation,
  synthesis and output batches, exact original/encoded sample checks and zero
  pending work. The final shared Realtime integration also drained all queues
  with no review work or duplicate commits.

- The final Windows EXE was rebuilt after the last application edit. Packaged
  smoke passed at **100%, 150% and 200%** scaling under the inherited shell
  environment, including all four tabs, settings roundtrips, native SDK imports,
  fullscreen/navigation and real digital-silence output. Narrow 200% navigation
  and 150% fullscreen screenshots were visually checked. **All 34 bundled
  application modules match the final source.** Test host: Windows build 26300;
  Windows 10 was not available.

## Release and limits

The public ZIP uses the existing explicit allowlist and blank-key environment
template. Promotion is gated on source-parity/privacy audit success; the final
result and archive SHA-256 are retained at `build/shared-mixer/release-audit.json`.
Standard
release paths remain `dist-app/ChurchTranslator/ChurchTranslator.exe` beside
`_internal`, and `dist-app/ChurchTranslator-Windows.zip`. The previous release is
retained locally under `build/shared-mixer/rollback/`; installed credentials, settings and private work
are preserved. The code and report are committed/pushed on the existing `main`.

No new paid API requests or private uploads were made. The tests establish PCM
ordering, overlap, bounds and scheduling for their tested cases. They do not
establish audible intelligibility of concurrent voices, natural church acceptance,
physical speaker gaps, real-provider recognition/translation accuracy, a two-hour
hardware endurance run or Windows 10 compatibility. Newly arriving audio can still
wait for decoding, the current hardware buffer and the next 20 ms block; speech
that is not ready is never invented or replaced. Ready consecutive same-language
chunks have no intentional inserted silence. Shared-device failure can affect
that device's lanes; a language-specific provider/decode failure cannot gate the
other language.
