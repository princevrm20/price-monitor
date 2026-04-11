from __future__ import annotations

from price_monitor.parser_price import extract_price


def test_amazon_empty_offscreen_uses_price_whole():
    html = """
    <html><body>
    <span class="a-price aok-align-center reinventPricePriceToPayMargin priceToPay apex-pricetopay-value">
      <span class="a-offscreen"> </span>
      <span aria-hidden="true">
        <span class="a-price-symbol">&#8377;</span>
        <span class="a-price-whole">578</span>
      </span>
    </span>
    </body></html>
    """
    url = "https://www.amazon.in/dp/B0DH83Y8RK"
    assert extract_price(html, ".a-price .a-offscreen", url) == 578.0


def test_amazon_whole_and_fraction():
    html = """
    <html><body>
    <span class="a-price priceToPay">
      <span class="a-offscreen"></span>
      <span class="a-price-whole">1,234</span><span class="a-price-fraction">56</span>
    </span>
    </body></html>
    """
    assert (
        extract_price(html, ".a-price.priceToPay .a-offscreen", "https://www.amazon.in/dp/X")
        == 1234.56
    )
