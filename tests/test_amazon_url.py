from __future__ import annotations

from price_monitor.amazon_url import shorten_amazon_product_url


def test_shorten_amazon_in_long_url():
    long_u = (
        "https://www.amazon.in/DesiDiya%C2%AE-Astronaut-Light-Projector-Electric/dp/B0DH83Y8RK/"
        "ref=sr_1_1_sspa?nsdOptOutParam=true&sr=8-1-spons"
    )
    assert shorten_amazon_product_url(long_u) == "https://www.amazon.in/dp/B0DH83Y8RK"


def test_shorten_passthrough_non_amazon():
    u = "https://example.com/p/123"
    assert shorten_amazon_product_url(u) == u
