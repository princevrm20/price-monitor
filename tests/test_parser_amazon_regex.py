from __future__ import annotations

from price_monitor.parser_price import _amazon_regex_price_from_html, extract_price


def test_amazon_regex_finds_price_to_pay_whole():
    html = """
    blah priceToPay blah
    <span class="a-price-whole">1,234</span><span class="a-price-fraction">56</span>
    """
    assert _amazon_regex_price_from_html(html) == 1234.56


def test_extract_price_regex_fallback_with_url():
    html = "<html>xx priceToPay yy <span class=\"a-price-whole\">99</span></html>"
    assert extract_price(html, None, "https://www.amazon.in/dp/B0TEST1234") == 99.0
