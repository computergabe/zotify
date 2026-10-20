"""Zotify GUI - a desktop front end for the computergabe/zotify fork.

Run directly (python gui/zotify_gui.py) or as the packaged ZotifyGUI.exe.
The same executable doubles as the download worker when started with
--zotify-worker, so the packaged app needs no separate Python install.
"""
import base64
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORKER_FLAG = "--zotify-worker"
APP_NAME = "Zotify GUI"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _app_dir() -> Path:
    """Data folder kept next to the app (portable); %APPDATA% if that isn't writable."""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    portable = base / "ZotifyGUI-data"
    try:
        portable.mkdir(exist_ok=True)
        probe = portable / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return portable
    except OSError:
        return Path(os.environ.get("APPDATA", Path.home())) / "ZotifyGUI"


APP_DIR = _app_dir()
SETTINGS_PATH = APP_DIR / "settings.json"
PLAYLIST_CACHE = APP_DIR / "playlists.json"
# the app keeps its own Zotify config, login and download history next to
# itself, separate from command-line Zotify (%APPDATA%\Zotify)
ZOTIFY_DIR = APP_DIR / "zotify"
ZOTIFY_CREDS = ZOTIFY_DIR / "credentials.json"
ZOTIFY_HISTORY = ZOTIFY_DIR / ".song_archive"
LOG_DIR = APP_DIR / "logs"
CLI_ZOTIFY_DIR = Path(os.environ.get("APPDATA", Path.home())) / "Zotify"
AUDIO_EXTS = {".aiff", ".flac", ".m4a", ".wav", ".mp3", ".ogg"}
SETTINGS_VERSION = 2
DEFAULT_SETTINGS = {
    "root": str(Path.home() / "Downloads" / "Zotify"),
    "codec": "flac",
    # "lossless" asks Spotify for FLAC; Zotify falls back to 320k Vorbis until
    # its upstream supports lossless streams
    "quality": "auto",
    "real_time": True,
    "wait": 5,
    "last_url": "",
    "version": SETTINGS_VERSION,
}


def resource_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base.joinpath(*parts)


def find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [Path(sys.executable).parent / "ffmpeg.exe"]
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet"
    candidates += [Path(p) for p in glob.glob(str(winget / "Packages" / "*FFmpeg*" / "**" / "bin" / "ffmpeg.exe"), recursive=True)]
    candidates += [winget / "Links" / "ffmpeg.exe"]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def load_settings() -> dict:
    try:
        stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)
    if stored.get("version", 1) < 2:
        stored["codec"] = "flac"  # v2 switched the default from AIFF to FLAC
    stored["version"] = SETTINGS_VERSION
    return {**DEFAULT_SETTINGS, **stored}


def save_settings(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def prepare_zotify_dir() -> None:
    """Create the app's Zotify folder; on first use, copy the login and download
    history from command-line Zotify so there's no new login or re-downloading."""
    ZOTIFY_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("credentials.json", ".song_archive"):
        mine, cli = ZOTIFY_DIR / name, CLI_ZOTIFY_DIR / name
        if not (mine.is_file() and mine.stat().st_size) and cli.is_file() and cli.stat().st_size:
            shutil.copy2(cli, mine)


def kill_tree(pid: int) -> None:
    """End a process and its children (the packaged worker is a launcher plus
    the real process, and FFmpeg may be running under it)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def cover_thumbnail(path: Path, size: int = 200) -> str | None:
    """Embedded cover art of an audio file as a small JPEG data URI."""
    try:
        import mutagen
        from PIL import Image
        audio = mutagen.File(path)
        data = None
        tags = getattr(audio, "tags", None) or {}
        for key in list(tags.keys()):
            if str(key).startswith("APIC"):
                data = tags[key].data
                break
            if key == "covr":
                data = bytes(tags[key][0])
                break
        if data is None and getattr(audio, "pictures", None):
            data = audio.pictures[0].data
        if data is None:
            return None
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((size, size))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def split_track_name(path: Path) -> tuple[str, str]:
    stem = path.stem
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return artist, title
    return "", stem


# ---- live listening ------------------------------------------------------------
# While a track downloads in real time, Zotify writes the decrypted Ogg Vorbis
# stream to a .tmp file. The app serves that growing file like an internet radio
# stream: the Vorbis header pages, then audio from a few seconds behind the
# download position, then new data as it arrives.

def open_shared(path: Path):
    """Open for reading without blocking Zotify from deleting or renaming the
    file (Python's open() on Windows doesn't allow delete sharing)."""
    if os.name != "nt":
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    GENERIC_READ, SHARE_ALL, OPEN_EXISTING = 0x80000000, 0x1 | 0x2 | 0x4, 3
    handle = kernel32.CreateFileW(str(path), GENERIC_READ, SHARE_ALL, None, OPEN_EXISTING, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise FileNotFoundError(ctypes.get_last_error(), "cannot open", str(path))
    return os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), "rb")


def _ogg_pages(data: bytes, start: int = 0):
    """Yield (offset, length, granule) of complete Ogg pages in data from start."""
    pos = start
    while pos + 27 <= len(data) and data[pos:pos + 4] == b"OggS":
        nsegs = data[pos + 26]
        if pos + 27 + nsegs > len(data):
            return
        length = 27 + nsegs + sum(data[pos + 27:pos + 27 + nsegs])
        if pos + length > len(data):
            return
        granule = int.from_bytes(data[pos + 6:pos + 14], "little", signed=True)
        yield pos, length, granule
        pos += length


def _ogg_header_span(path: Path) -> tuple[int, int] | None:
    """(start, end) of the Vorbis header pages. Spotify files can have a small
    non-Ogg prefix, so the stream starts at the first real Ogg page."""
    with open_shared(path) as f:
        data = f.read(256 * 1024)
    start = data.find(b"OggS")
    if start < 0:
        return None
    for offset, length, granule in _ogg_pages(data, start):
        if granule > 0:  # first audio page: everything before it is header
            return start, offset
    return None


class LiveAudio:
    # start this far behind the download: real-time mode downloads at the
    # track's average bitrate, so busy passages of variable-bitrate Vorbis
    # would otherwise catch up with the download and stutter
    LEAD_SECONDS = 18

    def __init__(self):
        self.path: Path | None = None
        self.rate = 40_000  # bytes/s, updated from progress events
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/live"

    def _handler(self):
        live = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if not self.path.startswith("/live"):
                    self.send_error(404)
                    return
                path = live.path
                span = None
                for _ in range(40):  # the header pages appear within moments of a track starting
                    if path and path.is_file():
                        span = _ogg_header_span(path)
                        if span:
                            break
                    time.sleep(0.25)
                    path = live.path
                if not span:
                    self.send_error(404, "Nothing downloading right now")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/ogg")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    live._stream(path, span, self.wfile)
                except (ConnectionError, OSError):
                    pass

        return Handler

    def _stream(self, path: Path, span: tuple[int, int], out) -> None:
        header_start, header_end = span
        lead = self.LEAD_SECONDS * self.rate
        # early in a track, wait until a full lead's worth has downloaded
        deadline = time.time() + self.LEAD_SECONDS + 5
        while (self.path == path and time.time() < deadline
               and path.is_file() and path.stat().st_size < header_end + lead):
            time.sleep(0.25)
        if self.path != path or not path.is_file():
            return
        with open_shared(path) as f:
            f.seek(header_start)
            out.write(f.read(header_end - header_start))
            size = path.stat().st_size
            target = max(header_end, size - lead)
            f.seek(target)
            chunk = f.read(max(size - target, 0))
            # resume at a page boundary at or after the target
            boundary = chunk.find(b"OggS")
            if boundary < 0:
                f.seek(header_end)
            else:
                f.seek(target + boundary)
            idle = 0.0
            while True:
                data = f.read(64 * 1024)
                if data:
                    out.write(data)
                    out.flush()
                    idle = 0.0
                    continue
                # track finished, skipped or a new one started: end this stream
                if self.path != path or not path.exists() or idle > 15:
                    return
                time.sleep(0.15)
                idle += 0.15


class Api:
    """Methods exposed to the page as window.pywebview.api.*"""

    def __init__(self):
        # underscore names: pywebview exposes every public attribute to the page and
        # walks into it, which hangs the UI thread when it reaches the native window
        self._window = None
        self._proc: subprocess.Popen | None = None
        self._settings = load_settings()
        self._lock = threading.Lock()
        self._live = LiveAudio()

    # ---- helpers -----------------------------------------------------------
    def _push(self, event: dict) -> None:
        if self._window:
            self._window.evaluate_js(f"window.onZotifyEvent({json.dumps(event)})")

    def _worker_cmd(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, WORKER_FLAG]
        return [sys.executable, str(Path(__file__).resolve()), WORKER_FLAG]

    def _worker_env(self, ffmpeg: str | None) -> dict:
        env = dict(os.environ)
        env["ZOTIFY_GUI_LOG_DIR"] = str(LOG_DIR)
        if ffmpeg:
            env["PATH"] = str(Path(ffmpeg).parent) + os.pathsep + env.get("PATH", "")
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _login_args(self) -> list[str]:
        return ["-c", str(ZOTIFY_DIR), "--creds", str(ZOTIFY_DIR), "--song-archive-location", str(ZOTIFY_DIR)]

    def _zotify_args(self, url: str) -> list[str]:
        s = self._settings
        args = self._login_args() + [
            "-q", s.get("quality", "auto"),
            "--codec", s["codec"],
            "-rp", s["root"],
            "-oe", "{playlist}/{artist} - {song_name}",
            "-ol", "Liked Songs/{artist} - {song_name}",
            "-e", "True",
            "--lyrics-to-file", "False",
            "--md-disc-track-totals", "False",
            "--bulk-wait-time", str(s.get("wait", 5)),
            # download in playlist order (Zotify otherwise sorts by duration)
            "--optimized-downloading", "False",
            # skip by the app's download history, so tracks moved out of the
            # download folder (e.g. into a DJ library) aren't re-downloaded
            "-ip", "True",
        ]
        if s["real_time"]:
            args += ["-rt", "True"]
        return args + [url]

    def _clean_temp_files(self) -> None:
        for tmp in Path(self._settings["root"]).rglob("*.tmp"):
            try:
                tmp.unlink()
            except OSError:
                pass

    def _read_output(self, proc: subprocess.Popen) -> None:
        from worker import EVENT_PREFIX
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line.startswith(EVENT_PREFIX):
                try:
                    event = json.loads(line[len(EVENT_PREFIX):])
                except ValueError:
                    continue
                self._handle_event(event)
            elif line.strip():
                self._push({"event": "log", "text": line})
        proc.wait()
        with self._lock:
            if self._proc is proc:
                self._proc = None
        self._live.path = None
        self._clean_temp_files()
        self._push({"event": "process_exit", "code": proc.returncode})

    def _handle_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "login_required":
            webbrowser.open(event["url"])
        elif kind == "track_start":
            self._live.path = Path(event["temp"]) if event.get("temp") else None
            event["live_url"] = self._live.url
        elif kind == "track_progress" and event.get("rate"):
            self._live.rate = max(16_000, int(event["rate"]))
        elif kind in ("track_done", "track_skipped"):
            self._live.path = None
            if kind == "track_done":
                path = Path(event["path"])
                event["cover"] = cover_thumbnail(path)
                event["bytes"] = path.stat().st_size if path.is_file() else 0
                event["artist"], event["title"] = split_track_name(path)
        self._push(event)

    def _send(self, cmd: str) -> bool:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
            return True
        except OSError:
            return False

    # ---- called from the page ----------------------------------------------
    def get_state(self) -> dict:
        prepare_zotify_dir()
        return {
            "settings": self._settings,
            "ffmpeg": find_ffmpeg(),
            "logged_in": ZOTIFY_CREDS.is_file() and ZOTIFY_CREDS.stat().st_size > 0,
            "running": self._proc is not None,
        }

    def update_settings(self, changes: dict) -> dict:
        self._settings.update({k: v for k, v in changes.items() if k in DEFAULT_SETTINGS})
        save_settings(self._settings)
        return self._settings

    def choose_folder(self) -> str | None:
        import webview
        picked = self._window.create_file_dialog(webview.FOLDER_DIALOG, directory=self._settings["root"])
        if picked:
            self.update_settings({"root": picked[0]})
            return picked[0]
        return None

    def playlist_info(self, url: str) -> dict:
        """Title and artwork for a Spotify link, via Spotify's public oEmbed endpoint."""
        url = url.strip().split("?")[0]
        if not re.match(r"^https://open\.spotify\.com/(intl-[a-z]+/)?(playlist|album|track|artist)/[A-Za-z0-9]+$", url):
            return {"ok": False, "error": "That doesn't look like a Spotify playlist, album, track or artist link."}
        try:
            q = urllib.parse.urlencode({"url": url})
            with urllib.request.urlopen(f"https://open.spotify.com/oembed?{q}", timeout=8) as r:
                data = json.loads(r.read().decode())
            return {"ok": True, "url": url, "title": data.get("title", ""), "image": data.get("thumbnail_url")}
        except Exception:
            return {"ok": True, "url": url, "title": "", "image": None}

    def my_playlists(self, refresh: bool = False) -> dict:
        """The account's playlists, cached; refresh=True asks Spotify again
        (takes about half a minute, mostly logging in)."""
        if not refresh:
            try:
                return {"ok": True, **json.loads(PLAYLIST_CACHE.read_text(encoding="utf-8"))}
            except (OSError, ValueError):
                pass
        prepare_zotify_dir()
        from worker import EVENT_PREFIX
        try:
            out = subprocess.run(
                self._worker_cmd() + ["--list-playlists"] + self._login_args(),
                stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=self._worker_env(None), timeout=180,
                creationflags=CREATE_NO_WINDOW,
            ).stdout
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Spotify took too long to answer. Try again."}
        for line in out.splitlines():
            if line.startswith(EVENT_PREFIX):
                event = json.loads(line[len(EVENT_PREFIX):])
                if event.get("event") == "playlists":
                    data = {"user": event["user"], "items": event["items"], "fetched": time.time()}
                    PLAYLIST_CACHE.write_text(json.dumps(data), encoding="utf-8")
                    return {"ok": True, **data}
                if event.get("event") == "login_required":
                    return {"ok": False, "error": "Log in first: start any download once to sign in to Spotify."}
        return {"ok": False, "error": "Couldn't load your playlists. Check the log in Settings."}

    def existing_tracks(self, folder_name: str) -> list[dict]:
        """Tracks of this playlist already downloaded: files still in the folder,
        plus ones listed in Zotify's history that have since been moved out."""
        folder = Path(self._settings["root"]) / folder_name
        tracks, seen = [], set()
        if folder.is_dir():
            for p in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime):
                if p.suffix.lower() in AUDIO_EXTS:
                    artist, title = split_track_name(p)
                    tracks.append({"path": str(p), "artist": artist, "title": title, "cover": cover_thumbnail(p)})
                    seen.add(p.stem.lower())
        try:
            history = ZOTIFY_HISTORY.read_text(encoding="utf-8").splitlines()
        except OSError:
            history = []
        for line in history:
            cols = line.split("\t")
            if len(cols) < 5:
                continue
            p = Path(cols[4])
            if p.parent.name.lower() == folder_name.lower() and p.stem.lower() not in seen:
                seen.add(p.stem.lower())
                tracks.append({"path": str(p), "artist": cols[2], "title": cols[3], "cover": None})
        return tracks

    def start(self, url: str) -> dict:
        with self._lock:
            if self._proc is not None:
                return {"ok": False, "error": "A download is already running."}
            ffmpeg = find_ffmpeg()
            if not ffmpeg:
                return {"ok": False, "error": "FFmpeg not found. Install it with: winget install Gyan.FFmpeg"}
            self.update_settings({"last_url": url})
            prepare_zotify_dir()
            self._proc = subprocess.Popen(
                self._worker_cmd() + self._zotify_args(url),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", env=self._worker_env(ffmpeg),
                creationflags=CREATE_NO_WINDOW,
            )
            threading.Thread(target=self._read_output, args=(self._proc,), daemon=True).start()
        return {"ok": True}

    def pause_after_track(self) -> bool:
        return self._send("pause")

    def cancel_pause(self) -> bool:
        return self._send("resume")

    def skip_track(self) -> bool:
        return self._send("skip")

    def stop_now(self) -> bool:
        """Stop right away: ask the worker to stop cleanly, and end the whole
        process tree if it hasn't within a few seconds (e.g. while logging in)."""
        proc = self._proc
        if proc is None:
            return False
        self._send("stop")

        def enforce():
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid)

        threading.Thread(target=enforce, daemon=True).start()
        return True

    def open_folder(self, sub: str = "") -> None:
        target = Path(self._settings["root"]) / sub
        if not target.is_dir():
            target = Path(self._settings["root"])
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    def open_link(self, url: str) -> None:
        if url.startswith("https://"):
            webbrowser.open(url)


def run_gui() -> None:
    import webview
    api = Api()
    # open at a size that fits the screen (taskbar and title bar included)
    width, height = 1180, 860
    try:
        screen = webview.screens[0]
        width = min(width, int(screen.width * 0.9))
        height = min(height, int(screen.height * 0.85))
    except Exception:
        pass
    window = webview.create_window(
        APP_NAME, str(resource_path("ui", "index.html")), js_api=api,
        width=width, height=height, min_size=(760, 560), resizable=True,
        background_color="#07020f",
    )
    api._window = window

    def on_closing():
        if api._proc is not None:
            kill_tree(api._proc.pid)
            api._clean_temp_files()

    window.events.closing += on_closing
    webview.start(debug=os.environ.get("ZOTIFY_GUI_DEBUG") == "1")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if len(sys.argv) > 1 and sys.argv[1] == WORKER_FLAG:
        from worker import run
        sys.exit(run(sys.argv[2:]))
    run_gui()
