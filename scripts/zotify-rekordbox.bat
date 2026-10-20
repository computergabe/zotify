@echo off
REM Zotify preset for DJ software (rekordbox, Serato, Traktor, Engine)
REM   - real-time downloads (playback speed) to lower ban risk
REM   - best source quality (320k, Premium), saved as lossless AIFF
REM   - ID3 tags + cover art, one folder and one .m3u8 per playlist
REM Needs `zotify` and `ffmpeg` on PATH.
REM Usage:
REM   zotify-rekordbox.bat <spotify playlist/album/track URL>
REM   zotify-rekordbox.bat -p      (pick from your playlists; needs a developer client)

zotify ^
  -rt True ^
  -q very_high ^
  --codec aiff ^
  -rp "%USERPROFILE%\Music\Zotify" ^
  -oe "{playlist}/{artist} - {song_name}" ^
  -ol "Liked Songs/{artist} - {song_name}" ^
  -e True ^
  --lyrics-to-file False ^
  --md-disc-track-totals False ^
  --bulk-wait-time 5 ^
  -ip True ^
  %*

pause
