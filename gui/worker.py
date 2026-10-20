"""Runs Zotify in a child process and reports progress to the GUI.

Zotify's internals are hooked so the GUI gets structured events instead of
scraped terminal text. Every event is one stdout line: EVENT_PREFIX + JSON.
Commands come in on stdin, one per line:
  pause   stop after the current track       resume  cancel a pending pause
  skip    abandon the current track          stop    stop right away
"""
import json
import os
import re
import subprocess
import sys
import threading
from time import time

EVENT_PREFIX = "@@ZGUI@@"
SPINNER = re.compile(r"^\s*\[[^\]]{3}\]\s*")  # Zotify's "[∙●∙] Logging in..." loader frames
CREATE_NO_WINDOW = 0x08000000

_pause_requested = threading.Event()
_skip_requested = threading.Event()
_stop_requested = threading.Event()
_out_lock = threading.Lock()


class SkipTrack(Exception):
    """Raised inside a track's download to abandon it and move to the next."""


def emit(event: str, **data) -> None:
    line = EVENT_PREFIX + json.dumps({"event": event, **data}, default=str)
    with _out_lock:
        sys.__stdout__.write(line + "\n")
        sys.__stdout__.flush()


def _listen_for_commands() -> None:
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd == "pause":
            _pause_requested.set()
            emit("pause_armed")
        elif cmd == "resume":
            _pause_requested.clear()
            emit("pause_disarmed")
        elif cmd == "skip":
            _skip_requested.set()
        elif cmd == "stop":
            _stop_requested.set()
            emit("stopping")


def _hide_console_windows() -> None:
    # FFmpeg is a console program: started from a windowed app it opens its own
    # console window, which steals focus, so start every child process hidden
    if os.name != "nt":
        return
    base = subprocess.Popen

    class HiddenPopen(base):
        def __init__(self, *args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = HiddenPopen


def _install_hooks() -> None:
    from pathlib import Path
    from zotify import api
    from zotify.termoutput import Printer, Interface

    # --- current track starts / byte progress / skip + stop -----------------
    original_fetch = api.DLContent.fetch_stream_content

    def fetch_stream_content(self, stream, temppath, parent_stack):
        album = getattr(self, "album", None)
        image_url = getattr(album, "image_url", None) or getattr(self, "image_url", None)
        artists = getattr(self, "artists", None) or []
        _skip_requested.clear()
        emit("track_start",
             name=getattr(self, "name", str(self)),
             artist=", ".join(getattr(a, "name", str(a)) for a in artists) if artists else "",
             album=getattr(album, "name", "") if album else "",
             duration_ms=getattr(self, "duration_ms", 0),
             size=getattr(stream, "size", 0),
             image_url=image_url,
             temp=str(temppath))
        try:
            return original_fetch(self, stream, temppath, parent_stack)
        except (SkipTrack, KeyboardInterrupt):
            try:
                Path(temppath).unlink(missing_ok=True)
            except OSError:
                pass
            raise

    api.DLContent.fetch_stream_content = fetch_stream_content

    for cls in (api.Track, api.Episode):
        original_download = cls.download

        def download(self, parent_stack, _original=original_download):
            try:
                return _original(self, parent_stack)
            except SkipTrack:
                emit("track_skipped", name=getattr(self, "name", str(self)))

        cls.download = download

    original_pbar = Printer.pbar

    def pbar(iterable=None, desc=None, total=None, unit='it', *args, **kwargs):
        bar = original_pbar(iterable, desc, total, unit, *args, **kwargs)
        if unit == 'B':
            original_update = bar.update
            state = {"done": 0, "last": 0.0, "start": time()}

            def update(n=1):
                if _stop_requested.is_set():
                    raise KeyboardInterrupt  # Zotify cleans up and writes m3u8 files
                if _skip_requested.is_set():
                    _skip_requested.clear()
                    raise SkipTrack
                state["done"] += n
                now = time()
                if now - state["last"] > 0.25 or state["done"] >= (total or 0):
                    state["last"] = now
                    elapsed = max(now - state["start"], 0.001)
                    emit("track_progress", done=state["done"], total=total,
                         rate=state["done"] / elapsed)
                return original_update(n)

            bar.update = update
        elif total or iterable is not None:
            count = total if total is not None else len(iterable)
            emit("total", name=str(desc or ""), count=count)
        return bar

    Printer.pbar = staticmethod(pbar)

    # --- track finished ----------------------------------------------------
    original_complete = Interface.dl_complete

    def dl_complete(dlcontent, path, time_elapsed_dl, time_elapsed_ffmpeg):
        emit("track_done", name=getattr(dlcontent, "name", ""), path=str(path))
        return original_complete(dlcontent, path, time_elapsed_dl, time_elapsed_ffmpeg)

    Interface.dl_complete = staticmethod(dl_complete)

    # --- pause after current track / stop between tracks --------------------
    original_wait = api.DLContent.wait_between_downloads

    def wait_between_downloads(skip_wait: bool = False):
        for _ in range(2):  # check before and after the wait
            if _stop_requested.is_set():
                raise KeyboardInterrupt
            if _pause_requested.is_set():
                emit("pausing")
                raise KeyboardInterrupt  # Zotify cleans up and writes m3u8 files
            if _ == 0:
                original_wait(skip_wait)

    api.DLContent.wait_between_downloads = staticmethod(wait_between_downloads)

    # --- messages (skips, errors, login link) ------------------------------
    original_new_print = Printer.new_print
    last_status = {"text": None}

    def new_print(channel, msg, *args, **kwargs):
        text = str(msg)
        channel_name = getattr(channel, "name", str(channel))
        spinner = SPINNER.match(text)
        if "Click on the following link to login" in text:
            url = text.split("\n", 1)[-1].strip()
            emit("login_required", url=url)
        elif spinner:
            status = text[spinner.end():].strip()
            if status != last_status["text"]:  # spinner frames repeat the same text
                last_status["text"] = status
                emit("status", text=status)
        elif channel_name != "DEBUG":
            emit("message", channel=channel_name, text=text)
        return original_new_print(channel, msg, *args, **kwargs)

    Printer.new_print = staticmethod(new_print)


def _hook_after_config_load() -> None:
    # zotify.api reads the config while it is imported, so hooks can only be
    # installed once Zotify has loaded its config
    from zotify.config import Config
    original_load = Config.load

    def load(cls, args):
        original_load(args)
        _install_hooks()

    Config.load = classmethod(load)

    # keep Zotify's log files out of the download folder, which other tools
    # may be sweeping into a DJ library
    log_dir = os.environ.get("ZOTIFY_GUI_LOG_DIR")
    if log_dir:
        import logging
        from pathlib import Path
        from zotify.config import LogHandler, Zotify

        def start_logger(cls, launch: str):
            logfile = "zotify_" + ("DEBUG_" if Zotify.CONFIG.debug() else "") + f"{launch}.log"
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            cls.LOG_PATH = Path(log_dir) / logfile
            logging.basicConfig(level=logging.DEBUG if Zotify.CONFIG.debug() else logging.CRITICAL,
                                filemode="x", filename=cls.LOG_PATH)
            cls.LOGGER = logging.getLogger("zotify.debug")
            return cls.LOGGER

        LogHandler.start_logger = classmethod(start_logger)


def _ensure_std_streams() -> None:
    # a windowed (no console) exe can start with sys.std* set to None even when
    # the GUI passed it pipes, so reattach them from the raw file descriptors
    for name, fd, mode in (("stdin", 0, "r"), ("stdout", 1, "w"), ("stderr", 2, "w")):
        if getattr(sys, name) is None:
            try:
                stream = open(fd, mode, encoding="utf-8", errors="replace", closefd=False, buffering=1)
            except OSError:
                stream = open(os.devnull, mode)
            setattr(sys, name, stream)
            setattr(sys, f"__{name}__", stream)
        elif hasattr(getattr(sys, name), "reconfigure"):
            # Zotify's spinner characters crash on Windows' default cp1252
            getattr(sys, name).reconfigure(encoding="utf-8", errors="replace")


def _list_playlists_after_login() -> None:
    """Replace the download step with listing the account's playlists, read from
    Spotify's internal playlist service (the Web API is rate limited for
    Zotify's shared client)."""
    from zotify.config import Zotify
    from librespot.proto import Playlist4External_pb2 as P4
    original_boot = Zotify.boot.__func__

    def boot(cls, args):
        original_boot(cls, args)
        session = Zotify.SESSION
        user = session.username()
        playlists, start, page = [], 0, 200
        while True:
            resp = session.api().send(
                "GET", f"/playlist/v2/user/{user}/rootlist?decorate=revision,attributes,length,owner"
                       f"&from={start}&length={page}", None, None)
            if resp.status_code != 200:
                emit("playlists_error", status=resp.status_code)
                sys.exit(1)
            content = P4.SelectedListContent()
            content.ParseFromString(resp.content)
            items, metas = content.contents.items, content.contents.meta_items
            for i, item in enumerate(items):
                if not item.uri.startswith("spotify:playlist:"):
                    continue  # folder markers
                meta = metas[i] if i < len(metas) else None
                picture = meta.attributes.picture.hex() if meta is not None and meta.attributes.picture else ""
                playlists.append({
                    "image": f"https://i.scdn.co/image/{picture}" if picture else None,
                    "id": item.uri.rsplit(":", 1)[-1],
                    "name": meta.attributes.name if meta is not None else "",
                    "count": meta.length if meta is not None else None,
                    "owner": meta.owner_username if meta is not None else "",
                })
            if len(items) < page:
                break
            start += page
        emit("playlists", user=user, items=playlists)
        sys.exit(0)

    Zotify.boot = classmethod(boot)


def run(zotify_args: list[str]) -> int:
    _ensure_std_streams()
    _hide_console_windows()
    listing = "--list-playlists" in zotify_args
    zotify_args = [a for a in zotify_args if a != "--list-playlists"]
    threading.Thread(target=_listen_for_commands, daemon=True).start()
    _hook_after_config_load()
    if listing:
        _list_playlists_after_login()
    from zotify.__main__ import main
    sys.argv = ["zotify", *zotify_args]
    emit("started")
    try:
        main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    except BaseException as e:  # report anything else to the GUI
        emit("crashed", error=f"{type(e).__name__}: {e}")
        code = 1
    else:
        code = 0
    emit("finished", paused=_pause_requested.is_set(), stopped=_stop_requested.is_set(), code=code)
    return code
