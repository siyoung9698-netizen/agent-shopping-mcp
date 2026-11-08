"""search_products -> get_buy_link. Catalog URLs only, no invented links."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta

from mcp.shared.memory import create_connected_server_and_client_session

import metering
import product_links
import server


def _rows():
    con = sqlite3.connect(server.DB_PATH)
    try:
        return con.execute("select product_key, price, ts from price_history").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


class UrlClassificationTests(unittest.TestCase):
    def test_seller_product_pages_are_kept_and_listings_are_not(self):
        product = "https://www.coupang.com/vp/products/1181656?itemId=5108922"
        fitpet = "https://www.fitpetmall.com/mall/products/1000033196"
        listing = "https://www.coupang.com/np/products/brand-shop?brandName=%EB%A1%9C%EC%96%84%EC%BA%90%EB%8B%8C"
        tracker = "https://www.lowchart.com/359008-894370"
        funiz = "https://funiz.com/goods/273469543"
        self.assertEqual(product_links.classify_stored_url(product), product_links.PRODUCT)
        self.assertEqual(product_links.classify_stored_url(fitpet), product_links.PRODUCT)
        self.assertEqual(product_links.classify_stored_url(listing), product_links.BRAND_LISTING)
        self.assertEqual(product_links.classify_stored_url(tracker), product_links.PRICE_TRACKER)
        self.assertEqual(product_links.classify_stored_url(funiz), product_links.PRICE_TRACKER)
        self.assertEqual(product_links.classify_stored_url(""), product_links.MISSING)
        self.assertEqual(product_links.classify_stored_url("not a url"), product_links.UNCONFIRMED)

        for blocked in (listing, tracker, funiz, ""):
            text = "\n".join(product_links.catalog_url_lines(blocked))
            self.assertIn("상품 URL: 없음", text)
            self.assertNotIn("http", text)

        kept = "\n".join(product_links.catalog_url_lines(product))
        self.assertIn(product, kept)
        self.assertIn("static_catalog", kept)

    def test_remote_search_pages_are_not_product_links(self):
        search = "https://search.shopping.naver.com/search/all?query=dog"
        gate = "https://smartstore.naver.com/shop/products/12345"
        blocked = "\n".join(product_links.remote_url_lines(search))
        self.assertIn("상품 URL: 없음", blocked)
        self.assertNotIn("search/all", blocked)
        passed = "\n".join(product_links.remote_url_lines(gate))
        self.assertIn(gate, passed)
        self.assertIn("naver_shopping_api", passed)
        listing = "https://www.coupang.com/np/products/brand-shop?brandName=x"
        self.assertNotIn("http", "\n".join(product_links.remote_url_lines(listing)))

    def test_partner_tag_does_not_change_the_catalog_match(self):
        product = next(item for item in server.STATIC_PRODUCTS if "/vp/products/" in (item.get("url") or ""))
        tagged = product["url"] + "&partner_id=partner"
        found = product_links.find_product_by_url(server.STATIC_PRODUCTS, tagged)
        self.assertEqual(found["id"], product["id"])
        self.assertIsNone(product_links.find_product_by_url(server.STATIC_PRODUCTS, "https://www.coupang.com/vp/products/1"))


class SearchToLinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MCP_API_KEYS"] = "dev-demo-key"
        os.environ["MCP_MONTHLY_QUOTA"] = "100"
        os.environ["MCP_API_KEY"] = "dev-demo-key"
        os.environ["MCP_USAGE_DB_PATH"] = os.path.join(self.tmp.name, "usage.db")
        os.environ.pop("FOODSAFETYKOREA_API_KEY", None)
        server.NAVER_ID = ""
        server.NAVER_SECRET = ""
        server.DB_PATH = os.path.join(self.tmp.name, "prices.db")
        server.AFFILIATE_ID = ""
        metering.reset_meter()

    async def asyncTearDown(self):
        server.AFFILIATE_ID = ""
        self.tmp.cleanup()

    def _session(self):
        return create_connected_server_and_client_session(
            server.mcp,
            read_timeout_seconds=timedelta(seconds=10),
            raise_exceptions=True,
        )

    async def test_dog_food_search_has_no_product_url_and_get_buy_link_refuses_the_listing(self):
        async with self._session() as client:
            result = await client.call_tool("search_products", {"keyword": "강아지사료", "limit": 2})
            self.assertFalse(result.isError, result)
            text = result.content[0].text
            self.assertIn("static_catalog", text)
            self.assertIn("2026-10-25", text)
            self.assertIn("시세가 아니다", text)
            self.assertEqual(text.count("상품 URL: 없음"), 2)
            self.assertNotIn("brand-shop", text)
            self.assertNotIn("lowchart.com", text)
            self.assertNotIn("현재가", text)
            self.assertNotIn("최저가", text)
            self.assertNotIn("http://", text)
            self.assertNotIn("https://", text)

            listing = next(item["url"] for item in server.STATIC_PRODUCTS if "brand-shop" in (item.get("url") or ""))
            server.AFFILIATE_ID = "partner"
            blocked = await client.call_tool("get_buy_link", {"product_url": listing})
            blocked_text = blocked.content[0].text
            self.assertFalse(blocked.isError, blocked)
            self.assertIn("Product link unavailable", blocked_text)
            self.assertNotIn("brand-shop", blocked_text)
            self.assertNotIn("partner_id", blocked_text)
            self.assertNotRegex(blocked_text, r"구매|buy now|구매하세요|지금 사")

        rows = _rows()
        self.assertGreater(len(rows), 0)
        self.assertTrue(all(row[2].startswith("2026-10-25") for row in rows))
        again_before = len(rows)
        async with self._session() as client:
            await client.call_tool("search_products", {"keyword": "강아지사료", "limit": 2})
        self.assertEqual(len(_rows()), again_before)

    async def test_catalog_product_url_round_trips_through_get_buy_link(self):
        async with self._session() as client:
            found = await client.call_tool("search_products", {"keyword": "솔가 마그네슘", "limit": 5})
            self.assertFalse(found.isError, found)
            text = found.content[0].text
            product = next(item for item in server.STATIC_PRODUCTS if "솔가 마그네슘" in item["name"])
            self.assertIn(product["url"], text)
            self.assertIn("URL 근거: static_catalog url", text)
            self.assertIn("시세가 아니다", text)
            linked = await client.call_tool("get_buy_link", {"product_url": product["url"], "product_name": product["name"]})
            self.assertFalse(linked.isError, linked)
            body = linked.content[0].text
            self.assertTrue(body.startswith("Product link\n" + product["url"]))
            self.assertIn("catalog_checked_at: 2026-10-25", body)
            self.assertIn("not the time of this call", body)
            self.assertNotIn("partner_id", body)

            server.AFFILIATE_ID = "partner"
            tagged = await client.call_tool("get_buy_link", {"product_url": product["url"]})
            tagged_text = tagged.content[0].text
            self.assertIn("partner_id=partner", tagged_text)
            self.assertIn("/vp/products/1181656", tagged_text)
            self.assertNotRegex(tagged_text, r"구매|buy now|구매하세요|지금 사")

    async def test_repeated_static_read_is_not_called_a_new_price(self):
        async with self._session() as client:
            await client.call_tool("search_products", {"keyword": "솔가 마그네슘"})
            timing = await client.call_tool(
                "check_price_timing",
                {"product_name": "솔가 마그네슘 위드 비타민 B6, 100정, 1개", "mall": "쿠팡"},
            )
        text = timing.content[0].text
        self.assertIn("1건", text)
        self.assertNotIn("현재가", text)
        self.assertIn("새 관측으로 쌓이지 않아", text)


if __name__ == "__main__":
    unittest.main()
