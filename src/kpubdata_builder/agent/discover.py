"""Discover API metadata from data.go.kr dataset pages.

Extracts endpoint URL, parameters, response fields, and authentication
info from the public API detail page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from kpubdata_builder import __version__

#: Honest identity for the portal's logs — no browser spoofing (#448).
_USER_AGENT = f"kpubdata-builder/{__version__} (+https://github.com/yeongseon/kpubdata-builder)"


def _refusal_message(url: str, code: int) -> str:
    """What an HTTP refusal means and what to do about it, by code.

    404 is ambiguous: this origin may be refused on detail pages (the
    portal's main page answers while ``/data/<id>/openapi.do`` does not),
    or the dataset number in the URL is simply wrong. 403 and 5xx are the
    edge refusing the origin outright.
    """
    route = (
        "run discover from the kr runner, or read the cached guide via "
        "kpubdata's scripts/fetch_guide.py"
    )
    if code == 404:
        return (
            f"the portal answered HTTP 404 for {url} — either this origin is "
            f"refused on detail pages (the portal's main page answers), or the "
            f"dataset number is wrong: check the URL, or {route}"
        )
    return f"the portal answered HTTP {code} for {url} — {route}"


class PortalUnreachable(RuntimeError):
    """The portal page could not be fetched, with the reason and the next step.

    The portal serves its detail pages selectively: from some origins the
    main page answers while ``/data/<id>/openapi.do`` refuses (403/404) or
    errors at the edge (5xx). No amount of parsing helps from a refused
    origin, so the message names where to run instead. ``status`` carries
    the HTTP code when there was one.
    """

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(slots=True)
class ParamInfo:
    """Discovered API parameter."""

    name: str
    required: bool = False
    type: str = "string"
    description: str = ""
    example: str = ""


@dataclass(slots=True)
class DiscoveryResult:
    """Result of discovering an API from data.go.kr."""

    dataset_id: str
    title: str = ""
    description: str = ""
    base_url: str = ""
    operation: str = ""
    params: list[ParamInfo] = field(default_factory=list)
    response_fields: list[str] = field(default_factory=list)
    data_go_kr_url: str = ""
    service_key_required: bool = True
    pagination: bool = True
    format_json: bool = True
    format_xml: bool = True

    def to_spec_yaml(self) -> str:
        """Generate a draft spec YAML from the discovery result."""
        lines = [
            f"id: {self.dataset_id}",
            "provider: datago",
            f"title: {self.title}",
        ]
        if self.description:
            lines.append(f"description: {self.description}")
        lines.extend(
            [
                "source:",
                f"  url: {self.data_go_kr_url}",
                "endpoint:",
                f"  base_url: {self.base_url}",
                f"  operation: {self.operation}",
                "  method: GET",
            ]
        )
        if self.format_json or self.format_xml:
            # Guess the format param name — data.go.kr uses various names
            lines.extend(
                [
                    "  format_param:",
                    "    name: dataType",
                    "    values:",
                ]
            )
            if self.format_json:
                lines.append("      json: json")
            if self.format_xml:
                lines.append("      xml: xml")
        lines.extend(
            [
                "auth:",
                "  type: query_param",
                "  param_name: serviceKey",
                "  provider_key: datago",
            ]
        )
        if self.params:
            lines.append("params:")
            for p in self.params:
                lines.append(f"- name: {p.name}")
                lines.append(f"  type: {p.type}")
                lines.append(f"  required: {str(p.required).lower()}")
                if p.description:
                    lines.append(f"  description: '{p.description}'")
                if p.example:
                    lines.append(f"  example: '{p.example}'")
        lines.extend(
            [
                "response:",
                "  format: json",
                "  envelope: datago_standard",
                "  items_path: response.body.items.item",
                "  total_count_path: response.body.totalCount",
                "  error:",
                "    style: header_result_code",
                "    code_path: response.header.resultCode",
                "    ok_values:",
                "    - '00'",
                "    - '000'",
                "    - 0",
                "pagination:",
                "  type: page_no_rows",
                "  page_param: pageNo",
                "  size_param: numOfRows",
                "  max_size: 1000",
            ]
        )
        if self.response_fields:
            lines.append("fields:")
            for fname in self.response_fields:
                lines.append(f"- name: {fname}")
                lines.append("  type: string")
        lines.extend(
            [
                "status: unstable",
                "# TODO: 활용신청 승인 후 fixture 기록 → status: active 전환",
            ]
        )
        return "\n".join(lines) + "\n"


def _fetch_page(url: str) -> str:
    """Fetch the detail page with an honest user agent and Accept headers.

    Accept and Accept-Language stay because a content-negotiating edge
    serves HTML pages to clients that ask for them. The user agent stays
    honest — this project does not spoof a browser to get past a bot
    filter on a public portal. An HTTP refusal and a network failure
    both raise PortalUnreachable naming the next step, because no amount
    of parsing helps until the page can be fetched.
    """
    req = Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:  # noqa: S310
            body = str(resp.read().decode("utf-8", errors="replace"))
    except HTTPError as exc:
        raise PortalUnreachable(_refusal_message(url, exc.code), status=exc.code) from exc
    except URLError as exc:
        raise PortalUnreachable(
            f"could not reach {url} ({exc.reason}) — check the network or the URL"
        ) from exc
    return body


def discover_from_url(url: str) -> DiscoveryResult:
    """Fetch a data.go.kr API detail page and extract metadata.

    Parameters:
        url: Full URL like https://www.data.go.kr/data/15155516/openapi.do

    Returns:
        DiscoveryResult with extracted metadata (best-effort).

    Raises:
        PortalUnreachable: The page could not be fetched from this origin.
    """
    # Extract the dataset number from the URL for fallback ID
    num_match = re.search(r"/data/(\d+)/", url)
    dataset_num = num_match.group(1) if num_match else "unknown"

    html = _fetch_page(url)

    result = DiscoveryResult(
        dataset_id=f"datago.dataset_{dataset_num}",
        data_go_kr_url=url,
    )

    # Try to extract title
    title_match = re.search(r"<title>([^<]+)</title>", html)
    if title_match:
        raw_title = title_match.group(1).strip()
        # Clean up common suffixes
        for suffix in ("| 공공데이터포털", "- 공공데이터포털"):
            raw_title = raw_title.replace(suffix, "").strip()
        result.title = raw_title

    # Try to extract endpoint from page content
    # data.go.kr pages often contain the API URL in various formats
    endpoint_match = re.search(r"(https?://apis\.data\.go\.kr/[^\s\"'<]+)", html)
    if endpoint_match:
        full_url = endpoint_match.group(1).rstrip("/")
        # Split into base_url and operation
        parts = full_url.rsplit("/", 1)
        if len(parts) == 2:
            result.base_url = parts[0]
            result.operation = parts[1]
        else:
            result.base_url = full_url
            result.operation = ""

    # Extract parameter names from HTML tables
    # Common patterns: <td>paramName</td> or parameter listings
    param_names = re.findall(
        r"<td[^>]*>\s*([\w]+)\s*</td>\s*<td[^>]*>\s*(필수|선택|[YN]|[01])",
        html,
        re.IGNORECASE,
    )
    seen: set[str] = set()
    for pname, required_str in param_names:
        if pname in seen or pname in ("serviceKey", "pageNo", "numOfRows", "dataType", "type"):
            continue
        seen.add(pname)
        required = required_str.strip() in ("필수", "Y", "1")
        result.params.append(ParamInfo(name=pname, required=required))

    return result
