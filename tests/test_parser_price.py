from __future__ import annotations

from price_monitor.parser_price import extract_price


def test_extract_price_json_ld_product():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Product","name":"X",
     "offers":{"@type":"Offer","price":"49.99","priceCurrency":"USD"}}
    </script>
    </head><body></body></html>
    """
    assert extract_price(html, None) == 49.99


def test_extract_price_json_ld_course():
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Course","name":"Intro",
     "offers":{"@type":"Offer","price":"12.50","priceCurrency":"USD"}}
    </script>
    </head><body></body></html>
    """
    assert extract_price(html, None) == 12.5


def test_extract_price_meta_og():
    html = """
    <html><head>
    <meta property="product:price:amount" content="120.50" />
    </head><body></body></html>
    """
    assert extract_price(html, None) == 120.5


def test_extract_price_css_selector():
    html = """
    <html><body>
    <div class="price">$1,234.56</div>
    </body></html>
    """
    assert extract_price(html, ".price") == 1234.56


def test_extract_price_selector_european_style():
    html = "<html><body><span id='p'>1.234,56 EUR</span></body></html>"
    assert extract_price(html, "#p") == 1234.56


def test_selector_wins_over_meta():
    html = """
    <html><head><meta property="product:price:amount" content="999" /></head>
    <body><div id="live">10.00</div></body></html>
    """
    assert extract_price(html, "#live") == 10.0
