from __future__ import annotations

import re


def shorten_amazon_product_url(url: str) -> str:
    """Use a short /dp/ASIN URL when possible (fewer redirects, smaller HTML)."""
    m = re.search(
        r'(https?://(?:www\.)?amazon\.(?:in|com|co\.uk|de|fr|es|it|ca|com\.au|com\.be|nl|se|pl|com\.tr|ae|sa|eg))'
        r'.*?/dp/([A-Z0-9]{10})',
        url,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        return f"{m.group(1)}/dp/{m.group(2)}"
    return url
