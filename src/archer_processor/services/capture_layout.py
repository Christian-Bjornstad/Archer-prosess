"""Temporarily expose nested scrolling content for evidence capture."""
from contextlib import contextmanager


@contextmanager
def expanded_capture_layout(page, selector: str):
    import json

    page.evaluate("""() => {
        const saved = [], nodes = new Set();
        for (const target of document.querySelectorAll(SELECTOR)) {
            for (let node = target; node; node = node.parentElement) nodes.add(node);
        }
        window.__vpmCaptureLayout = {saved, x:scrollX, y:scrollY};
        for (const node of nodes) {
            saved.push({node, style:node.getAttribute('style'), x:node.scrollLeft, y:node.scrollTop});
            node.scrollLeft = 0; node.scrollTop = 0;
            const style = getComputedStyle(node);
            if (node.matches('.collapse, .accordion-collapse, .accordion-body')) {
                node.style.setProperty('display', 'block', 'important');
                node.style.setProperty('height', 'auto', 'important');
            }
            if (node.scrollHeight > node.clientHeight + 1 && /auto|scroll|hidden/.test(style.overflowY)) {
                node.style.setProperty('height', node.scrollHeight + 'px', 'important');
                node.style.setProperty('max-height', 'none', 'important');
                node.style.setProperty('overflow-y', 'visible', 'important');
                if (style.position === 'fixed' || style.position === 'absolute')
                    node.style.setProperty('position', 'relative', 'important');
            }
            if (node.scrollWidth > node.clientWidth + 1 && /auto|scroll|hidden/.test(style.overflowX)) {
                node.style.setProperty('width', node.scrollWidth + 'px', 'important');
                node.style.setProperty('max-width', 'none', 'important');
                node.style.setProperty('overflow-x', 'visible', 'important');
            }
        }
        window.scrollTo(0, 0);
    }""".replace("SELECTOR", json.dumps(selector)))
    try:
        yield
    finally:
        page.evaluate("""() => {
            const state = window.__vpmCaptureLayout;
            if (!state) return;
            for (const {node, style, x, y} of state.saved.reverse()) {
                if (style === null) node.removeAttribute('style');
                else node.setAttribute('style', style);
                node.scrollLeft=x; node.scrollTop=y;
            }
            window.scrollTo(state.x, state.y);
            delete window.__vpmCaptureLayout;
        }""")
