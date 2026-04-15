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
import xml.etree.ElementTree as ET

# ── Configuração ──────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent.parent
CONFIG_PATH   = BASE_DIR / "config.yaml"
OUTPUT_PATH   = BASE_DIR / "index.html"
TEMPLATE_PATH = BASE_DIR / "scripts" / "template.html"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
GEMINI_ENABLED   = bool(CONFIG.get("gemini", {}).get("enabled", False))
GEMINI_MODEL     = CONFIG["gemini"]["model"]
GEMINI_MODEL_CANDIDATES = [m for m in [
    GEMINI_MODEL,
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-flash-latest",
] if m]
GEMINI_RATE_LIMITED_UNTIL = 0.0
TZ               = ZoneInfo(CONFIG["app"]["timezone"])
HIGHLIGHTS_COUNT = CONFIG["app"]["highlights_per_category"]
HOURS_BACK       = 24
HOME_BUILDER_CFG = CONFIG.get("home_builder", {})
APP_TITLE        = CONFIG.get("app", {}).get("title", "feed")
APP_SUBTITLE     = CONFIG.get("app", {}).get("subtitle", "seu briefing diário")

GITHUB_USER      = CONFIG["app"].get("github_user", "")
GITHUB_REPO      = CONFIG["app"].get("github_repo", "feed")
UPDATE_PWD_HASH  = CONFIG["app"].get("update_password_hash", "")

# ── Helpers ───────────────────────────────────────────────────────────────────

def now_local():
    return datetime.now(TZ)

PT_WEEKDAYS = {
    0: "segunda-feira",
    1: "terça-feira",
    2: "quarta-feira",
    3: "quinta-feira",
    4: "sexta-feira",
    5: "sábado",
    6: "domingo",
}

PT_MONTHS = {
    1: "janeiro",
    2: "fevereiro",
    3: "março",
    4: "abril",
    5: "maio",
    6: "junho",
    7: "julho",
    8: "agosto",
    9: "setembro",
    10: "outubro",
    11: "novembro",
    12: "dezembro",
}

def format_pt_long_date(dt):
    weekday = PT_WEEKDAYS.get(dt.weekday(), "")
    month = PT_MONTHS.get(dt.month, "")
    return f"{weekday}, {dt.day:02d} de {month} de {dt.year}"

def format_pt_generated_at(dt):
    return f"{format_pt_long_date(dt)} · {dt.strftime('%H:%M')}"

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

def parse_datetime(value):
    if not value:
        return None
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    except Exception:
        return None

def is_blocked(title, summary, blocked_keywords):
    if not blocked_keywords:
        return False
    text = (title + " " + summary).lower()
    return any(kw.lower() in text for kw in blocked_keywords)

def esc(text):
    """Escapa caracteres HTML básicos."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def format_decimal_br(value, decimals=2):
    try:
        num = float(value)
    except Exception:
        return "N/D"
    txt = f"{num:,.{decimals}f}"
    return txt.replace(",", "X").replace(".", ",").replace("X", ".")

def format_change_pct(value):
    try:
        num = float(value)
    except Exception:
        return ("N/D", "flat")
    sign = "+" if num > 0 else ""
    css = "up" if num > 0 else "down" if num < 0 else "flat"
    return (f"{sign}{format_decimal_br(num, 2)}%", css)

def fetch_market_snapshot():
    """Busca cotações de mercado para mostrar no topo da home."""
    targets = [
        {"symbol": "USDBRL=X", "label": "Dólar/Real", "prefix": "R$ ", "decimals": 2},
        {"symbol": "BTC-USD", "label": "Bitcoin/Dólar", "prefix": "US$ ", "decimals": 2},
        {"symbol": "OIL-BRL", "label": "Barril de Petróleo", "prefix": "US$ ", "decimals": 2},
        {"symbol": "IBOV", "label": "Ibovespa (IBOV)", "prefix": "", "decimals": 0},
    ]
    out = {
        item["symbol"]: {
            "label": item["label"],
            "price": "N/D",
            "change_pct": "N/D",
            "change_css": "flat",
        }
        for item in targets
    }

    try:
        # Câmbio e BTC (AwesomeAPI)
        r = requests.get(
            "https://economia.awesomeapi.com.br/json/last/USD-BRL,BTC-USD",
            headers=FEEDPARSER_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        payload = r.json()
        fx = payload.get("USDBRL", {})
        btc = payload.get("BTCUSD", {})

        for symbol, row in (("USDBRL=X", fx), ("BTC-USD", btc)):
            if not row:
                continue
            price_raw = row.get("bid") or row.get("ask")
            pct_raw = row.get("pctChange")
            item = next(t for t in targets if t["symbol"] == symbol)
            out[symbol]["price"] = (
                f"{item['prefix']}{format_decimal_br(price_raw, item['decimals'])}"
                if price_raw is not None else "N/D"
            )
            pct_txt, pct_css = format_change_pct(pct_raw)
            out[symbol]["change_pct"] = pct_txt
            out[symbol]["change_css"] = pct_css

        # Barril de petróleo (Stooq - CL.F)
        oil_resp = requests.get("https://stooq.com/q/l/?s=cl.f&i=d", headers=FEEDPARSER_HEADERS, timeout=15)
        oil_resp.raise_for_status()
        line = oil_resp.text.strip().splitlines()[0] if oil_resp.text.strip() else ""
        cols = [c.strip() for c in line.split(",")]
        if len(cols) >= 7 and cols[3] != "N/D" and cols[6] != "N/D":
            open_price = float(cols[3])
            close_price = float(cols[6])
            pct_raw = ((close_price - open_price) / open_price) * 100 if open_price else 0.0
            out["OIL-BRL"]["price"] = f"US$ {format_decimal_br(close_price, 2)}"
            pct_txt, pct_css = format_change_pct(pct_raw)
            out["OIL-BRL"]["change_pct"] = pct_txt
            out["OIL-BRL"]["change_css"] = pct_css

        # Ibovespa (HG Brasil Finance - funciona sem chave, com limite)
        hg_resp = requests.get("https://api.hgbrasil.com/finance", headers=FEEDPARSER_HEADERS, timeout=15)
        hg_resp.raise_for_status()
        hg = hg_resp.json()
        ibov = (hg.get("results", {}).get("stocks", {}) or {}).get("IBOVESPA")
        if ibov:
            points = ibov.get("points")
            variation = ibov.get("variation")
            if points is not None:
                out["IBOV"]["price"] = f"{format_decimal_br(points, 0)} pts"
            pct_txt, pct_css = format_change_pct(variation)
            out["IBOV"]["change_pct"] = pct_txt
            out["IBOV"]["change_css"] = pct_css
    except Exception as e:
        print(f"  ⚠️  Não foi possível buscar cotações de mercado: {e}")

    ordered = [out[item["symbol"]] for item in targets]
    return ordered

def build_market_snapshot_html(market_snapshot):
    cards = []
    for item in market_snapshot:
        cards.append(
            f"""<article class="market-card">
  <div class="market-label">{esc(item['label'])}</div>
  <div class="market-price">{esc(item['price'])}</div>
  <div class="market-change {esc(item['change_css'])}">{esc(item['change_pct'])}</div>
</article>"""
        )
    return f"""<div class="market-strip">
  <div class="section-label">Mercado agora</div>
  <div class="market-grid">{"".join(cards)}</div>
</div>"""

def looks_portuguese(text):
    """Heurística leve para reduzir chamadas de tradução."""
    if not text:
        return False
    lower = text.lower()
    pt_hints = [
        " que ", " para ", " com ", " uma ", " um ", " não ", " dos ", " das ",
        " de ", " em ", " no ", " na ", " ao ", " aos ", " às ", " por ",
    ]
    score = sum(1 for hint in pt_hints if hint in f" {lower} ")
    if re.search(r"[ãõáéíóúâêôç]", lower):
        score += 1
    return score >= 3

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
            if "sitemap" in feed_cfg["url"]:
                xml_text = requests.get(feed_cfg["url"], headers=FEEDPARSER_HEADERS, timeout=30).text
                root = ET.fromstring(xml_text)
                ns = {
                    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
                    "news": "http://www.google.com/schemas/sitemap-news/0.9",
                    "image": "http://www.google.com/schemas/sitemap-image/1.1",
                }
                for node in root.findall("sm:url", ns):
                    link = (node.findtext("sm:loc", default="", namespaces=ns) or "").strip()
                    title = (node.findtext("news:news/news:title", default="", namespaces=ns) or "").strip()
                    summary = ""
                    pub_raw = (
                        node.findtext("news:news/news:publication_date", default="", namespaces=ns)
                        or node.findtext("sm:lastmod", default="", namespaces=ns)
                    )
                    pub = parse_datetime(pub_raw) or (now_local() - timedelta(hours=1))
                    if pub < cutoff:
                        continue
                    image = (node.findtext("image:image/image:loc", default="", namespaces=ns) or "").strip()
                    if is_blocked(title, summary, blocked_keywords):
                        blocked_count += 1
                        continue
                    articles_by_source[source_name].append({
                        "source": source_name,
                        "title": title or link,
                        "summary": summary,
                        "link": link,
                        "image": image,
                        "published": pub.strftime("%d/%m %H:%M"),
                        "published_iso": pub.isoformat(),
                    })
                articles_by_source[source_name].sort(key=lambda x: x["published_iso"], reverse=True)
                continue

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

def get_home_builder_config():
    category_sources = {}
    category_order = []
    for item in HOME_BUILDER_CFG.get("categories", []):
        cat_id = item.get("id")
        if not cat_id:
            continue
        category_order.append(cat_id)
        category_sources[cat_id] = set(item.get("sources", []))
    return {
        "enabled": bool(HOME_BUILDER_CFG.get("enabled", True)),
        "use_gemini": bool(HOME_BUILDER_CFG.get("use_gemini", True)),
        "max_articles_per_category": int(HOME_BUILDER_CFG.get("max_articles_per_category", 10)),
        "max_summary_items": int(HOME_BUILDER_CFG.get("max_summary_items", 4)),
        "category_order": category_order,
        "category_sources": category_sources,
    }

def dedupe_articles_by_title(articles):
    seen = set()
    uniq = []
    for art in articles:
        key = re.sub(r"\s+", " ", (art.get("title") or "").lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(art)
    return uniq

def deterministic_category_match(article, category_niche):
    text = f"{article.get('title', '')} {article.get('summary', '')}".lower()
    keywords = [k.strip().lower() for k in category_niche.split(",")]
    keywords = [k for k in keywords if len(k) >= 4]
    if not keywords:
        return True
    return any(kw in text for kw in keywords)

def select_home_articles_for_category(cat, home_cfg):
    allowed_sources = home_cfg["category_sources"].get(cat["id"], set())
    filtered = [
        a for a in cat["all_articles"]
        if (not allowed_sources or a["source"] in allowed_sources)
        and deterministic_category_match(a, cat.get("nicho", ""))
    ]
    filtered = dedupe_articles_by_title(filtered)
    return filtered[:home_cfg["max_articles_per_category"]]

# ── Gemini ────────────────────────────────────────────────────────────────────

def call_gemini(prompt, json_mode=False):
    global GEMINI_RATE_LIMITED_UNTIL
    if not GEMINI_ENABLED:
        return None
    if not GEMINI_API_KEY:
        return None
    now_ts = time.time()
    if now_ts < GEMINI_RATE_LIMITED_UNTIL:
        remaining = int(GEMINI_RATE_LIMITED_UNTIL - now_ts)
        print(f"  ⚠️  Gemini em cooldown de rate limit ({remaining}s).")
        return None
    generation_config = {
        "temperature": 0.4,
        "maxOutputTokens": CONFIG["gemini"]["max_tokens"],
    }
    if json_mode:
        generation_config["responseMimeType"] = "application/json"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }
    for model in GEMINI_MODEL_CANDIDATES:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        for attempt in range(3):
            try:
                r = requests.post(url, json=body, timeout=30)
                if r.status_code == 404:
                    print(f"  ⚠️  Modelo Gemini indisponível: {model}")
                    break
                if r.status_code == 429:
                    retry_after = r.headers.get("Retry-After")
                    wait_s = int(retry_after) if retry_after and retry_after.isdigit() else min(5 * (attempt + 1), 20)
                    if attempt < 2:
                        print(f"  ⚠️  Rate limit no {model}, aguardando {wait_s}s (tentativa {attempt+1}/3)...")
                        time.sleep(wait_s)
                        continue
                    GEMINI_RATE_LIMITED_UNTIL = time.time() + max(wait_s, 60)
                    print(f"  ⚠️  Gemini error ({model}): 429 Too Many Requests. Cooldown ativado.")
                    return None
                r.raise_for_status()
                data = r.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    print(f"  ⚠️  Gemini sem candidates. model={model} promptFeedback={data.get('promptFeedback')}")
                    return None
                parts = candidates[0].get("content", {}).get("parts", [])
                text_parts = [p.get("text", "") for p in parts if p.get("text")]
                if not text_parts:
                    print(f"  ⚠️  Gemini sem texto útil. model={model} finishReason={candidates[0].get('finishReason')}")
                    return None
                return "\n".join(text_parts).strip()
            except Exception as e:
                print(f"  ⚠️  Gemini error ({model}): {e}")
                return None
    print("  ⚠️  Nenhum modelo Gemini disponível para esta chave/API.")
    return None

def translate_articles(articles_by_source):
    """Traduz títulos e descrições para português via Gemini."""
    if not GEMINI_ENABLED:
        print("   → ℹ️  IA desativada no config (gemini.enabled=false).")
        return articles_by_source
    if not GEMINI_API_KEY:
        print("   → ℹ️  GEMINI_API_KEY ausente. Tradução desativada.")
        return articles_by_source

    candidates = [
        art
        for arts in articles_by_source.values()
        for art in arts
        if art.get("title") or art.get("summary")
    ]

    to_translate = [
        art for art in candidates
        if not looks_portuguese(f"{art.get('title', '')} {art.get('summary', '')}")
    ]

    if not to_translate:
        print("   → ℹ️  Sem artigos elegíveis para tradução.")
        return articles_by_source

    print(f"   → 🌐 Traduzindo {len(to_translate)} de {len(candidates)} artigos...")
    batch_size = 20
    translated_titles = 0
    translated_summaries = 0

    for start in range(0, len(to_translate), batch_size):
        batch = to_translate[start:start + batch_size]
        items = "\n".join(
            f"{i+1}. TITULO: {a['title']} | DESC: {a['summary'][:200]}"
            for i, a in enumerate(batch)
        )

        prompt = f"""Traduza os títulos e descrições abaixo para português brasileiro.
Se já estiverem em português, mantenha o sentido e a naturalidade.
Mantenha o tom jornalístico. Responda SOMENTE com JSON array no formato:
[{{"t": "titulo traduzido", "d": "descricao traduzida"}}, ...]
Um objeto por item, na mesma ordem.

Itens:
{items}"""

        resp = call_gemini(prompt, json_mode=True)
        if not resp:
            continue

        try:
            clean = re.sub(r"```(?:json)?|```", "", resp).strip()
            try:
                translated = json.loads(clean)
            except Exception:
                match = re.search(r"\[[\s\S]*\]", clean)
                if not match:
                    raise
                translated = json.loads(match.group(0))
            for i, art in enumerate(batch):
                if i < len(translated):
                    t = translated[i]
                    title = t.get("t") or t.get("title") or t.get("titulo")
                    summary = t.get("d") or t.get("description") or t.get("descricao") or t.get("summary")
                    if title:
                        art["title"] = str(title).strip()
                        translated_titles += 1
                    if summary:
                        art["summary"] = str(summary).strip()
                        translated_summaries += 1
        except Exception as e:
            print(f"   ⚠️  Erro ao parsear tradução: {e}")
    print(f"   → ✅ Traduções aplicadas: títulos={translated_titles}, descrições={translated_summaries}")

    return articles_by_source


def highlight_articles(all_articles, category):
    if not all_articles or not GEMINI_ENABLED or not GEMINI_API_KEY:
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
    if not GEMINI_ENABLED:
        return "Resumo do dia sem IA (modo gratuito)."
    if not GEMINI_API_KEY:
        return "Resumo do dia indisponível — configure a GEMINI_API_KEY."
    bullets = "\n".join(f"- {h}" for h in all_highlights[:30])
    prompt = f"""Você é um jornalista brasileiro escrevendo o briefing matinal para um leitor de tecnologia, cultura pop e notícias do mundo.

Com base nos destaques abaixo, escreva um parágrafo único e fluido (4-6 frases) resumindo o dia. Seja direto, informativo e com leve tom editorial. Não use bullet points. Escreva em português brasileiro.

Destaques:
{bullets}"""
    resp = call_gemini(prompt)
    return resp or "Não foi possível gerar o resumo hoje."

def generate_category_summary_items(cat, candidate_articles, home_cfg):
    max_items = max(1, home_cfg["max_summary_items"])
    if not candidate_articles:
        return []

    can_use_ai = home_cfg["use_gemini"] and GEMINI_ENABLED and bool(GEMINI_API_KEY)
    if can_use_ai:
        listing = "\n".join(
            f"{i+1}. [{a['source']}] {a['title']} | {a.get('summary', '')[:180]} | {a['link']}"
            for i, a in enumerate(candidate_articles)
        )
        prompt = f"""Você é editor de newsletter. Com base na categoria "{cat['label']}" e nas notícias abaixo,
selecione os {max_items} eventos mais relevantes das últimas 24h e escreva um resumo curto (1 frase por item).
Responda SOMENTE com JSON array no formato:
[{{"index": 1, "text": "resumo"}}, ...]
`index` é o número da notícia escolhida na lista (1-based), sem repetir.

Notícias:
{listing}"""
        resp = call_gemini(prompt, json_mode=True)
        if resp:
            try:
                clean = re.sub(r"```(?:json)?|```", "", resp).strip()
                rows = json.loads(clean)
                out = []
                for row in rows:
                    idx = int(row.get("index", 0)) - 1
                    if idx < 0 or idx >= len(candidate_articles):
                        continue
                    art = candidate_articles[idx]
                    text = str(row.get("text", "")).strip()
                    if not text:
                        continue
                    out.append({
                        "text": text,
                        "source": art["source"],
                        "url": art["link"],
                    })
                    if len(out) >= max_items:
                        break
                if out:
                    return out
            except Exception as e:
                print(f"   ⚠️  Erro ao parsear resumo da categoria {cat['id']}: {e}")

    # Fallback determinístico (70% regras): headlines mais recentes já filtradas
    return [{
        "text": art["title"],
        "source": art["source"],
        "url": art["link"],
    } for art in candidate_articles[:max_items]]

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
    date_time_str = art.get("published", "")
    if art.get("published_iso"):
        try:
            dt = datetime.fromisoformat(art["published_iso"]).astimezone(TZ)
            date_time_str = dt.strftime("%d/%m/%Y • %H:%M")
        except Exception:
            pass
    img_html = ""
    if art.get("image"):
        img_html = f'''<div class="ni-img"><img src="{esc(art["image"])}" alt="" loading="lazy" onerror="this.closest('.ni-img').style.display='none'"></div>'''
    desc_html = f'<p class="ni-desc">{esc(art["summary"][:180])}{"…" if len(art["summary"]) > 180 else ""}</p>' if art.get("summary") else ""
    return f"""<a class="news-item" href="{esc(art['link'])}" target="_blank" rel="noopener">
  {img_html}
  <div class="ni-body">
    <span class="ni-time">{esc(date_time_str)}</span>
    <span class="ni-title">{esc(art['title'])}</span>
    {desc_html}
  </div>
  <span class="news-arrow">→</span>
</a>"""

def build_home_category_summaries(categories_data):
    home_cfg = get_home_builder_config()
    if not home_cfg["enabled"]:
        return '<div class="home-section"><div class="section-label">Resumo por categoria desativado</div></div>'

    cat_by_id = {c["id"]: c for c in categories_data}
    cards = []
    for cat_id in home_cfg["category_order"]:
        cat = cat_by_id.get(cat_id)
        if not cat:
            continue
        candidates = select_home_articles_for_category(cat, home_cfg)
        summary_items = generate_category_summary_items(cat, candidates, home_cfg)
        if not summary_items:
            continue
        list_items = "\n".join(
            f"""<li class="summary-item">
  <div class="summary-text">{esc(item['text'])}</div>
  <a class="summary-source" href="{esc(item['url'])}" target="_blank" rel="noopener">Fonte: {esc(item['source'])}</a>
</li>"""
            for item in summary_items
        )
        cards.append(f"""<article class="summary-card">
  <div class="summary-head">
    <h3 class="summary-title">{cat['emoji']} {esc(cat['label'])}</h3>
    <span class="summary-count">{len(candidates)} notícia(s) analisada(s)</span>
  </div>
  <ul class="summary-list">{list_items}</ul>
</article>""")

    if not cards:
        return '<div class="home-section"><div class="section-label">Sem resumos disponíveis</div></div>'

    return f"""<div class="home-section">
  <div class="section-label">Resumo 24h por categoria</div>
  <div class="summary-grid">
    {"".join(cards)}
  </div>
</div>"""

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
    now_str     = format_pt_generated_at(now)
    now_short   = now.strftime("%d/%m · %H:%M")
    home_date   = format_pt_long_date(now).capitalize()
    total       = sum(sum(len(v) for v in cat["articles_by_source"].values()) for cat in categories_data)

    sidebar_nav      = build_sidebar_nav(categories_data)
    market_snapshot  = fetch_market_snapshot()
    market_html      = build_market_snapshot_html(market_snapshot)
    home_category_summaries = build_home_category_summaries(categories_data)
    category_views   = "\n".join(build_category_view(cat) for cat in categories_data)

    return (template
        .replace("{{APP_TITLE}}", esc(APP_TITLE))
        .replace("{{APP_SUBTITLE}}", esc(APP_SUBTITLE))
        .replace("{{GENERATED_AT}}", now_str)
        .replace("{{GENERATED_AT_SHORT}}", now_short)
        .replace("{{HOME_DATE}}", home_date)
        .replace("{{DAILY_SUMMARY}}", esc(daily_summary))
        .replace("{{TOTAL_ARTICLES}}", str(total))
        .replace("{{MARKET_SNAPSHOT}}", market_html)
        .replace("{{SIDEBAR_NAV}}", sidebar_nav)
        .replace("{{HOME_CATEGORY_SUMMARIES}}", home_category_summaries)
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

    for cat in CONFIG["categories"]:
        print(f"📡 {cat['label']}...")
        articles_by_source = fetch_category(cat)
        all_articles       = flatten_articles(articles_by_source)
        total              = len(all_articles)
        print(f"   → {total} artigos")

        categories_data.append({
            "id":                cat["id"],
            "label":             cat["label"],
            "emoji":             cat["emoji"],
                    "nicho":             cat.get("nicho", ""),
            "articles_by_source": articles_by_source,
            "all_articles":      all_articles,
        })
        time.sleep(0.35)

    home_cfg = get_home_builder_config()
    if home_cfg["use_gemini"] and GEMINI_ENABLED and GEMINI_API_KEY:
        daily_summary = "Curadoria híbrida 70/30: regras de fonte + seleção editorial por IA."
    else:
        daily_summary = "Curadoria por regras (modo gratuito): categorias e fontes que você definiu nas últimas 24h."

    print("🎨 Gerando HTML...")
    html = build_html(categories_data, daily_summary)
    OUTPUT_PATH.write_text(html, encoding="utf-8")

    total_all = sum(len(c["all_articles"]) for c in categories_data)
    print(f"\n✅ Gerado: {OUTPUT_PATH}")
    print(f"   Total: {total_all} artigos")
    print(f"{'='*52}\n")

if __name__ == "__main__":
    main()
