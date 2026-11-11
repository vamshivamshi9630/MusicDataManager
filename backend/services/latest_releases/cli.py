import os
import sys
import json
import logging
from backend.services.latest_releases.discovery import LatestReleasesDiscoveryEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("musicdata.cli")


def main():
    args = sys.argv[1:]
    command = args[0] if args else "scan"

    if command == "scan":
        actor = os.environ.get("SCHEDULER_ACTOR") or (
            "github_actions_cron" if os.environ.get("GITHUB_ACTIONS", "").lower() == "true" else "cli_cron"
        )
        limit_albums = int(os.environ.get("LATEST_RELEASES_LIMIT_ALBUMS", "20"))
        logger.info(f"Executing Latest Releases Discovery Scan (actor={actor}, limit_albums={limit_albums})...")

        try:
            engine = LatestReleasesDiscoveryEngine()
            result = engine.run_scan(locked_by=actor, limit_albums=limit_albums)
        except Exception as e:
            # Never emit full connection strings or secret values
            logger.error(f"Fatal CLI scan error ({type(e).__name__}): {e}")
            sys.exit(1)

        logger.info(f"Scan Result: {json.dumps(result, indent=2)}")

        if result.get("success"):
            sys.exit(0)
        elif result.get("status") == "LOCKED":
            logger.info("Scan safely skipped because another scan is currently in progress.")
            sys.exit(0)
        else:
            logger.error(f"Scan failed: {result.get('error')}")
            sys.exit(1)
    else:
        print(f"Unknown command: {command}. Usage: python -m backend.services.latest_releases.cli scan")
        sys.exit(1)


if __name__ == "__main__":
    main()
