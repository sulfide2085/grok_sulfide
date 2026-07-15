"""DOM helpers for xAI device-code approval UI."""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

logger = logging.getLogger("grok_sulfide.cpa.browser_ui")

LogFn = Callable[[str], None]

def _page_url(page: Any) -> str:
    try:
        return page.url or ""
    except Exception:
        return ""


def _visible_text(page: Any) -> str:
    try:
        t = page.run_js(
            "return (document.body && (document.body.innerText || document.body.textContent)) || '';"
        )
        if isinstance(t, str) and t.strip():
            return t
    except Exception:
        logger.debug("suppressed exception", exc_info=True)
    try:
        raw = getattr(page, "raw_text", None)
        if callable(raw):
            t = raw()
            if isinstance(t, str) and t.strip():
                return t
        if isinstance(raw, str) and raw.strip():
            return raw
    except Exception:
        logger.debug("suppressed exception", exc_info=True)
    return ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _find_button_exact(page: Any, label: str) -> Any | None:
    try:
        for el in page.eles("tag:button") or []:
            try:
                if _norm(el.text or "") == label:
                    return el
            except Exception:
                continue
    except Exception:
        logger.debug("suppressed exception", exc_info=True)
    try:
        return page.ele(f"xpath://button[normalize-space(.)='{label}']", timeout=0.3)
    except Exception:
        return None


def _cookie_banner_visible(text: str) -> bool:
    """Return whether a cookie/privacy overlay is blocking the page."""
    t = text or ""
    tl = t.lower()
    signals = (
        "隐私偏好",
        "全部允许",
        "全部拒绝",
        "privacy preference",
        "manage cookies",
        "we use cookies",
        "我们使用 cookie",
        "accept all cookies",
        "cookie preferences",
    )
    return any(signal in t or signal in tl for signal in signals)


def _dismiss_cookie_banner(page: Any, log: LogFn) -> bool:
    """Dismiss the cookie overlay without confusing it with OAuth Allow."""
    if not _cookie_banner_visible(_visible_text(page)):
        return False

    labels = [
        "全部允许",
        "接受所有",
        "接受全部",
        "Accept all",
        "Accept All",
        "Allow all",
        "Allow All",
        "I agree",
        "Agree",
    ]
    hit = _click_exact(page, labels, log, real=False)
    if hit:
        log(f"cookie banner dismissed via {hit!r}")
        _sleep(0.8)
        return True

    try:
        hit = page.run_js(
            """
const labels = new Set([
  '全部允许','接受所有','接受全部',
  'Accept all','Accept All','Allow all','Allow All','I agree','Agree'
]);
const candidates = Array.from(document.querySelectorAll('button,[role="button"],a'));
const button = candidates.find((node) =>
  labels.has(String(node.innerText || node.textContent || '').trim())
);
if (button) { button.click(); return String(button.innerText || '').trim(); }
return '';
            """
        )
        if hit:
            log(f"cookie banner dismissed via JS {hit!r}")
            _sleep(0.8)
            return True
    except Exception as e:
        log(f"cookie banner JS dismiss failed: {e}")

    hit = _click_exact(page, ["全部拒绝", "Reject all", "Reject All", "Decline"], log)
    if hit:
        log(f"cookie banner dismissed via reject {hit!r}")
        _sleep(0.8)
        return True
    return False


def _click_exact(
    page: Any,
    labels: list[str],
    log: LogFn,
    *,
    real: bool = False,
) -> str | None:
    """Click button by EXACT visible text. real=True uses physical click (needed for consent)."""
    for label in labels:
        el = _find_button_exact(page, label)
        if not el:
            continue
        try:
            if real:
                try:
                    el.scroll.to_see()
                except Exception:
                    logger.debug("suppressed exception", exc_info=True)
                el.click()
                log(f"clicked REAL exact {label!r}")
            else:
                el.click(by_js=True)
                log(f"clicked JS exact {label!r}")
            return label
        except Exception as e:
            log(f"click {label!r} failed: {e}")
            if real:
                try:
                    el.click(by_js=True)
                    log(f"clicked JS fallback exact {label!r}")
                    return label
                except Exception as e2:
                    log(f"js fallback {label!r} failed: {e2}")
    return None


def _fill(page: Any, selector: str, value: str, log: LogFn, label: str) -> bool:
    """Fill an input reliably without logging its sensitive value."""
    try:
        el = page.ele(selector, timeout=0.8)
    except Exception as e:
        log(f"{label} input lookup failed: {e}")
        return False
    if el is None:
        return False

    for attempt in range(1, 4):
        try:
            current = str(getattr(el, "value", "") or el.attr("value") or "")
            if current == value:
                return True
            el.clear()
            el.input(value)
            current = str(getattr(el, "value", "") or el.attr("value") or "")
            if current == value:
                log(f"filled {label}")
                return True
        except Exception as e:
            if attempt == 3:
                log(f"fill {label} failed: {e}")
                return False
        _sleep(0.2)
    return False


def _wait_turnstile(page: Any, log: LogFn, timeout: float = 45.0) -> bool:
    """Wait/click Cloudflare Turnstile on the mint browser page."""
    deadline = time.time() + timeout
    clicked = False
    while time.time() < deadline:
        try:
            el = page.ele("css:input[name='cf-turnstile-response']", timeout=0.3)
            if el is not None:
                v = (el.attr("value") or "").strip()
                if len(v) > 20:
                    log(f"turnstile ready len={len(v)}")
                    return True
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        # Mimic register-machine: shadow-root checkbox click
        try:
            challenge_input = page.ele("@name=cf-turnstile-response", timeout=0.2)
            if challenge_input is not None:
                wrapper = challenge_input.parent()
                iframe = None
                try:
                    iframe = wrapper.shadow_root.ele("tag:iframe")
                except Exception:
                    iframe = None
                if iframe is not None:
                    try:
                        iframe.run_js(
                            """
window.dtp = 1;
function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
let sx = getRandomInt(800, 1200);
let sy = getRandomInt(400, 700);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy });
                            """
                        )
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)
                    try:
                        body_sr = iframe.ele("tag:body").shadow_root
                        btn = body_sr.ele("tag:input")
                        if btn is not None:
                            btn.click()
                            if not clicked:
                                log("clicked turnstile shadow checkbox")
                                clicked = True
                    except Exception:
                        logger.debug("suppressed exception", exc_info=True)
        except Exception:
            logger.debug("suppressed exception", exc_info=True)

        if not clicked:
            try:
                page.run_js(
                    """
const nodes = Array.from(document.querySelectorAll('div,span,iframe')).filter((n) => {
  const txt = (n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute?.('src') || '');
  return String(txt).toLowerCase().includes('turnstile');
});
if (nodes.length && typeof nodes[0].click === 'function') nodes[0].click();
                    """
                )
                clicked = True
                log("clicked turnstile container via JS")
            except Exception:
                logger.debug("suppressed exception", exc_info=True)
        _sleep(0.9)
    log("turnstile not ready")
    return False


