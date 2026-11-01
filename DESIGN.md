# threads2video — design

Turns a Threads post URL into a vertical (or 16:9) narrated short video.

## Reference project: RedditVideoMakerBot

- Repo: <https://github.com/elebumm/RedditVideoMakerBot>. It exists, it is active (last push 2026-10-30, ~12.5k stars), and its license is **GPL-3.0**.
- What it does: `reddit/subreddit.py` uses praw to fetch the post and comments. `video_creation/screenshot_downloader.py` takes Playwright screenshots of the live reddit DOM. `TTS/engine_wrapper.py` runs one TTS call per comment and measures each duration with MoviePy. `video_creation/final_video.py` builds an ffmpeg-python graph that concats the audio and chains `overlay(enable='between(t,a,b)')` per screenshot. The background is cut with MoviePy at a random offset.
- Ideas worth keeping:
  - one audio file per segment, with its measured duration driving the overlay window
  - the `overlay ... enable=between(t,…)` filter chain
  - a random background start offset
  - text clean-up before TTS (strip URLs, add sentence-final periods)
- Ideas we drop:
  - global `settings.config` dict access everywhere
  - Reddit/praw coupling in every module
  - screenshotting the live site (fragile, login walls, and it can't translate or blur)
  - MoviePy for cutting (slow)
  - `os.system` ffmpeg calls

**Decision: write a clean implementation and reuse ideas only, not code.** Very little of the reference survives the Reddit → Threads change. Threads has no praw equivalent, we render our own cards instead of screenshotting, and we need translation, a pluggable TTS, and a GUI. A fork would carry Reddit-specific code and its global-config design, and copying any of its code would make this project a GPL-3.0 derivative. Because we copy no code, the license of this project is our choice.

**Licensing caveat:** the maintained Piper package (`piper-tts` 1.8, from `OHF-Voice/piper1-gpl`) is itself **GPL-3.0-or-later**. The original MIT `rhasspy/piper` is archived. A PyInstaller exe that bundles `piper-tts` is therefore a GPL-covered distribution either way. Open decision for the owner:
- (a) license this project GPL-3.0-or-later (simplest), or
- (b) keep it permissive (e.g. MIT) and don't bundle Piper: users install it separately, and we call it as an external process.

**Owner decision (2026-11-01): the project is licensed GPL-3.0-or-later, and Piper is bundled.**

## TTS findings (milestone 2)

- **Piper Vietnamese:** only `vi_VN-vais1000-medium` works with piper-tts 1.8. `25hours_single-low` and `vivos-x_low` were trained without the tone digits (`2`, `6`, …) that current espeak-ng emits, so they would read Vietnamese without tones. The provider detects this from the small `.onnx.json` and skips such models. The voice-download listing flags them before download.
- **Speed:** Piper's `length_scale` isn't proportional (0.7 gives only 14% shorter audio). edge-tts returned "no audio" 3 of 10 times when a `rate` was set, versus 0 of 10 without one. So every provider synthesizes at 1.0 and the manager applies `atempo`. Measured error versus the expected duration is under 15 ms.
- **Durations:** each segment is converted to 48 kHz mono s16 WAV, and its duration is read from the WAV header (frames / rate). Leading and trailing silence is trimmed (edge pads about 0.9 s), so the composer's `gap_s` alone controls spacing.
- **Voice variety:** voice "slots" (OP, default, or author index) don't depend on the provider. Each provider maps a slot to its own pool, so a fallback provider still keeps authors consistent. Piper single-speaker models get pitch variants (±6%, +12% via `asetrate`); edge uses its native pitch offsets.
- **CapCut:** the only maintained client (`K07VN/capcut-tts-api`) has no license, so it can't be bundled or vendored in a GPL project. `tts/capcut.py` is an adapter that only works if the user installs that package themselves.

## Card rendering (milestone 3)

- **Template:** `render/templates/card.html` holds the layout, CSS for both themes, self-drawn SVG icons (not Meta assets), and a `renderCard(data)` JS function. The page is loaded once and re-filled per card, so a whole video's cards render in about a second.
- **Scaling:** the card is laid out at 420 CSS px. The device scale factor is set so the PNG is exactly `width_portrait × 1080` (or `width_landscape × 1920`) pixels wide, which keeps text sharp instead of upscaled.
- **Transparency:** `#wrap` is screenshotted with `omit_background`, so corners and the shadow margin are transparent.
- **Fitting:** text shrinks in 0.5 px steps down to 12 CSS px. If the card still doesn't fit, the image is dropped. As a last resort, text is clamped with `-webkit-line-clamp` and a warning is logged, because the narration then says more than the card shows.
- **Images:** avatars and images are downloaded right before rendering, because Threads CDN URLs are signed and expire. They're cached in `work/<code>/assets/` and passed in as data URIs. A failed download falls back to an initial-letter avatar.

## Composer (milestone 4)

- **One FFmpeg pass:**
  - The background is looped with `-stream_loop -1 -ss <random>`, then `scale…increase,crop`.
  - The narration and SFX tracks are built in Python.
  - Music is looped, set to its volume, faded in and out, and trimmed to the video length.
  - Cards and captions come next.
  - The audio goes through `amix normalize=0` and then `alimiter`.
- **Cards and captions:** each one is an input of the form `-itsoffset start -loop 1 -framerate fps -t visible -i png`, overlaid with `eof_action=pass`. FFmpeg passes the background through until the card's first frame and only decodes the PNG while the card is on screen; this was verified on the real output. Fade uses `fade=…:alpha=1` at the card's start time. Slide uses a time-dependent overlay `y`.
- **Audio placement:** narration and SFX are placed at exact sample offsets in numpy. On the real post, cross-correlating the output against the source showed every segment and pop within +5 ms of its target. That constant 5 ms is the limiter's look-ahead, well under one frame.
- **Loudness:** each segment is normalised to -14 dBFS RMS over its voiced 20 ms blocks, with peaks capped at 0.95. Before this, edge-tts output mixed to about -24 LUFS; the final mix now measures about -16 LUFS.
- **No libass:** this machine's FFmpeg build, like many Windows builds, lacks libass. So captions are rendered as PNGs with Chromium, using the same overlay mechanism as cards. Caption text is sized by the frame's short side, and cards are capped at 68% of the frame height when captions are on.
- **Progress and cancel:** `FFmpeg.run_progress` parses `-progress pipe:1` (`out_time_us`) into 0..1 and terminates FFmpeg when `cancel()` returns true.

## Translation and GUI (milestone 5)

**Translation**
- **API:** plain `httpx` calls to `v1beta/models/{model}:generateContent`, with `responseMimeType: application/json` and an array-of-strings `responseSchema`. There is no SDK, which keeps the packaged app smaller.
- **Default model:** `gemini-3.5-flash-lite`, the current stable Flash-Lite according to the models page on 2026-11-01. The docs list no `-latest` alias for it.
- **Batching:** up to 25 texts or 8,000 characters per request. If the reply has the wrong number of items, the batch is retried one text at a time.
- **Retries:** 429 and 5xx responses are retried, honouring `Retry-After`.
- **Errors:** a bad key or an unknown model gives a clear message. The bad-key message was checked against the live API with an invalid key.
- **Untested:** actual translation with a valid key; there wasn't one available.

**GUI**
- PySide6. All slow work runs in `QThread`s (`gui/workers.py`): the pipeline job, voice-list loading, previews, the Piper download, the Gemini check, and login.
- Log records cross threads through a `logging.Handler` that emits a Qt signal.
- Review-before-TTS blocks the worker on a `threading.Event` while the dialog runs on the UI thread.
- Cancel sets a flag that the pipeline checks on every progress report. FFmpeg is terminated mid-encode. A running Playwright fetch finishes its page load first, which takes a few seconds.
- The GUI uses per-user folders (`appdirs.py`); the CLI keeps paths relative to the working directory.

## Batch mode and packaging (milestone 6)

**Batch mode**
- **Input:** the GUI URL box takes one or more URLs, one per line, with duplicates removed. The CLI takes several URLs or `-f urls.txt`.
- **Queue:** jobs run one after another in the same worker. A failure is recorded per row (the error shows as a tooltip) and the queue continues.
- **After a batch:** "Retry failed" re-runs only the failed rows. Background and music are picked again for each video.

**One browser**
- Every launch uses Playwright's full Chromium (`channel="chromium"`, new headless mode). One ~170 MB download covers headless fetching and rendering as well as the headed login window, so the separate headless-shell build is never needed.
- **Packaged app:** the browser goes into the user data folder on first run (`browser_env.install_browser`, which runs the bundled Playwright driver), or comes bundled with `--bundle-browser`.
- **Verified:** installing into an empty folder and launching from it, both from source and from the frozen CLI.

**PyInstaller**
- `packaging/threads2video.spec` builds two executables in one folder: a windowed GUI and a console CLI. They come from two `Analysis` objects sharing one `COLLECT`; PyInstaller 6 dropped `MERGE`.
- **Collected by hand:** Playwright's Node.js driver (no hook exists) and Piper's `espeakbridge` plus `espeak-ng-data`.
- **Verified on macOS:**
  - the frozen CLI produced a full video (edge TTS, Piper TTS, Chromium cards, FFmpeg)
  - the frozen GUI starts cleanly
- **Not verified:** the Windows build. It's only built by CI (`.github/workflows/ci.yml`), which hasn't run yet.

**Bugs fixed while testing**
- **FFmpeg crashed in the frozen app:** PyInstaller sets `DYLD_LIBRARY_PATH`/`LD_LIBRARY_PATH` to the bundle, so the system ffmpeg loaded Qt's bundled libavutil and failed with `Symbol not found: _av_vlog`. `runtime.restore_child_env()` restores the original values (`*_ORIG`) for all child processes at startup.
- **edge-tts:**
  - The service randomly returns no audio, 30–60% of the time on some words such as names. It now gets 5 quick attempts.
  - One websocket hung for over 10 minutes despite edge-tts's own timeouts; a hard `asyncio.wait_for` limit now applies.
  - The GUI always configures a fallback engine.
- **Console windows on Windows:** FFmpeg subprocesses use `CREATE_NO_WINDOW`, so the windowed app doesn't flash a console per call.

## Folder structure

```
thread2vid/
├── pyproject.toml            # deps, entry points (threads2video, threads2video-gui)
├── README.md
├── DESIGN.md
├── config.example.yaml       # committed; real config.yaml is git-ignored (holds API key)
├── threads2video/
│   ├── __main__.py           # `python -m threads2video URL --config config.yaml`
│   ├── cli.py
│   ├── config.py             # pydantic AppConfig ⇄ YAML/JSON, env overrides for secrets
│   ├── models.py             # Author, MediaItem, Post, ThreadData, Segment, TimelineEntry
│   ├── log.py                # logging setup (console + file + GUI handler)
│   ├── retry.py              # small retry/backoff helper
│   ├── pipeline.py           # orchestrates the stages, progress callback, cancel flag
│   ├── fetcher/
│   │   ├── urls.py           # parse/normalise threads.com / threads.net URLs
│   │   ├── parser.py         # pure: raw JSON payloads → ThreadData (offline-testable)
│   │   └── browser.py        # Playwright: load page, capture JSON, scroll, login helper
│   ├── selection.py          # top-N by likes + M OP replies, filters           (M2)
│   ├── translate/gemini.py   # batch translate, keeps original                 (M5)
│   ├── tts/                  # base.py (TTSProvider), piper.py, edge.py,
│   │                         # capcut.py (unstable), manager.py (fallback chain, voice assignment)  (M2)
│   ├── render/               # cards.py + templates/card.html (Playwright → transparent PNG)  (M3)
│   ├── compose/              # timeline.py (pure timing math), ffmpeg.py (filtergraph builder + runner)  (M4)
│   └── gui/                  # PySide6 main window, workers (QThread), batch queue  (M5/M6)
├── tests/
│   ├── fixtures/thread_sample/   # anonymised raw payloads from a real post
│   └── test_*.py
└── tools/make_fixture.py     # turns a cache dir into an anonymised test fixture
```

## Module interfaces

```python
# fetcher
def fetch_thread(url: str, opts: FetchConfig, *, cache_dir: Path) -> ThreadData
def parse_payloads(payloads: list[Any], *, code: str, source_url: str) -> ThreadData   # pure
def login_interactive(profile_dir: Path) -> None   # opens a headed browser so the user can log in once

# selection (M2)
def select_segments(thread: ThreadData, cfg: SelectionConfig) -> list[Segment]
#   Segment: id, kind ("post"|"reply"|"op_reply"), author, text_original, text (editable/translated), likes

# translation (M5)
class Translator(Protocol):
    def translate(self, texts: list[str], target_lang: str) -> list[str]

# tts (M2)
class TTSProvider(Protocol):
    name: str
    stable: bool
    def voices(self, lang: str) -> list[Voice]
    def synthesize(self, text: str, voice: Voice, out: Path, *, speed: float) -> Path
class TTSManager:                   # fallback chain + per-author voice assignment
    def synthesize_segments(self, segs: list[Segment], out_dir: Path) -> list[AudioClip]  # AudioClip(path, duration_s)

# render (M3)
def render_cards(segs: list[Segment], thread: ThreadData, style: CardStyle, out_dir: Path) -> list[Path]

# compose (M4)
def build_timeline(durations: list[float], *, lead_in: float, gap: float) -> list[TimelineEntry]  # pure
def compose(timeline, cards, audio, cfg: VideoConfig, out: Path, progress: Callable[[float], None]) -> Path
```

Each stage writes its outputs into `work/<post-code>/{raw,audio,cards}/`. A failed run can then be resumed, and every intermediate file can be inspected.

## Assumptions

- Python 3.11–3.13 for development (3.12 venv here). The system Python 3.15 lacks wheels for some deps (onnxruntime).
- FFmpeg comes from `PATH` or `config.ffmpeg_path`. Windows builds bundle `ffmpeg.exe` next to the exe. We call it through `subprocess`, not MoviePy.
- "Top comments" means direct and nested replies sorted by `like_count`. Threads' own ranking isn't exposed.
- "OP replies" means replies inside the reply threads whose author is the post author. The OP's own self-thread continuation is kept separately (`ThreadData.self_thread`) and can be included as extra post text.

## Risks

1. **Threads scraping stability (high).** There's no public API for reading other people's replies. The official Threads Graph API only covers your own media and keyword search. Mitigations:
   - We never hard-code GraphQL `doc_id`s or query names.
   - We passively capture every JSON payload (embedded `<script type="application/json">` blobs and `/api/graphql` responses).
   - We find posts structurally: dicts with `pk` + `user` + `text_post_app_info`.
   - We classify them by their position in the tree (`direct_replies`, `self_thread`; skipping `relatedPosts`/`quoted_post`).
   - Raw payloads are always cached, so a schema change can be diagnosed and the parser fixed offline against the cache.

   Expect breakage roughly every few months anyway.
2. **Logged-out reply cap.** On 2026-11-01 a logged-out session saw 10 of 19 direct replies. Scrolling didn't paginate further; `has_next_page` stayed true. Getting more replies needs a logged-in persistent profile. Scraping a logged-in session also carries Meta ToS and account-ban risk: use a secondary account and modest request rates.
3. **CapCut TTS (unofficial).** It relies on reverse-engineered endpoints and tokens. It can break or be revoked at any time, and it may violate ByteDance's terms. It ships disabled by default, is marked `stable=False`, and any error falls through to edge-tts.
4. **edge-tts** uses Microsoft's Edge "Read Aloud" endpoint. It's free but undocumented, and Microsoft has throttled it before. It only has 2 Vietnamese voices (HoaiMy F, NamMinh M), so "random voice per author" uses pitch/rate variation on top of them. Piper `vi_VN-vivos-x_low` is multi-speaker (65 speakers) and gives real variety.
5. **Windows-first, developed on macOS.** Paths, the ffmpeg binary, fonts (emoji in cards) and PyInstaller + Playwright browser bundling need a check on a real Windows machine.
6. **Gemini model names change.** The model is a free-text config field with a default. We validate it by listing models when the key is entered in the GUI.
