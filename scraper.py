"""Collect titles and abstracts from a ScienceDirect journal's latest issue.

The scraper starts at the journal issue page and follows each article link in
the same real-Chrome session. ScienceDirect may reject direct HTTP requests or
direct article-page navigation even when the public issue page is available.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import random
import re
import sys
import time
from http.client import HTTPException
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from playwright.sync_api import (
    Browser,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


DEFAULT_JOURNAL_URL = (
    "https://www.sciencedirect.com/journal/machine-learning-with-applications/latest"
)
ARTICLE_LINK_SELECTOR = "li.js-article-list-item a.article-content-title"
BLOCK_MARKERS = (
    "there was a problem providing the content you requested",
    "reference number:",
    "access denied",
    "verify you are human",
)


class ScrapeError(RuntimeError):
    """Raised when the requested public metadata cannot be collected."""


@dataclass
class Article:
    index: int
    title: str
    abstract: str
    abstract_source: str
    authors: List[str]
    doi: str
    pii: str
    article_type: str
    access: str
    article_number: str
    url: str


@dataclass
class Issue:
    journal: str
    volume: str
    issue_label: str
    requested_url: str
    source_url: str


def normalize_space(value: Optional[str]) -> str:
    """Collapse browser text whitespace while preserving readable paragraphs."""
    if not value:
        return ""
    paragraphs = []
    for paragraph in re.split(r"\n\s*\n", value.replace("\r", "\n")):
        cleaned = re.sub(r"\s+", " ", paragraph).strip()
        if cleaned:
            paragraphs.append(cleaned)
    return "\n\n".join(paragraphs)


def normalize_inline(value: Optional[str]) -> str:
    """Collapse all whitespace for fields that must stay on one line."""
    return re.sub(r"\s+", " ", value or "").strip()


def clean_abstract(value: Optional[str]) -> str:
    """Remove the visible section heading without altering abstract content."""
    text = normalize_space(value)
    text = re.sub(r"^abstract\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


def extract_doi(value: Optional[str]) -> str:
    if not value:
        return ""
    match = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", value, re.IGNORECASE)
    return match.group(0).rstrip(".,;)") if match else ""


def extract_pii(url: Optional[str]) -> str:
    if not url:
        return ""
    match = re.search(r"/pii/([^/?#]+)", url)
    return match.group(1) if match else ""


def split_authors(value: Optional[str]) -> List[str]:
    text = normalize_inline(value)
    return [author.strip() for author in text.split(",") if author.strip()]


def rebuild_openalex_abstract(inverted_index: Optional[Dict[str, List[int]]]) -> str:
    """Rebuild normal text from OpenAlex's word -> positions representation."""
    if not inverted_index:
        return ""
    positioned_words = []
    for word, positions in inverted_index.items():
        for position in positions:
            positioned_words.append((position, word))
    positioned_words.sort(key=lambda item: item[0])
    return normalize_space(" ".join(word for _, word in positioned_words))


def strip_jats(value: Optional[str]) -> str:
    """Convert a Crossref JATS abstract fragment to plain text."""
    if not value:
        return ""
    return normalize_space(html.unescape(re.sub(r"<[^>]+>", " ", value)))


def launch_browser(playwright: Playwright, headless: bool) -> Browser:
    """Use installed Google Chrome so no bundled browser download is needed."""
    return playwright.chromium.launch(
        channel="chrome",
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-notifications",
            "--disable-popup-blocking",
        ],
    )


class ScienceDirectScraper:
    def __init__(
        self,
        page: Page,
        timeout: int = 45,
        min_delay: float = 1.0,
        max_delay: float = 2.0,
        try_article_pages: bool = True,
    ) -> None:
        self.page = page
        self.timeout_ms = timeout * 1000
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.article_pages_available = try_article_pages
        page.set_default_timeout(self.timeout_ms)
        page.set_default_navigation_timeout(self.timeout_ms)

    def _delay(self) -> None:
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    def _body_text(self) -> str:
        try:
            return self.page.locator("body").inner_text(timeout=5000)
        except PlaywrightError:
            return ""

    def _is_blocked(self) -> bool:
        # ScienceDirect can leave the document title as "Just a moment..."
        # even after replacing the challenge body with the real article.
        if self.page.locator("#abstracts").count() > 0:
            return False
        title = self.page.title().lower()
        body = self._body_text().lower()
        if any(marker in body for marker in BLOCK_MARKERS):
            return True
        return title in {"just a moment...", "access denied"}

    def _wait_out_interstitial(self) -> None:
        """Give a transient browser check time to finish; never solve CAPTCHAs."""
        end = time.monotonic() + self.timeout_ms / 1000
        while time.monotonic() < end:
            try:
                title = self.page.title().lower()
            except PlaywrightError:
                time.sleep(1)
                continue
            if self.page.locator("#abstracts").count() > 0:
                return
            if title != "just a moment...":
                return
            time.sleep(1)

    def _dismiss_cookie_banner(self) -> None:
        for selector in (
            "#onetrust-reject-all-handler",
            "#onetrust-accept-btn-handler",
            "button[aria-label='Close']",
        ):
            locator = self.page.locator(selector)
            if locator.count() == 0:
                continue
            try:
                locator.first.evaluate("element => element.click()")
                self.page.wait_for_timeout(300)
                return
            except PlaywrightError:
                continue

    def _goto_with_retry(self, url: str, attempts: int = 2) -> None:
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                self.page.goto(
                    url, wait_until="domcontentloaded", timeout=self.timeout_ms
                )
                return
            except PlaywrightError as exc:
                last_error = exc
                if attempt < attempts:
                    self.page.wait_for_timeout(1000)
        raise ScrapeError(f"Unable to open {url}: {last_error}")

    def open_issue(self, journal_url: str) -> Issue:
        self._goto_with_retry(journal_url)
        self._wait_out_interstitial()
        # Loading an issue and clicking through immediately is frequently
        # rejected. This dwell also lets the page finish its client bootstrap.
        self.page.wait_for_timeout(8000)
        self._dismiss_cookie_banner()
        try:
            self.page.wait_for_selector(
                ARTICLE_LINK_SELECTOR, state="attached", timeout=self.timeout_ms
            )
        except PlaywrightTimeoutError as exc:
            if self._is_blocked():
                raise ScrapeError(
                    "ScienceDirect rejected this browser session. Run without "
                    "--headless, use a normal network, and do not attempt to "
                    "bypass a CAPTCHA."
                ) from exc
            raise ScrapeError("No article cards were found on the issue page.") from exc

        title_parts = [part.strip() for part in self.page.title().split("|")]
        journal = title_parts[0]
        if len(title_parts) >= 3 and not title_parts[1].lower().startswith("vol "):
            journal = title_parts[1]
        body = self._body_text()[:5000]
        volume_match = re.search(r"\bVolume\s+(\d+)", body, re.IGNORECASE)
        issue_match = re.search(
            r"(In progress\s*\([^)]+\)|"
            r"(?:January|February|March|April|May|June|July|August|September|"
            r"October|November|December)\s+\d{4})",
            body,
            re.IGNORECASE,
        )
        return Issue(
            journal=journal,
            volume=volume_match.group(1) if volume_match else "",
            issue_label=issue_match.group(1) if issue_match else "",
            requested_url=journal_url,
            source_url=self.page.url,
        )

    def collect_article_cards(self, limit: int = 10) -> List[Dict[str, str]]:
        cards = self.page.eval_on_selector_all(
            "li.js-article-list-item",
            r"""
            (items) => items.map((card) => {
              const link = card.querySelector('a.article-content-title');
              if (!link) return null;
              const allText = card.textContent || '';
              const hiddenDoi = [...card.querySelectorAll('[hidden]')]
                .map((node) => (node.textContent || '').trim())
                .find((text) => text.includes('doi.org/10.')) || '';
              const doi = hiddenDoi
                ? hiddenDoi.split('doi.org/')[1].trim()
                : (allText.match(/10\.\d{4,9}\/[-._;()/:A-Z0-9]+/i) || [''])[0];
              return {
                title: (link.innerText || link.textContent || '').trim(),
                url: link.href,
                pii: link.id || '',
                doi,
                authors: (card.querySelector('.js-article__item__authors')?.innerText || '').trim(),
                article_type: (card.querySelector('.js-article-subtype')?.innerText || '').trim(),
                access: (card.querySelector('.js-open-access')?.innerText || '').trim(),
                article_number: (card.querySelector('.js-article-page-range')?.innerText || '').trim(),
              };
            }).filter(Boolean)
            """,
        )
        unique: List[Dict[str, str]] = []
        seen = set()
        for card in cards:
            pii = card.get("pii") or extract_pii(card.get("url"))
            if not pii or pii in seen:
                continue
            seen.add(pii)
            card["pii"] = pii
            card["doi"] = extract_doi(card.get("doi"))
            for key in ("title", "authors", "article_type", "access", "article_number"):
                card[key] = normalize_inline(card.get(key))
            unique.append(card)
            if limit > 0 and len(unique) >= limit:
                break
        if not unique:
            raise ScrapeError("The issue page loaded, but no unique article links were found.")
        return unique

    def _open_article(self, article_url: str) -> None:
        """Open an article with the issue page as the HTTP referrer."""
        issue_url = self.page.url
        self.page.goto(
            article_url,
            referer=issue_url,
            wait_until="domcontentloaded",
            timeout=self.timeout_ms,
        )

    def _meta(self, name: str) -> str:
        locator = self.page.locator(f'meta[name="{name}"]')
        if locator.count() == 0:
            return ""
        return normalize_space(locator.first.get_attribute("content"))

    def _extract_abstract(self) -> str:
        selectors: Sequence[str] = (
            "#abstracts .abstract.author",
            "#abstracts",
            "div.abstract.author",
            "section#abstracts",
        )
        for selector in selectors:
            locator = self.page.locator(selector)
            for index in range(locator.count()):
                text = clean_abstract(locator.nth(index).inner_text())
                if len(text) >= 40:
                    return text
        description = self._meta("description")
        return clean_abstract(description) if len(description) >= 40 else ""

    def _json_request(self, url: str) -> Dict[str, object]:
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "ScienceDirectLatestIssueDemo/1.0",
            },
        )
        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                with urlopen(request, timeout=self.timeout_ms / 1000) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (HTTPError, URLError, TimeoutError, HTTPException, OSError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1)
        if last_error is not None:
            raise last_error
        return {}

    def _fallback_abstract(self, doi: str) -> tuple[str, str]:
        """Resolve an abstract by DOI when ScienceDirect blocks article pages."""
        if not doi:
            return "", ""
        encoded_work_id = quote(f"https://doi.org/{doi}", safe=":/")
        try:
            work = self._json_request(
                f"https://api.openalex.org/works/{encoded_work_id}"
            )
            abstract = rebuild_openalex_abstract(work.get("abstract_inverted_index"))
            if abstract:
                return abstract, "openalex"
        except (HTTPError, URLError, TimeoutError, HTTPException, OSError, ValueError):
            pass

        try:
            work = self._json_request(
                f"https://api.crossref.org/works/{quote(doi, safe='')}"
            )
            abstract = strip_jats(work.get("message", {}).get("abstract"))
            if abstract:
                return abstract, "crossref"
        except (HTTPError, URLError, TimeoutError, HTTPException, OSError, ValueError):
            pass
        return "", ""

    def scrape_article(self, card: Dict[str, str], index: int) -> Article:
        pii = card["pii"]
        title = normalize_inline(card.get("title"))
        doi = extract_doi(card.get("doi"))
        abstract = ""
        abstract_source = ""

        if self.article_pages_available:
            try:
                self._open_article(card["url"])
                self.page.wait_for_selector(
                    "#abstracts", state="attached", timeout=self.timeout_ms
                )
                if not self._is_blocked():
                    title = self._meta("citation_title") or title
                    doi = extract_doi(self._meta("citation_doi")) or doi
                    abstract = self._extract_abstract()
                    abstract_source = "sciencedirect"
            except PlaywrightError:
                # Avoid repeatedly hitting an article endpoint after the first
                # denial in this session; subsequent records use DOI metadata.
                self.article_pages_available = False

        if not abstract:
            abstract, abstract_source = self._fallback_abstract(doi)
        if not abstract:
            raise ScrapeError(
                f"No public abstract was found on ScienceDirect, OpenAlex, or Crossref: {card['url']}"
            )

        return Article(
            index=index,
            title=title,
            abstract=abstract,
            abstract_source=abstract_source,
            authors=split_authors(card.get("authors")),
            doi=extract_doi(doi),
            pii=pii,
            article_type=normalize_inline(card.get("article_type")),
            access=normalize_inline(card.get("access")),
            article_number=normalize_inline(card.get("article_number")),
            url=card["url"],
        )

    def _return_to_issue(self, issue_url: str) -> None:
        if "/science/article/pii/" not in self.page.url:
            return
        try:
            self.page.go_back(
                wait_until="domcontentloaded", timeout=self.timeout_ms
            )
            self._wait_out_interstitial()
            self.page.wait_for_selector(
                ARTICLE_LINK_SELECTOR, state="attached", timeout=self.timeout_ms
            )
            self.page.wait_for_timeout(3000)
        except PlaywrightError:
            self._goto_with_retry(issue_url)
            self.page.wait_for_selector(
                ARTICLE_LINK_SELECTOR, state="attached", timeout=self.timeout_ms
            )
            self.page.wait_for_timeout(5000)

    def scrape(self, journal_url: str, limit: int = 10) -> Dict[str, object]:
        issue = self.open_issue(journal_url)
        cards = self.collect_article_cards(limit=limit)
        print(
            f"Found {len(cards)} article(s) in {issue.journal}, "
            f"volume {issue.volume or 'unknown'}.",
            flush=True,
        )

        articles: List[Article] = []
        failures: List[Dict[str, str]] = []
        for index, card in enumerate(cards, start=1):
            print(
                f"[{index}/{len(cards)}] {card['title']} "
                f"(DOI: {card.get('doi') or 'missing'})",
                flush=True,
            )
            try:
                article = self.scrape_article(card, index)
                articles.append(article)
                print(f"  abstract: {len(article.abstract)} characters", flush=True)
            except (ScrapeError, PlaywrightError) as exc:
                failures.append(
                    {"title": card["title"], "url": card["url"], "error": str(exc)}
                )
                print(f"  skipped: {exc}", file=sys.stderr, flush=True)
            finally:
                self._return_to_issue(issue.source_url)
                self._delay()

        if not articles:
            raise ScrapeError("No article abstracts were collected.")
        return {
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "issue": asdict(issue),
            "article_count": len(articles),
            "failure_count": len(failures),
            "articles": [asdict(article) for article in articles],
            "failures": failures,
        }


def write_outputs(result: Dict[str, object], output_dir: Path) -> List[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "articles.json"
    csv_path = output_dir / "articles.csv"
    markdown_path = output_dir / "articles.md"

    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    articles = result["articles"]
    fieldnames = [
        "index",
        "title",
        "abstract",
        "abstract_source",
        "authors",
        "doi",
        "pii",
        "article_type",
        "access",
        "article_number",
        "url",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for article in articles:
            row = dict(article)
            row["authors"] = "; ".join(row["authors"])
            writer.writerow(row)

    issue = result["issue"]
    lines = [
        f"# {issue['journal']} — latest issue",
        "",
        f"- Volume: {issue['volume'] or 'N/A'}",
        f"- Issue: {issue['issue_label'] or 'N/A'}",
        f"- Source: {issue['source_url']}",
        f"- Scraped at (UTC): {result['scraped_at']}",
        f"- Articles collected: {result['article_count']}",
        "",
    ]
    for article in articles:
        lines.extend(
            [
                f"## {article['index']}. {article['title']}",
                "",
                f"- Authors: {'; '.join(article['authors'])}",
                f"- DOI: {article['doi'] or 'N/A'}",
                f"- URL: {article['url']}",
                f"- Abstract source: {article['abstract_source']}",
                "",
                article["abstract"],
                "",
            ]
        )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return [json_path, csv_path, markdown_path]


def validate_journal_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc not in {
        "www.sciencedirect.com",
        "sciencedirect.com",
    }:
        raise argparse.ArgumentTypeError(
            "journal URL must be an https://www.sciencedirect.com/... address"
        )
    return value


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape titles and abstracts from a ScienceDirect latest issue."
    )
    parser.add_argument(
        "--journal-url",
        type=validate_journal_url,
        default=DEFAULT_JOURNAL_URL,
        help="ScienceDirect journal /latest URL",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="maximum articles to collect; 0 means the full issue",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("output"), help="output directory"
    )
    parser.add_argument("--timeout", type=int, default=45, help="browser timeout in seconds")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="run Chrome without a window (ScienceDirect may reject headless sessions)",
    )
    parser.add_argument(
        "--metadata-fallback-only",
        action="store_true",
        help="read titles/DOIs from ScienceDirect and resolve abstracts by DOI",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    if args.limit < 0:
        print("--limit cannot be negative", file=sys.stderr)
        return 2
    try:
        with sync_playwright() as playwright:
            browser = launch_browser(playwright, headless=args.headless)
            try:
                context = browser.new_context(
                    locale="en-US", viewport={"width": 1440, "height": 1000}
                )
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                )
                page = context.new_page()
                scraper = ScienceDirectScraper(
                    page=page,
                    timeout=args.timeout,
                    try_article_pages=not args.metadata_fallback_only,
                )
                result = scraper.scrape(args.journal_url, limit=args.limit)
                paths = write_outputs(result, args.output_dir)
            finally:
                browser.close()
        print("Saved:")
        for path in paths:
            print(f"  {path.resolve()}")
        if result["failure_count"]:
            print(
                f"Completed with {result['failure_count']} skipped article(s).",
                file=sys.stderr,
            )
        return 0
    except (ScrapeError, PlaywrightError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
