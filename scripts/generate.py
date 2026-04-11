#!/usr/bin/env python3
"""
feed - Daily News Generator
Busca RSS, filtra com Gemini e gera HTML estático.
"""

import os
import json
import yaml
import feedparser
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import time
import re

# ── Configuração ──────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
OUTPUT_PATH = BASE_DIR / "index.html"
TEMPLATE_PATH = BASE_DIR / "scripts" / "template.html"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = CONFIG["gemini"]["model"]
TZ = ZoneInfo(CONFIG["app"]["timezone"])
HIGHLIGHTS_COUNT = CONFIG["app"]["highlights_per_category"]
HOURS_BACK = 24

# ── Helpers ───────────────────────────────────────────────────────────────────

def now_local():
    return datetime.now(TZ)

def cutoff_time():
    return now_local() - timedelta(hours=HOURS_BACK)

def parse_entry_date(entry):
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).astimezone(TZ)
            except Exception:
                pass
    return now_local() - timedelta(hours=1)  # fallback: considera recente

def get_thumbnail(entry):
    # 1. media:thumbnail
    media = getattr(entry, "media_thumbnail", None)
    if media and isinstance(media, list) and media[0].get("url"):
        return media[0]["url"]
    # 2. enclosure de imagem
    for enc in getattr(entry, "enclosures", []):
        if enc.get("type", "").startswith("image"):
            return enc.get("url", "")
    # 3. media:content
    for mc in getattr(entry, "media_content", []):
        if mc.get("url") and "image" in mc.get("type", "image"):
            return mc["url"]
    # 4. og:image via summary (regex rápido)
    summary = getattr(entry, "summary", "") or ""
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary)
    if match:
        return match.group(1)
    return ""

def clean_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()

# ── Busca RSS ─────────────────────────────────────────────────────────────────

def fetch_category(category):
    cutoff = cutoff_time()
    articles = []
    for feed_cfg in category["feeds"]:
        try:
            parsed = feedparser.parse(feed_cfg["url"])
            for entry in parsed.entries:
                pub = parse_entry_date(entry)
                if pub < cutoff:
                    continue
                articles.append({
                    "source": feed_cfg["name"],
                    "title": entry.get("title", "").strip(),
                    "summary": clean_html(entry.get("summary", entry.get("description", "")))[:400],
                    "link": entry.get("link", ""),
                    "image": get_thumbnail(entry),
                    "published": pub.strftime("%d/%m %H:%M"),
                    "published_iso": pub.isoformat(),
                })
        except Exception as e:
            print(f"  ⚠️  Erro no feed {feed_cfg['name']}: {e}")
    articles.sort(key=lambda x: x["published_iso"], reverse=True)
    return articles

# ── Gemini ────────────────────────────────────────────────────────────────────

def call_gemini(prompt):
    if not GEMINI_API_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": CONFIG["gemini"]["max_tokens"]},
    }
    try:
        r = requests.post(url, json=body, timeout=30)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"  ⚠️  Gemini error: {e}")
        return None

def highlight_articles(articles, category):
    if not articles or not GEMINI_API_KEY:
        return [a["title"] for a in articles[:HIGHLIGHTS_COUNT]]

    titles_list = "\n".join(
        f"{i+1}. [{a['source']}] {a['title']}" for i, a in enumerate(articles[:40])
    )
    prompt = f"""Você é um curador de notícias para um leitor brasileiro interessado em: {category['nicho']}

Abaixo estão as manchetes das últimas 24h. Selecione os {HIGHLIGHTS_COUNT} títulos mais relevantes e impactantes para esse perfil.
Responda SOMENTE com um JSON array com os números das manchetes selecionadas, ex: [1, 3, 7, 12, 15]

Manchetes:
{titles_list}"""

    resp = call_gemini(prompt)
    if not resp:
        return [a["title"] for a in articles[:HIGHLIGHTS_COUNT]]
    try:
        indices = json.loads(re.search(r'\[[\d,\s]+\]', resp).group())
        return [articles[i-1]["title"] for i in indices if 0 < i <= len(articles)]
    except Exception:
        return [a["title"] for a in articles[:HIGHLIGHTS_COUNT]]

def generate_daily_summary(all_highlights):
    if not GEMINI_API_KEY:
        return "Resumo do dia indisponível — configure a GEMINI_API_KEY."

    bullets = "\n".join(f"- {h}" for h in all_highlights[:30])
    prompt = f"""Você é um jornalista brasileiro escrevendo o briefing matinal para um leitor de tecnologia, cultura pop e notícias do mundo.

Com base nos destaques abaixo, escreva um parágrafo único e fluido (4-6 frases) resumindo o dia. Seja direto, informativo e com leve tom editorial. Não use bullet points. Escreva em português brasileiro.

Destaques:
{bullets}"""

    resp = call_gemini(prompt)
    return resp or "Não foi possível gerar o resumo hoje."

# ── Template HTML ─────────────────────────────────────────────────────────────

def build_html(categories_data, daily_summary):
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()

    # Gera as abas
    tabs_nav = ""
    tabs_content = ""

    for i, cat in enumerate(categories_data):
        active = "active" if i == 0 else ""
        tabs_nav += f'<button class="tab-btn {active}" data-tab="{cat["id"]}">{cat["emoji"]} {cat["label"]} <span class="count">{len(cat["articles"])}</span></button>\n'

        highlight_titles = set(cat["highlights"])

        highlights_html = ""
        all_html = ""

        for art in cat["articles"]:
            is_highlight = art["title"] in highlight_titles
            img_html = f'<img src="{art["image"]}" alt="" loading="lazy" onerror="this.parentElement.classList.add(\'no-img\')">' if art["image"] else ""
            card = f"""<article class="card {'highlight' if is_highlight else ''}">
  <div class="card-img {'empty' if not art['image'] else ''}">
    {img_html}
    {'<span class="badge">⚡ Destaque</span>' if is_highlight else ''}
  </div>
  <div class="card-body">
    <span class="source">{art['source']}</span>
    <h3><a href="{art['link']}" target="_blank" rel="noopener">{art['title']}</a></h3>
    <p>{art['summary'][:200]}{'…' if len(art['summary']) > 200 else ''}</p>
    <div class="card-footer">
      <time>{art['published']}</time>
      <a href="{art['link']}" target="_blank" rel="noopener" class="read-more">Ler →</a>
    </div>
  </div>
</article>"""
            if is_highlight:
                highlights_html += card
            all_html += card

        tabs_content += f"""<div class="tab-panel {active}" id="tab-{cat['id']}">
  <section class="highlights-section">
    <h2 class="section-title">⚡ Destaques</h2>
    <div class="cards-grid highlights-grid">{highlights_html or '<p class="empty-state">Sem destaques hoje.</p>'}</div>
  </section>
  <section class="all-section">
    <h2 class="section-title">📋 Todas as notícias <small>({len(cat['articles'])} nas últimas 24h)</small></h2>
    <div class="cards-grid all-grid">{all_html or '<p class="empty-state">Nenhuma notícia encontrada.</p>'}</div>
  </section>
</div>\n"""

    now_str = now_local().strftime("%A, %d de %B de %Y · %H:%M")
    total = sum(len(c["articles"]) for c in categories_data)

    return (template
        .replace("{{TABS_NAV}}", tabs_nav)
        .replace("{{TABS_CONTENT}}", tabs_content)
        .replace("{{DAILY_SUMMARY}}", daily_summary)
        .replace("{{GENERATED_AT}}", now_str)
        .replace("{{TOTAL_ARTICLES}}", str(total))
    )

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*50}")
    print(f"  feed — {now_local().strftime('%d/%m/%Y %H:%M')} ({CONFIG['app']['timezone']})")
    print(f"{'='*50}\n")

    categories_data = []
    all_highlights = []

    for cat in CONFIG["categories"]:
        print(f"📡 Buscando: {cat['label']}...")
        articles = fetch_category(cat)
        print(f"   → {len(articles)} artigos nas últimas 24h")

        print(f"   → Selecionando destaques com IA...")
        highlights = highlight_articles(articles, cat)
        all_highlights.extend(highlights)

        categories_data.append({
            "id": cat["id"],
            "label": cat["label"],
            "emoji": cat["emoji"],
            "articles": articles,
            "highlights": highlights,
        })
        time.sleep(1)  # evita rate limit do Gemini

    print("\n✍️  Gerando resumo do dia...")
    daily_summary = generate_daily_summary(all_highlights)

    print("🎨 Montando HTML...")
    html = build_html(categories_data, daily_summary)

    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"\n✅ Gerado: {OUTPUT_PATH}")
    print(f"   Total de artigos: {sum(len(c['articles']) for c in categories_data)}")
    print(f"{'='*50}\n")

if __name__ == "__main__":
    main()
