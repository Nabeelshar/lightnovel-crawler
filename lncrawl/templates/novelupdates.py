import logging
import re
from typing import Mapping
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup, Tag
from readability import Document

from lncrawl.core.browser import EC
from lncrawl.core.crawler import Crawler
from lncrawl.core.exeptions import LNException
from lncrawl.models import Chapter, SearchResult
from lncrawl.templates.browser.chapter_only import ChapterOnlyBrowserTemplate
from lncrawl.templates.browser.searchable import SearchableBrowserTemplate

logger = logging.getLogger(__name__)


automation_warning = """<div style="opacity: 0.5; padding: 14px; text-align: center; border: 1px solid #000; font-style: italic; font-size: 0.825rem">
    Parsed with an automated reader. The content accuracy is not guranteed.
</div>"""


class NovelupdatesTemplate(SearchableBrowserTemplate, ChapterOnlyBrowserTemplate):
    is_template = True
    _cached_crawlers: Mapping[str, Crawler] = {}
    _title_matcher = re.compile(r"^(c|ch|chap|chapter)?[^\w\d]*(\d+)$", flags=re.I)

    def select_search_items(self, query: str):
        query = dict(sf=1, sh=query, sort="srank", order="desc")
        soup = self.get_soup(
            f"https://www.novelupdates.com/series-finder/?{urlencode(query)}"
        )
        yield from soup.select(".l-main .search_main_box_nu")

    def select_search_items_in_browser(self, query: str):
        query = dict(sf=1, sh=query, sort="srank", order="desc")
        self.visit(f"https://www.novelupdates.com/series-finder/?{urlencode(query)}")
        overlay = self.browser.find("#uniccmp")
        if overlay:
            overlay.remove()
        yield from self.browser.soup.select(".l-main .search_main_box_nu")

    def parse_search_item(self, tag: Tag) -> SearchResult:
        a = tag.select_one(".search_title a[href]")

        info = []
        rank = tag.select_one(".genre_rank")
        rating = tag.select_one(".search_ratings")
        chapter_count = tag.select_one('.ss_desk i[title="Chapter Count"]')
        last_updated = tag.select_one('.ss_desk i[title="Last Updated"]')
        reviewers = tag.select_one('.ss_desk i[title="Reviews"]')
        if rating:
            info.append(rating.text.strip())
        if rank:
            info.append("Rank " + rank.text.strip())
        if reviewers:
            info.append(reviewers.parent.text.strip())
        if chapter_count:
            info.append(chapter_count.parent.text.strip())
        if last_updated:
            info.append(last_updated.parent.text.strip())

        return SearchResult(
            title=a.text.strip(),
            info=" | ".join(info),
            url=self.absolute_url(a["href"]),
        )

    def get_novel_soup(self) -> BeautifulSoup:
        if self.novel_url.startswith("https://www.novelupdates.com"):
            return self.get_soup(self.novel_url)
        else:
            return self.guess_novelupdates_link(self.novel_url)

    def visit_novel_page_in_browser(self) -> BeautifulSoup:
        self.visit(self.novel_url)
        overlay = self.browser.find("#uniccmp")
        if overlay:
            overlay.remove()

    def guess_novelupdates_link(self, url: str) -> str:
        # Guess novel title
        response = self.get_response(url)
        reader = Document(response.text)
        title = reader.short_title()
        logger.info("Original title = %s", title)

        title = title.rsplit("-", 1)[0].strip() or title
        title = re.sub(r"[^\w\d ]+", " ", title.lower())
        title = " ".join(title.split(" ")[:10])
        logger.info("Guessed title = %s", title)

        # Search by guessed title in novelupdates
        novels = self.search_novel(title)
        if len(novels) != 1:
            raise LNException("Not supported for " + self.novel_url)

        self.novel_url = novels[0].url
        return self.get_soup(self.novel_url)

    def parse_title(self, soup: BeautifulSoup) -> str:
        return soup.select_one(".seriestitlenu").text

    def parse_title_in_browser(self) -> str:
        self.browser.wait(".seriestitlenu")
        return self.parse_title(self.browser.soup)

    def parse_cover(self, soup: BeautifulSoup) -> str:
        img_tag = soup.select_one(".seriesimg img[src]")
        if img_tag:
            return img_tag["src"]

    def parse_authors(self, soup: BeautifulSoup):
        for a in soup.select("#showauthors a#authtag"):
            yield a.text.strip()

    def select_chapter_tags(self, soup: BeautifulSoup):
        postid = soup.select_one("input#mypostid")["value"]
        response = self.submit_form(
            "https://www.novelupdates.com/wp-admin/admin-ajax.php",
            data=dict(
                action="nd_getchapters",
                mygrr="1",
                mypostid=postid,
            ),
        )
        soup = self.make_soup(response)
        yield from reversed(soup.select(".sp_li_chp a[data-id]"))

    def select_chapter_tags_in_browser(self):
        el = self.browser.find(".my_popupreading_open")
        el.scroll_to_view()
        el.click()
        self.browser.wait("#my_popupreading li.sp_li_chp")
        for a in self.browser.find_all("#my_popupreading li.sp_li_chp a[data-id]"):
            yield a.as_tag()

    def parse_chapter_item(self, tag: Tag, id: int) -> Chapter:
        title = tag.text.strip().title()
        title_match = self._title_matcher.match(title)
        if title_match:  # skip simple titles
            title = f"Chapter {title_match.group(2)}"
        return Chapter(
            id=id,
            title=title,
            url=self.absolute_url(tag["href"]),
        )

    def download_chapter_body(self, chapter: Chapter) -> str:
        from lncrawl.core.sources import crawler_list, prepare_crawler

        response = self.scraper.head(chapter.url, allow_redirects=True)
        logger.info("%s => %s", chapter.url, response.url)
        chapter.url = response.url
        parsed_url = urlparse(chapter.url)
        base_url = "%s://%s/" % (parsed_url.scheme, parsed_url.hostname)

        if base_url in crawler_list:
            try:
                crawler = self._cached_crawlers.get(base_url)
                if not crawler:
                    crawler = prepare_crawler(chapter.url)
                    self._cached_crawlers[base_url] = crawler
                return crawler.download_chapter_body(chapter)
            except Exception as e:
                logger.info("Failed with original crawler.", e)

        return super().download_chapter_body(chapter)

    def download_chapter_body_in_scraper(self, chapter: Chapter) -> None:
        response = self.get_response(chapter.url)
        return self.parse_chapter_body(chapter, response.text)

    def download_chapter_body_in_browser(self, chapter: Chapter) -> str:
        self.visit(chapter.url)
        overlay = self.browser.find("#uniccmp")
        if overlay:
            overlay.remove()
        self.browser.wait("title")
        self.browser.wait(
            "#challenge-running",
            expected_conditon=EC.invisibility_of_element,
        )
        return self.parse_chapter_body(chapter, self.browser.html)

    def select_chapter_body(self, soup: BeautifulSoup) -> Tag:
        return super().select_chapter_body(soup)

    def parse_chapter_body(self, chapter: Chapter, text: str) -> str:
        reader = Document(text)
        chapter.title = reader.short_title()
        summary = reader.summary(True)
        return automation_warning + summary
