"""Runs Zotify in a child process and reports progress to the GUI.

Zotify's internals are hooked so the GUI gets structured events instead of
scraped terminal text. Every event is one stdout line: EVENT_PREFIX + JSON.
Commands come in on stdin, one per line: "pause" (stop after the current
track) and "resume" (cancel a pending pause).
"""
import json
import os
import re
import sys
import threading
from time import time

EVENT_PREFIX = "@@ZGUI@@"
SPINNER = re.compile(r"^\s*\[[^\]]{3}\]\s*")  # Zotify's "[∙●∙] Logging in..." loader frames

_pause_requested = threading.Event()
_out_lock = threading.Lock()


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


def _install_hooks() -> None:
    from zotify import api, termoutput
    from zotify.termoutput import Printer, Interface

    # --- current track starts / byte progress ------------------------------
    original_fetch = api.DLContent.fetch_stream_content

    def fetch_stream_content(self, stream, temppath, parent_stack):
        album = getattr(self, "album", None)
        image_url = getattr(album, "image_url", None) or getattr(self, "image_url", None)
        artists = getattr(self, "artists", None) or []
        emit("track_start",
             name=getattr(self, "name", str(self)),
             artist=", ".join(getattr(a, "name", str(a)) for a in artists) if artists else "",
             album=getattr(album, "name", "") if album else "",
             duration_ms=getattr(self, "duration_ms", 0),
             size=getattr(stream, "size", 0),
             image_url=image_url)
        return original_fetch(self, stream, temppath, parent_stack)

    api.DLContent.fetch_stream_content = fetch_stream_content

    original_pbar = Printer.pbar

    def pbar(iterable=None, desc=None, total=None, unit='it', *args, **kwargs):
        bar = original_pbar(iterable, desc, total, unit, *args, **kwargs)
        if unit == 'B':
            original_update = bar.update
            state = {"done": 0, "last": 0.0, "start": time()}

            def update(n=1):
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

    # --- pause after current track -----------------------------------------
    original_wait = api.DLContent.wait_between_downloads

    def wait_between_downloads(skip_wait: bool = False):
        if _pause_requested.is_set():
            emit("pausing")
            raise KeyboardInterrupt  # Zotify cleans up and writes m3u8 files
        original_wait(skip_wait)
        if _pause_requested.is_set():
            emit("pausing")
            raise KeyboardInterrupt

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
    import os
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


def run(zotify_args: list[str]) -> int:
    _ensure_std_streams()
    threading.Thread(target=_listen_for_commands, daemon=True).start()
    _hook_after_config_load()
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
    emit("finished", paused=_pause_requested.is_set(), code=code)
    return code
