#!/usr/bin/env python3
"""
feed - Daily News Generator
Busca RSS, filtra com Gemini e gera HTML estático.
"""

import os
import json
import yaml
import hashlib
import feedparser
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import time
import re

# ── Configuração ──────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent.parent
CONFIG_PATH   = BASE_DIR / "config.yaml"
OUTPUT_PATH   = BASE_DIR / "index.html"
TEMPLATE_PATH = BASE_DIR / "scripts" / "template.html"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL     = CONFIG["gemini"]["model"]
TZ               = ZoneInfo(CONFIG["app"]["timezone"])
HIGHLIGHTS_COUNT = CONFIG["app"]["highlights_per_category"]
HOURS_BACK       = 24

GITHUB_USER      = CONFIG["app"].get("github_user", "")
GITHUB_REPO      = CONFIG["app"].get("github_repo", "feed")
UPDATE_PWD_HASH  = CONFIG["app"].get("update_password_hash", "")

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
    return now_local() - timedelta(hours=1)

def get_thumbnail(entry):
    media = getattr(entry, "media_thumbnail", None)
    if media and isinstance(media, list) and media[0].get("url"):
        return media[0]["url"]
    for enc in getattr(entry, "enclosures", []):
        if enc.get("type", "").startswith("image"):
            return enc.get("url", "")
    for mc in getattr(entry, "media_content", []):
        if mc.get("url") and "image" in mc.get("type", "image"):
            return mc["url"]
    summary = getattr(entry, "summary", "") or ""
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', summary)
    if match:
        return match.group(1)
    return ""

def clean_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()

def is_blocked(title, summary, blocked_keywords):
    if not blocked_keywords:
        return False
    text = (title + " " + summary).lower()
    return any(kw.lower() in text for kw in blocked_keywords)

def esc(text):
    """Escapa caracteres HTML básicos."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

# ── Busca RSS ─────────────────────────────────────────────────────────────────

FEEDPARSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def fetch_category(category):
    cutoff = cutoff_time()
    blocked_keywords = category.get("blocked_keywords", [])
    articles_by_source = {}
    blocked_count = 0

    for feed_cfg in category["feeds"]:
        source_name = feed_cfg["name"]
        articles_by_source[source_name] = []
        try:
            parsed = feedparser.parse(feed_cfg["url"], request_headers=FEEDPARSER_HEADERS)
            for entry in parsed.entries:
                pub = parse_entry_date(entry)
                if pub < cutoff:
                    continue
                title   = entry.get("title", "").strip()
                summary = clean_html(entry.get("summary", entry.get("description", "")))[:400]
                if is_blocked(title, summary, blocked_keywords):
                    blocked_count += 1
                    continue
                articles_by_source[source_name].append({
                    "source":       source_name,
                    "title":        title,
                    "summary":      summary,
                    "link":         entry.get("link", ""),
                    "image":        get_thumbnail(entry),
                    "published":    pub.strftime("%d/%m %H:%M"),
                    "published_iso": pub.isoformat(),
                })
        except Exception as e:
            print(f"  ⚠️  Erro no feed {source_name}: {e}")

        # ordena por mais recente
        articles_by_source[source_name].sort(key=lambda x: x["published_iso"], reverse=True)

    if blocked_count:
        print(f"   → 🚫 {blocked_count} artigo(s) bloqueado(s) por palavra-chave")

    return articles_by_source

def flatten_articles(articles_by_source):
    """Retorna todos os artigos em lista única ordenada por data."""
    all_articles = [a for arts in articles_by_source.values() for a in arts]
    all_articles.sort(key=lambda x: x["published_iso"], reverse=True)
    return all_articles

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

def translate_articles(articles_by_source):
    """Traduz títulos e descrições em inglês para português via Gemini."""
    if not GEMINI_API_KEY:
        return articles_by_source

    # Coleta artigos que precisam de tradução
    to_translate = []
    for source_name, arts in articles_by_source.items():
        for art in arts:
            text = art["title"] + " " + art["summary"]
            non_ascii = sum(1 for c in text if ord(c) > 127)
            ratio = non_ascii / max(len(text), 1)
            # Se menos de 3% de caracteres especiais, provavelmente é inglês
            if ratio < 0.03 and len(art["title"]) > 10:
                to_translate.append(art)

    if not to_translate:
        return articles_by_source

    print(f"   → 🌐 Traduzindo {len(to_translate)} artigos em inglês...")

    # Monta payload de tradução em lote
    items = "\n".join(
        f"{i+1}. TITULO: {a['title']} | DESC: {a['summary'][:200]}"
        for i, a in enumerate(to_translate)
    )

    prompt = f"""Traduza os títulos e descrições abaixo do inglês para o português brasileiro.
Mantenha o tom jornalístico. Responda SOMENTE com JSON array no formato:
[{{"t": "titulo traduzido", "d": "descricao traduzida"}}, ...]
Um objeto por item, na mesma ordem.

Itens:
{items}"""

    resp = call_gemini(prompt)
    if not resp:
        return articles_by_source

    try:
        clean = re.sub(r"```(?:json)?|```", "", resp).strip()
        translated = json.loads(clean)
        for i, art in enumerate(to_translate):
            if i < len(translated):
                t = translated[i]
                if t.get("t"): art["title"]   = t["t"]
                if t.get("d"): art["summary"] = t["d"]
    except Exception as e:
        print(f"   ⚠️  Erro ao parsear tradução: {e}")

    return articles_by_source


def highlight_articles(all_articles, category):
    if not all_articles or not GEMINI_API_KEY:
        return set(a["title"] for a in all_articles[:HIGHLIGHTS_COUNT])

    titles_list = "\n".join(
        f"{i+1}. [{a['source']}] {a['title']}" for i, a in enumerate(all_articles[:40])
    )
    prompt = f"""Você é um curador de notícias para um leitor brasileiro interessado em: {category['nicho']}

Abaixo estão manchetes das últimas 24h. Selecione os {HIGHLIGHTS_COUNT} títulos mais relevantes e impactantes.
Responda SOMENTE com um JSON array com os números selecionados, ex: [1, 3, 7, 12, 15]

Manchetes:
{titles_list}"""

    resp = call_gemini(prompt)
    if not resp:
        return set(a["title"] for a in all_articles[:HIGHLIGHTS_COUNT])
    try:
        indices = json.loads(re.search(r'\[[\d,\s]+\]', resp).group())
        return set(all_articles[i-1]["title"] for i in indices if 0 < i <= len(all_articles))
    except Exception:
        return set(a["title"] for a in all_articles[:HIGHLIGHTS_COUNT])

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

# ── HTML builders ─────────────────────────────────────────────────────────────

def build_card(art):
    img_part = ""
    if art["image"]:
        img_part = f'<img src="{esc(art["image"])}" alt="" loading="lazy" onerror="this.closest(\'.card-img\').classList.add(\'empty\')">'
        img_cls  = "card-img"
    else:
        img_cls  = "card-img empty"

    return f"""<a class="card" href="{esc(art['link'])}" target="_blank" rel="noopener">
  <div class="{img_cls}">{img_part}</div>
  <div class="card-body">
    <span class="card-source">{esc(art['source'])}</span>
    <div class="card-title">{esc(art['title'])}</div>
    <div class="card-summary">{esc(art['summary'][:180])}</div>
    <div class="card-footer">
      <span class="card-time">{esc(art['published'])}</span>
      <span class="card-cta">Ler →</span>
    </div>
  </div>
</a>"""

def build_news_item(art):
    time_str = art["published"].split()[1] if " " in art["published"] else art["published"]
    img_html = ""
    if art.get("image"):
        img_html = f'''<div class="ni-img"><img src="{esc(art["image"])}" alt="" loading="lazy" onerror="this.closest('.ni-img').style.display='none'"></div>'''
    desc_html = f'<p class="ni-desc">{esc(art["summary"][:180])}{"…" if len(art["summary"]) > 180 else ""}</p>' if art.get("summary") else ""
    return f"""<a class="news-item" href="{esc(art['link'])}" target="_blank" rel="noopener">
  {img_html}
  <div class="ni-body">
    <span class="ni-time">{esc(time_str)}</span>
    <span class="ni-title">{esc(art['title'])}</span>
    {desc_html}
  </div>
  <span class="news-arrow">→</span>
</a>"""

def build_home_sections(categories_data):
    html = ""
    for cat in categories_data:
        highlights = [a for a in cat["all_articles"] if a["title"] in cat["highlight_titles"]]
        if not highlights:
            continue
        cards = "\n".join(build_card(a) for a in highlights)
        html += f"""<div class="home-section">
  <div class="section-label">{cat['emoji']} {esc(cat['label'])}</div>
  <div class="cards-grid">{cards}</div>
</div>\n"""
    return html

def build_category_view(cat):
    cat_id   = cat["id"]
    articles_by_source = cat["articles_by_source"]
    total    = sum(len(v) for v in articles_by_source.values())

    # anchor bar
    anchor_pills = ""
    for source_name, arts in articles_by_source.items():
        if not arts:
            continue
        anchor_id = f"{cat_id}-{re.sub(r'[^a-z0-9]', '-', source_name.lower())}"
        anchor_pills += f'<a class="anchor-pill" href="#{anchor_id}">{esc(source_name)}</a>\n'

    # source blocks
    source_blocks = ""
    for source_name, arts in articles_by_source.items():
        if not arts:
            continue
        anchor_id   = f"{cat_id}-{re.sub(r'[^a-z0-9]', '-', source_name.lower())}"
        news_items  = "\n".join(build_news_item(a) for a in arts)
        source_blocks += f"""<div class="source-block" id="{anchor_id}">
  <div class="source-header">
    <span class="source-name">{esc(source_name)}</span>
    <span class="source-count">{len(arts)} notícia{'s' if len(arts) != 1 else ''}</span>
  </div>
  <div class="news-list">{news_items}</div>
</div>\n"""

    return f"""<div class="view" id="view-{cat_id}">
  <div class="cat-header">
    <h2 class="cat-title"><span>{cat['emoji']}</span> {esc(cat['label'])}</h2>
    <span class="cat-total">{total} notícia{'s' if total != 1 else ''} nas últimas 24h</span>
  </div>
  <div class="anchor-bar">{anchor_pills}</div>
  {source_blocks}
</div>\n"""

def build_sidebar_nav(categories_data):
    html = ""
    for cat in categories_data:
        cat_id  = cat["id"]
        total   = sum(len(v) for v in cat["articles_by_source"].values())
        subs    = ""
        for source_name, arts in cat["articles_by_source"].items():
            if not arts:
                continue
            anchor_id = f"{cat_id}-{re.sub(r'[^a-z0-9]', '-', source_name.lower())}"
            subs += f'<a class="nav-sub" href="#{anchor_id}" onclick="showView(\'{cat_id}\')">{esc(source_name)} <span style="opacity:.5;font-size:.65rem">({len(arts)})</span></a>\n'

        html += f"""<button class="nav-btn" data-view="{cat_id}" onclick="showView('{cat_id}')">
  <span class="nav-emoji">{cat['emoji']}</span>
  <span class="nav-label">{esc(cat['label'])}</span>
  <span class="nav-count">{total}</span>
</button>
<div class="nav-subs">{subs}</div>\n"""
    return html

def build_html(categories_data, daily_summary):
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = f.read()

    now         = now_local()
    now_str     = now.strftime("%A, %d de %B de %Y · %H:%M")
    now_short   = now.strftime("%d/%m · %H:%M")
    home_date   = now.strftime("%A, %d de %B de %Y").capitalize()
    total       = sum(sum(len(v) for v in cat["articles_by_source"].values()) for cat in categories_data)

    sidebar_nav      = build_sidebar_nav(categories_data)
    home_sections    = build_home_sections(categories_data)
    category_views   = "\n".join(build_category_view(cat) for cat in categories_data)

    return (template
        .replace("{{GENERATED_AT}}", now_str)
        .replace("{{GENERATED_AT_SHORT}}", now_short)
        .replace("{{HOME_DATE}}", home_date)
        .replace("{{DAILY_SUMMARY}}", esc(daily_summary))
        .replace("{{TOTAL_ARTICLES}}", str(total))
        .replace("{{SIDEBAR_NAV}}", sidebar_nav)
        .replace("{{HOME_SECTIONS}}", home_sections)
        .replace("{{CATEGORY_VIEWS}}", category_views)
        .replace("{{GITHUB_USER}}", GITHUB_USER)
        .replace("{{GITHUB_REPO}}", GITHUB_REPO)
        .replace("{{UPDATE_PASSWORD_HASH}}", UPDATE_PWD_HASH)
    )

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*52}")
    print(f"  feed — {now_local().strftime('%d/%m/%Y %H:%M')} ({CONFIG['app']['timezone']})")
    print(f"{'='*52}\n")

    categories_data = []
    all_highlights  = []

    for cat in CONFIG["categories"]:
        print(f"📡 {cat['label']}...")
        articles_by_source = fetch_category(cat)
        articles_by_source = translate_articles(articles_by_source)
        all_articles       = flatten_articles(articles_by_source)
        total              = len(all_articles)
        print(f"   → {total} artigos")

        print(f"   → Selecionando destaques...")
        highlight_titles = highlight_articles(all_articles, cat)
        all_highlights.extend(list(highlight_titles))

        categories_data.append({
            "id":                cat["id"],
            "label":             cat["label"],
            "emoji":             cat["emoji"],
            "articles_by_source": articles_by_source,
            "all_articles":      all_articles,
            "highlight_titles":  highlight_titles,
        })
        time.sleep(1)

    print("\n✍️  Resumo do dia...")
    daily_summary = generate_daily_summary(all_highlights)

    print("🎨 Gerando HTML...")
    html = build_html(categories_data, daily_summary)
    OUTPUT_PATH.write_text(html, encoding="utf-8")

    total_all = sum(len(c["all_articles"]) for c in categories_data)
    print(f"\n✅ Gerado: {OUTPUT_PATH}")
    print(f"   Total: {total_all} artigos")
    print(f"{'='*52}\n")

if __name__ == "__main__":
    main()
