# threads2video

Turns a Threads post (threads.com URL) into a narrated short video for TikTok, Reels or Shorts:

- The post and its top comments, plus the author's own replies, are read aloud by TTS.
- Each one appears as a Threads-style card, timed to its narration.
- Everything plays over a background video, with music and a "pop" as each card appears.
- Texts can be translated with Gemini first. Vietnamese is the default.

It's one click in the desktop app or one command in the terminal, and you can queue many posts at once.

License: GPL-3.0-or-later ([LICENSE](LICENSE)). Architecture, decisions and risks are in [DESIGN.md](DESIGN.md).

---

## Install

### Windows app (no Python needed)

1. Download `threads2video-<version>-windows-amd64.zip`. The CI workflow publishes it as an artifact, or you can build it yourself (see [Building the app](#building-the-app)).
2. Unzip it anywhere and run `threads2video-gui.exe`.
3. On first start the app offers to download the Chromium browser it needs (about 170 MB, once). Builds made with `--bundle-browser` include it already.

FFmpeg is included in the Windows build. The folder also has `threads2video.exe` for the command line.

### From source (Windows, macOS, Linux)

Requires Python 3.11–3.13 and FFmpeg.
- **Windows:** `winget install Gyan.FFmpeg`
- **macOS:** `brew install ffmpeg`
- **Linux:** your distro's `ffmpeg` package (6.1 or newer)

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"                 # Windows: .venv\Scripts\pip install -e ".[dev]"
.venv/bin/python -m playwright install chromium --no-shell
```

Then start `threads2video-gui`, or run the CLI with `threads2video`.

---

## Using the app

```bash
threads2video-gui            # or: python -m threads2video.gui
```

1. Paste one or more post URLs, one per line. Each becomes its own video.
2. Choose a background video and music. Each can be a file, or a folder to pick from at random.
3. Adjust the settings in the four sections:
   1. **Source:** URLs, "Log in to Threads", background video, background music
   2. **Reading:** number of top comments, music volume, TTS engine, default voice, OP voice
   3. **Video & effects:** number of OP replies, reading speed, random voice per author, aspect ratio (9:16 or 16:9), card theme, position and animation, burned-in subtitles
   4. **Language & AI:**
      - reading language, review/edit before reading, hiding names (blur or black)
      - sound effect file or folder, volume and preview button
      - voice preview, "Download Piper voice"
      - background music on/off
      - Gemini translation: on/off, API key, model, "Check key / list models"
4. Press **Generate video**.

While it runs:
- **Progress:** the queue table shows each post's state, the progress bar covers the whole batch, and the log panel shows details.
- **Cancel** stops the batch. A failed post doesn't stop the others; **Retry failed** re-runs just those.
- **Watching a result:** double-click a finished row to play its video, or use **Open output folder**.
- **Reuse already-fetched post data** re-renders with changed settings without contacting Threads again.

**Where things are saved:**
- Settings: `config.yaml` in your user folder (`%APPDATA%\threads2video`, `~/Library/Application Support/threads2video` or `~/.config/threads2video`).
- Videos: `~/Videos/threads2video`, or `~/Movies/threads2video` on macOS.
- Caches, work files, Piper voices and the browser profile live in the app's data folder.

---

## Command line

```bash
threads2video https://www.threads.com/@user/post/CODE -c config.yaml
threads2video URL1 URL2 URL3                # batch: each URL becomes a video
threads2video -f urls.txt                   # batch from a file: one URL per line, # comments allowed
```

Start from [`config.example.yaml`](config.example.yaml): copy it to `config.yaml`, which is git-ignored because it may hold your API key. Without `-c`, `./config.yaml` is used if it exists. The CLI keeps its `cache/`, `work/` and `output/` folders in the current directory.

| Option | Effect |
|---|---|
| `--until fetch\|select\|translate\|tts\|render\|compose` | Stop after a stage |
| `--from-cache` | Use the saved JSON of an earlier fetch; no network |
| `--reuse-segments` | Use `work/<code>/segments.json` as you edited it (see below) |
| `--login` | Open a browser to log in to Threads (needs `fetch.profile_dir`) |
| `--download-piper-voice [KEY]` | List Piper voices for the language, or download one |
| `--install-browser` | Download the Chromium build that Playwright needs |
| `-v` | Verbose logging |

**Reviewing texts in the CLI:** set `selection.review_before_tts: true`. The run then stops after selection (and translation). Edit the `text` fields in `work/<code>/segments.json`, then re-run with `--reuse-segments`.

### What each stage writes

| Stage | Output |
|---|---|
| fetch | `cache/threads/<code>/raw/*.json` (every captured payload) and `thread.json` |
| select / translate | `work/<code>/segments.json` |
| tts | `work/<code>/audio/NN_<id>.wav` (48 kHz mono, silence trimmed, exact durations) |
| render | `work/<code>/cards/NN_<id>.png` (transparent cards) |
| compose | `work/<code>/compose/` (narration, SFX and captions), then `output/<user>_<code>_9x16.mp4` (H.264/AAC) |

Unchanged audio and translations are reused on re-runs.

---

## Configuration reference

All settings live in one YAML file (or JSON), and the GUI edits the same file. Main options:

**Comments** (`selection`)

| Option | Default | Effect |
|---|---|---|
| `top_comments` | 5 | Number of non-OP replies, taken by like count |
| `op_replies` | 2 | Replies written by the post's author. Each follows the comment it answers; that comment is pulled in for context (`op_reply_with_parent`). |
| `min_chars` / `max_chars` | 3 / 400 | Skip replies shorter or longer than this |
| `skip_links` / `skip_emoji_only` | true | Skip replies that contain links, or that are only emoji or symbols |
| `include_self_thread` | false | Also read the author's thread continuation posts |

**Voice** (`tts`)

| Option | Default | Effect |
|---|---|---|
| `provider` | `edge` | `edge`, `piper` or `capcut` |
| `fallback` | `[edge, piper]` | Engines to try when the chosen one fails; unusable ones are skipped |
| `voice` / `op_voice` | HoaiMy / NamMinh | The voice IDs for the chosen engine |
| `random_voice_per_author` | true | Each commenter keeps one voice for the whole video, never the OP's |
| `speed` | 1.10 | Applied afterwards by FFmpeg, so it's exact for every engine |

**Language** (`language`, `translate`)

| Option | Default | Effect |
|---|---|---|
| `language` | `vi` | Reading language: TTS voices and the translation target |
| `translate.enabled` | false | Translate the texts before reading |
| `translate.model` | `gemini-3.5-flash-lite` | Gemini model |
| `translate.api_key` | — | Your key, or set the `GEMINI_API_KEY` environment variable |

**Cards** (`card`)

| Option | Default | Effect |
|---|---|---|
| `theme` | `dark` | `dark` or `light` |
| `identity` | `show` | `show`, `blur` or `black`: hide usernames, avatars and @mentions |
| `width_portrait` / `width_landscape` | 0.88 / 0.55 | Card width as a fraction of the frame width |
| `max_height` | 0.8 | Cap as a fraction of the frame height; long text shrinks to fit |
| `font_scale` | 1.0 | Text size |
| `show_media` / `show_counts` | true | Show the post's image; show like and reply counts |

**Video** (`video`)

| Option | Default | Effect |
|---|---|---|
| `aspect` | `9:16` | 1080×1920 or 1920×1080 |
| `background` | none | File or folder. Cropped to fill, muted, looped if short, started at a random point. With none set, an animated gradient is used. |
| `card_position` / `animation` | center / fade | Position: `center`, `top` or `bottom`. Animation: `fade`, `slide` or `none`. |
| `subtitles` | false | Burned-in captions of the narration |
| `lead_in_s` / `pre_roll_s` / `gap_s` / `tail_s` | 0.2 / 0.15 / 0.35 / 0.8 | Pauses: before the first card, between a card appearing and its narration, between comments, and at the end |
| `crf` / `preset` | 20 / veryfast | x264 quality and speed. Raise `crf` (e.g. 23) for smaller files. |

**Audio** (`audio`)

| Option | Default | Effect |
|---|---|---|
| `music`, `music_enabled`, `music_volume` | –, true, 20 | Background music: file or folder, with fade-in and fade-out |
| `sfx_dir`, `sfx_enabled`, `sfx_volume` | built-in pop, true, 40 | Sound as each card appears: file or folder (random per card) |

**Fetching** (`fetch`)

| Option | Default | Effect |
|---|---|---|
| `profile_dir` | – | Saved browser profile for a logged-in session |
| `cookies_file` | – | Alternative to a profile: exported cookies (JSON or Netscape) |
| `max_scrolls`, `retries`, `timeout_s` | 12, 2, 45 | How hard to try when loading replies |

Narration is levelled per segment, so the mix lands around -16 LUFS whichever voice produced it.

---

## TTS engines

| Engine | Notes |
|---|---|
| **edge** (default) | Microsoft Edge "Read Aloud" voices. Free and needs internet, but the endpoint is undocumented. It has 2 Vietnamese voices; pitch variants give each commenter a distinct voice. The service sometimes returns no audio, so each call is retried up to 5 times, with a hard time limit. |
| **piper** | Offline. Use "Download Piper voice" or `--download-piper-voice vi_VN-vais1000-medium`. That's the only Vietnamese model that works with current Piper. The others (`25hours_single`, `vivos`) would drop tones, so they are flagged and skipped. |
| **capcut** | **Unofficial and unstable.** It uses the reverse-engineered [`capcut_tts_api`](https://github.com/K07VN/capcut-tts-api) package. That package has no license, so it isn't bundled: install it yourself (`pip install git+https://github.com/K07VN/capcut-tts-api`). Any failure falls back to the next engine. |

## Logged-in session (more replies)

Logged out, Threads shows only about the first 10 replies, and the author's replies usually aren't among them. To get more:
- **GUI:** click **Log in to Threads**, log in in the browser window that opens, then close it.
- **CLI:** set `fetch.profile_dir` and run `threads2video --login`.

Later fetches reuse the session. Using a secondary account is recommended. Automated access may break Threads' terms; use modest volumes.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Chromium is not installed" | The GUI offers the download. In the CLI, run `threads2video --install-browser`. |
| "FFmpeg not found" | Install it (see above), or set `ffmpeg_path` to the folder that contains ffmpeg and ffprobe. |
| "Post … not found" | The post is private or deleted, or Threads showed a login wall. Log in (see above). Check `cache/threads/<code>/raw/` to see what Threads returned. |
| Only ~10 replies; no OP replies | Expected when logged out. Log in. |
| Threads changed its page and fetching breaks | The raw JSON is always saved. Re-parse it with `--from-cache` after updating `threads2video/fetcher/parser.py`. |
| "Gemini rejected the API key" / "model not found" | Use **Check key / list models** and pick a model from the list. |
| edge-tts keeps failing | Switch to Piper. Edge automatically falls back to Piper when a Piper voice is installed. |
| Emoji show as boxes on cards (Linux) | Install `fonts-noto-color-emoji`. |

Logs go to the GUI's log panel. They're also saved to `logs/threads2video.log` in the data folder (GUI) or in `cache/` (CLI).

---

## Development

```bash
python -m pytest            # offline: uses an anonymised cached post; no Threads, TTS or Gemini access
```

- **What the tests cover:** URL parsing, the Threads parser, comment selection, config, text cleanup and voice assignment, timing math, audio placement, the FFmpeg command builder, translation (with a mocked API), the GUI (offscreen), and one end-to-end run from the cached fixture to an MP4.
- **What they need:** FFmpeg and Playwright's Chromium. Tests that need either one are skipped when it's missing.
- **Making a new fixture:** `tools/make_fixture.py cache/threads/<code> tests/fixtures/<name>` turns a cached fetch into an anonymised fixture.

### Building the app

```bash
pip install pyinstaller
python packaging/build.py --ffmpeg-dir C:\ffmpeg\bin           # Windows, static FFmpeg build
python packaging/build.py --bundle-browser                       # also bundle Chromium (offline use)
```

This produces:
- `dist/threads2video/` containing `threads2video-gui(.exe)` (windowed) and `threads2video(.exe)` (console)
- `dist/threads2video-<version>-<os>-<arch>.zip`

Notes:
- PyInstaller doesn't cross-compile, so build Windows exes on Windows. [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs the tests on Windows and Linux and builds the Windows zip on every push.
- **FFmpeg:** on Windows, use a static FFmpeg such as Gyan's "essentials" build. Homebrew and Linux distro binaries depend on system libraries and aren't bundled; the app then uses the FFmpeg on `PATH`.

### Layout

```
threads2video/
  fetcher/      urls.py, parser.py (raw JSON → model; offline), browser.py (Playwright, login, cookies)
  selection.py  which replies and in what order
  translate/    gemini.py
  tts/          base.py, edge.py, piper.py, capcut.py, manager.py (fallback, voices, exact WAV), text.py
  render/       cards.py + templates/card.html, captions.py
  compose/      timeline.py (pure timing), audio.py (narration/SFX tracks), video.py (FFmpeg graph)
  gui/          main_window.py, dialogs.py, workers.py, app.py
  pipeline.py   stages for one URL (shared by CLI and GUI)
  config.py, models.py, ffmpeg.py, browser_env.py, appdirs.py, runtime.py, cli.py
packaging/      PyInstaller spec + build script
tools/          make_fixture.py
```

---

## Licenses and third-party notes

- **This project:** GPL-3.0-or-later. It's a clean implementation; ideas come from [RedditVideoMakerBot](https://github.com/elebumm/RedditVideoMakerBot) (GPL-3.0), but no code was copied.
- **Bundled components:**
  - [piper-tts](https://github.com/OHF-Voice/piper1-gpl): GPL-3.0
  - PySide6 / Qt: LGPL-3.0
  - Playwright: Apache-2.0
  - FFmpeg builds such as Gyan's: GPL
- **Piper voices** have their own licenses; see each voice's `MODEL_CARD`. `vi_VN-vais1000-medium` is trained on the VAIS-1000 corpus (CC BY 4.0).
- **Unofficial services:**
  - Threads data is fetched from its web page, not an official API.
  - edge-tts uses an undocumented Microsoft endpoint.
  - The CapCut engine is unofficial.

  Any of these can stop working without notice. Make sure your use, including republishing other people's posts, complies with the relevant terms and with the authors' rights. `card.identity: blur` helps with privacy.
