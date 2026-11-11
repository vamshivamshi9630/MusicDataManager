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
        logger.info("Executing Render Cron Job: Latest Releases Discovery Scan...")
        engine = LatestReleasesDiscoveryEngine()
        result = engine.run_scan(locked_by="render_cron_job", limit_albums=20)
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
