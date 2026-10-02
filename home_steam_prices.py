"""Ev bağlantısından tüm itemların fiyatını doğrudan Steam'den alıp GitHub'a gönderir.

GitHub Actions IP'lerine Steam fiyat sorguları kapalı; ev IP'si dakikada ~10 sorguya izinli.
Windows Görev Zamanlayıcı bunu 4 saatte bir pythonw ile çalıştırır (pencere açılmaz).
Yalnızca ayrı bir klonda çalışır (.home-job-clone dosyası); her turda origin/main'e eşitlenir.
Günlük: home_steam_prices.log
"""

import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MARKER = ROOT / ".home-job-clone"
LOG = logging.getLogger("home")


class GitError(Exception):
    pass


def git(*args):
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=300,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise GitError(f"git {args[0]}: {(result.stderr or result.stdout).strip()[:300]}")
    return result.stdout


def sync():
    # Ayrı iş klonu: yerel değişiklik tutulmaz, çakışma olmaz.
    git("fetch", "-q", "origin", "main")
    git("reset", "-q", "--hard", "origin/main")


def main():
    handler = RotatingFileHandler(ROOT / "home_steam_prices.log", maxBytes=300_000, backupCount=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s: %(message)s")
    if not MARKER.exists():
        LOG.error("Bu klasör ayrı iş klonu değil (%s yok); yerel değişiklikleri korumak için çalışmadı.", MARKER.name)
        return 1
    try:
        sync()
        import tracker  # Güncel kod eşitlemeden sonra yüklenir.

        now = datetime.now(timezone.utc)
        names = tracker.steam_refresh_order(now)
        client = tracker.HttpClient()
        client.steam_delay = tracker.HOME_STEAM_DELAY
        LOG.info("%s item Steam'e soruluyor.", len(names))
        found = tracker.fetch_steam_prices(client, names, budget=None)
        LOG.info("%s/%s item için Steam fiyatı alındı.", len(found), len(names))
        if not found:
            return 0
        for _ in range(3):
            sync()  # Bu arada gelen GitHub commit'lerinin üstüne yazılır.
            tracker.store_steam_prices(tracker.read_steam_prices(), found, now)
            if not git("status", "--porcelain", "--", "data/steam_prices.json").strip():
                return 0
            git("add", "--", "data/steam_prices.json")
            git("commit", "-q", "-m", f"chore: update Steam prices from home connection ({len(found)} items)")
            try:
                git("push", "-q", "origin", "HEAD:main")
                LOG.info("GitHub'a gönderildi.")
                return 0
            except GitError as error:
                LOG.warning("Gönderilemedi (%s); yeniden deneniyor.", error)
        LOG.error("Steam fiyatları GitHub'a gönderilemedi.")
        return 1
    except Exception:
        LOG.exception("Ev Steam fiyat işi başarısız.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
