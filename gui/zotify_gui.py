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
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

WORKER_FLAG = "--zotify-worker"
APP_NAME = "Zotify GUI"
SETTINGS_PATH = Path(os.environ.get("APPDATA", Path.home())) / "ZotifyGUI" / "settings.json"
ZOTIFY_CREDS = Path(os.environ.get("APPDATA", Path.home())) / "Zotify" / "credentials.json"
AUDIO_EXTS = {".aiff", ".flac", ".m4a", ".wav", ".mp3", ".ogg"}
DEFAULT_SETTINGS = {
    "root": str(Path.home() / "Music" / "Zotify"),
    "codec": "aiff",
    "real_time": True,
    "wait": 5,
    "last_url": "",
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
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


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


class Api:
    """Methods exposed to the page as window.pywebview.api.*"""

    def __init__(self):
        self.window = None
        self.proc: subprocess.Popen | None = None
        self.settings = load_settings()
        self._lock = threading.Lock()

    # ---- helpers -----------------------------------------------------------
    def _push(self, event: dict) -> None:
        if self.window:
            self.window.evaluate_js(f"window.onZotifyEvent({json.dumps(event)})")

    def _worker_cmd(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, WORKER_FLAG]
        return [sys.executable, str(Path(__file__).resolve()), WORKER_FLAG]

    def _zotify_args(self, url: str) -> list[str]:
        s = self.settings
        args = [
            "-q", "auto",
            "--codec", s["codec"],
            "-rp", s["root"],
            "-oe", "{playlist}/{artist} - {song_name}",
            "-ol", "Liked Songs/{artist} - {song_name}",
            "-e", "True",
            "--lyrics-to-file", "False",
            "--md-disc-track-totals", "False",
            "--bulk-wait-time", str(s.get("wait", 5)),
        ]
        if s["real_time"]:
            args += ["-rt", "True"]
        return args + [url]

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
            if self.proc is proc:
                self.proc = None
        self._push({"event": "process_exit", "code": proc.returncode})

    def _handle_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "login_required":
            webbrowser.open(event["url"])
        elif kind == "track_done":
            path = Path(event["path"])
            event["cover"] = cover_thumbnail(path)
            event["bytes"] = path.stat().st_size if path.is_file() else 0
            event["artist"], event["title"] = split_track_name(path)
        self._push(event)

    # ---- called from the page ----------------------------------------------
    def get_state(self) -> dict:
        return {
            "settings": self.settings,
            "ffmpeg": find_ffmpeg(),
            "logged_in": ZOTIFY_CREDS.is_file() and ZOTIFY_CREDS.stat().st_size > 0,
            "running": self.proc is not None,
        }

    def update_settings(self, changes: dict) -> dict:
        self.settings.update({k: v for k, v in changes.items() if k in DEFAULT_SETTINGS})
        save_settings(self.settings)
        return self.settings

    def choose_folder(self) -> str | None:
        import webview
        picked = self.window.create_file_dialog(webview.FOLDER_DIALOG, directory=self.settings["root"])
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

    def existing_tracks(self, folder_name: str) -> list[dict]:
        folder = Path(self.settings["root"]) / folder_name
        if not folder.is_dir():
            return []
        tracks = []
        for p in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime):
            if p.suffix.lower() in AUDIO_EXTS:
                artist, title = split_track_name(p)
                tracks.append({"path": str(p), "artist": artist, "title": title, "cover": cover_thumbnail(p)})
        return tracks

    def start(self, url: str) -> dict:
        with self._lock:
            if self.proc is not None:
                return {"ok": False, "error": "A download is already running."}
            ffmpeg = find_ffmpeg()
            if not ffmpeg:
                return {"ok": False, "error": "FFmpeg not found. Install it with: winget install Gyan.FFmpeg"}
            self.update_settings({"last_url": url})
            env = dict(os.environ)
            env["PATH"] = str(Path(ffmpeg).parent) + os.pathsep + env.get("PATH", "")
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUNBUFFERED"] = "1"
            self.proc = subprocess.Popen(
                self._worker_cmd() + self._zotify_args(url),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            threading.Thread(target=self._read_output, args=(self.proc,), daemon=True).start()
        return {"ok": True}

    def _send(self, cmd: str) -> bool:
        proc = self.proc
        if proc is None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
            return True
        except OSError:
            return False

    def pause_after_track(self) -> bool:
        return self._send("pause")

    def cancel_pause(self) -> bool:
        return self._send("resume")

    def stop_now(self) -> bool:
        proc = self.proc
        if proc is None:
            return False
        proc.kill()
        # drop the half-downloaded temp file so the folder stays clean
        for tmp in Path(self.settings["root"]).rglob("*.tmp"):
            try:
                tmp.unlink()
            except OSError:
                pass
        return True

    def open_folder(self, sub: str = "") -> None:
        target = Path(self.settings["root"]) / sub
        if not target.is_dir():
            target = Path(self.settings["root"])
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    def open_link(self, url: str) -> None:
        if url.startswith("https://"):
            webbrowser.open(url)


def run_gui() -> None:
    import webview
    api = Api()
    window = webview.create_window(
        APP_NAME, str(resource_path("ui", "index.html")), js_api=api,
        width=1180, height=860, min_size=(900, 680), background_color="#07020f",
    )
    api.window = window

    def on_closing():
        if api.proc is not None:
            api.stop_now()

    window.events.closing += on_closing
    webview.start(debug=os.environ.get("ZOTIFY_GUI_DEBUG") == "1")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == WORKER_FLAG:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from worker import run
        sys.exit(run(sys.argv[2:]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    run_gui()
