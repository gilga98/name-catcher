"""Cloudflare adds its Web Analytics script to every page, and the script reports the page's URL.

The security policy blocks it by default, and only pages with no secret in their URL opt in. Family and invite links
are bearer tokens, so board, join, account and admin pages must never use this.
"""

from csp.decorators import csp_update

allow_web_analytics = csp_update({"script-src": ["https://static.cloudflareinsights.com"]})
