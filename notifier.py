#!/usr/bin/env python3
import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.request import Request, urlopen


def load_headers(headers_json: Optional[str]) -> Dict[str, str]:
    if not headers_json:
        return {}
    try:
        return json.loads(headers_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON for headers: {exc}") from exc


def fetch_url(url: str, method: str, headers: Dict[str, str], timeout: int) -> str:
    request = Request(url, method=method, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def matches(
    content: str, keywords: List[str], regexes: List[str]
) -> Tuple[bool, List[str]]:
    hits: List[str] = []
    for keyword in keywords:
        if keyword in content:
            hits.append(f"keyword:{keyword}")
    for regex in regexes:
        if re.search(regex, content):
            hits.append(f"regex:{regex}")
    return bool(hits), hits


def build_region_regex(regions: List[str]) -> Optional[str]:
    cleaned = [region.strip().upper() for region in regions if region.strip()]
    if not cleaned:
        return None
    pattern = "|".join(re.escape(region) for region in cleaned)
    return rf"\b(?:{pattern})\b"


def region_is_available(
    content: str, regions: List[str], unavailable_text: str
) -> Tuple[bool, List[str]]:
    available: List[str] = []
    for region in regions:
        region_token = region.strip().upper()
        if not region_token:
            continue
        region_pattern = re.escape(region_token)
        unavailable_pattern = (
            rf"\b{region_pattern}\b.*?{re.escape(unavailable_text)}"
        )
        if re.search(unavailable_pattern, content, re.IGNORECASE | re.DOTALL):
            continue
        if re.search(rf"\b{region_pattern}\b", content, re.IGNORECASE):
            available.append(region_token)
    return bool(available), available


def run_command(command: str) -> None:
    subprocess.run(shlex.split(command), check=False)


def send_telegram_message(token: str, chat_id: str, text: str, timeout: int) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout):
        return None


def align_to_next_hour() -> None:
    now = datetime.now()
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    sleep_seconds = max(0, int((next_hour - now).total_seconds()))
    if sleep_seconds:
        time.sleep(sleep_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Poll a URL and notify when content matches a keyword or regex."
    )
    parser.add_argument("--url", required=True, help="Target URL to poll.")
    parser.add_argument(
        "--method", default="GET", choices=["GET", "HEAD"], help="HTTP method to use."
    )
    parser.add_argument(
        "--headers",
        help='Optional JSON string of headers, e.g. \'{"Authorization":"Bearer ..."}\'',
    )
    parser.add_argument(
        "--keyword",
        action="append",
        default=[],
        help="Plain-text keyword to match in the response body (repeatable).",
    )
    parser.add_argument(
        "--regex",
        action="append",
        default=[],
        help="Regex pattern to match in the response body (repeatable).",
    )
    parser.add_argument(
        "--region",
        action="append",
        default=[],
        help="Region code to restrict notifications (repeatable, e.g. SG, IN, AU).",
    )
    parser.add_argument(
        "--region-available",
        action="store_true",
        help="Only notify when a region appears without the unavailable text.",
    )
    parser.add_argument(
        "--region-unavailable-text",
        default="full and unavailable",
        help="Text indicating a region is unavailable (default: %(default)s).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=25,
        help="Seconds between checks (default: 60).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Request timeout in seconds (default: 10).",
    )
    parser.add_argument(
        "--command",
        help="Shell command to run once a match is detected.",
    )
    parser.add_argument(
        "--telegram-token",
        help="Telegram bot token used to send a notification.",
    )
    parser.add_argument(
        "--telegram-chat-id",
        help="Telegram chat ID that should receive the notification.",
    )
    parser.add_argument(
        "--telegram-message",
        default="Resource is available.",
        help="Message text to send to Telegram (default: %(default)s).",
    )
    parser.add_argument(
        "--include-matches",
        action="store_true",
        help="Append matching keywords/regexes to the notification message.",
    )
    parser.add_argument(
        "--align-hour",
        action="store_true",
        help="Align checks to the top of each hour before polling.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Exit after the first successful match.",
    )
    return parser


def format_region_message(message: str, regions: List[str]) -> str:
    if not regions:
        return message
    region_text = ", ".join(regions)
    if "{regions}" in message:
        return message.replace("{regions}", region_text)
    return f"{message}\nAvailable regions: {region_text}"


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if not args.keyword and not args.regex:
        parser.error("Provide at least one of --keyword or --regex.")
    if (args.telegram_token and not args.telegram_chat_id) or (
        args.telegram_chat_id and not args.telegram_token
    ):
        parser.error("Provide both --telegram-token and --telegram-chat-id together.")

    headers = {"User-Agent": "hidencloud-notifier/1.0"}
    headers.update(load_headers(args.headers))
    region_regex = build_region_regex(args.region)
    region_unavailable_text = args.region_unavailable_text

    if args.align_hour:
        align_to_next_hour()

    while True:
        try:
            content = fetch_url(args.url, args.method, headers, args.timeout)
        except Exception as exc:
            timestamp = datetime.now().isoformat(timespec="seconds")
            print(f"[{timestamp}] Request failed: {exc}", file=sys.stderr)
        else:
            region_ok = True
            available_regions: List[str] = []
            if region_regex:
                region_ok = re.search(region_regex, content) is not None
            if region_ok and args.region_available and args.region:
                region_ok, available_regions = region_is_available(
                    content, args.region, region_unavailable_text
                )
            matched, hits = matches(content, args.keyword, args.regex)
            if matched and region_ok:
                timestamp = datetime.now().isoformat(timespec="seconds")
                print(f"[{timestamp}] Match detected.")
                if args.command:
                    run_command(args.command)
                if args.telegram_token and args.telegram_chat_id:
                    message = args.telegram_message
                    if args.region_available and available_regions:
                        message = format_region_message(message, available_regions)
                    if args.include_matches:
                        match_details = hits[:]
                        if region_regex:
                            match_details.append(f"region:{region_regex}")
                        if available_regions:
                            match_details.append(
                                f"region_available:{', '.join(available_regions)}"
                            )
                        message = f"{message}\nMatches: {', '.join(match_details)}"
                    send_telegram_message(
                        args.telegram_token,
                        args.telegram_chat_id,
                        message,
                        args.timeout,
                    )
                if args.once:
                    return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
