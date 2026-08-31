"""Confluence links resolve against the site root, not the proxy root.

`origin` addresses the Atlassian proxy; the Confluence site sits one segment
below it at `/wiki`. An attachment's `downloadLink` omits that segment, so
joining it straight onto `origin` produced a path the control plane rejected
with a 422 and Atlassian would not have served either.
"""

import pytest

from app.atlassian import _confluence_absolute, _next_url


CLOUD = "66666666-6666-4666-8666-666666666666"
ORIGIN = f"https://api.atlassian.com/ex/confluence/{CLOUD}"


def test_a_site_relative_download_link_gains_the_wiki_segment() -> None:
    resolved = _confluence_absolute(
        ORIGIN, "/rest/api/content/7995393/child/attachment/att7045122/download"
    )
    assert resolved == (
        f"{ORIGIN}/wiki/rest/api/content/7995393/child/attachment/att7045122/download"
    )


def test_a_link_that_already_carries_wiki_is_not_doubled() -> None:
    link = "/wiki/rest/api/content/1/child/attachment/att1/download"
    assert _confluence_absolute(ORIGIN, link) == f"{ORIGIN}{link}"


def test_an_absolute_link_is_passed_through_untouched() -> None:
    absolute = f"{ORIGIN}/wiki/download/attachments/1/spec.pdf"
    assert _confluence_absolute(ORIGIN, absolute) == absolute


@pytest.mark.parametrize(
    "link",
    ["/download/attachments/7995393/spec.pdf", "/rest/api/content/1/child/attachment/a/download"],
)
def test_every_resolved_link_stays_under_the_tenant_origin(link: str) -> None:
    assert _confluence_absolute(ORIGIN, link).startswith(f"{ORIGIN}/wiki/")


def test_pagination_links_are_left_alone() -> None:
    """`_links.next` from the v2 API already includes /wiki and must not change."""

    payload = {"_links": {"next": "/wiki/api/v2/pages?cursor=abc"}}
    assert _next_url(ORIGIN, payload) == f"{ORIGIN}/wiki/api/v2/pages?cursor=abc"
