from __future__ import annotations

import argparse
import hashlib
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

BASE_URL = "https://www.merckvetmanual.com"
DEFAULT_START_URL = f"{BASE_URL}/veterinary-topics"
OUTPUT_DIR = "merck_vet_manual"
ALLOWED_PREFIX = "/veterinary-topics"
REQUEST_DELAY = 0.4
TIMEOUT = 30
MAX_PAGES = 5000
MIN_TEXT_LENGTH = 120
SITEMAP_INDEX_URL = f"{BASE_URL}/sitemap.xml"
TOPIC_SITEMAP_CANDIDATES = (
    "veterinary-topic.xml.gz",
    "veterinary-topic.xml",
)
MAX_OUTPUT_PATH_LEN = 240
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

SKIP_TEXT_EXACT = {
    "veterinary professionals",
    "pet owners",
    "resources",
    "quizzes",
    "search",
}

SKIP_URL_PARTS = (
    "/resources",
    "/quizzes",
    "/pethealth",
    "/pet-owner",
    "/multimedia",
    "/news",
)

class MerckVetParser:
    def __init__(
        self,
        start_url: str = DEFAULT_START_URL,
        output_root: str = OUTPUT_DIR,
        delay: float = REQUEST_DELAY,
        max_pages: int = MAX_PAGES,
    ):
        self.start_url = start_url
        self.output_root = Path(output_root)
        self.delay = delay
        self.max_pages = max_pages
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.visited: set[str] = set()
        self.saved_pages = 0
        self.failed_urls: list[str] = []
        self.allowed_path_roots: set[str] = set()

    def fetch_soup(self, url: str) -> BeautifulSoup:
        logging.info("GET %s", url)
        response = self.session.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        time.sleep(self.delay)
        return BeautifulSoup(response.text, "html.parser")

    def run(self) -> None:
        self.output_root.mkdir(parents=True, exist_ok=True)
        start_soup = self.fetch_soup(self.start_url)
        sections = self.extract_top_sections(start_soup)
        if not sections:
            logging.info("Top-level sections were not found in page HTML; trying sitemap fallback.")
            sections = self.extract_top_sections_from_sitemap()
        if not sections:
            raise RuntimeError(
                "Could not locate top-level sections on the start page. "
                "The site markup may have changed or the page may be rendering differently."
            )

        self.allowed_path_roots = self.collect_path_roots([self.start_url, *sections.values()])
        logging.info("Found %s top-level sections", len(sections))
        for title, url in sections.items():
            if len(self.visited) >= self.max_pages:
                break
            self.crawl(url=url, section_title=title, parent_trail=[title])

        logging.info("Saved pages: %s", self.saved_pages)
        if self.failed_urls:
            logging.warning("Failed URLs: %s", len(self.failed_urls))
            for bad_url in self.failed_urls:
                logging.warning("FAILED %s", bad_url)

    def crawl(self, url: str, section_title: str, parent_trail: list[str]) -> None:
        stack: list[tuple[str, list[str]]] = [(url, parent_trail)]

        while stack:
            if len(self.visited) >= self.max_pages:
                logging.warning("Reached max_pages=%s, stopping crawl.", self.max_pages)
                return

            current_url, current_trail = stack.pop()
            current_url = self.normalize_url(current_url)
            if not current_url or current_url in self.visited:
                continue

            self.visited.add(current_url)
            try:
                soup = self.fetch_soup(current_url)
            except Exception as exc:  # noqa: BLE001
                logging.exception("Failed to fetch %s: %s", current_url, exc)
                self.failed_urls.append(current_url)
                continue

            page_title = self.extract_page_title(soup) or current_trail[-1]
            breadcrumbs = self.extract_breadcrumbs(soup)
            trail = self.build_trail(section_title, breadcrumbs, page_title, current_trail)

            article_text = self.extract_article_text(soup)
            if article_text:
                self.save_article(trail, article_text)

            child_links = self.extract_child_links(soup, current_url=current_url)
            if not child_links:
                continue

            # Reverse for stable DFS order similar to recursive traversal.
            child_items = list(child_links.items())
            for child_title, child_url in reversed(child_items):
                child_trail = trail + [child_title]
                stack.append((child_url, child_trail))

    def normalize_url(self, url: str) -> str:
        if not url:
            return ""
        absolute = urljoin(BASE_URL, url)
        absolute, _ = urldefrag(absolute)
        parsed = urlparse(absolute)
        if parsed.netloc and "merckvetmanual.com" not in parsed.netloc:
            return ""
        # Canonicalize URL to avoid revisiting the same page via tracking params.
        parsed = parsed._replace(query="", params="")
        absolute = parsed.geturl()
        if any(part in parsed.path.lower() for part in SKIP_URL_PARTS):
            return ""
        if self.allowed_path_roots:
            path_root = self.extract_path_root(parsed.path)
            if not path_root:
                return ""
            if path_root not in self.allowed_path_roots:
                return ""
        elif ALLOWED_PREFIX and not parsed.path.startswith(ALLOWED_PREFIX):
            return ""
        return absolute

    def extract_top_sections(self, soup: BeautifulSoup) -> "OrderedDict[str, str]":
        results: "OrderedDict[str, str]" = OrderedDict()

        for container in self.find_sections_containers(soup):
            for a in container.find_all("a", href=True):
                title = self.clean_text(a.get_text(" ", strip=True))
                href = self.normalize_url(a["href"])
                if not title or not href:
                    continue
                if not self.is_top_section_link(title, href):
                    continue
                results.setdefault(title, href)

        if results:
            return results

        main = self.find_main_container(soup) or soup
        for a in main.find_all("a", href=True):
            title = self.clean_text(a.get_text(" ", strip=True))
            href = self.normalize_url(a["href"])
            if not title or not href:
                continue
            if not self.is_top_section_link(title, href):
                continue
            results.setdefault(title, href)

        return results

    def find_sections_containers(self, soup: BeautifulSoup) -> list[Tag]:
        containers: list[Tag] = []

        for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "strong", "span", "div", "p"]):
            text = self.clean_text(heading.get_text(" ", strip=True)).lower()
            if text != "sections":
                continue
            parent = heading.parent if isinstance(heading.parent, Tag) else None
            if parent:
                containers.append(parent)
            containers.append(heading)
            next_block = heading.find_next(["div", "section", "ul", "ol"])
            if isinstance(next_block, Tag):
                containers.append(next_block)

        for text_node in soup.find_all(string=True):
            if not isinstance(text_node, NavigableString):
                continue
            text = self.clean_text(str(text_node)).lower()
            if text != "sections":
                continue
            parent = text_node.parent if isinstance(text_node.parent, Tag) else None
            if parent:
                containers.append(parent)
                if isinstance(parent.parent, Tag):
                    containers.append(parent.parent)

        unique: list[Tag] = []
        seen: set[int] = set()
        for item in containers:
            if not isinstance(item, Tag):
                continue
            marker = id(item)
            if marker in seen:
                continue
            seen.add(marker)
            unique.append(item)
        return unique

    def is_top_section_link(self, title: str, href: str) -> bool:
        title_lower = title.lower()
        if title_lower in SKIP_TEXT_EXACT:
            return False
        if href == self.start_url:
            return False

        parsed = urlparse(href)
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) == 1:
            return True
        if len(path_parts) == 2 and path_parts[0] == "veterinary-topics":
            return True

        return False

    def extract_top_sections_from_sitemap(self) -> "OrderedDict[str, str]":
        results: "OrderedDict[str, str]" = OrderedDict()

        try:
            sitemap_url = self.find_topic_sitemap_url()
            if not sitemap_url:
                return results
            response = self.session.get(sitemap_url, timeout=TIMEOUT)
            response.raise_for_status()
            root = ET.fromstring(response.text)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Failed to parse sitemap fallback: %s", exc)
            return results

        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        root_counts: dict[str, int] = {}
        for loc in root.findall("sm:url/sm:loc", ns):
            url = self.normalize_url_for_section_discovery(loc.text or "")
            if not url:
                continue
            path_root = self.extract_path_root(urlparse(url).path)
            if not path_root:
                continue
            root_counts[path_root] = root_counts.get(path_root, 0) + 1

        for root_name, _ in sorted(root_counts.items(), key=lambda item: item[1], reverse=True):
            title = root_name.replace("-", " ").title()
            results[title] = urljoin(BASE_URL, f"/{root_name}")

        return results

    def find_topic_sitemap_url(self) -> str:
        try:
            response = self.session.get(SITEMAP_INDEX_URL, timeout=TIMEOUT)
            response.raise_for_status()
            root = ET.fromstring(response.text)
        except Exception as exc:  # noqa: BLE001
            logging.warning("Failed to load sitemap index: %s", exc)
            return ""

        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for loc in root.findall("sm:sitemap/sm:loc", ns):
            value = (loc.text or "").strip()
            if not value:
                continue
            lowered = value.lower()
            if any(candidate in lowered for candidate in TOPIC_SITEMAP_CANDIDATES):
                return value
        return ""

    @staticmethod
    def extract_path_root(path: str) -> str:
        parts = [part for part in path.split("/") if part]
        return parts[0].lower() if parts else ""

    @staticmethod
    def normalize_url_for_section_discovery(url: str) -> str:
        if not url:
            return ""
        absolute = urljoin(BASE_URL, url)
        absolute, _ = urldefrag(absolute)
        parsed = urlparse(absolute)
        if parsed.netloc and "merckvetmanual.com" not in parsed.netloc:
            return ""
        if any(part in parsed.path.lower() for part in SKIP_URL_PARTS):
            return ""
        return absolute

    def collect_path_roots(self, urls: Iterable[str]) -> set[str]:
        roots: set[str] = set()
        for url in urls:
            parsed = urlparse(urljoin(BASE_URL, url))
            root = self.extract_path_root(parsed.path)
            if root:
                roots.add(root)
        return roots

    def extract_child_links(self, soup: BeautifulSoup, current_url: str) -> "OrderedDict[str, str]":
        results: "OrderedDict[str, str]" = OrderedDict()
        main = self.find_main_container(soup) or soup
        current_title = (self.extract_page_title(soup) or "").strip().lower()

        candidate_anchors = []
        for a in main.find_all("a", href=True):
            title = self.clean_text(a.get_text(" ", strip=True))
            href = self.normalize_url(a["href"])
            if not title or not href:
                continue
            if href == current_url:
                continue
            if title.lower() == current_title:
                continue
            if title.lower() in SKIP_TEXT_EXACT:
                continue
            if not self.is_probably_navigation_link(a):
                continue
            candidate_anchors.append((title, href))

        for title, href in candidate_anchors:
            results.setdefault(title, href)

        return results

    @staticmethod
    def is_probably_navigation_link(anchor: Tag) -> bool:
        title = anchor.get_text(" ", strip=True)
        if not title or len(title) > 180:
            return False

        href = (anchor.get("href") or "").lower()
        if href.endswith("#"):
            return False

        for parent in anchor.parents:
            if not isinstance(parent, Tag):
                continue
            attrs_blob = " ".join(
                [
                    parent.get("id", ""),
                    " ".join(parent.get("class", [])),
                    parent.get("data-testid", ""),
                    parent.get("role", ""),
                    parent.get("aria-label", ""),
                ]
            ).lower()
            if any(keyword in attrs_blob for keyword in ("accordion", "expand", "section", "topic", "index", "tree", "list", "nav", "menu")):
                return True
            if parent.name in {"ul", "ol", "li", "details", "summary"}:
                return True
        return False

    @staticmethod
    def extract_page_title(soup: BeautifulSoup) -> str:
        for selector in ["main h1", "article h1", "h1", "main h2", "article h2"]:
            node = soup.select_one(selector)
            if node:
                text = node.get_text(" ", strip=True)
                if text:
                    return text
        if soup.title:
            return soup.title.get_text(" ", strip=True)
        return ""

    def extract_breadcrumbs(self, soup: BeautifulSoup) -> list[str]:
        crumbs: list[str] = []
        for nav in soup.find_all(["nav", "div", "ol", "ul"]):
            attrs_blob = " ".join(
                [
                    nav.get("aria-label", ""),
                    nav.get("id", ""),
                    " ".join(nav.get("class", [])),
                ]
            ).lower()
            if "breadcrumb" not in attrs_blob:
                continue
            texts = [
                self.clean_text(x.get_text(" ", strip=True))
                for x in nav.find_all(["a", "span", "li"])
                if self.clean_text(x.get_text(" ", strip=True))
            ]
            crumbs = self.unique_preserve_order(texts)
            if crumbs:
                break
        return crumbs

    def build_trail(self, section_title: str, breadcrumbs: list[str], page_title: str, fallback_trail: list[str]) -> list[str]:
        trail = [section_title]
        crumb_tail = []
        for crumb in breadcrumbs:
            normalized = crumb.strip()
            if not normalized:
                continue
            lower = normalized.lower()
            if lower in {"home", "veterinary topics", section_title.lower()}:
                continue
            crumb_tail.append(normalized)

        if crumb_tail:
            trail.extend(crumb_tail)
        elif fallback_trail:
            trail.extend(fallback_trail[1:])

        if not trail or trail[-1].strip().lower() != page_title.strip().lower():
            trail.append(page_title)

        trail = [self.clean_text(x) for x in trail if self.clean_text(x)]
        return self.unique_preserve_order(trail)

    def extract_article_text(self, soup: BeautifulSoup) -> str:
        container = self.find_article_container(soup) or self.find_main_container(soup)
        if not container:
            return ""

        container = BeautifulSoup(str(container), "html.parser")
        for tag in container.find_all(["script", "style", "noscript", "svg", "img", "button", "form", "aside", "footer", "nav"]):
            tag.decompose()

        lines: list[str] = []
        for node in container.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li"]):
            text = self.clean_text(node.get_text(" ", strip=True))
            if not text:
                continue
            if self.should_skip_text_line(text):
                continue
            lines.append(text)

        lines = self.compact_lines(lines)
        text = "\n\n".join(lines).strip()
        if len(text) < MIN_TEXT_LENGTH:
            return ""
        return text

    def find_main_container(self, soup: BeautifulSoup) -> Optional[Tag]:
        for selector in ["main", "article", "div[role='main']", "#main", ".main-content", ".content"]:
            node = soup.select_one(selector)
            if node:
                return node
        body = soup.body
        return body if isinstance(body, Tag) else None

    def find_article_container(self, soup: BeautifulSoup) -> Optional[Tag]:
        selectors = [
            "article",
            "main article",
            ".topic-content",
            ".content-body",
            ".article-content",
            ".topic-page",
            ".topic",
            ".content",
            "main",
        ]
        candidates = [soup.select_one(selector) for selector in selectors]
        candidates = [c for c in candidates if isinstance(c, Tag)]
        if not candidates:
            return None
        return max(candidates, key=lambda tag: len(tag.get_text(" ", strip=True)))

    def save_article(self, trail: list[str], text: str) -> None:
        sanitized = [self.safe_name(part, max_len=80) for part in trail]
        compacted = self.compact_trail_for_path_limit(sanitized)

        directory = self.output_root
        for part in compacted:
            directory /= part
        file_path = directory / "_index.txt"

        try:
            directory.mkdir(parents=True, exist_ok=True)
            file_path.write_text(text, encoding="utf-8")
        except OSError:
            # Last-resort fallback for Windows path limits.
            digest = hashlib.sha1(" / ".join(sanitized).encode("utf-8")).hexdigest()[:16]
            leaf = compacted[-1] if compacted else "untitled"
            directory = self.output_root / "_by_hash" / f"{leaf}_{digest}"
            directory.mkdir(parents=True, exist_ok=True)
            file_path = directory / "_index.txt"
            file_path.write_text(text, encoding="utf-8")

        self.saved_pages += 1
        logging.info("Saved %s", file_path)

    def compact_trail_for_path_limit(self, parts: list[str]) -> list[str]:
        if not parts:
            return ["untitled"]

        result = parts[:]
        floor = 24
        while self.output_file_path_len(result) > MAX_OUTPUT_PATH_LEN:
            idx = max(range(len(result)), key=lambda i: len(result[i]))
            current = result[idx]
            if len(current) <= floor:
                break
            trimmed = current[: max(10, floor - 10)].rstrip(" .")
            tag = hashlib.sha1(current.encode("utf-8")).hexdigest()[:8]
            result[idx] = f"{trimmed}~{tag}"

        if self.output_file_path_len(result) > MAX_OUTPUT_PATH_LEN:
            digest = hashlib.sha1(" / ".join(parts).encode("utf-8")).hexdigest()[:12]
            head = result[0] if result else "section"
            tail = result[-1] if result else "article"
            result = [self.safe_name(head, max_len=40), f"{self.safe_name(tail, max_len=40)}_{digest}"]

        return result

    def output_file_path_len(self, parts: list[str]) -> int:
        path = (Path.cwd() / self.output_root / Path(*parts) / "_index.txt").resolve()
        return len(str(path))

    @staticmethod
    def safe_name(value: str, max_len: int = 140) -> str:
        value = value.strip().replace("\u00a0", " ")
        value = re.sub(r'[\\/:*?"<>|]+', "_", value)
        value = re.sub(r"\s+", " ", value)
        value = value.rstrip(" .")
        value = value[:max_len].rstrip(" .")
        return value or "untitled"

    @staticmethod
    def clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    @staticmethod
    def unique_preserve_order(items: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in items:
            key = item.strip().lower()
            if not item or key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    @staticmethod
    def compact_lines(lines: list[str]) -> list[str]:
        compacted: list[str] = []
        prev = None
        for line in lines:
            if line == prev:
                continue
            compacted.append(line)
            prev = line
        return compacted

    @staticmethod
    def should_skip_text_line(text: str) -> bool:
        lowered = text.strip().lower()
        if lowered in SKIP_TEXT_EXACT:
            return True
        if lowered.startswith("copyright"):
            return True
        if lowered.startswith("listen"):
            return True
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse Merck Veterinary Manual sections into a folder tree with TXT files.")
    parser.add_argument("--start-url", default=DEFAULT_START_URL, help="Root veterinary topics URL.")
    parser.add_argument("--output", default=OUTPUT_DIR, help="Output directory.")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY, help="Delay between requests in seconds.")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES, help="Safety limit for the total number of fetched pages.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging level.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s [%(levelname)s] %(message)s")
    parser = MerckVetParser(start_url=args.start_url, output_root=args.output, delay=args.delay, max_pages=args.max_pages)
    parser.run()


if __name__ == "__main__":
    main()
