"""
Kompass CZ leads scraper.

Crawls category listings on https://cz.kompass.com and uses ScrapeGraphAI's
SmartScraperGraph to pull business-owner contact details from each company
profile page: owner name, company, website, email and phone.

Mechanical work (finding the category pages, paginating listings, collecting
company profile links) is done with plain HTTP + BeautifulSoup, since it is
deterministic and doesn't need an LLM. The LLM is only used for the hard part:
reading each company profile page - whose layout varies - and pulling out the
structured contact fields.

Results are streamed to a CSV file as they are found (so a long run can be
interrupted without losing progress), duplicates are removed by email, and
rows with neither an email nor a phone number are skipped.

IMPORTANT
---------
- This script does NOT ship with pre-baked category URLs: Kompass's markup
  and category tree can change at any time, and guessing wrong URLs would
  silently scrape nothing (or the wrong thing). On first run it *discovers*
  each category/sub-category URL by reading the link text on
  https://cz.kompass.com, but you should sanity-check the discovered URLs
  (printed with --verbose) or just paste the correct ones into
  CATEGORY_TARGETS[i]["category_url"] yourself.
- Check https://cz.kompass.com/robots.txt and Kompass's Terms of Service
  before running this at any scale. The script checks robots.txt itself and
  will skip disallowed pages, but that does not by itself make bulk scraping
  of a paid B2B directory compliant with its ToS - that judgment call is
  yours to make for your use case.
- Be polite: keep --delay reasonable and don't parallelize requests against
  the same host.
"""

import argparse
import csv
import os
import re
import sys
import time
import urllib.robotparser
from dataclasses import dataclass
from typing import Iterator, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from scrapegraphai.graphs import SmartScraperGraph

load_dotenv()

BASE_URL = "https://cz.kompass.com"
USER_AGENT = "Mozilla/5.0 (compatible; leads-research-bot/1.0; +contact: local use)"
CSV_FIELDS = ["owner_name", "company", "website", "email", "phone", "segment", "source_url"]

# Company profile links on Kompass live under a two-letter locale segment
# followed by a numeric company id, e.g. /c/some-company-name/CZ123456.
# Adjust this pattern if Kompass's markup differs from what you see live.
PROFILE_LINK_PATTERN = re.compile(r"/c/[^/]+/[A-Z]{2}\d+", re.IGNORECASE)

# Each entry describes one of the requested business segments. "category"
# must match the visible link text of a top-level category on
# https://cz.kompass.com (case-insensitive substring match). "subcategory",
# if set, is looked up the same way *within* the category page. Fill in
# "category_url" directly to skip discovery entirely once you've confirmed
# the real URL in a browser.
CATEGORY_TARGETS = [
    {
        "segment": "Машиностроение (металлургия, металлообработка, станкостроение)",
        "category": "Metalurgie, kovy, kovýroba, strojírenství a inženýring",
        "subcategory": None,
        "category_url": None,
    },
    {
        "segment": "FMCG / продукты питания",
        "category": "Potraviny, potravinářský průmysl",
        "subcategory": None,
        "category_url": None,
    },
    {
        "segment": "Спортивное питание / БАДы",
        "category": "Zdravá výživa",
        "subcategory": "sportovní výživa, doplňky stravy",
        "category_url": None,
    },
    {
        "segment": "Вода (производство напитков)",
        "category": "Potraviny, potravinářský průmysl",
        "subcategory": "Výroba nápojů",
        "category_url": None,
    },
    {
        "segment": "Туризм",
        "category": "Volný čas a cestovní ruch",
        "subcategory": None,
        "category_url": None,
    },
    {
        "segment": "Образование",
        "category": "Vzdělávání, školení, organizace a asociace",
        "subcategory": None,
        "category_url": None,
    },
]


class Lead(BaseModel):
    owner_name: str = Field(
        description="Full name of the business owner, director, manager or main "
        "contact person shown on the page. Empty string if not present."
    )
    company: str = Field(description="Registered company name. Empty string if not present.")
    website: str = Field(description="Company website URL. Empty string if not listed.")
    email: str = Field(description="Contact email address. Empty string if not listed.")
    phone: str = Field(description="Contact phone number. Empty string if not listed.")


EXTRACTION_PROMPT = (
    "Extract the business owner/director/manager contact name, the company name, "
    "the company website, the contact email address and the contact phone number "
    "from this company profile page. Only use information actually present on the "
    "page. If a field is missing, hidden behind a login, or not shown, return an "
    "empty string for it - never invent or guess a value."
)


@dataclass
class CrawlConfig:
    max_pages_per_category: int
    max_companies_per_category: Optional[int]
    delay: float
    verbose: bool


class PoliteFetcher:
    """Thin HTTP client that respects robots.txt and rate-limits itself."""

    def __init__(self, delay: float, verbose: bool):
        self.delay = delay
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last_request_ts = 0.0

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots_cache:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(urljoin(origin, "/robots.txt"))
            try:
                rp.read()
            except Exception:
                pass
            self._robots_cache[origin] = rp
        return self._robots_cache[origin]

    def allowed(self, url: str) -> bool:
        try:
            return self._robots_for(url).can_fetch(USER_AGENT, url)
        except Exception:
            return True

    def get(self, url: str) -> Optional[str]:
        if not self.allowed(url):
            if self.verbose:
                print(f"[robots] disallowed, skipping: {url}")
            return None

        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

        try:
            resp = self.session.get(url, timeout=20)
            self._last_request_ts = time.monotonic()
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            if self.verbose:
                print(f"[http] failed to fetch {url}: {exc}")
            return None


def find_link_by_text(html: str, base_url: str, text_query: str) -> Optional[str]:
    """Find the href of the <a> tag whose text best matches text_query."""

    soup = BeautifulSoup(html, "html.parser")
    query_norm = text_query.strip().lower()
    best_href = None
    best_len = None
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True).lower()
        if not label:
            continue
        if query_norm in label or label in query_norm:
            if best_len is None or len(label) < best_len:
                best_href = a["href"]
                best_len = len(label)
    if best_href is None:
        return None
    return urljoin(base_url, best_href)


def resolve_category_url(fetcher: PoliteFetcher, target: dict) -> Optional[str]:
    if target.get("category_url"):
        return target["category_url"]

    home_html = fetcher.get(BASE_URL + "/")
    if home_html is None:
        return None

    category_url = find_link_by_text(home_html, BASE_URL, target["category"])
    if category_url is None:
        print(f"[discover] could not find category '{target['category']}' on {BASE_URL}")
        return None

    if not target.get("subcategory"):
        return category_url

    category_html = fetcher.get(category_url)
    if category_html is None:
        return None

    subcategory_url = find_link_by_text(category_html, category_url, target["subcategory"])
    if subcategory_url is None:
        print(
            f"[discover] could not find subcategory '{target['subcategory']}' "
            f"inside '{target['category']}'"
        )
        return None
    return subcategory_url


def find_next_page(html: str, current_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")

    rel_next = soup.find("a", rel="next")
    if rel_next and rel_next.get("href"):
        return urljoin(current_url, rel_next["href"])

    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True).lower()
        if label in {"další", "dalsi", "next", ">"}:
            return urljoin(current_url, a["href"])

    return None


def iter_company_links(
    fetcher: PoliteFetcher, category_url: str, config: CrawlConfig
) -> Iterator[str]:
    seen = set()
    url = category_url
    pages_visited = 0

    while url and pages_visited < config.max_pages_per_category:
        html = fetcher.get(url)
        pages_visited += 1
        if html is None:
            break

        soup = BeautifulSoup(html, "html.parser")
        found_on_page = 0
        for a in soup.find_all("a", href=True):
            href = urljoin(url, a["href"])
            if PROFILE_LINK_PATTERN.search(urlparse(href).path) and href not in seen:
                seen.add(href)
                found_on_page += 1
                yield href

        if config.verbose:
            print(f"[list] {url} -> {found_on_page} new company links (page {pages_visited})")

        url = find_next_page(html, url)

    if pages_visited == 0 and config.verbose:
        print(f"[list] no pages fetched for {category_url}")


def extract_lead(url: str, graph_config: dict, verbose: bool) -> Optional[Lead]:
    try:
        graph = SmartScraperGraph(
            prompt=EXTRACTION_PROMPT,
            source=url,
            schema=Lead,
            config=graph_config,
        )
        result = graph.run()
    except Exception as exc:
        if verbose:
            print(f"[extract] failed for {url}: {exc}")
        return None

    if isinstance(result, Lead):
        return result
    if isinstance(result, dict):
        try:
            return Lead(**result)
        except Exception:
            return None
    return None


def build_graph_config(verbose: bool) -> dict:
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("SCRAPEGRAPHAI_MODEL", "openai/gpt-4o-mini")
    if not api_key:
        print(
            "WARNING: OPENAI_API_KEY is not set. SmartScraperGraph calls will fail. "
            "Set it in your environment or a .env file before running for real.",
            file=sys.stderr,
        )
    return {
        "llm": {"api_key": api_key, "model": model},
        "verbose": verbose,
        "headless": True,
    }


def run(config: CrawlConfig, output_path: str) -> None:
    fetcher = PoliteFetcher(delay=config.delay, verbose=config.verbose)
    graph_config = build_graph_config(config.verbose)

    seen_emails: set[str] = set()
    total_written = 0

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()

        for target in CATEGORY_TARGETS:
            print(f"=== Segment: {target['segment']} ===")
            category_url = resolve_category_url(fetcher, target)
            if category_url is None:
                print(f"Skipping segment '{target['segment']}': category page not found.")
                continue
            print(f"Category URL: {category_url}")

            companies_for_segment = 0
            for company_url in iter_company_links(fetcher, category_url, config):
                if (
                    config.max_companies_per_category
                    and companies_for_segment >= config.max_companies_per_category
                ):
                    break

                lead = extract_lead(company_url, graph_config, config.verbose)
                companies_for_segment += 1
                if lead is None:
                    continue

                email = lead.email.strip().lower()
                phone = lead.phone.strip()

                if not email and not phone:
                    continue
                if email and email in seen_emails:
                    continue
                if email:
                    seen_emails.add(email)

                writer.writerow(
                    {
                        "owner_name": lead.owner_name.strip(),
                        "company": lead.company.strip(),
                        "website": lead.website.strip(),
                        "email": email,
                        "phone": phone,
                        "segment": target["segment"],
                        "source_url": company_url,
                    }
                )
                f.flush()
                total_written += 1

    print(f"Done. Wrote {total_written} deduplicated leads with contacts to {output_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="leads.csv", help="Output CSV path")
    parser.add_argument(
        "--max-pages-per-category",
        type=int,
        default=20,
        help="Safety cap on listing pages paginated per category",
    )
    parser.add_argument(
        "--max-companies-per-category",
        type=int,
        default=None,
        help="Optional cap on companies scraped per category (useful for a test run)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="Minimum seconds between HTTP requests to cz.kompass.com",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(
        CrawlConfig(
            max_pages_per_category=args.max_pages_per_category,
            max_companies_per_category=args.max_companies_per_category,
            delay=args.delay,
            verbose=args.verbose,
        ),
        args.output,
    )
