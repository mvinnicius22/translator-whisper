#!/usr/bin/env python3
"""
Stage C renderer: lecture_notes.md → lecture_notes.pdf + flashcards.csv

Pipeline:
  1. Parse YAML front matter
  2. Pre-process special fenced blocks and inline patterns
  3. Render markdown body → HTML (markdown-it-py + container + dollarmath plugins)
  4. Build TOC, aggregate avisos box, collect glossário
  5. Assemble full HTML page
  6. Playwright → PDF
  7. Export flashcards.csv
"""

import argparse
import csv
import json
import re
import sys
import textwrap
import uuid
from pathlib import Path

import yaml
from markdown_it import MarkdownIt
from mdit_py_plugins.container import container_plugin
from mdit_py_plugins.dollarmath import dollarmath_plugin


# ── Constants ─────────────────────────────────────────────────────────────────

CALLOUT_TYPES = [
    "aviso", "conceito", "definicao", "exemplo",
    "atencao", "prova", "exercicio", "resumo", "qa",
]

CALLOUT_CONFIG = {
    "aviso":     {"label": "Aviso",           "icon": "📢", "color": "#e65100", "bg": "#fff3e0"},
    "conceito":  {"label": "Conceito",         "icon": "💡", "color": "#1565c0", "bg": "#e3f2fd"},
    "definicao": {"label": "Definição",        "icon": "📖", "color": "#2e7d32", "bg": "#e8f5e9"},
    "exemplo":   {"label": "Exemplo",          "icon": "🔍", "color": "#6a1b9a", "bg": "#f3e5f5"},
    "atencao":   {"label": "Atenção",          "icon": "⚠️",  "color": "#f57f17", "bg": "#fffde7"},
    "prova":     {"label": "Cai em prova",     "icon": "📝", "color": "#880e4f", "bg": "#fce4ec"},
    "exercicio": {"label": "Exercício",        "icon": "✏️",  "color": "#004d40", "bg": "#e0f2f1"},
    "resumo":    {"label": "Resumo",           "icon": "🗒️",  "color": "#37474f", "bg": "#eceff1"},
    "qa":        {"label": "Pergunta & Resposta", "icon": "💬", "color": "#0277bd", "bg": "#e1f5fe"},
}

CHART_TYPE_MAP = {"bar": "bar", "line": "line", "pie": "pie", "scatter": "scatter"}


# ── Front matter ──────────────────────────────────────────────────────────────

def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split YAML front matter from body. Returns (metadata_dict, body_text)."""
    if not text.startswith("---"):
        return {}, text
    parts = re.split(r'^---\s*$', text, maxsplit=2, flags=re.MULTILINE)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()


# ── Pre-processing ────────────────────────────────────────────────────────────

def preprocess(body: str, slides_dir: Path | None, output_dir: Path) -> tuple[str, list, dict]:
    """
    Replace special fenced blocks and inline patterns with HTML snippets.
    Returns (processed_body, flashcard_pairs, slide_cites).
    slide_cites maps slide_num -> anchor id of its first inline citation.
    """
    flashcards = []
    slide_cites: dict[int, str] = {}  # n -> "slide-cite-N"

    # ── ```mermaid blocks ───────────────────────────────────────────────────
    def replace_mermaid(m):
        content = m.group(1).strip()
        cid = uuid.uuid4().hex[:8]
        return (
            f'<div class="mermaid-block">'
            f'<div class="mermaid" id="mermaid-{cid}">{_esc(content)}</div>'
            f'</div>'
        )
    body = re.sub(r'```mermaid\n(.*?)```', replace_mermaid, body, flags=re.DOTALL)

    # ── ```chart blocks ─────────────────────────────────────────────────────
    def replace_chart(m):
        raw = m.group(1).strip()
        try:
            spec = json.loads(raw)
        except json.JSONDecodeError:
            return f'<pre class="parse-error">Invalid chart spec:\n{_esc(raw)}</pre>'
        return _chart_to_html(spec)
    body = re.sub(r'```chart\n(.*?)```', replace_chart, body, flags=re.DOTALL)

    # ── ```timeline blocks ──────────────────────────────────────────────────
    def replace_timeline(m):
        lines = [l.strip() for l in m.group(1).strip().splitlines() if l.strip()]
        items = []
        for line in lines:
            # Expected: "MM:SS — title" or "MM:SS - title"
            match = re.match(r'^(\d{1,2}:\d{2}(?::\d{2})?)\s*[—\-]\s*(.+)$', line)
            if match:
                items.append((match.group(1), match.group(2)))
        if not items:
            return ""
        rows = "".join(
            f'<div class="timeline-item">'
            f'<span class="timeline-time">{t}</span>'
            f'<span class="timeline-title">{_esc(title)}</span>'
            f'</div>'
            for t, title in items
        )
        return f'<div class="timeline-block">{rows}</div>'
    body = re.sub(r'```timeline\n(.*?)```', replace_timeline, body, flags=re.DOTALL)

    # ── ```flashcards blocks ────────────────────────────────────────────────
    def replace_flashcards(m):
        raw = m.group(1).strip()
        pairs = _parse_flashcards(raw)
        flashcards.extend(pairs)
        return "<!-- FLASHCARDS_APPENDIX -->"
    # Allow unclosed fence (agent sometimes omits closing ```)
    body = re.sub(r'```flashcards\n(.*?)(?:```|$)', replace_flashcards, body, flags=re.DOTALL)

    # ── Slide embeds ![[slide:N]] → clickable chip linking to appendix ─────
    def replace_slide_chip(m):
        n = int(m.group(1))
        if n not in slide_cites:
            slide_cites[n] = f"slide-cite-{n}"
        return (
            f'<a href="#material-slide-{n}" id="slide-cite-{n}" '
            f'class="slide-ref-chip">📎 Slide {n}</a>'
        )

    body = re.sub(r'!\[\[slide:(\d+)\]\]', replace_slide_chip, body)

    # ── Time-anchored headings ## [MM:SS] Title ─────────────────────────────
    def replace_time_heading(m):
        level = len(m.group(1))
        ts = m.group(2)
        title = m.group(3).strip()
        slug = _slugify(title)
        chip = f'<span class="time-anchor">{ts}</span>'
        return f'<h{level} id="{slug}">{chip} {_esc(title)}</h{level}>'
    body = re.sub(
        r'^(#{2,4})\s+\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s+(.+)$',
        replace_time_heading,
        body,
        flags=re.MULTILINE,
    )

    # ── Plain headings (add id for TOC anchors) ─────────────────────────────
    def replace_plain_heading(m):
        level = len(m.group(1))
        title = m.group(2).strip()
        slug = _slugify(title)
        return f'<h{level} id="{slug}">{_esc(title)}</h{level}>'
    body = re.sub(
        r'^(#{2,4})\s+(?!\[)(.+)$',
        replace_plain_heading,
        body,
        flags=re.MULTILINE,
    )

    return body, flashcards, slide_cites


def _parse_flashcards(raw: str) -> list[tuple[str, str]]:
    pairs = []
    current_q = None
    current_r_lines: list[str] = []

    def _flush():
        if current_q and current_r_lines:
            pairs.append((current_q, " ".join(current_r_lines)))

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("P:"):
            _flush()
            current_q = stripped[2:].strip()
            current_r_lines = []
        elif stripped.startswith("R:") and current_q is not None:
            current_r_lines = [stripped[2:].strip()]
        elif current_r_lines is not None and stripped and current_q is not None:
            # Continuation line of the current answer
            current_r_lines.append(stripped)

    _flush()
    return pairs


def _chart_to_html(spec: dict) -> str:
    tipo = CHART_TYPE_MAP.get(spec.get("tipo", "bar"), "bar")
    titulo = spec.get("titulo", "")
    labels = spec.get("labels", [])
    series = spec.get("series", [])
    eixo_x = spec.get("eixo_x", "")
    eixo_y = spec.get("eixo_y", "")
    cid = uuid.uuid4().hex[:8]

    datasets = [
        {
            "label": s.get("nome", ""),
            "data": s.get("dados", []),
            "backgroundColor": _chart_colors(len(s.get("dados", []))),
        }
        for s in series
    ]

    config = {
        "type": tipo,
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "responsive": True,
            "plugins": {"title": {"display": bool(titulo), "text": titulo}},
            "scales": {
                "x": {"title": {"display": bool(eixo_x), "text": eixo_x}},
                "y": {"title": {"display": bool(eixo_y), "text": eixo_y}},
            } if tipo != "pie" else {},
        },
    }

    config_json = json.dumps(config)
    return (
        f'<div class="chart-block">'
        f'<canvas id="chart-{cid}"></canvas>'
        f'<script>new Chart(document.getElementById("chart-{cid}"), {config_json});</script>'
        f'</div>'
    )


def _chart_colors(n: int) -> list[str]:
    palette = [
        "rgba(54,162,235,0.7)", "rgba(255,99,132,0.7)", "rgba(75,192,192,0.7)",
        "rgba(255,159,64,0.7)", "rgba(153,102,255,0.7)", "rgba(255,205,86,0.7)",
    ]
    return [palette[i % len(palette)] for i in range(n)]


def _slugify(text: str) -> str:
    text = re.sub(r'[^\w\s-]', '', text.lower())
    return re.sub(r'[\s]+', '-', text).strip('-')


def _esc(text: str) -> str:
    return (text.replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;').replace('"', '&quot;'))


# ── Markdown rendering ────────────────────────────────────────────────────────

def build_md() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True, "breaks": False})
    md.use(dollarmath_plugin, allow_labels=True, allow_space=True)
    for ct in CALLOUT_TYPES:
        md.use(container_plugin, ct)
    md.enable("table")
    return md


def _make_container_open(ct: str):
    cfg = CALLOUT_CONFIG.get(ct, {"label": ct.title(), "icon": "•", "color": "#555", "bg": "#f9f9f9"})

    def _render(tokens, idx, options, env):
        return (
            f'<div class="callout callout-{ct}" '
            f'style="border-left-color:{cfg["color"]};background:{cfg["bg"]}">'
            f'<div class="callout-header" style="color:{cfg["color"]}">'
            f'<span class="callout-icon">{cfg["icon"]}</span>'
            f'<span class="callout-label">{cfg["label"]}</span>'
            f'</div>'
            f'<div class="callout-body">'
        )

    return _render


def patch_container_renderers(md: MarkdownIt):
    for ct in CALLOUT_TYPES:
        open_rule = f"container_{ct}_open"
        close_rule = f"container_{ct}_close"
        if open_rule in md.renderer.rules:
            md.renderer.rules[open_rule] = _make_container_open(ct)
        if close_rule in md.renderer.rules:
            md.renderer.rules[close_rule] = lambda tokens, idx, options, env: '</div></div>'


def markdown_to_html(body: str) -> str:
    md = build_md()
    patch_container_renderers(md)
    return md.render(body)


# ── TOC ───────────────────────────────────────────────────────────────────────

def build_toc(html_body: str) -> str:
    """Build a clickable TOC from h2/h3 tags already in the HTML body."""
    headings = re.findall(r'<h([23])[^>]*id="([^"]+)"[^>]*>.*?</h\1>', html_body, re.DOTALL)
    if not headings:
        return ""

    items = []
    for level, slug in headings:
        # Extract visible text (strip inner tags)
        full = re.search(
            rf'<h{level}[^>]*id="{re.escape(slug)}"[^>]*>(.*?)</h{level}>',
            html_body, re.DOTALL
        )
        if full:
            inner = re.sub(r'<span class="time-anchor">[^<]*</span>', '', full.group(1))
            title = re.sub(r'<[^>]+>', '', inner).strip()
        else:
            title = slug
        indent = "    " if level == "3" else ""
        items.append(
            f'{indent}<li><a href="#{slug}">{_esc(title)}</a></li>'
        )

    return '<nav class="toc"><h2>Sumário</h2><ul>\n' + "\n".join(items) + "\n</ul></nav>"


# ── Avisos aggregation ────────────────────────────────────────────────────────

def extract_avisos_box(md_body: str) -> str:
    """Scan for ::: aviso blocks and build a top-of-page highlights box."""
    pattern = re.compile(r'^:::[ \t]+aviso[ \t]*$(.*?)^:::[ \t]*$', re.MULTILINE | re.DOTALL)
    matches = pattern.findall(md_body)
    if not matches:
        return ""

    cfg = CALLOUT_CONFIG["aviso"]
    items = "".join(
        f"<li>{_esc(m.strip())}</li>" for m in matches if m.strip()
    )
    return (
        f'<div class="avisos-box" style="border-color:{cfg["color"]};background:{cfg["bg"]}">'
        f'<div class="avisos-title" style="color:{cfg["color"]}">'
        f'{cfg["icon"]} Avisos da Aula</div>'
        f'<ul>{items}</ul>'
        f'</div>'
    )


# ── Glossário ─────────────────────────────────────────────────────────────────

def build_glossario(html_body: str) -> str:
    """Collect all definicao callout bodies for the glossário appendix."""
    divs = re.findall(
        r'<div class="callout callout-definicao"[^>]*>.*?<div class="callout-body">(.*?)</div></div>',
        html_body, re.DOTALL
    )
    if not divs:
        return ""
    items = "".join(f'<div class="glossario-item">{d.strip()}</div>' for d in divs)
    return (
        '<section class="appendix" id="glossario">'
        '<h2>Glossário</h2>'
        f'{items}'
        '</section>'
    )


# ── Flashcards appendix ───────────────────────────────────────────────────────

def build_flashcards_appendix(pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return ""
    cards = "".join(
        f'<div class="flashcard">'
        f'<div class="flashcard-q"><strong>P:</strong> {_esc(q)}</div>'
        f'<div class="flashcard-a"><strong>R:</strong> {_esc(a)}</div>'
        f'</div>'
        for q, a in pairs
    )
    return (
        '<section class="appendix" id="flashcards">'
        '<h2>Flashcards</h2>'
        f'<div class="flashcards-grid">{cards}</div>'
        '</section>'
    )


def export_flashcards_csv(pairs: list[tuple[str, str]], csv_path: Path):
    if not pairs:
        return
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(["Front", "Back"])
        w.writerows(pairs)
    print(f"  Flashcards CSV: {csv_path}")


# ── Slides appendix ───────────────────────────────────────────────────────────

def build_slides_appendix(slides_dir: Path | None, output_dir: Path,
                          slide_cites: dict[int, str]) -> str:
    if not slides_dir or not slides_dir.exists():
        return ""
    imgs = sorted(
        slides_dir.glob("slide-*.png"),
        key=lambda p: int(re.search(r'\d+', p.stem).group()),
    )
    if not imgs:
        return ""
    figs = []
    for img_path in imgs:
        n = int(re.search(r'\d+', img_path.stem).group())
        rel = img_path.relative_to(output_dir)
        caption = f"Slide {n}"
        if n in slide_cites:
            caption += ' <span class="slide-back-label">↩ voltar ao texto</span>'
        fig = (
            f'<figure id="material-slide-{n}" class="slide-figure">'
            f'<img src="{rel}" alt="Slide {n}" class="slide-img">'
            f'<figcaption>{caption}</figcaption>'
            f'</figure>'
        )
        if n in slide_cites:
            fig = f'<a href="#{slide_cites[n]}" class="slide-figure-link">{fig}</a>'
        figs.append(fig)
    return (
        '<section class="appendix" id="material-apoio">'
        '<h2>Material de Apoio</h2>'
        f'<div class="slides-grid">{"".join(figs)}</div>'
        '</section>'
    )


# ── Cover ─────────────────────────────────────────────────────────────────────

def build_cover(meta: dict) -> str:
    titulo = _esc(meta.get("titulo", "Notas de Aula"))
    disciplina = _esc(meta.get("disciplina", ""))
    professor = _esc(meta.get("professor", ""))
    data = _esc(meta.get("data", ""))

    extras = ""
    if disciplina:
        extras += f'<div class="cover-meta">{disciplina}</div>'
    if professor:
        extras += f'<div class="cover-meta">Prof. {professor}</div>'
    if data:
        extras += f'<div class="cover-meta">{data}</div>'

    return (
        '<div class="cover">'
        '<div class="cover-inner">'
        '<div class="cover-accent"></div>'
        f'<h1 class="cover-title">{titulo}</h1>'
        f'{extras}'
        '</div>'
        '</div>'
    )


# ── HTML template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
{css}
</style>
</head>
<body>
{cover}
<div class="page-break"></div>
{avisos_box}
{toc}
<main>
{body}
{flashcards_appendix}
{glossario}
{slides_appendix}
</main>
<script>
(function() {{
  // Mermaid
  mermaid.initialize({{ startOnLoad: true, theme: 'neutral' }});

  // Signal render ready after all async libs settle
  var ready = false;
  function checkReady() {{
    if (ready) return;
    var pending = document.querySelectorAll('.mermaid:not([data-processed])').length;
    if (pending === 0) {{
      Promise.all([
        typeof MathJax !== 'undefined' ? MathJax.startup.promise : Promise.resolve(),
      ]).then(function() {{
        ready = true;
        window.renderReady = true;
      }});
    }}
  }}
  // Mermaid fires svgInserted for each diagram; fall back to timeout
  mermaid.initialize({{
    startOnLoad: true,
    theme: 'neutral',
    securityLevel: 'loose',
  }});
  setTimeout(function() {{
    window.renderReady = true;
  }}, 4000);
}})();
</script>
</body>
</html>
"""

NOTES_CSS = """\
/* ── Reset & base ── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Georgia', 'Times New Roman', serif;
  font-size: 11pt;
  line-height: 1.7;
  color: #1a1a1a;
  background: #fff;
  padding: 0;
}
h1 { font-size: 2em; margin-bottom: 0.4em; }
h2 { font-size: 1.45em; margin: 1.8em 0 0.5em; border-bottom: 2px solid #ddd; padding-bottom: 0.2em; }
h3 { font-size: 1.2em; margin: 1.3em 0 0.4em; }
h4 { font-size: 1em; margin: 1em 0 0.3em; font-style: italic; }
p  { margin-bottom: 0.8em; }
ul, ol { margin: 0.5em 0 0.8em 1.6em; }
li { margin-bottom: 0.3em; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 0.95em; }
th { background: #37474f; color: #fff; padding: 0.5em 0.8em; text-align: left; }
td { padding: 0.45em 0.8em; border-bottom: 1px solid #e0e0e0; }
tr:nth-child(even) td { background: #f5f5f5; }
code { font-family: 'Courier New', monospace; font-size: 0.88em; background: #f4f4f4;
  padding: 0.1em 0.35em; border-radius: 3px; }
pre { background: #f4f4f4; padding: 1em; border-radius: 4px; overflow-x: auto;
  margin: 0.8em 0; font-size: 0.88em; }
a { color: #1565c0; }

/* ── Page layout (print) ── */
@page {
  size: A4;
  margin: 20mm 18mm 22mm 18mm;
}
@media print {
  body { background: #fff; }
  .page-break { page-break-after: always; break-after: page; }
  h2, h3 { page-break-after: avoid; }
  .callout, figure, .flashcard { break-inside: avoid; }
}
.page-break { page-break-after: always; break-after: page; }

/* ── Cover ── */
.cover {
  display: flex; align-items: center; justify-content: center;
  min-height: 90vh; text-align: center;
}
.cover-inner { max-width: 480px; }
.cover-accent {
  height: 6px; background: linear-gradient(90deg, #1565c0, #26c6da);
  border-radius: 3px; margin-bottom: 2.5em;
}
.cover-title {
  font-size: 2.2em; font-weight: bold; color: #1a1a1a; margin-bottom: 0.8em; line-height: 1.25;
}
.cover-meta { color: #555; font-size: 1em; margin-bottom: 0.3em; }

/* ── TOC ── */
.toc {
  background: #f8f9fa; border: 1px solid #e0e0e0; border-radius: 6px;
  padding: 1.2em 1.5em; margin-bottom: 2em;
}
.toc h2 { font-size: 1.1em; border: none; margin: 0 0 0.8em; }
.toc ul { list-style: none; margin: 0; padding: 0; }
.toc li { margin-bottom: 0.35em; }
.toc li a { color: #1565c0; text-decoration: none; font-size: 0.95em; }
.toc li a:hover { text-decoration: underline; }
.toc li + li { border-top: 1px solid #eee; padding-top: 0.35em; }
/* Indent h3 items */
.toc li:has(a[href*="###"]) { padding-left: 1.2em; }

/* ── Avisos box ── */
.avisos-box {
  border: 2px solid; border-radius: 6px; padding: 1em 1.2em; margin-bottom: 2em;
}
.avisos-title { font-weight: bold; font-size: 1em; margin-bottom: 0.5em; }
.avisos-box ul { margin: 0 0 0 1.2em; }

/* ── Callouts ── */
.callout {
  border-left: 4px solid; border-radius: 4px; padding: 0.8em 1em; margin: 1em 0;
}
.callout-header {
  display: flex; align-items: center; gap: 0.4em;
  font-weight: bold; font-size: 0.9em; margin-bottom: 0.5em;
}
.callout-icon { font-size: 1em; }
.callout-body { font-size: 0.97em; }

/* Q&A special layout */
.callout-qa .callout-body p {
  margin: 0.3em 0;
  line-height: 1.6;
}
.callout-qa .callout-body p strong:first-child {
  display: inline;
  font-weight: 700;
  color: #0277bd;
  margin-right: 0.3em;
}

/* ── Time anchor chip ── */
.time-anchor {
  display: inline-block; background: #f5f5f5; color: #546e7a;
  font-size: 0.72em; font-family: monospace; font-weight: 600;
  padding: 0.1em 0.5em; border-radius: 3px; margin-right: 0.5em;
  vertical-align: middle; border: 1px solid #cfd8dc;
}

/* ── Timeline ── */
.timeline-block {
  border-left: 3px solid #1565c0; margin: 1em 0; padding-left: 1em;
}
.timeline-item {
  display: flex; gap: 1em; margin-bottom: 0.5em; align-items: baseline;
}
.timeline-time {
  font-family: monospace; font-size: 0.85em; color: #1565c0;
  min-width: 5em; font-weight: bold;
}
.timeline-title { color: #333; }

/* ── Charts ── */
.chart-block {
  max-width: 560px; margin: 1.2em auto; padding: 0.5em;
  border: 1px solid #e0e0e0; border-radius: 6px;
}

/* ── Mermaid ── */
.mermaid-block {
  margin: 1.2em 0; overflow-x: auto; text-align: center;
}
.mermaid-block svg { max-width: 100%; height: auto; }

/* ── Slide reference chip (inline) ── */
.slide-ref-chip {
  display: inline-flex; align-items: center; gap: 0.25em;
  background: #fce4ec; color: #ad1457; border: 1px solid #f48fb1;
  border-radius: 4px; padding: 0.15em 0.55em;
  font-size: 0.82em; font-family: sans-serif; font-weight: 600;
  text-decoration: none; vertical-align: middle; white-space: nowrap;
}

/* ── Slides appendix ── */
.slides-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 1em; }
.slide-figure { text-align: center; border: 1px solid #e0e0e0; border-radius: 4px; padding: 0.5em; }
.slide-img { max-width: 100%; height: auto; border-radius: 2px; }
figcaption { font-size: 0.8em; color: #777; margin-top: 0.3em; }
.slide-figure-link { display: block; text-decoration: none; color: inherit; }
.slide-figure-link:hover .slide-figure { border-color: #1565c0; box-shadow: 0 1px 5px rgba(21,101,192,0.18); }
.slide-back-label { display: block; margin-top: 0.4em; font-size: 0.88em; font-weight: 600; color: #1565c0; }

/* ── Flashcards ── */
.flashcards-grid { display: grid; grid-template-columns: 1fr; gap: 0.8em; }
.flashcard {
  border: 1px solid #e0e0e0; border-radius: 6px; padding: 0.8em 1em;
  background: #fafafa;
}
.flashcard-q { color: #0277bd; margin-bottom: 0.4em; }
.flashcard-a { color: #2e7d32; }

/* ── Glossário ── */
.glossario-item {
  border-bottom: 1px solid #eee; padding: 0.5em 0; margin-bottom: 0.3em;
}

/* ── Appendix ── */
.appendix { margin-top: 3em; padding-top: 1em; border-top: 2px solid #eee; }
.appendix h2 { font-size: 1.3em; }

/* ── Parse error ── */
.parse-error { background: #ffebee; border-left: 4px solid #c62828; padding: 0.8em; }
"""


# ── Assemble ──────────────────────────────────────────────────────────────────

def assemble_html(meta: dict, body_html: str, toc_html: str, avisos_html: str,
                  flashcards_html: str, glossario_html: str, slides_html: str) -> str:
    cover = build_cover(meta)
    # Replace flashcards marker with actual appendix HTML
    body_html = body_html.replace("<!-- FLASHCARDS_APPENDIX -->", "")

    return HTML_TEMPLATE.format(
        title=meta.get("titulo", "Notas de Aula"),
        css=NOTES_CSS,
        cover=cover,
        avisos_box=avisos_html,
        toc=toc_html,
        body=body_html,
        flashcards_appendix=flashcards_html,
        glossario=glossario_html,
        slides_appendix=slides_html,
    )


# ── Playwright PDF ────────────────────────────────────────────────────────────

def render_pdf(html_path: Path, pdf_path: Path):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Error: playwright not installed. Run: pip install playwright && playwright install chromium",
              file=sys.stderr)
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(f"file://{html_path.resolve()}", wait_until="networkidle")

        # Wait for all async renders (mermaid, MathJax, Chart.js)
        try:
            page.wait_for_function("window.renderReady === true", timeout=8000)
        except Exception:
            pass  # Proceed even if timeout — content still renders

        page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=(
                '<div style="font-size:9px;color:#999;width:100%;text-align:center;'
                'margin-bottom:4mm">'
                '<span class="pageNumber"></span> / <span class="totalPages"></span>'
                '</div>'
            ),
            margin={"top": "20mm", "bottom": "22mm", "left": "18mm", "right": "18mm"},
        )
        browser.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Render lecture_notes.md → PDF")
    parser.add_argument("--md", required=True, help="Path to lecture_notes.md")
    parser.add_argument("--slides-dir", default="", help="Directory with slide-N.png images")
    args = parser.parse_args()

    notes_md = Path(args.md)
    output_dir = notes_md.parent
    slides_dir = Path(args.slides_dir) if args.slides_dir else None
    # Auto-detect slides directory next to the markdown file (handles --skip-agent re-renders)
    if slides_dir is None:
        auto = output_dir / "slides"
        if auto.exists() and any(auto.glob("slide-*.png")):
            slides_dir = auto

    raw = notes_md.read_text(encoding="utf-8")
    meta, body = parse_front_matter(raw)

    # Scan markdown for avisos before pre-processing replaces the containers
    avisos_html = extract_avisos_box(body)

    # Pre-process special blocks
    body, flashcard_pairs, slide_cites = preprocess(body, slides_dir, output_dir)

    # Markdown → HTML
    body_html = markdown_to_html(body)

    # Build structural elements
    toc_html = build_toc(body_html)
    flashcards_html = build_flashcards_appendix(flashcard_pairs)
    glossario_html = build_glossario(body_html)
    slides_html = build_slides_appendix(slides_dir, output_dir, slide_cites)

    # Write intermediate HTML (useful for debugging)
    html_path = output_dir / "lecture_notes.html"
    full_html = assemble_html(
        meta, body_html, toc_html, avisos_html,
        flashcards_html, glossario_html, slides_html,
    )
    html_path.write_text(full_html, encoding="utf-8")

    # PDF
    pdf_path = output_dir / "lecture_notes.pdf"
    render_pdf(html_path, pdf_path)
    print(f"  PDF: {pdf_path}")

    # Anki CSV
    if flashcard_pairs:
        csv_path = output_dir / "flashcards.csv"
        export_flashcards_csv(flashcard_pairs, csv_path)


if __name__ == "__main__":
    main()
