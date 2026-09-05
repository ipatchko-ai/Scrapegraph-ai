# Kompass CZ leads scraper

Collects business-owner leads (name, company, website, email, phone) from
company profile pages listed under specific categories on
`https://cz.kompass.com`, and writes them to `leads.csv`.

Requested segments (Czech category names as they appear on the site):

| Segment | Category | Subcategory |
|---|---|---|
| Machinery / metalworking | Metalurgie, kovy, kovýroba, strojírenství a inženýring | - |
| FMCG / food | Potraviny, potravinářský průmysl | - |
| Sports nutrition / supplements | Zdravá výživa | sportovní výživa, doplňky stravy |
| Water / beverage production | Potraviny, potravinářský průmysl | Výroba nápojů |
| Tourism / leisure | Volný čas a cestovní ruch | - |
| Education | Vzdělávání, školení, organizace a asociace | - |

## How it works

1. **Discovery & pagination (no LLM):** the script fetches
   `https://cz.kompass.com`, finds the link matching each category name (and
   subcategory, if any), then paginates the listing pages with plain HTTP +
   BeautifulSoup, collecting company profile links. This part is mechanical
   and doesn't need an LLM.
2. **Extraction (LLM, via ScrapeGraphAI):** each company profile page is fed
   to `SmartScraperGraph` with a Pydantic schema (`owner_name`, `company`,
   `website`, `email`, `phone`). Using an LLM here — instead of hand-written
   CSS selectors — makes extraction resilient to layout differences between
   profile pages.
3. **Output:** rows are streamed to `leads.csv` as they're found. Rows with
   neither an email nor a phone are skipped, and duplicates are removed by
   email (case-insensitive).

## Before you run this for real

- **Verify the discovered category URLs.** Kompass's markup/category tree
  can change. Run with `--verbose` first and check the printed "Category
  URL:" lines actually point where you expect. If discovery fails or finds
  the wrong page, hardcode the correct URL directly in
  `CATEGORY_TARGETS[i]["category_url"]` inside
  `kompass_cz_leads_scraper.py`.
- **Verify the company-link pattern.** `PROFILE_LINK_PATTERN` in the script
  is a best-effort guess at what a company profile URL looks like on
  Kompass. If the crawl finds 0 company links on a listing page, inspect the
  page's HTML and adjust the regex.
- **Check `https://cz.kompass.com/robots.txt` and Kompass's Terms of
  Service.** The script checks robots.txt itself and skips disallowed URLs,
  but that alone doesn't make bulk scraping of a commercial B2B directory
  compliant with its ToS for your use case — that's your call to make.
- **Be polite.** Keep `--delay` reasonable (default 1.5s) and don't run
  multiple instances in parallel against the same host.
- Kompass often masks emails/phone numbers for anonymous visitors (e.g.
  "show phone number" behind a login/paywall). Those fields will legitimately
  come back empty for such pages — the extraction prompt is instructed to
  return an empty string rather than guess.

## Setup

```bash
cp .env.example .env
# edit .env and set OPENAI_API_KEY
pip install -e ../..  # or however you install scrapegraphai + its deps
```

## Usage

```bash
# small test run first
python kompass_cz_leads_scraper.py --verbose --max-companies-per-category 5

# full run
python kompass_cz_leads_scraper.py --output leads.csv
```

### CLI options

- `--output` — output CSV path (default `leads.csv`)
- `--max-pages-per-category` — safety cap on listing pages paginated per
  category (default 20)
- `--max-companies-per-category` — optional cap on companies scraped per
  category, useful for a cheap test run before a full crawl
- `--delay` — minimum seconds between HTTP requests to cz.kompass.com
  (default 1.5)
- `--verbose` — print discovery/pagination/extraction diagnostics

## Output columns

`owner_name, company, website, email, phone, segment, source_url`

`segment` and `source_url` are extra columns (which segment the lead came
from, and the profile page it was scraped from) beyond the five fields
requested, kept for traceability.
