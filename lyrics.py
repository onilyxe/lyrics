#!/usr/bin/env python3

import argparse
import logging
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

try:
    import requests
except ImportError:
    print("Потрібен requests: pip install requests")
    sys.exit(1)

try:
    import mutagen
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, USLT, SYLT, Encoding
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
except ImportError:
    print("Потрібен mutagen: pip install mutagen")
    sys.exit(1)

try:
    import lyricsgenius
except ImportError:
    lyricsgenius = None

GENIUS_TOKEN = "0000000000000000000000000000000000000000000000000000"


class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    RED     = "\033[31m"
    CYAN    = "\033[36m"
    MAGENTA = "\033[35m"
    BLUE    = "\033[34m"
    WHITE   = "\033[97m"
    OK      = "\033[32m"
    WARN    = "\033[33m"
    ERR     = "\033[31m"
    INFO    = "\033[36m"
    TAG     = "\033[1;36m"
    API     = "\033[1;33m"
    SRC     = "\033[35m"
    PREVIEW = "\033[2;37m"
    FILE    = "\033[1;37m"


@dataclass
class LyricsResult:
    plain: str | None = None
    synced: str | None = None
    source: str = ""
    api_artist: str = ""
    api_title: str = ""


@dataclass
class PendingReview:
    filepath: Path
    artist: str
    title: str
    candidates: list
    result: LyricsResult | None = None
    reason: str = ""


_GENIUS_SUFFIXES = re.compile(
    r"\s*\("
    r"(?:Translation|Romanized|Romanización|Traduzione|Traduction|Перевод|"
    r"[A-Za-z\s]+ Translation|[A-Za-z\s]+ Romanization)"
    r"\)\s*$",
    re.IGNORECASE,
)

_PAREN_SUFFIX = re.compile(r"\s*\([^)]+\)\s*$")


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = s.lower().strip()
    s = re.sub(r"[\s\-]+$", "", s)
    s = re.sub(r"^[\s\-]+", "", s)
    s = s.replace("-", " ").replace("–", " ").replace("—", " ")
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def strip_api_suffixes(s: str) -> str:
    s = _GENIUS_SUFFIXES.sub("", s)
    return s.strip()


def normalize_for_compare(s: str) -> str:
    s = strip_api_suffixes(s)
    s = re.sub(r"\s*\[[^\]]*\]", "", s)
    s = _PAREN_SUFFIX.sub("", s)
    return normalize(s)


def strings_match(a: str, b: str) -> bool:
    return normalize_for_compare(a) == normalize_for_compare(b)


def strings_close(a: str, b: str) -> bool:
    na, nb = normalize_for_compare(a), normalize_for_compare(b)
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    return False


def get_metadata(filepath: Path) -> tuple[str, str] | None:
    try:
        if filepath.suffix.lower() == ".mp3":
            audio = MP3(filepath)
            if audio.tags is None:
                return None
            artist = str(audio.tags.get("TPE1", "")).strip()
            title = str(audio.tags.get("TIT2", "")).strip()
        elif filepath.suffix.lower() == ".flac":
            audio = FLAC(filepath)
            artist = (audio.get("artist", [""])[0]).strip()
            title = (audio.get("title", [""])[0]).strip()
        elif filepath.suffix.lower() in (".m4a", ".mp4", ".aac"):
            audio = MP4(filepath)
            artist = (audio.tags.get("©ART", [""])[0]).strip() if audio.tags else ""
            title = (audio.tags.get("©nam", [""])[0]).strip() if audio.tags else ""
        else:
            return None

        if not artist or not title:
            return None
        return artist, title
    except Exception as e:
        print(f"  ⚠ Помилка читання тегів {filepath.name}: {e}")
        return None


def has_lyrics(filepath: Path) -> bool:
    try:
        if filepath.suffix.lower() == ".mp3":
            audio = MP3(filepath)
            if audio.tags is None:
                return False
            for key in audio.tags:
                if key.startswith("USLT"):
                    text = str(audio.tags[key])
                    if text.strip():
                        return True
            return False
        elif filepath.suffix.lower() == ".flac":
            audio = FLAC(filepath)
            lyrics = audio.get("lyrics", [""])[0]
            return bool(lyrics.strip())
        elif filepath.suffix.lower() in (".m4a", ".mp4", ".aac"):
            audio = MP4(filepath)
            if audio.tags is None:
                return False
            lyrics = audio.tags.get("©lyr", [""])[0]
            return bool(lyrics.strip())
    except Exception:
        return False
    return False


LRCLIB_BASE = "https://lrclib.net/api"
LRCLIB_SEARCH = f"{LRCLIB_BASE}/search"
LRCLIB_GET = f"{LRCLIB_BASE}/get"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "lyrics_tagger/1.0 (https://github.com/onilyxe/lyrics)"
})


def fetch_lrclib_exact(artist: str, title: str) -> LyricsResult | None:
    try:
        r = SESSION.get(LRCLIB_GET, params={
            "artist_name": artist,
            "track_name": title,
        }, timeout=10)

        if r.status_code == 200:
            data = r.json()
            result = LyricsResult(
                plain=data.get("plainLyrics") or None,
                synced=data.get("syncedLyrics") or None,
                source="lrclib",
                api_artist=data.get("artistName", ""),
                api_title=data.get("trackName", ""),
            )
            if result.plain or result.synced:
                return result
    except requests.RequestException:
        pass
    return None


def fetch_lrclib_search(artist: str, title: str) -> LyricsResult | None:
    try:
        r = SESSION.get(LRCLIB_SEARCH, params={
            "q": f"{artist} {title}",
        }, timeout=10)

        if r.status_code == 200:
            results = r.json()
            if results and len(results) > 0:
                data = results[0]
                return LyricsResult(
                    plain=data.get("plainLyrics") or None,
                    synced=data.get("syncedLyrics") or None,
                    source="lrclib-search",
                    api_artist=data.get("artistName", ""),
                    api_title=data.get("trackName", ""),
                )
    except requests.RequestException:
        pass
    return None


def fetch_genius(artist: str, title: str, token: str) -> LyricsResult | None:
    if lyricsgenius is None:
        print("  ⚠ lyricsgenius не встановлено, пропускаю Genius")
        return None

    try:
        logging.getLogger("lyricsgenius").setLevel(logging.WARNING)
        genius = lyricsgenius.Genius(token, remove_section_headers=False)
        genius.timeout = 15
        song = genius.search_song(title, artist)
        if song and song.lyrics:
            lyrics = song.lyrics
            lines = lyrics.split("\n")
            if lines and ("Lyrics" in lines[0] or "lyrics" in lines[0]):
                lines = lines[1:]
            if lines and re.match(r"^\d*Embed$", lines[-1].strip()):
                lines = lines[:-1]
            lyrics = "\n".join(lines).strip()

            if lyrics:
                return LyricsResult(
                    plain=lyrics,
                    synced=None,
                    source="genius",
                    api_artist=song.artist or "",
                    api_title=song.title or "",
                )
    except Exception as e:
        print(f"  ⚠ Genius помилка: {e}")

    return None


def write_lyrics_mp3(filepath: Path, result: LyricsResult) -> bool:
    try:
        audio = MP3(filepath)
        if audio.tags is None:
            audio.add_tags()

        if result.plain:
            audio.tags.add(USLT(
                encoding=Encoding.UTF8,
                lang="eng",
                desc="",
                text=result.plain,
            ))

        audio.save()
        return True
    except Exception as e:
        print(f"  ✗ Помилка запису в {filepath.name}: {e}")
        return False


def write_lyrics_flac(filepath: Path, result: LyricsResult) -> bool:
    try:
        audio = FLAC(filepath)

        if result.plain:
            audio["lyrics"] = result.plain

        audio.save()
        return True
    except Exception as e:
        print(f"  ✗ Помилка запису в {filepath.name}: {e}")
        return False


def write_lyrics_m4a(filepath: Path, result: LyricsResult) -> bool:
    try:
        audio = MP4(filepath)
        if audio.tags is None:
            audio.add_tags()

        if result.plain:
            audio.tags["©lyr"] = [result.plain]

        audio.save()
        return True
    except Exception as e:
        print(f"  ✗ Помилка запису в {filepath.name}: {e}")
        return False


def write_lyrics(filepath: Path, result: LyricsResult) -> bool:
    if filepath.suffix.lower() == ".mp3":
        return write_lyrics_mp3(filepath, result)
    elif filepath.suffix.lower() == ".flac":
        return write_lyrics_flac(filepath, result)
    elif filepath.suffix.lower() in (".m4a", ".mp4", ".aac"):
        return write_lyrics_m4a(filepath, result)
    return False


def assess_confidence(artist: str, title: str, result: LyricsResult) -> str:
    artist_exact = strings_match(artist, result.api_artist)
    title_exact = strings_match(title, result.api_title)

    if artist_exact and title_exact:
        return "exact"

    artist_close = strings_close(artist, result.api_artist)
    title_close = strings_close(title, result.api_title)

    if artist_close and title_close:
        return "fuzzy"
    if artist_exact and title_close:
        return "fuzzy"
    if artist_close and title_exact:
        return "fuzzy"

    return "mismatch"


def review_pending(pending: list[PendingReview]) -> int:
    if not pending:
        return 0

    print("\n" + "=" * 60)
    print(f"{C.BOLD}🔍 РЕВʼЮ: {len(pending)} трек(ів) з неточним збігом{C.RESET}")
    print("=" * 60)

    written = 0
    for i, item in enumerate(pending, 1):
        print(f"\n--- [{i}/{len(pending)}] ---")
        print(f"  {C.FILE}Файл:       {item.filepath.name}{C.RESET}")
        print(f"  {C.TAG}Твої теги:  {item.artist} — {item.title}{C.RESET}")

        accepted = False
        for ci, (candidate, confidence) in enumerate(item.candidates):
            reason = _make_reason(confidence, candidate)
            src_num = f"[{ci+1}/{len(item.candidates)}]"
            print(f"  {C.SRC}Джерело {src_num}: {candidate.source}{C.RESET}")
            print(f"  {C.API}API повернув: {candidate.api_artist} — {candidate.api_title}{C.RESET}")
            print(f"  Причина:    {C.WARN}{reason}{C.RESET}")

            s_plain = f"{C.OK}✓ plain{C.RESET}" if candidate.plain else f"{C.ERR}✗ plain{C.RESET}"
            print(f"  Дані:       {s_plain}")

            _show_candidate(candidate, reason)

            while True:
                if ci < len(item.candidates) - 1:
                    prompt = f"  {C.BOLD}Записати? [y/n/s(kip)/q(uit)] (n → наступне джерело): {C.RESET}"
                else:
                    prompt = f"  {C.BOLD}Записати? [y/n/q(uit)] (останнє джерело): {C.RESET}"

                answer = input(prompt).strip().lower()
                if answer in ("y", "yes", "д", "так"):
                    if write_lyrics(item.filepath, candidate):
                        print(f"  {C.OK}✓ Записано!{C.RESET}")
                        written += 1
                    accepted = True
                    break
                elif answer in ("n", "no", "н", "ні"):
                    if ci < len(item.candidates) - 1:
                        print(f"  {C.DIM}→ Пробуємо наступне джерело...{C.RESET}")
                    else:
                        print(f"  {C.DIM}→ Пропущено (джерела закінчились){C.RESET}")
                    break
                elif answer in ("s", "skip"):
                    print(f"  {C.DIM}→ Пропущено весь трек{C.RESET}")
                    accepted = True
                    break
                elif answer in ("q", "quit", "в", "вихід"):
                    print(f"  {C.DIM}→ Решту пропущено{C.RESET}")
                    return written
                else:
                    print(f"  {C.WARN}? Введи y, n, s або q{C.RESET}")

            if accepted:
                break

    return written


_SOURCE_ORDER = ["lrclib", "lrclib-search", "genius"]


def _print_found(result: LyricsResult):
    s_plain = f"{C.OK}✓ plain{C.RESET}" if result.plain else f"{C.ERR}✗ plain{C.RESET}"
    base_src = result.source.split("+")[0] if "+" in result.source else result.source
    src_idx = _SOURCE_ORDER.index(base_src) + 1 if base_src in _SOURCE_ORDER else "?"
    print(f"  Знайдено [{C.SRC}{result.source}{C.RESET}] (#{src_idx}/3): {s_plain}")


def _write_or_dry(filepath: Path, result: LyricsResult, dry_run: bool, stats: dict) -> bool:
    if dry_run:
        print(f"  {C.DIM}→ [dry-run] Записав би{C.RESET}")
        stats["written"] += 1
        return True
    else:
        if write_lyrics(filepath, result):
            print(f"  {C.OK}✓ Записано!{C.RESET}")
            stats["written"] += 1
            return True
        else:
            stats["errors"] += 1
            return False


def _make_reason(confidence: str, result: LyricsResult) -> str:
    if confidence == "mismatch":
        return f"Сильне розходження: '{result.api_artist} — {result.api_title}'"
    return f"Неточний збіг: '{result.api_artist} — {result.api_title}'"


def _show_candidate(result: LyricsResult, reason: str):
    preview_text = result.plain or result.synced or ""
    preview_lines = preview_text.strip().split("\n")[:6]
    if preview_lines:
        print(f"  Превʼю:")
        for line in preview_lines:
            clean = re.sub(r"\[\d{2}:\d{2}\.\d{2,3}\]\s*", "", line)
            if clean.strip():
                print(f"    {C.PREVIEW}│ {clean.strip()}{C.RESET}")
        if len(preview_text.strip().split("\n")) > 6:
            print(f"    {C.PREVIEW}│ ...{C.RESET}")


def main():
    parser = argparse.ArgumentParser(
        description="Вбудовує тексти пісень у теги .mp3, .flac та .m4a файлів"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Перезаписувати файли, де вже є текст",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Лише показати що знайдеться, не записувати",
    )
    args = parser.parse_args()

    genius_token = GENIUS_TOKEN if GENIUS_TOKEN != "0000000000000000000000000000000000000000000000000000" else ""

    cwd = Path(".")
    files = sorted(
        [f for f in cwd.iterdir()
         if f.is_file() and f.suffix.lower() in (".mp3", ".flac", ".m4a", ".mp4", ".aac")]
    )

    if not files:
        print("Немає .mp3, .flac або .m4a файлів у поточній папці.")
        return

    print(f"Знайдено файлів: {len(files)}")
    if args.dry_run:
        print("⚡ DRY-RUN: нічого не буде записано\n")
    if not genius_token:
        print(f"{C.WARN}⚠ Genius токен не вказано — 3-є джерело вимкнено{C.RESET}")
    print()

    stats = {"written": 0, "skipped_has_lyrics": 0, "skipped_no_tags": 0,
             "not_found": 0, "errors": 0, "reviewed": 0}
    pending: list[PendingReview] = []

    for i, filepath in enumerate(files, 1):
        print(f"{C.DIM}[{i}/{len(files)}]{C.RESET} {C.FILE}{filepath.name}{C.RESET}")

        meta = get_metadata(filepath)
        if meta is None:
            print(f"  {C.DIM}→ Пропуск: немає artist/title в тегах{C.RESET}")
            stats["skipped_no_tags"] += 1
            continue

        artist, title = meta
        print(f"  {C.TAG}{artist} — {title}{C.RESET}")

        if not args.overwrite and has_lyrics(filepath):
            print(f"  {C.DIM}→ Пропуск: текст вже є{C.RESET}")
            stats["skipped_has_lyrics"] += 1
            continue

        candidates: list[tuple[LyricsResult, str]] = []
        written_this_track = False

        r = fetch_lrclib_exact(artist, title)
        if r:
            conf = assess_confidence(artist, title, r)
            if conf == "exact" and r.plain:
                _print_found(r)
                if _write_or_dry(filepath, r, args.dry_run, stats):
                    written_this_track = True
            elif r.plain:
                candidates.append((r, conf))

        if not written_this_track:
            r = fetch_lrclib_search(artist, title)
            if r:
                conf = assess_confidence(artist, title, r)
                if conf == "exact" and r.plain:
                    _print_found(r)
                    if _write_or_dry(filepath, r, args.dry_run, stats):
                        written_this_track = True
                elif r.plain:
                    candidates.append((r, conf))

        if not written_this_track and genius_token:
            r = fetch_genius(artist, title, genius_token)
            if r and r.plain:
                conf = assess_confidence(artist, title, r)
                candidates.append((r, conf))

        if written_this_track:
            time.sleep(0.3)
            continue

        exact_candidates = [(r, c) for r, c in candidates if c == "exact"]
        if exact_candidates:
            best = exact_candidates[0][0]
            _print_found(best)
            _write_or_dry(filepath, best, args.dry_run, stats)
            time.sleep(0.3)
            continue

        review_candidates = [(r, c) for r, c in candidates if c in ("fuzzy", "mismatch")]
        if review_candidates:
            first_r, first_c = review_candidates[0]
            reason = _make_reason(first_c, first_r)
            _print_found(first_r)
            print(f"  {C.WARN}⚠ {reason} → на ревʼю{C.RESET}")
            pending.append(PendingReview(
                filepath=filepath, artist=artist, title=title,
                candidates=review_candidates,
                reason=reason,
            ))
        else:
            print(f"  {C.ERR}✗ Текст не знайдено{C.RESET}")
            stats["not_found"] += 1

        time.sleep(0.3)

    if pending and not args.dry_run:
        reviewed = review_pending(pending)
        stats["reviewed"] = reviewed
    elif pending and args.dry_run:
        print(f"\n⚡ [dry-run] {len(pending)} трек(ів) потрапили б на ревʼю")

    print("\n" + "=" * 60)
    print(f"{C.BOLD}📊 ПІДСУМКИ:{C.RESET}")
    print(f"  {C.OK}Записано одразу:   {stats['written']}{C.RESET}")
    print(f"  {C.OK}Записано з ревʼю:  {stats['reviewed']}{C.RESET}")
    print(f"  {C.DIM}Текст вже був:     {stats['skipped_has_lyrics']}{C.RESET}")
    print(f"  {C.DIM}Немає тегів:       {stats['skipped_no_tags']}{C.RESET}")
    print(f"  {C.ERR}Не знайдено:       {stats['not_found']}{C.RESET}")
    print(f"  {C.ERR}Помилки:           {stats['errors']}{C.RESET}")
    print(f"  {C.WARN}На ревʼю було:     {len(pending)}{C.RESET}")
    print("=" * 60)


if __name__ == "__main__":
    main()
