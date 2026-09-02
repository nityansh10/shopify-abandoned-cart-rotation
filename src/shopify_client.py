"""Fetch abandoned checkouts from the Shopify Admin API.

Two transports are supported:
  * "rest"    -> GET /admin/api/<ver>/checkouts.json   (returns email/phone directly,
                 including for guest checkouts -> best lead quality today)
  * "graphql" -> abandonedCheckouts query              (the long-term API; contact
                 details come from customer / addresses only)

Both return the same normalised lead dict, so main.py doesn't care which is used.
"""

import os
import time
import logging

import requests

log = logging.getLogger(__name__)

DEFAULT_API_VERSION = "2026-07"
PAGE_SIZE = 250


class ShopifyError(RuntimeError):
    pass


class ShopifyClient:
    def __init__(self, domain=None, token=None, api_version=None, mode=None):
        # Either naming convention works, so an existing .env needs no renaming.
        self.domain = (domain
                       or os.environ.get("SHOPIFY_STORE_DOMAIN")
                       or os.environ.get("SHOPIFY_STORE_URL", "")).strip()
        self.token = (token
                      or os.environ.get("SHOPIFY_ADMIN_TOKEN")
                      or os.environ.get("SHOPIFY_ACCESS_TOKEN", "")).strip()
        self.api_version = (api_version or os.environ.get("SHOPIFY_API_VERSION")
                            or DEFAULT_API_VERSION).strip()
        self.mode = (mode or os.environ.get("SHOPIFY_API_MODE") or "rest").strip().lower()

        if not self.domain:
            raise ShopifyError("SHOPIFY_STORE_URL is not set (e.g. my-store.myshopify.com)")
        if not self.token:
            raise ShopifyError("SHOPIFY_ACCESS_TOKEN is not set (the shpat_... token)")

        self.domain = self.domain.replace("https://", "").replace("http://", "").strip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": self.token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    # ------------------------------------------------------------------ http
    def _request(self, method, url, **kwargs):
        """One HTTP call, retrying on 429 and 5xx."""
        for attempt in range(5):
            resp = self.session.request(method, url, timeout=30, **kwargs)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 2))
                log.warning("Rate limited by Shopify, sleeping %.1fs", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = 2 ** attempt
                log.warning("Shopify %s, retrying in %ss", resp.status_code, wait)
                time.sleep(wait)
                continue
            if resp.status_code in (401, 403):
                raise ShopifyError(
                    "Shopify rejected the token (HTTP {}). Check SHOPIFY_ACCESS_TOKEN and make "
                    "sure the app has the read_orders scope plus access to protected customer "
                    "data. Response: {}".format(resp.status_code, resp.text[:400])
                )
            if resp.status_code >= 400:
                raise ShopifyError("Shopify HTTP {}: {}".format(resp.status_code, resp.text[:600]))
            return resp
        raise ShopifyError("Shopify kept failing after 5 attempts: {}".format(url))

    # ----------------------------------------------------------------- public
    def fetch_abandoned_checkouts(self, created_at_min):
        """created_at_min: timezone-aware datetime. Returns a list of normalised leads."""
        if self.mode == "graphql":
            raw = self._fetch_graphql(created_at_min)
        else:
            raw = self._fetch_rest(created_at_min)
        log.info("Shopify returned %d abandoned checkout(s) via %s", len(raw), self.mode)
        return raw

    def shop_name(self):
        url = "https://{}/admin/api/{}/shop.json".format(self.domain, self.api_version)
        return self._request("GET", url).json().get("shop", {}).get("name", "(unknown)")

    # ------------------------------------------------------------------- REST
    def _fetch_rest(self, created_at_min):
        url = "https://{}/admin/api/{}/checkouts.json".format(self.domain, self.api_version)
        params = {
            "limit": PAGE_SIZE,
            "status": "open",
            "created_at_min": created_at_min.isoformat(),
        }
        leads, guard = [], 0
        while url and guard < 50:
            guard += 1
            resp = self._request("GET", url, params=params)
            params = None  # later pages already carry their params in the Link header
            for checkout in resp.json().get("checkouts", []):
                leads.append(self._normalise_rest(checkout))
            url = _next_page_link(resp.headers.get("Link", ""))
        return leads

    @staticmethod
    def _normalise_rest(c):
        cust = c.get("customer") or {}
        addr = (c.get("shipping_address") or c.get("billing_address") or {})

        first = (cust.get("first_name") or addr.get("first_name") or "").strip()
        last = (cust.get("last_name") or addr.get("last_name") or "").strip()

        items, qty = [], 0
        for li in c.get("line_items") or []:
            title = (li.get("title") or "").strip()
            variant = (li.get("variant_title") or "").strip()
            n = int(li.get("quantity") or 0)
            qty += n
            if variant and variant.lower() != "default title":
                title = "{} ({})".format(title, variant)
            items.append("{} x{}".format(title, n))

        return {
            "checkout_id": str(c.get("id") or c.get("token") or ""),
            "name": c.get("name") or "",
            "created_at": c.get("created_at") or "",
            "completed_at": c.get("completed_at"),
            "email": (c.get("email") or cust.get("email") or "").strip(),
            "phone": (c.get("phone") or addr.get("phone") or cust.get("phone") or "").strip(),
            "customer_name": "{} {}".format(first, last).strip(),
            "city": addr.get("city") or "",
            "province": addr.get("province") or "",
            "country": addr.get("country") or "",
            "items": " | ".join(items),
            "item_count": qty,
            "total_price": c.get("total_price") or "",
            "currency": c.get("currency") or c.get("presentment_currency") or "",
            "recovery_url": c.get("abandoned_checkout_url") or "",
        }

    # ---------------------------------------------------------------- GraphQL
    GQL = """
    query AbandonedCheckouts($first: Int!, $after: String, $q: String) {
      abandonedCheckouts(first: $first, after: $after, query: $q, sortKey: CREATED_AT) {
        pageInfo { hasNextPage endCursor }
        edges {
          node {
            id
            name
            createdAt
            completedAt
            abandonedCheckoutUrl
            totalPriceSet { shopMoney { amount currencyCode } }
            customer { firstName lastName email phone }
            shippingAddress { firstName lastName phone city province country }
            billingAddress { firstName lastName phone city province country }
            lineItems(first: 50) { edges { node { title quantity } } }
          }
        }
      }
    }
    """

    def _fetch_graphql(self, created_at_min):
        url = "https://{}/admin/api/{}/graphql.json".format(self.domain, self.api_version)
        q = "status:open AND created_at:>='{}'".format(
            created_at_min.strftime("%Y-%m-%dT%H:%M:%SZ"))
        after, leads, guard = None, [], 0
        while guard < 50:
            guard += 1
            payload = {"query": self.GQL, "variables": {"first": 50, "after": after, "q": q}}
            body = self._request("POST", url, json=payload).json()
            if body.get("errors"):
                raise ShopifyError("GraphQL errors: {}".format(body["errors"]))
            conn = body["data"]["abandonedCheckouts"]
            for edge in conn["edges"]:
                leads.append(self._normalise_graphql(edge["node"]))
            if not conn["pageInfo"]["hasNextPage"]:
                break
            after = conn["pageInfo"]["endCursor"]
        return leads

    @staticmethod
    def _normalise_graphql(n):
        cust = n.get("customer") or {}
        addr = (n.get("shippingAddress") or n.get("billingAddress") or {})
        money = (n.get("totalPriceSet") or {}).get("shopMoney") or {}

        first = (cust.get("firstName") or addr.get("firstName") or "").strip()
        last = (cust.get("lastName") or addr.get("lastName") or "").strip()

        items, qty = [], 0
        for edge in (n.get("lineItems") or {}).get("edges") or []:
            node = edge.get("node") or {}
            k = int(node.get("quantity") or 0)
            qty += k
            items.append("{} x{}".format(node.get("title") or "", k))

        return {
            "checkout_id": (n.get("id") or "").rsplit("/", 1)[-1],
            "name": n.get("name") or "",
            "created_at": n.get("createdAt") or "",
            "completed_at": n.get("completedAt"),
            "email": (cust.get("email") or "").strip(),
            "phone": (cust.get("phone") or addr.get("phone") or "").strip(),
            "customer_name": "{} {}".format(first, last).strip(),
            "city": addr.get("city") or "",
            "province": addr.get("province") or "",
            "country": addr.get("country") or "",
            "items": " | ".join(items),
            "item_count": qty,
            "total_price": money.get("amount") or "",
            "currency": money.get("currencyCode") or "",
            "recovery_url": n.get("abandonedCheckoutUrl") or "",
        }


def _next_page_link(link_header):
    """Pull the rel="next" URL out of a Shopify Link header."""
    for part in link_header.split(","):
        if 'rel="next"' in part:
            start, end = part.find("<"), part.find(">")
            if start != -1 and end != -1:
                return part[start + 1:end]
    return None
