from __future__ import annotations

from price_monitor.parser_price import extract_price


def test_ixigo_flight_listing_returns_min_flight_offer():
    url = "https://www.ixigo.com/cheap-flights/new-delhi-pune-del-pnq"
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[
      {"@type":"Flight","name":"IndiGo","offers":{"@type":"Offer","price":"8999","priceCurrency":"INR"}},
      {"@type":"Flight","name":"SpiceJet","offers":{"@type":"Offer","price":"6050","priceCurrency":"INR"}}
    ]}
    </script>
    </head><body>noise 1.0</body></html>
    """
    assert extract_price(html, None, product_url=url) == 6050.0
