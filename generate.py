#!/usr/bin/env python3
"""
Restaurant Dropout - Weekly Meal Prep Page Generator
Fetches the latest Substack post every Friday and regenerates index.html.

Run manually:  python3 generate.py
Auto-refresh:  cron runs this every Friday at 5pm (see README)
"""

import json, re, os, sys, subprocess, urllib.request, urllib.error, html as htmllib
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "config.json"
CACHE_DIR   = SCRIPT_DIR / ".cache"
OUTPUT_FILE = SCRIPT_DIR / "index.html"

SUP_MAP = {"¹": 1, "²": 2, "³": 3, "⁴": 4, "⁵": 5}
SUP_CHARS = "".join(SUP_MAP)

# ─── CONFIG ───────────────────────────────────────────────────────────────────

def load_config():
    if not CONFIG_FILE.exists():
        sys.exit(f"✗ config.json not found at {CONFIG_FILE}")
    return json.loads(CONFIG_FILE.read_text())

# ─── HTTP HELPERS ─────────────────────────────────────────────────────────────

def api_get(url, cookie):
    req = urllib.request.Request(url, headers={
        "Cookie": f"substack.sid={cookie}",
        "User-Agent": "Mozilla/5.0"
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            notify("⚠️ Session cookie expired — update config.json")
            sys.exit("✗ 401 Unauthorized — session cookie may have expired. Update config.json.")
        raise

def download_file(url, dest, cookie):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={
        "Cookie": f"substack.sid={cookie}",
        "User-Agent": "Mozilla/5.0"
    })
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        f.write(r.read())

# ─── FETCH POST ───────────────────────────────────────────────────────────────

def fetch_latest_post(base_url, cookie):
    posts = api_get(f"{base_url}/api/v1/posts?limit=1&offset=0", cookie)
    if not posts:
        sys.exit("✗ No posts found.")
    return posts[0]

def extract_post_meta(post):
    """Pull title, subtitle, date, week number, cover image, recipe images, PDF urls."""
    body = post.get("body_html", "")
    title = htmllib.unescape(post.get("title", ""))
    subtitle = htmllib.unescape(post.get("subtitle", ""))
    date = post.get("post_date", "")[:10]
    post_url = post.get("canonical_url", "")

    # Week number from title or subtitle
    week_match = re.search(r"[Ww]ee?k\s*(\d+)", title + " " + subtitle)
    week_num = week_match.group(1) if week_match else "?"

    # Cover image
    cover = post.get("cover_image", "")

    # Recipe images — from file-embed thumbnail URLs just before each recipe PDF
    recipe_pdf_ids = {
        "day1": None, "day2": None, "day3": None, "day4": None, "day5": None,
        "grocery_list": None, "prep_list": None, "printable_menu": None,
        "consolidated_recipes": None,
    }
    recipe_images = {}

    # Find all file-embed blocks and their preceding CDN images
    pdf_labels = {
        "ranch cutlets": "day1",
        "boursin butter salmon": "day2",
        "grilled peppercorn": "day3",
        "charred cabbage": "day4",
        "lemony white fish": "day5",
        "grocery list": "grocery_list",
        "prep list": "prep_list",
        "printable menu": "printable_menu",
        "consolidated recipes": "consolidated_recipes",
        "consolidated lists": "consolidated_lists",
        "all documents": "all_documents",
    }

    # Extract all (label, pdf_url, thumbnail_url) triples
    embed_pattern = re.compile(
        r'(https://substackcdn\.com/image/fetch/[^"]+)"[^>]*>'
        r'</image><div class="file-embed-details"><div class="file-embed-details-h1">([^<]+)</div>'
        r'.*?href="(https://restaurantdropout\.substack\.com/api/v1/file/[^"]+\.pdf)"',
        re.DOTALL
    )
    pdfs = {}
    for img_url, label, pdf_url in embed_pattern.findall(body):
        label_lc = label.lower()
        for key, slot in pdf_labels.items():
            if key in label_lc and slot not in pdfs:
                pdfs[slot] = pdf_url
                # Resize CDN image for display
                img = re.sub(r"w_\d+,h_\d+,c_fill", "w_600,h_800,c_fill", img_url)
                recipe_images[slot] = img
                break

    return {
        "title": title,
        "subtitle": subtitle,
        "date": date,
        "week_num": week_num,
        "post_url": post_url,
        "cover": cover,
        "pdfs": pdfs,
        "recipe_images": recipe_images,
    }

# ─── PDF PARSING ──────────────────────────────────────────────────────────────

def pdf_text(path, left_only=False, right_only=False):
    """Extract text from PDF, optionally splitting into left/right columns."""
    try:
        import pdfplumber
    except ImportError:
        sys.exit("✗ pdfplumber not installed. Run: pip3 install pdfplumber")

    pages_text = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            x0, y0, x1, y1 = page.bbox
            if left_only:
                text = page.within_bbox((x0, y0, x0 + (x1 - x0) * 0.48, y1)).extract_text() or ""
            elif right_only:
                text = page.within_bbox((x0 + (x1 - x0) * 0.52, y0, x1, y1)).extract_text() or ""
            else:
                text = page.extract_text() or ""
            pages_text.append(text)
    return "\n".join(pages_text)

def parse_grocery(path):
    """
    Returns list of dicts: {category, name, qty, days: [1,2,3,4,5], note}
    Parses both columns of both pages of the grocery PDF.
    Each column is extracted separately to preserve correct category assignment.
    """
    if not path or not Path(path).exists():
        return []

    try:
        import pdfplumber
    except ImportError:
        return []

    # Known category headers (longest first for matching)
    KNOWN_CATS = [
        "Eggs & Dairy", "Suggested Proteins", "Acids & Vinegars",
        "Spices & Aromatics", "Shelf-Stable", "Produce", "Cheese",
        "Proteins", "Fats", "Other",
    ]

    SKIP_RE = re.compile(
        r'^(GROCER|ESSENTI|LIST|KEY|& TIME|Essentials|Time Savers?|'
        r'Protein Substitutions?|Swaps?|Red =|Restaurant Dropout|\d+ Restaurant'
        r'|¹\s*=|²\s*=|³\s*=|⁴\s*=|⁵\s*=|[🔄*]|ADD\b)'
    )

    # Item: Name (qty)superscripts  — superscript chars or * for rollover
    item_re = re.compile(
        r'([A-Za-zÀ-ÿ0-9 &,\-\.\/\'½¼¾]+?)\s*\(([^)]+)\)\s*([¹²³⁴⁵*]+)'
    )

    def parse_col(text, default_cat):
        col_items = []
        current_cat = default_cat
        seen = set()

        # Fuzzy category detection: known category → (canonical name, partial match suffixes)
        CAT_SUFFIXES = [
            ("Produce",             ["produce"]),
            ("Eggs & Dairy",        ["eggs & dairy", "ggs & dairy", "gs & dairy", "s & dairy"]),
            ("Cheese",              ["cheese", "heese", "eese"]),
            ("Suggested Proteins",  ["suggested proteins", "uggested proteins", "ggested proteins",
                                     "gested proteins", "proteins"]),
            ("Shelf-Stable",        ["shelf-stable", "shelf stable"]),
            ("Fats",                ["fats"]),
            ("Acids & Vinegars",    ["acids & vinegars", "acids and vinegars"]),
            ("Spices & Aromatics",  ["spices & aromatics", "spices and aromatics"]),
            ("Other",               ["other"]),
        ]

        def detect_cat(line):
            lc = line.lower().strip()
            for cat_name, suffixes in CAT_SUFFIXES:
                for s in suffixes:
                    if lc == s or lc.endswith(s) or lc.startswith(s):
                        return cat_name
            return None

        SUP = r'[¹²³⁴⁵*]+'

        lines = text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            i += 1
            if not line or SKIP_RE.match(line):
                continue

            # ── Join incomplete lines ─────────────────────────────────────────
            # Case: "Garlic & Fine Herbs Boursin Cheese" + "(5.3oz)²³"
            if not re.search(SUP, line) and not re.search(r'\([^)]+\)', line):
                if i < len(lines) and re.match(r'\s*\([^)]+\)\s*[¹²³⁴⁵]', lines[i]):
                    line = line + " " + lines[i].strip()
                    i += 1

            # Case: "1-2 (9oz) Boneless Skinless Chicken" + "Breasts¹⁴"
            if re.search(r'\([^)]+\)', line) and not re.search(SUP, line):
                if i < len(lines) and re.search(SUP, lines[i]):
                    line = line + " " + lines[i].strip()
                    i += 1

            # Case: "2 (4oz) White Fish Fillets (like" + "Tilapia)⁵"
            if line.count('(') > line.count(')') and i < len(lines):
                line = line + " " + lines[i].strip()
                i += 1

            # ── Strip superscripts for category detection ─────────────────────
            no_sup = re.sub(r'[¹²³⁴⁵*]', '', line).strip()

            # ── Category header or essentials item (no qty parens) ───────────
            if not re.search(r'\([^)]+\)', no_sup):
                cat = detect_cat(no_sup)
                if cat:
                    current_cat = cat
                    continue
                # Not a category — check for essentials format "Name SUP"
                ess_m = re.match(r'^([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 &,\-\.\'½¼¾]*?)\s*(' + SUP + r')$', line)
                if ess_m:
                    name = ess_m.group(1).strip()
                    tags = ess_m.group(2)
                    days = sorted({SUP_MAP[c] for c in tags if c in SUP_MAP})
                    key = name.lower()
                    if name and key not in seen:
                        seen.add(key)
                        col_items.append({"category": current_cat, "name": name,
                                          "qty": "as needed", "days": days, "note": ""})
                continue

            # ── Number-prefixed protein items: "2 (4oz) Salmon Fillets²" ──────
            num_m = re.match(
                r'^(\d[\d\s\-/]*)\s*\(([^)]+)\)\s*'
                r'(?:(cans?|bunches?|stalks?|fillets?|pieces?)\s+)?'
                r'([A-Za-zÀ-ÿ][^¹²³⁴⁵*]*?)\s*(' + SUP + r')$', line)
            if num_m:
                count     = num_m.group(1).strip()
                unit_qty  = num_m.group(2).strip()
                unit_word = (num_m.group(3) or "").strip()
                name      = num_m.group(4).strip().rstrip(", ")
                tags      = num_m.group(5)
                qty_str   = f"{count} ({unit_qty}){' ' + unit_word if unit_word else ''}"
                days = sorted({SUP_MAP[c] for c in tags if c in SUP_MAP})
                note = "*" if "*" in tags else ""
                key = name.lower()
                if name and key not in seen:
                    seen.add(key)
                    col_items.append({"category": current_cat, "name": name,
                                      "qty": qty_str, "days": days, "note": note})
                continue

            # ── Standard items: "Name (qty)SUP" ──────────────────────────────
            for m in re.finditer(
                r'([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9 &,\-\.\/\'½¼¾]*?)\s*\(([^)]+)\)\s*(' + SUP + r')',
                line
            ):
                name = m.group(1).strip().rstrip(", ")
                qty  = m.group(2).strip()
                tags = m.group(3)
                days = sorted({SUP_MAP[c] for c in tags if c in SUP_MAP})
                note = "*" if "*" in tags else ""
                key  = name.lower()
                if name and key not in seen:
                    seen.add(key)
                    col_items.append({"category": current_cat, "name": name,
                                      "qty": qty, "days": days, "note": note})

        return col_items, seen

    all_items = []
    all_seen  = set()

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            x0, y0, x1, y1 = page.bbox
            w = x1 - x0
            mid = x0 + w * 0.5

            left_text  = page.within_bbox((x0,      y0, mid,  y1)).extract_text() or ""
            right_text = page.within_bbox((mid, y0, x1,  y1)).extract_text() or ""

            left_items,  left_seen  = parse_col(left_text,  "Produce")
            right_items, right_seen = parse_col(right_text, "Eggs & Dairy")

            for item in left_items + right_items:
                key = item["name"].lower()
                if key not in all_seen:
                    all_seen.add(key)
                    all_items.append(item)

    return all_items

def parse_prep(path):
    """Returns list of {title, time, items: [str]}"""
    if not path or not Path(path).exists():
        return []
    text = pdf_text(path)
    blocks = []
    current = None
    SKIP = {"PREP LIST", "KEY", "Restaurant Dropout", "Green =", "= estimated"}
    task_re = re.compile(r'^[☐□✓]\s*(.+)')

    for line in text.split("\n"):
        line = line.strip()
        if not line or any(line.startswith(s) for s in SKIP):
            continue
        if re.match(r'^\d+ Restaurant Dropout', line):
            continue
        # Day-key lines: ¹ = ...
        if re.match(r'^[¹²³⁴⁵]\s*=', line):
            continue
        m = task_re.match(line)
        if m:
            # New section
            task_text = m.group(1)
            # Extract optional time hint like "(30 minutes)"
            time_m = re.search(r'\(\s*([\d–]+\s*(?:minutes?|min|hours?|hrs?))\s*\)', task_text)
            time_hint = time_m.group(1) if time_m else ""
            title = re.sub(r'\(.*?\)', '', task_text).strip()
            current = {"title": title, "time": time_hint, "items": []}
            blocks.append(current)
        elif current is not None and line:
            current["items"].append(line)

    return blocks

def parse_recipe(path):
    """Returns {title, subtitle, servings, ingredients: [str], instructions: [str], note: str}"""
    if not path or not Path(path).exists():
        return None
    # Left column = ingredients, right column = instructions
    left  = pdf_text(path, left_only=True)
    right = pdf_text(path, right_only=True)
    full  = pdf_text(path)

    # Title/subtitle from full text header
    lines = [l.strip() for l in full.split("\n") if l.strip()]
    title    = lines[0] if lines else ""
    subtitle = ""
    servings = ""
    for i, l in enumerate(lines[1:4], 1):
        if l.startswith("WITH ") or l.startswith("with "):
            subtitle = l
        elif "serving" in l.lower() or "makes" in l.lower():
            servings = l

    # Ingredients: left column, skip header lines
    ingredients = []
    skip_words = {"INGREDIENTS", "INSTRUCTIONS", title, subtitle, servings,
                  "Restaurant Dropout", "Makes"}
    for line in left.split("\n"):
        line = line.strip()
        if not line or line in skip_words or line.startswith("Restaurant Dropout"):
            continue
        if re.match(r'^\d+\s+Restaurant', line):
            continue
        if any(line.startswith(s) for s in ("INGREDIENTS", "INSTRUCTIONS")):
            continue
        if "serving" in line.lower() and "makes" in line.lower():
            continue
        ingredients.append(line)

    # Instructions: right column, split by "Step N"
    instructions = []
    current_step = []
    for line in right.split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.match(r'^Step\s+\d+', line):
            if current_step:
                instructions.append(" ".join(current_step))
            current_step = [re.sub(r'^Step\s+\d+\s*', '', line).strip()]
        elif current_step is not None:
            current_step.append(line)
    if current_step:
        instructions.append(" ".join(current_step))

    # Notes: look for "Note:" section
    note = ""
    note_m = re.search(r'Note[s]?:\s*(.*?)(?=\n\n|\Z)', full, re.DOTALL | re.IGNORECASE)
    if note_m:
        note = " ".join(note_m.group(1).split())

    return {
        "title": title,
        "subtitle": subtitle,
        "servings": servings,
        "ingredients": [i for i in ingredients if i],
        "instructions": [s for s in instructions if s],
        "note": note,
    }

def parse_sauces(path):
    """Parse consolidated recipe PDF pages 6+ for sauce/brine/marinade sub-recipes."""
    if not path or not Path(path).exists():
        return []
    sauces = []
    try:
        import pdfplumber
        with pdfplumber.open(path) as pdf:
            # Sauces start around page 6 (0-indexed: page 5)
            for page in pdf.pages[5:]:
                w = page.width
                left  = page.within_bbox((0, 0, w * 0.48, page.height)).extract_text() or ""
                right = page.within_bbox((w * 0.52, 0, w, page.height)).extract_text() or ""
                full  = page.extract_text() or ""
                lines = [l.strip() for l in full.split("\n") if l.strip()]
                if not lines:
                    continue
                title = lines[0]
                yield_m = re.search(r'Makes\s+(?:about\s+)?(.+)', full)
                yield_str = yield_m.group(1).strip() if yield_m else ""

                ingredients = []
                for line in left.split("\n"):
                    line = line.strip()
                    if not line or line in {title, "INGREDIENTS", "INSTRUCTIONS"}:
                        continue
                    if "Restaurant Dropout" in line or re.match(r'^\d+\s', line):
                        continue
                    if "Makes" in line:
                        continue
                    ingredients.append(line)

                instructions = []
                current = []
                for line in right.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    if re.match(r'^Step\s+\d+', line):
                        if current:
                            instructions.append(" ".join(current))
                        current = [re.sub(r'^Step\s+\d+\s*', '', line).strip()]
                    elif current is not None:
                        current.append(line)
                if current:
                    instructions.append(" ".join(current))

                note_m = re.search(r'Notes?:\s*(.*?)(?=\n\n|\Z)', full, re.DOTALL)
                note = " ".join(note_m.group(1).split()) if note_m else ""

                sauces.append({
                    "title": title,
                    "yield": yield_str,
                    "ingredients": [i for i in ingredients if i],
                    "instructions": [s for s in instructions if s],
                    "note": note,
                })
    except Exception as e:
        print(f"  ⚠ Could not parse sauces: {e}")
    return sauces

# ─── METRIC CONVERSION ────────────────────────────────────────────────────────

FRAC_UNICODE = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 0.333, "⅔": 0.667}

def _parse_amount(s):
    """Parse a quantity string like '1½', '1-2', '2', '½' into a float (midpoint for ranges)."""
    s = s.strip()
    for frac, val in FRAC_UNICODE.items():
        if s == frac:
            return val
        if frac in s:
            parts = s.split(frac)
            try:
                return float(parts[0] or 0) + val
            except ValueError:
                return val
    range_m = re.match(r'([\d.]+)\s*(?:to|-)\s*([\d.]+)', s)
    if range_m:
        return (float(range_m.group(1)) + float(range_m.group(2))) / 2
    try:
        return float(s)
    except ValueError:
        return None

def metric_convert(name, qty):
    """Return a metric equivalent string (e.g. '~227g', '~475mL') or ''."""
    if not qty or qty in ("as needed", "optional", "-"):
        return ""
    qty_lc = qty.lower().strip()
    name_lc = name.lower()

    LIQUIDS = {"buttermilk", "milk", "oil", "vinegar", "stock", "broth", "juice", "water", "wine"}
    is_liquid = any(w in name_lc for w in LIQUIDS)

    # ── Cans first: "2 (15oz) cans" ── show drained weight for beans
    cans_m = re.match(r'^([\d½¼¾⅓⅔\s.\-to]+)\s*\(([\d.]+)\s*oz\)\s*cans?', qty_lc)
    if cans_m:
        count = _parse_amount(cans_m.group(1)) or 1
        return f"~{count * 255:.0f}g drained"

    # ── Items with oz in parentheses: "2 (4oz)", "1-2 (9oz)", "1 (10-12oz)" ──
    paren_oz = re.match(r'^([\d½¼¾⅓⅔\s.\-to]+)\s*\(([\d½¼¾⅓⅔.\-to]+)\s*-?\s*(?:oz|ounces?)\)', qty, re.IGNORECASE)
    if paren_oz:
        count   = _parse_amount(paren_oz.group(1)) or 1
        oz_each = _parse_amount(paren_oz.group(2)) or 0
        total_g = count * oz_each * 28.35
        return f"~{total_g:.0f}g"

    # ── Extract leading number + unit ─────────────────────────────────────────
    lead = re.match(r'^([\d½¼¾⅓⅔.]+(?:\s*(?:to|-)\s*[\d½¼¾⅓⅔.]+)?)\s*(.*)', qty)
    if not lead:
        return ""
    amount = _parse_amount(lead.group(1))
    unit   = lead.group(2).lower().strip().rstrip("s").rstrip(".")  # singularise
    if amount is None:
        return ""

    # ── oz ────────────────────────────────────────────────────────────────────
    if unit.startswith("oz") or unit == "":
        if unit.startswith("oz") or re.match(r'^\d+oz', qty_lc):
            if is_liquid:
                return f"~{amount * 29.57:.0f}mL"
            return f"~{amount * 28.35:.0f}g"

    # ── cups ──────────────────────────────────────────────────────────────────
    if "cup" in unit:
        if is_liquid or "stock" in name_lc or "brine" in name_lc or "marinade" in name_lc or "sauce" in name_lc:
            return f"~{amount * 237:.0f}mL"
        if "panko" in name_lc or "breadcrumb" in name_lc:
            return f"~{amount * 60:.0f}g"
        if "flour" in name_lc:
            return f"~{amount * 128:.0f}g"
        if "sugar" in name_lc:
            return f"~{amount * 200:.0f}g"
        if "pea" in name_lc or "bean" in name_lc:
            return f"~{amount * 160:.0f}g"
        if "feta" in name_lc or "cheese" in name_lc:
            return f"~{amount * 120:.0f}g"
        return ""  # avoid wrong mL for solid foods we can't classify

    # ── tablespoon / teaspoon ─────────────────────────────────────────────────
    if "tbsp" in unit or "tablespoon" in unit:
        if "butter" in name_lc:
            return f"~{amount * 14:.0f}g"
        if is_liquid or any(w in name_lc for w in {"oil", "vinegar", "sauce", "brine", "marinade", "buttermilk"}):
            return f"~{amount * 15:.0f}mL"
        return ""  # skip mL for solid tablespoon measures (herbs, spices, etc.)
    if "tsp" in unit or "teaspoon" in unit:
        return f"~{amount * 5:.0f}mL"

    # ── count-based with named units ──────────────────────────────────────────
    if "clove" in unit:
        return f"~{amount * 4:.0f}g"
    if "heart" in unit:          # baby gem lettuce
        return f"~{amount * 82:.0f}g"
    if "medium" in unit:
        if "potato" in name_lc:
            return f"~{amount * 150:.0f}g"
    if "head" in unit:
        if "cabbage" in name_lc:
            return f"~{amount * 450:.0f}g"   # ½ head is typical; full head ~900g
    if "bunch" in unit or "large bunch" in qty_lc or "small bunch" in qty_lc:
        if "dill" in name_lc or "chive" in name_lc:
            return f"~{amount * 42:.0f}g"
        if "mint" in name_lc:
            return f"~{amount * 10:.0f}g"
        if "asparagus" in name_lc:
            return f"~{amount * 450:.0f}g"

    # ── plain count (no unit word): use per-item lookup ───────────────────────
    if not unit or unit in ("large", "small", "medium", "optional"):
        if "lemon" in name_lc:
            return f"~{amount * 110:.0f}g"
        if "egg" in name_lc:
            return f"~{amount * 57:.0f}g"
        if "cucumber" in name_lc:
            return f"~{amount * 340:.0f}g"

    return ""


# ─── HTML GENERATION ──────────────────────────────────────────────────────────

DAY_COLORS = ["#7a9e7e", "#c16b3a", "#8b6f4e", "#6b8fad", "#9b7fb5"]

def esc(s):
    return htmllib.escape(str(s))

# Known unit words (order matters: longer first to avoid partial matches)
_UNITS_RE = re.compile(
    r'^([\d½¼¾⅓⅔][.\d½¼¾⅓⅔\s/\-]*?)\s*'
    r'(tablespoons?|teaspoons?|tbsps?|tsps?|fluid\s+oz|fl\.?\s*oz|ounces?|oz|'
    r'pounds?|lbs?|cups?|cloves?|bunches?|bunch|heads?|hearts?|stalks?|'
    r'sprigs?|cans?|slices?|pieces?|fillets?|medium|large|small)\.?\s+'
    r'(.+)$',
    re.IGNORECASE
)
_AMOUNT_ONLY_RE = re.compile(
    r'^([\d½¼¾⅓⅔][.\d½¼¾⅓⅔\s/\-]*?)\s+(.+)$'
)

def ingredient_metric(line):
    """Parse a recipe ingredient line and return a metric equivalent string, or ''."""
    line = line.strip()
    if not line:
        return ""

    # ── "N (X-ounce) name" or "N (Xoz) name" ─────────────────────────────────
    paren_m = re.match(
        r'^([\d½¼¾⅓⅔.]+(?:\s*(?:to|-)\s*[\d½¼¾⅓⅔.]+)?)\s*'
        r'\(([^)]*?)-?\s*(?:oz|ounces?)\)\s*.+$',
        line, re.IGNORECASE
    )
    if paren_m:
        count  = _parse_amount(paren_m.group(1)) or 1
        # clean "10- to 12" → "10 to 12" for range parsing
        oz_str = re.sub(r'\s*-\s*', ' ', paren_m.group(2)).strip()
        oz_each = _parse_amount(oz_str) or 0
        return f"~{count * oz_each * 28.35:.0f}g"

    # ── amount + unit + name ───────────────────────────────────────────────────
    m = _UNITS_RE.match(line)
    if m:
        amount_str = m.group(1).strip()
        unit       = m.group(2).strip()
        name       = m.group(3).split(",")[0].split("(")[0].strip()
        return metric_convert(name, f"{amount_str} {unit}")

    # ── amount + name (unit may trail the name: "2 garlic cloves, minced") ────
    m2 = _AMOUNT_ONLY_RE.match(line)
    if m2:
        amount_str = m2.group(1).strip()
        rest       = m2.group(2)
        unit_trail = re.match(
            r'^(.+?)\s+(cloves?|bunches?|heads?|hearts?|cans?|medium|large|small)\b',
            rest, re.IGNORECASE
        )
        if unit_trail:
            name = unit_trail.group(1).split(",")[0].strip()
            unit = unit_trail.group(2)
            return metric_convert(name, f"{amount_str} {unit}")
        name = rest.split(",")[0].split("(")[0].strip()
        return metric_convert(name, amount_str)

    return ""

def grocery_html(items):
    """Generate grocery list HTML from parsed items."""
    if not items:
        return "<p>Grocery list unavailable — check the Substack post.</p>"

    # Group by category
    from collections import OrderedDict
    cats = OrderedDict()
    for item in items:
        cat = item["category"]
        cats.setdefault(cat, []).append(item)

    html = []
    html.append('<div class="grocery-grid">')
    for cat, cat_items in cats.items():
        if not cat_items:
            continue
        html.append(f'  <div class="grocery-category">')
        html.append(f'    <h3>{esc(cat)}</h3><ul>')
        for i, item in enumerate(cat_items):
            gid = f"g_{cat[:3].lower()}_{i}"
            days_str = ",".join(str(d) for d in item["days"]) if item["days"] else "1,2,3,4,5"
            sup_str = "".join(["¹²³⁴⁵"[d-1] for d in item["days"]]) if item["days"] else ""
            note = " *rollover" if item.get("note") == "*" else ""
            metric = metric_convert(item["name"], item["qty"])
            metric_html = f'<div class="qty-metric">{esc(metric)}</div>' if metric else ""
            html.append(f'''      <li class="grocery-item" data-days="{days_str}">
        <input type="checkbox" id="{gid}">
        <label class="grocery-item-label" for="{gid}">
          <span class="grocery-item-name">{esc(item["name"])}</span>
          <span class="meal-tags">{sup_str}{note}</span>
        </label>
        <div class="grocery-item-convert">
          <div class="qty-main">{esc(item["qty"])}</div>
          {metric_html}
        </div>
      </li>''')
        html.append("    </ul></div>")
    html.append("</div>")
    return "\n".join(html)

def prep_html(blocks):
    """Generate prep list HTML from parsed blocks."""
    if not blocks:
        return "<p>Prep list unavailable — check the Substack post.</p>"
    html = ['<div class="prep-timeline">']
    for i, block in enumerate(blocks):
        time_badge = f'<span class="time-badge">{esc(block["time"])}</span>' if block["time"] else ""
        html.append(f'''  <div class="prep-block">
    <h3>{esc(block["title"])} {time_badge}</h3>
    <ul class="prep-items">''')
        for j, item in enumerate(block["items"]):
            pid = f"p{i}_{j}"
            html.append(f'''      <li><input type="checkbox" id="{pid}"><label for="{pid}">{esc(item)}</label></li>''')
        if not block["items"]:
            html.append(f'''      <li><input type="checkbox" id="p{i}_0"><label for="p{i}_0">{esc(block["title"])}</label></li>''')
        html.append("    </ul>\n  </div>")
    html.append("</div>")
    return "\n".join(html)

def recipe_panel_html(recipe, day_num, is_active=False):
    if not recipe:
        return f'<div id="recipe-day{day_num}" class="recipe-panel{" active" if is_active else ""}"><p>Recipe unavailable.</p></div>'
    color = DAY_COLORS[day_num - 1]
    subtitle_part = f'<div class="recipe-sub">{esc(recipe["subtitle"])}</div>' if recipe.get("subtitle") else ""
    note_part = f'<div class="recipe-note"><strong>Note</strong>{esc(recipe["note"])}</div>' if recipe.get("note") else ""

    ingr_parts = []
    for i in recipe["ingredients"]:
        m = ingredient_metric(i)
        metric_span = f' <span class="ingr-metric">{esc(m)}</span>' if m else ""
        ingr_parts.append(f"          <li>{esc(i)}{metric_span}</li>")
    ingr_lines = "\n".join(ingr_parts)
    inst_lines = "\n".join(f"          <li>{esc(s)}</li>" for s in recipe["instructions"])

    return f'''  <div id="recipe-day{day_num}" class="recipe-panel{" active" if is_active else ""}">
    <div class="recipe-header">
      <div class="recipe-header-top">
        <div>
          <div class="recipe-day-label" style="color:{color}">Day {day_num}</div>
          <h2>{esc(recipe["title"])}</h2>
          {subtitle_part}
          <div class="servings">{esc(recipe.get("servings","Makes 2 servings"))}</div>
        </div>
        <div class="recipe-actions">
          <button class="save-cookbook-btn" id="save-btn-day{day_num}" onclick="saveRecipeToCookbook('day{day_num}')">&#9825; Save to Cookbook</button>
        </div>
      </div>
    </div>
    <div class="recipe-body">
      <div class="ingredients-col">
        <h3>Ingredients</h3>
        <ul>
{ingr_lines}
        </ul>
      </div>
      <div class="instructions-col">
        <h3>Instructions</h3>
        <ol>
{inst_lines}
        </ol>
        {note_part}
      </div>
    </div>
  </div>'''

def sauce_card_html(sauce):
    ingr = "\n".join(f"              <li>{esc(i)}</li>" for i in sauce["ingredients"])
    inst = "\n".join(f"              <li>{esc(s)}</li>" for s in sauce["instructions"])
    note = f'<div class="recipe-note" style="margin-top:12px;"><strong>Note</strong>{esc(sauce["note"])}</div>' if sauce.get("note") else ""
    return f'''      <div class="sauce-card">
        <h3>{esc(sauce["title"])}</h3>
        <div class="yield">{esc(sauce.get("yield",""))}</div>
        <div class="sauce-two-col">
          <div><h4>Ingredients</h4><ul>
{ingr}
          </ul></div>
          <div><h4>Instructions</h4><ol>
{inst}
          </ol></div>
        </div>
        {note}
      </div>'''

def overview_card_html(day_num, title, subtitle, img_url):
    color = DAY_COLORS[day_num - 1]
    img_tag = f'<img class="day-card-img" src="{esc(img_url)}" alt="{esc(title)}" loading="lazy" onerror="this.style.display=\'none\'">' if img_url else ""
    return f'''    <div class="day-card" data-day="{day_num}" onclick="openRecipe(\'day{day_num}\')">
      {img_tag}
      <div class="day-card-body">
        <div class="day-num" style="color:{color}">Day {day_num}</div>
        <h3>{esc(title)}</h3>
        <div class="day-sub">{esc(subtitle)}</div>
      </div>
    </div>'''

def build_html(meta, grocery_items, prep_blocks, recipes, sauces):
    """Assemble the full HTML page."""

    # Overview cards
    day_titles = []
    for i, r in enumerate(recipes, 1):
        if r:
            full_title = r["title"].title() if r["title"].isupper() else r["title"]
            sub = r["subtitle"].replace("WITH ", "with ").replace("WITH\n", "with ") if r.get("subtitle") else ""
            day_titles.append((full_title, sub))
        else:
            day_titles.append((f"Day {i}", ""))

    overview_cards = "\n".join(
        overview_card_html(
            i + 1,
            day_titles[i][0],
            day_titles[i][1],
            meta["recipe_images"].get(f"day{i+1}", "")
        )
        for i in range(len(day_titles))
    )

    # Recipe panels
    recipe_panels = "\n".join(
        recipe_panel_html(r, i + 1, is_active=(i == 0))
        for i, r in enumerate(recipes)
    )

    # Recipe tab buttons
    recipe_tabs = "\n".join(
        f'''    <button class="recipe-tab{" active" if i==0 else ""}" data-recipe="day{i+1}">
      <span class="rt-day">Day {i+1}</span>
      <span class="rt-name">{esc(day_titles[i][0])}</span>
    </button>'''
        for i in range(len(day_titles))
    )

    # Sauce cards
    sauce_cards = "\n".join(sauce_card_html(s) for s in sauces) if sauces else "<p>Sauce recipes unavailable.</p>"

    # Filter toggles
    filter_toggles = "\n".join(
        f'''      <label class="filter-toggle selected" data-day="{i+1}">
        <input type="checkbox" checked data-filter="{i+1}">
        <div>
          <span class="ft-day">Day {i+1}</span>
          <span class="ft-name">{esc(day_titles[i][0][:20])}</span>
        </div>
      </label>'''
        for i in range(len(day_titles))
    )

    # Grocery HTML
    grocery_section = grocery_html(grocery_items)

    # Prep HTML
    prep_section = prep_html(prep_blocks)

    week_label = f"Week {meta['week_num']}" if meta["week_num"] != "?" else meta["date"]

    # Embed current week's recipe data for the Save to Cookbook button
    week_recipes = {}
    for i, r in enumerate(recipes, 1):
        if r:
            week_recipes[f"day{i}"] = {
                "id": f"week{meta['week_num']}-day{i}",
                "recipeKey": f"day{i}",
                "title": r["title"],
                "subtitle": r.get("subtitle", ""),
                "weekNum": meta["week_num"],
                "weekLabel": week_label,
                "postUrl": meta["post_url"],
                "coverImg": meta["recipe_images"].get(f"day{i}", ""),
                "ingredients": r["ingredients"],
                "instructions": r["instructions"],
            }
    week_recipes_json = json.dumps(week_recipes, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Restaurant Dropout — {esc(week_label)}</title>
  <style>
    :root {{
      --cream:#faf6f0;--warm-white:#fff8f2;--sage:#7a9e7e;--sage-light:#d4e6d5;
      --charcoal:#2c2c2c;--muted:#6b6560;--border:#e8e0d8;--accent:#c16b3a;--accent-light:#f5e8df;
    }}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:'Georgia',serif;background:var(--cream);color:var(--charcoal);font-size:16px;line-height:1.65}}
    header{{background:var(--charcoal);color:#fff;padding:28px 24px 24px;text-align:center}}
    header .week-label{{font-size:11px;letter-spacing:3px;text-transform:uppercase;color:#bbb;margin-bottom:6px}}
    header h1{{font-size:clamp(22px,4vw,36px);font-weight:normal;line-height:1.2}}
    header .subtitle{{margin-top:8px;font-size:14px;color:#bbb;font-style:italic}}
    header .post-link{{display:inline-block;margin-top:14px;font-size:12px;color:#bbb;text-decoration:none;border-bottom:1px solid #666;padding-bottom:1px;letter-spacing:1px}}
    header .post-link:hover{{color:#fff;border-color:#fff}}
    nav{{background:var(--warm-white);border-bottom:2px solid var(--border);position:sticky;top:0;z-index:100;overflow-x:auto;white-space:nowrap}}
    nav::-webkit-scrollbar{{height:3px}} nav::-webkit-scrollbar-thumb{{background:var(--border)}}
    nav ul{{display:inline-flex;list-style:none;padding:0 16px}}
    nav ul li a{{display:block;padding:14px 18px;text-decoration:none;font-size:13px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-2px;transition:color .2s,border-color .2s}}
    nav ul li a:hover{{color:var(--charcoal)}}
    nav ul li a.active{{color:var(--accent);border-bottom-color:var(--accent);font-weight:bold}}
    .section{{display:none;max-width:960px;margin:0 auto;padding:32px 20px 60px}}
    .section.active{{display:block}}
    .overview-intro{{font-size:17px;line-height:1.75;max-width:680px;margin-bottom:32px}}
    .week-menu{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:0;margin-bottom:32px}}
    .day-card{{background:var(--warm-white);border:1px solid var(--border);overflow:hidden;cursor:pointer;transition:transform .15s,box-shadow .15s;display:flex;flex-direction:column}}
    .day-card:hover{{transform:translateY(-3px);box-shadow:0 6px 20px rgba(0,0,0,.1);z-index:1;position:relative}}
    .day-card-img{{width:100%;aspect-ratio:3/4;object-fit:cover;display:block;background:var(--border)}}
    .day-card-body{{padding:16px 18px 20px;flex:1}}
    .day-card .day-num{{font-size:22px;font-weight:normal;margin-bottom:4px;line-height:1}}
    .day-card h3{{font-size:15px;font-weight:bold;line-height:1.25;margin-bottom:3px}}
    .day-card .day-sub{{font-size:13px;color:var(--muted);font-style:italic}}
    .notes-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;margin-top:32px}}
    .note-box{{background:var(--warm-white);border:1px solid var(--border);border-radius:8px;padding:16px 18px}}
    .note-box h4{{font-size:12px;letter-spacing:2px;text-transform:uppercase;color:var(--accent);margin-bottom:8px}}
    .note-box p{{font-size:14px;color:var(--muted);line-height:1.6}}
    .section-header{{margin-bottom:24px}}
    .section-header h2{{font-size:26px;font-weight:normal}}
    .section-header p{{font-size:14px;color:var(--muted);margin-top:4px;font-style:italic}}
    .recipe-filter{{background:var(--warm-white);border:1px solid var(--border);border-radius:10px;padding:18px 20px;margin-bottom:24px}}
    .recipe-filter h3{{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:14px}}
    .filter-toggles{{display:flex;flex-wrap:wrap;gap:10px}}
    .filter-toggle{{display:flex;align-items:center;gap:8px;padding:8px 14px;border-radius:24px;border:2px solid var(--border);cursor:pointer;background:var(--cream);transition:all .15s;user-select:none}}
    .filter-toggle input{{display:none}}
    .filter-toggle.selected{{border-color:currentColor;background:#fff}}
    .filter-toggle[data-day="1"]{{color:#7a9e7e}}.filter-toggle[data-day="2"]{{color:#c16b3a}}
    .filter-toggle[data-day="3"]{{color:#8b6f4e}}.filter-toggle[data-day="4"]{{color:#6b8fad}}
    .filter-toggle[data-day="5"]{{color:#9b7fb5}}
    .filter-toggle .ft-day{{font-size:13px;font-weight:bold}}
    .filter-toggle .ft-name{{font-size:11px;color:var(--muted)}}
    .filter-toggle.selected .ft-name{{color:inherit;opacity:.7}}
    .filter-all-btn{{font-size:12px;color:var(--muted);text-decoration:underline;cursor:pointer;background:none;border:none;margin-top:10px}}
    .grocery-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:20px;margin-bottom:28px}}
    .grocery-category{{background:var(--warm-white);border:1px solid var(--border);border-radius:10px;padding:20px}}
    .grocery-category h3{{font-size:11px;letter-spacing:2.5px;text-transform:uppercase;color:var(--accent);margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--border)}}
    .grocery-category ul{{list-style:none}}
    .grocery-item{{display:grid;grid-template-columns:20px 1fr auto;align-items:flex-start;gap:8px;padding:7px 0;border-bottom:1px solid #f0ece6;transition:opacity .2s}}
    .grocery-item:last-child{{border-bottom:none}}
    .grocery-item.hidden{{display:none}}
    .grocery-item input[type=checkbox]{{margin-top:3px;width:16px;height:16px;accent-color:var(--sage);cursor:pointer}}
    .grocery-item-label{{cursor:pointer}}
    .grocery-item-label.checked{{text-decoration:line-through;color:#bbb}}
    .grocery-item-name{{font-size:14px;line-height:1.35}}
    .grocery-item-convert{{font-size:11px;text-align:right;line-height:1.3}}
    .qty-main{{font-size:13px;font-weight:bold;color:var(--charcoal)}}
    .qty-metric{{font-size:13px;font-weight:bold;color:var(--sage);margin-top:2px;letter-spacing:.3px}}
    .meal-tags{{font-size:10px;color:#bbb;display:block;margin-top:1px}}
    .prep-timeline{{max-width:680px}}
    .prep-block{{margin-bottom:28px;padding-left:24px;border-left:3px solid var(--sage-light);position:relative}}
    .prep-block::before{{content:'';position:absolute;left:-8px;top:4px;width:13px;height:13px;border-radius:50%;background:var(--sage);border:2px solid var(--cream)}}
    .prep-block h3{{font-size:13px;letter-spacing:1.5px;text-transform:uppercase;color:var(--sage);margin-bottom:12px;display:flex;align-items:center;gap:8px}}
    .time-badge{{font-size:11px;background:var(--sage-light);color:var(--sage);padding:2px 8px;border-radius:20px;letter-spacing:0;text-transform:none}}
    .prep-items{{list-style:none}}
    .prep-items li{{display:flex;align-items:flex-start;gap:10px;padding:8px 0;font-size:14px;border-bottom:1px solid var(--border)}}
    .prep-items li:last-child{{border-bottom:none}}
    .prep-items li input[type=checkbox]{{margin-top:3px;flex-shrink:0;width:16px;height:16px;accent-color:var(--sage);cursor:pointer}}
    .prep-items li label{{cursor:pointer;line-height:1.5}}
    .prep-items li label.checked{{text-decoration:line-through;color:#bbb}}
    .break-divider{{text-align:center;font-size:12px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin:24px 0;display:flex;align-items:center;gap:12px}}
    .break-divider::before,.break-divider::after{{content:'';flex:1;height:1px;background:var(--border)}}
    .recipe-nav{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:28px}}
    .recipe-tab{{padding:10px 16px;border-radius:10px;border:1px solid var(--border);background:var(--warm-white);cursor:pointer;transition:all .15s;text-align:left;min-width:110px}}
    .recipe-tab:hover{{border-color:#bbb}}
    .recipe-tab.active{{background:var(--charcoal);color:#fff;border-color:var(--charcoal)}}
    .recipe-tab.sauce-tab.active{{background:var(--sage);border-color:var(--sage)}}
    .recipe-tab .rt-day{{font-size:13px;font-weight:bold;letter-spacing:1px;display:block;color:inherit}}
    .recipe-tab .rt-name{{font-size:10px;display:block;margin-top:2px;line-height:1.3;opacity:.65}}
    .recipe-tab[data-recipe="day1"]:not(.active) .rt-day{{color:#7a9e7e}}
    .recipe-tab[data-recipe="day2"]:not(.active) .rt-day{{color:#c16b3a}}
    .recipe-tab[data-recipe="day3"]:not(.active) .rt-day{{color:#8b6f4e}}
    .recipe-tab[data-recipe="day4"]:not(.active) .rt-day{{color:#6b8fad}}
    .recipe-tab[data-recipe="day5"]:not(.active) .rt-day{{color:#9b7fb5}}
    .recipe-tab[data-recipe="sauces"]:not(.active) .rt-day{{color:var(--sage)}}
    .recipe-panel{{display:none}}.recipe-panel.active{{display:block}}
    .recipe-header{{margin-bottom:28px}}
    .recipe-header .recipe-day-label{{font-size:11px;letter-spacing:3px;text-transform:uppercase;margin-bottom:6px}}
    .recipe-header h2{{font-size:clamp(20px,3vw,30px);font-weight:normal;line-height:1.2}}
    .recipe-header .recipe-sub{{font-size:16px;color:var(--muted);font-style:italic;margin-top:4px}}
    .recipe-header .servings{{margin-top:10px;font-size:13px;color:var(--muted);letter-spacing:1px}}
    .recipe-body{{display:grid;grid-template-columns:1fr 1fr;gap:32px;align-items:start}}
    @media(max-width:640px){{.recipe-body{{grid-template-columns:1fr}}}}
    .ingredients-col h3,.instructions-col h3{{font-size:11px;letter-spacing:2.5px;text-transform:uppercase;color:var(--accent);margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid var(--border)}}
    .ingredients-col ul{{list-style:none}}
    .ingredients-col ul li{{padding:6px 0;font-size:14px;border-bottom:1px solid #f0ece6;line-height:1.4}}
    .ingredients-col ul li:last-child{{border-bottom:none}}
    .ingredients-col ul li.section-break{{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--sage);margin-top:12px;border-bottom:none}}
    .ingr-metric{{font-size:12px;font-weight:bold;color:var(--sage);margin-left:6px;white-space:nowrap}}
    .instructions-col ol{{list-style:none;counter-reset:steps}}
    .instructions-col ol li{{counter-increment:steps;padding:10px 0 10px 36px;font-size:14px;line-height:1.6;border-bottom:1px solid #f0ece6;position:relative}}
    .instructions-col ol li:last-child{{border-bottom:none}}
    .instructions-col ol li::before{{content:counter(steps);position:absolute;left:0;top:12px;width:24px;height:24px;border-radius:50%;background:var(--charcoal);color:#fff;font-size:11px;display:flex;align-items:center;justify-content:center}}
    .recipe-note{{margin-top:20px;padding:14px 16px;background:var(--accent-light);border-radius:8px;font-size:13px;line-height:1.6}}
    .recipe-note strong{{display:block;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--accent);margin-bottom:6px}}
    .sauce-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:24px}}
    @media(max-width:480px){{.sauce-grid{{grid-template-columns:1fr}}}}
    .sauce-card{{background:var(--warm-white);border:1px solid var(--border);border-radius:10px;padding:22px}}
    .sauce-card h3{{font-size:16px;font-weight:normal;margin-bottom:4px}}
    .sauce-card .yield{{font-size:12px;color:var(--muted);margin-bottom:16px;font-style:italic}}
    .sauce-two-col{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
    @media(max-width:480px){{.sauce-two-col{{grid-template-columns:1fr}}}}
    .sauce-card h4{{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--sage);margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid var(--border)}}
    .sauce-card ul{{list-style:none}}
    .sauce-card ul li{{font-size:13px;padding:4px 0;border-bottom:1px solid #f0ece6;line-height:1.4}}
    .sauce-card ul li:last-child{{border-bottom:none}}
    .sauce-card ol{{list-style:none;counter-reset:steps}}
    .sauce-card ol li{{counter-increment:steps;font-size:13px;padding:6px 0 6px 28px;border-bottom:1px solid #f0ece6;position:relative;line-height:1.5}}
    .sauce-card ol li:last-child{{border-bottom:none}}
    .sauce-card ol li::before{{content:counter(steps);position:absolute;left:0;top:8px;width:18px;height:18px;border-radius:50%;background:var(--sage);color:#fff;font-size:10px;display:flex;align-items:center;justify-content:center}}
    .recipe-header-top{{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}}
    .recipe-actions{{display:flex;gap:8px;flex-shrink:0;margin-top:4px}}
    .save-cookbook-btn{{font-size:12px;padding:7px 14px;border-radius:20px;border:1.5px solid var(--accent);background:transparent;color:var(--accent);cursor:pointer;letter-spacing:.5px;transition:all .15s;white-space:nowrap}}
    .save-cookbook-btn:hover{{background:var(--accent);color:#fff}}
    .save-cookbook-btn.saved{{background:var(--sage);border-color:var(--sage);color:#fff;cursor:default}}
    .cb-toolbar{{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:24px}}
    .cb-search{{flex:1;min-width:180px;padding:9px 14px;border:1.5px solid var(--border);border-radius:8px;font-size:14px;font-family:inherit;background:var(--warm-white)}}
    .cb-search:focus{{outline:none;border-color:var(--sage)}}
    .cb-filter-btns{{display:flex;gap:6px}}
    .cb-filter-btn{{padding:8px 14px;border-radius:20px;border:1.5px solid var(--border);background:var(--warm-white);font-size:12px;letter-spacing:.5px;cursor:pointer;transition:all .15s}}
    .cb-filter-btn.active{{border-color:var(--charcoal);background:var(--charcoal);color:#fff}}
    .cb-new-btn{{padding:9px 18px;border-radius:8px;border:none;background:var(--accent);color:#fff;font-size:13px;cursor:pointer;letter-spacing:.5px;white-space:nowrap}}
    .cb-new-btn:hover{{opacity:.88}}
    .cb-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:20px;margin-bottom:40px}}
    .cb-empty{{padding:48px 20px;text-align:center;color:var(--muted);font-size:15px;line-height:1.7;grid-column:1/-1}}
    .cb-card{{background:var(--warm-white);border:1px solid var(--border);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}}
    .cb-card.cb-starred{{border-color:#f0c040;box-shadow:0 0 0 2px #f0c04033}}
    .cb-card-top{{padding:16px 16px 0}}
    .cb-card-meta{{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:10px}}
    .cb-week-badge{{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;background:var(--accent-light);color:var(--accent);padding:3px 8px;border-radius:20px}}
    .cb-made-badge{{font-size:10px;background:var(--sage-light);color:var(--sage);padding:3px 8px;border-radius:20px;letter-spacing:.5px}}
    .cb-last-made{{font-size:10px;color:var(--muted)}}
    .cb-card-btns{{display:flex;gap:6px;align-items:center;margin-left:auto}}
    .cb-star{{background:none;border:none;font-size:20px;cursor:pointer;color:#ccc;line-height:1;padding:0 2px;transition:color .15s}}
    .cb-star.on{{color:#f0c040}}
    .cb-log-btn-sm{{font-size:11px;padding:4px 10px;border-radius:14px;border:1.5px solid var(--sage);color:var(--sage);background:none;cursor:pointer;white-space:nowrap}}
    .cb-log-btn-sm:hover{{background:var(--sage);color:#fff}}
    .cb-remove{{background:none;border:none;font-size:16px;color:#ccc;cursor:pointer;padding:0 2px;line-height:1}}
    .cb-remove:hover{{color:#c00}}
    .cb-card-title{{font-size:17px;font-weight:normal;padding:0 16px 2px}}
    .cb-card-sub{{font-size:13px;color:var(--muted);font-style:italic;padding:0 16px 10px}}
    .cb-details{{margin:0 16px 0;border-top:1px solid var(--border)}}
    .cb-details summary{{font-size:12px;letter-spacing:1px;text-transform:uppercase;color:var(--muted);padding:10px 0;cursor:pointer;user-select:none}}
    .cb-details summary:hover{{color:var(--charcoal)}}
    .cb-recipe-body{{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:10px 0 12px}}
    @media(max-width:500px){{.cb-recipe-body{{grid-template-columns:1fr}}}}
    .cb-recipe-body h4{{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--accent);margin-bottom:8px}}
    .cb-recipe-body ul{{list-style:none}}
    .cb-recipe-body ul li,.cb-recipe-body ol li{{font-size:12px;padding:3px 0;border-bottom:1px solid #f0ece6;line-height:1.4}}
    .cb-recipe-body ul li:last-child,.cb-recipe-body ol li:last-child{{border-bottom:none}}
    .cb-recipe-body ol{{padding-left:18px;font-size:12px}}
    .cb-copy-btn{{font-size:10px;padding:3px 8px;border-radius:10px;border:1px solid var(--border);background:var(--cream);cursor:pointer;float:right;margin-top:-2px}}
    .cb-copy-btn:hover{{background:var(--sage-light)}}
    .cb-substack-link{{font-size:12px;color:var(--muted);text-decoration:none;padding:10px 16px;display:block;border-top:1px solid var(--border);letter-spacing:.3px}}
    .cb-substack-link:hover{{color:var(--accent)}}
    .cb-log-inline{{padding:12px 16px;background:var(--cream);border-top:1px solid var(--border)}}
    .cb-log-inline-hdr{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
    .cb-log-inline-hdr h4{{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--muted)}}
    .cb-log-item{{border-bottom:1px solid var(--border);padding:8px 0}}
    .cb-log-item:last-child{{border-bottom:none}}
    .cb-log-item-hdr{{display:flex;gap:8px;align-items:center;margin-bottom:4px}}
    .cb-log-date{{font-size:11px;color:var(--muted)}}
    .cb-log-rating{{font-size:13px;color:#f0c040;letter-spacing:-1px}}
    .cb-log-del{{background:none;border:none;color:#ccc;cursor:pointer;font-size:13px;margin-left:auto;padding:0}}
    .cb-log-del:hover{{color:#c00}}
    .cb-log-notes{{font-size:13px;line-height:1.5;color:var(--charcoal)}}
    .cb-divider{{font-size:11px;letter-spacing:2.5px;text-transform:uppercase;color:var(--muted);text-align:center;padding:16px 0 20px;display:flex;align-items:center;gap:12px}}
    .cb-divider::before,.cb-divider::after{{content:'';flex:1;height:1px;background:var(--border)}}
    .cb-log-section{{margin-top:8px}}
    .cb-log-section h3{{font-size:13px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:16px}}
    .cb-log-entry{{background:var(--warm-white);border:1px solid var(--border);border-radius:10px;padding:16px 18px;margin-bottom:12px}}
    .cb-log-entry.manual{{border-left:3px solid var(--accent)}}
    .cb-log-entry-hdr{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}}
    .cb-entry-title{{font-size:15px;font-weight:bold}}
    .cb-manual-tag{{font-size:10px;letter-spacing:1px;background:var(--accent-light);color:var(--accent);padding:2px 7px;border-radius:10px;text-transform:uppercase}}
    .cb-log-entry-notes{{font-size:14px;line-height:1.6;color:var(--charcoal);margin-bottom:10px}}
    .cb-manual-text{{font-size:13px;line-height:1.6;white-space:pre-wrap;background:var(--cream);padding:10px 12px;border-radius:6px;margin-top:6px}}
    .cb-modal-overlay{{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:500;display:flex;align-items:center;justify-content:center;padding:16px}}
    .cb-modal{{background:#fff;border-radius:14px;width:100%;max-width:540px;max-height:90vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.25)}}
    .cb-modal-hdr{{display:flex;justify-content:space-between;align-items:center;padding:20px 22px 16px;border-bottom:1px solid var(--border)}}
    .cb-modal-hdr h3{{font-size:16px;font-weight:normal}}
    .cb-modal-close{{background:none;border:none;font-size:22px;color:var(--muted);cursor:pointer;line-height:1}}
    .cb-modal-body{{padding:20px 22px 24px}}
    .cb-type-toggle{{display:flex;gap:0;margin-bottom:20px;border:1.5px solid var(--border);border-radius:8px;overflow:hidden}}
    .cb-type-btn{{flex:1;padding:9px;font-size:13px;border:none;background:transparent;cursor:pointer;transition:all .15s;font-family:inherit}}
    .cb-type-btn.active{{background:var(--charcoal);color:#fff}}
    .cb-form-group{{margin-bottom:16px}}
    .cb-form-group label{{display:block;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:6px}}
    .cb-form-group input,.cb-form-group select,.cb-form-group textarea{{width:100%;padding:9px 12px;border:1.5px solid var(--border);border-radius:8px;font-size:14px;font-family:inherit;background:var(--cream)}}
    .cb-form-group input:focus,.cb-form-group select:focus,.cb-form-group textarea:focus{{outline:none;border-color:var(--sage)}}
    .cb-form-group textarea{{min-height:80px;resize:vertical}}
    .cb-rating-row{{display:flex;gap:4px;font-size:26px;cursor:pointer}}
    .cb-rating-star{{color:#ddd;transition:color .1s;line-height:1}}
    .cb-rating-star.lit{{color:#f0c040}}
    .cb-modal-footer{{display:flex;gap:10px;justify-content:flex-end;padding-top:8px}}
    .cb-btn-cancel{{padding:9px 18px;border-radius:8px;border:1.5px solid var(--border);background:transparent;font-size:13px;cursor:pointer;font-family:inherit}}
    .cb-btn-save{{padding:9px 20px;border-radius:8px;border:none;background:var(--accent);color:#fff;font-size:13px;cursor:pointer;font-family:inherit}}
    .cb-btn-save:hover{{opacity:.88}}
    .cb-toast{{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(80px);background:var(--charcoal);color:#fff;padding:11px 22px;border-radius:24px;font-size:13px;letter-spacing:.3px;z-index:600;transition:transform .3s ease,opacity .3s ease;opacity:0;pointer-events:none}}
    .cb-toast.show{{transform:translateX(-50%) translateY(0);opacity:1}}
    .last-updated{{font-size:11px;color:var(--muted);letter-spacing:1px;text-align:center;padding:10px;margin-bottom:-20px}}
    footer{{text-align:center;padding:24px;font-size:12px;color:var(--muted);border-top:1px solid var(--border)}}
    footer a{{color:var(--accent);text-decoration:none}}
  </style>
</head>
<body>

<div id="pw-gate" style="position:fixed;inset:0;background:var(--charcoal);z-index:9999;display:flex;align-items:center;justify-content:center">
  <div style="background:#fff;border-radius:14px;padding:44px 36px;max-width:340px;width:90%;text-align:center;font-family:'Georgia',serif">
    <div style="font-size:10px;letter-spacing:3px;text-transform:uppercase;color:#bbb;margin-bottom:10px">Restaurant Dropout</div>
    <h2 style="font-size:26px;font-weight:normal;margin-bottom:6px">Meal Prep</h2>
    <p style="font-size:13px;color:#6b6560;margin-bottom:28px;font-style:italic">Enter the password to continue</p>
    <input type="password" id="pw-input" placeholder="Password" autofocus
      style="width:100%;padding:12px;border:1.5px solid #e8e0d8;border-radius:8px;font-size:16px;font-family:'Georgia',serif;text-align:center;margin-bottom:12px;box-sizing:border-box"
      onkeydown="if(event.key==='Enter')checkPw()">
    <button onclick="checkPw()"
      style="width:100%;padding:12px;background:#2c2c2c;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;font-family:'Georgia',serif;letter-spacing:.5px">
      Enter
    </button>
    <p id="pw-err" style="color:#c16b3a;font-size:13px;margin-top:12px;display:none">Incorrect password — try again</p>
  </div>
</div>
<script>
(function(){{
  if(localStorage.getItem('rd-auth')==='ok'){{
    document.getElementById('pw-gate').style.display='none';
  }}
}})();
function checkPw(){{
  if(document.getElementById('pw-input').value==='nilam'){{
    localStorage.setItem('rd-auth','ok');
    document.getElementById('pw-gate').style.display='none';
  }} else {{
    document.getElementById('pw-err').style.display='block';
    document.getElementById('pw-input').value='';
    document.getElementById('pw-input').focus();
  }}
}}
</script>

<header>
  <div class="week-label">Restaurant Dropout &bull; {esc(week_label)}</div>
  <h1>{esc(meta["title"])}</h1>
  <div class="subtitle">{esc(meta["subtitle"])}</div>
  <a class="post-link" href="{esc(meta["post_url"])}" target="_blank">View on Substack &rarr;</a>
</header>

<nav>
  <ul>
    <li><a href="#" class="nav-link active" data-target="overview">Overview</a></li>
    <li><a href="#" class="nav-link" data-target="grocery">Grocery List</a></li>
    <li><a href="#" class="nav-link" data-target="prep">Prep List</a></li>
    <li><a href="#" class="nav-link" data-target="recipes">Recipes</a></li>
    <li><a href="#" class="nav-link" data-target="cookbook">My Cookbook</a></li>
  </ul>
</nav>

<!-- OVERVIEW -->
<section id="overview" class="section active">
  <p class="overview-intro">{esc(meta["subtitle"])} Click any meal to jump to its recipe.</p>
  <div class="week-menu">
{overview_cards}
  </div>
</section>

<!-- GROCERY -->
<section id="grocery" class="section">
  <div class="section-header">
    <h2>Grocery List</h2>
    <p>{esc(week_label)} &middot; Select which recipes you&rsquo;re making, then check off as you shop</p>
  </div>
  <div class="recipe-filter">
    <h3>I&rsquo;m cooking this week:</h3>
    <div class="filter-toggles">
{filter_toggles}
    </div>
    <button class="filter-all-btn" onclick="toggleAllFilters()">Select / deselect all</button>
  </div>
  {grocery_section}
</section>

<!-- PREP -->
<section id="prep" class="section">
  <div class="section-header">
    <h2>Sunday Prep List</h2>
    <p>{esc(week_label)} &middot; Check off as you go</p>
  </div>
  {prep_section}
</section>

<!-- RECIPES -->
<section id="recipes" class="section">
  <div class="recipe-nav">
{recipe_tabs}
    <button class="recipe-tab sauce-tab" data-recipe="sauces">
      <span class="rt-day">Bases</span>
      <span class="rt-name">Sauces &amp; Brines</span>
    </button>
  </div>

{recipe_panels}

  <div id="recipe-sauces" class="recipe-panel">
    <div class="recipe-header">
      <div class="recipe-day-label" style="color:var(--sage)">Prep Day</div>
      <h2>Sauces, Brines &amp; Marinades</h2>
      <div class="recipe-sub">Make all of these on Sunday during prep</div>
    </div>
    <div class="sauce-grid">
{sauce_cards}
    </div>
  </div>
</section>

<!-- MY COOKBOOK -->
<section id="cookbook" class="section">
  <div class="section-header">
    <h2>My Cookbook</h2>
    <p>Recipes you&rsquo;ve loved &mdash; saved, starred, and logged across every week</p>
  </div>
  <div class="cb-toolbar">
    <input type="text" class="cb-search" id="cb-search" placeholder="Search by recipe name or week&hellip;" oninput="renderCookbook()">
    <div class="cb-filter-btns">
      <button class="cb-filter-btn active" id="cb-filter-all" onclick="setCbFilter('all')">All</button>
      <button class="cb-filter-btn" id="cb-filter-starred" onclick="setCbFilter('starred')">&#9733; Starred</button>
    </div>
    <button class="cb-new-btn" onclick="openLogModal(null, null)">+ Add Entry</button>
  </div>
  <div class="cb-grid" id="cb-grid">
    <div class="cb-empty"><p>No saved recipes yet.<br>Hit <strong>&ldquo;&#9825; Save to Cookbook&rdquo;</strong> on any recipe you love!</p></div>
  </div>
  <div class="cb-divider">Cook Log</div>
  <div class="cb-log-section">
    <div id="cb-log-list"><p style="color:var(--muted);font-size:14px">No log entries yet.</p></div>
  </div>
</section>

<!-- LOG ENTRY MODAL -->
<div class="cb-modal-overlay" id="cb-modal" style="display:none" onclick="if(event.target===this)closeLogModal()">
  <div class="cb-modal">
    <div class="cb-modal-hdr">
      <h3 id="cb-modal-title">Add Cook Log Entry</h3>
      <button class="cb-modal-close" onclick="closeLogModal()">&times;</button>
    </div>
    <div class="cb-modal-body">
      <div class="cb-type-toggle">
        <button class="cb-type-btn active" id="cb-type-linked" onclick="setCbType('linked')">Saved Recipe</button>
        <button class="cb-type-btn" id="cb-type-manual" onclick="setCbType('manual')">Manual Entry</button>
      </div>
      <div id="cb-linked-fields">
        <div class="cb-form-group">
          <label>Recipe</label>
          <select id="cb-recipe-select"><option value="">Choose a saved recipe&hellip;</option></select>
        </div>
      </div>
      <div id="cb-manual-fields" style="display:none">
        <div class="cb-form-group">
          <label>Recipe Name</label>
          <input type="text" id="cb-manual-title" placeholder="e.g. Grandma&rsquo;s Lemon Pasta">
        </div>
        <div class="cb-form-group">
          <label>Ingredients (optional)</label>
          <textarea id="cb-manual-ingr" placeholder="Paste or type ingredients&hellip;" style="min-height:100px"></textarea>
        </div>
        <div class="cb-form-group">
          <label>Instructions (optional)</label>
          <textarea id="cb-manual-inst" placeholder="Paste or type steps&hellip;" style="min-height:100px"></textarea>
        </div>
      </div>
      <div class="cb-form-group">
        <label>Date Made</label>
        <input type="date" id="cb-log-date">
      </div>
      <div class="cb-form-group">
        <label>Notes</label>
        <textarea id="cb-log-notes" placeholder="What worked, what you&rsquo;d change, substitutions&hellip;"></textarea>
      </div>
      <div class="cb-form-group">
        <label>Rating</label>
        <div class="cb-rating-row" id="cb-rating-row">
          <span class="cb-rating-star" data-v="1" onclick="setCbRating(1)">&#9733;</span>
          <span class="cb-rating-star" data-v="2" onclick="setCbRating(2)">&#9733;</span>
          <span class="cb-rating-star" data-v="3" onclick="setCbRating(3)">&#9733;</span>
          <span class="cb-rating-star" data-v="4" onclick="setCbRating(4)">&#9733;</span>
          <span class="cb-rating-star" data-v="5" onclick="setCbRating(5)">&#9733;</span>
        </div>
      </div>
      <div class="cb-modal-footer">
        <button class="cb-btn-cancel" onclick="closeLogModal()">Cancel</button>
        <button class="cb-btn-save" onclick="submitLogEntry()">Save Entry</button>
      </div>
    </div>
  </div>
</div>

<div class="cb-toast" id="cb-toast"></div>

<div class="last-updated">Last updated: {meta["date"]} &bull; <a href="#" onclick="location.reload()">Refresh</a></div>
<footer>Built from <a href="https://restaurantdropout.substack.com" target="_blank">Restaurant Dropout</a> {esc(week_label)} &middot; For personal use only</footer>

<script>
// ── WEEK DATA (regenerated each Friday) ──────────────────────────────────────
const WEEK_RECIPES = {week_recipes_json};

// ── STORAGE HELPERS ──────────────────────────────────────────────────────────
function getCookbook() {{ return JSON.parse(localStorage.getItem('rd-cookbook') || '[]'); }}
function putCookbook(d) {{ localStorage.setItem('rd-cookbook', JSON.stringify(d)); }}
function getCookLog()  {{ return JSON.parse(localStorage.getItem('rd-cooklog')  || '[]'); }}
function putCookLog(d) {{ localStorage.setItem('rd-cooklog',  JSON.stringify(d)); }}

// ── MISC UTILS ───────────────────────────────────────────────────────────────
function hx(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}
function uid() {{ return Date.now().toString(36) + Math.random().toString(36).slice(2); }}
function showToast(msg) {{
  const t = document.getElementById('cb-toast');
  t.textContent = msg; t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}}
function todayStr() {{ return new Date().toISOString().slice(0,10); }}

// ── SAVE RECIPE TO COOKBOOK ───────────────────────────────────────────────────
function saveRecipeToCookbook(key) {{
  const r = WEEK_RECIPES[key];
  if (!r) return;
  const book = getCookbook();
  if (book.find(x => x.id === r.id)) {{ showToast('Already in your cookbook!'); return; }}
  book.unshift({{ ...r, savedDate: todayStr(), starred: false }});
  putCookbook(book);
  const btn = document.getElementById('save-btn-' + key);
  if (btn) {{ btn.textContent = '✓ Saved'; btn.classList.add('saved'); btn.disabled = true; }}
  renderCookbook(); renderCookLog();
  showToast('Saved to your cookbook!');
}}

function toggleStar(id) {{
  const book = getCookbook();
  const r = book.find(x => x.id === id);
  if (r) {{ r.starred = !r.starred; putCookbook(book); renderCookbook(); }}
}}

function removeFromCookbook(id) {{
  if (!confirm('Remove this recipe from your cookbook?')) return;
  putCookbook(getCookbook().filter(x => x.id !== id));
  renderCookbook();
  showToast('Removed.');
  // Reset the Save button on the Recipes tab if this week's recipe was removed
  Object.entries(WEEK_RECIPES).forEach(([key, r]) => {{
    if (r.id === id) {{
      const btn = document.getElementById('save-btn-' + key);
      if (btn) {{ btn.textContent = '\u2665 Save to Cookbook'; btn.classList.remove('saved'); btn.disabled = false; }}
    }}
  }});
}}

function copyIngredients(id) {{
  const r = getCookbook().find(x => x.id === id);
  if (!r) return;
  const text = r.title + '\\n\\n' + r.ingredients.join('\\n');
  navigator.clipboard.writeText(text).then(() => showToast('Ingredients copied!'));
}}

// ── RENDER COOKBOOK GRID ──────────────────────────────────────────────────────
let cbFilter = 'all';
function setCbFilter(f) {{
  cbFilter = f;
  document.getElementById('cb-filter-all').classList.toggle('active', f === 'all');
  document.getElementById('cb-filter-starred').classList.toggle('active', f === 'starred');
  renderCookbook();
}}

function renderCookbook() {{
  const query = (document.getElementById('cb-search')?.value || '').toLowerCase();
  let book = getCookbook();
  if (cbFilter === 'starred') book = book.filter(r => r.starred);
  if (query) book = book.filter(r =>
    r.title.toLowerCase().includes(query) ||
    (r.weekLabel||'').toLowerCase().includes(query) ||
    (r.subtitle||'').toLowerCase().includes(query)
  );
  const grid = document.getElementById('cb-grid');
  if (!grid) return;
  if (book.length === 0) {{
    const msg = getCookbook().length === 0
      ? 'No saved recipes yet.<br>Hit <strong>&ldquo;&#9825; Save to Cookbook&rdquo;</strong> on any recipe you love!'
      : 'No recipes match your search.';
    grid.innerHTML = '<div class="cb-empty"><p>' + msg + '</p></div>';
    return;
  }}
  const log = getCookLog();
  grid.innerHTML = book.map(r => {{
    const entries = log.filter(e => e.recipeId === r.id).sort((a,b)=>b.date.localeCompare(a.date));
    const madeCount = entries.length;
    const lastMade  = entries[0]?.date || null;
    const ingHtml   = r.ingredients.map(i => '<li>' + hx(i) + '</li>').join('');
    const instHtml  = r.instructions.map(s => '<li>' + hx(s) + '</li>').join('');
    const logHtml   = entries.length === 0 ? '' : `
      <div class="cb-log-inline">
        <div class="cb-log-inline-hdr">
          <h4>Cook Log</h4>
        </div>
        ${{entries.map(e => `
          <div class="cb-log-item">
            <div class="cb-log-item-hdr">
              <span class="cb-log-date">${{e.date}}</span>
              ${{e.rating ? '<span class="cb-log-rating">' + '&#9733;'.repeat(e.rating) + '</span>' : ''}}
              <button class="cb-log-del" onclick="deleteLogEntry('${{e.id}}')" title="Delete">&#10005;</button>
            </div>
            ${{e.notes ? '<p class="cb-log-notes">' + hx(e.notes) + '</p>' : ''}}
          </div>`).join('')}}
      </div>`;
    return `
    <div class="cb-card${{r.starred ? ' cb-starred' : ''}}" id="cbcard-${{r.id}}">
      <div class="cb-card-top">
        <div style="display:flex;justify-content:space-between;align-items:flex-start">
          <div class="cb-card-meta">
            <span class="cb-week-badge">${{hx(r.weekLabel)}}</span>
            ${{madeCount > 0 ? '<span class="cb-made-badge">Made ' + madeCount + '&times;</span>' : ''}}
            ${{lastMade ? '<span class="cb-last-made">Last: ' + lastMade + '</span>' : ''}}
          </div>
          <div class="cb-card-btns">
            <button class="cb-star${{r.starred ? ' on' : ''}}" onclick="toggleStar('${{r.id}}')" title="${{r.starred ? 'Unstar' : 'Star'}}">&#9733;</button>
            <button class="cb-log-btn-sm" onclick="openLogModal('${{r.id}}', '${{hx(r.title)}}')">+ Log</button>
            <button class="cb-remove" onclick="removeFromCookbook('${{r.id}}')" title="Remove">&#10005;</button>
          </div>
        </div>
      </div>
      <h3 class="cb-card-title">${{hx(r.title)}}</h3>
      ${{r.subtitle ? '<div class="cb-card-sub">' + hx(r.subtitle) + '</div>' : ''}}
      <details class="cb-details">
        <summary>View full recipe</summary>
        <div class="cb-recipe-body">
          <div>
            <h4>Ingredients <button class="cb-copy-btn" onclick="copyIngredients('${{r.id}}')">Copy</button></h4>
            <ul>${{ingHtml}}</ul>
          </div>
          <div>
            <h4>Instructions</h4>
            <ol>${{instHtml}}</ol>
          </div>
        </div>
      </details>
      ${{r.postUrl ? '<a class="cb-substack-link" href="' + hx(r.postUrl) + '" target="_blank">View original on Substack &rarr;</a>' : ''}}
      ${{logHtml}}
    </div>`;
  }}).join('');
}}

// ── RENDER COOK LOG (standalone) ──────────────────────────────────────────────
function renderCookLog() {{
  const all = getCookLog().sort((a,b) => b.date.localeCompare(a.date));
  const book = getCookbook();
  const el = document.getElementById('cb-log-list');
  if (!el) return;
  if (all.length === 0) {{
    el.innerHTML = '<p style="color:var(--muted);font-size:14px">No log entries yet. Use &ldquo;+ Add Entry&rdquo; to start your cook journal.</p>';
    return;
  }}
  el.innerHTML = all.map(e => {{
    const isManual = e.type === 'manual';
    const recipe   = e.recipeId ? book.find(r => r.id === e.recipeId) : null;
    const subtitle = recipe?.subtitle || '';
    const ingr     = e.ingredients || (recipe?.ingredients || []).join('\\n');
    const inst     = e.instructions || (recipe?.instructions || []).join('\\n');
    return `
    <div class="cb-log-entry${{isManual ? ' manual' : ''}}">
      <div class="cb-log-entry-hdr">
        <strong class="cb-entry-title">${{hx(e.title)}}</strong>
        ${{isManual ? '<span class="cb-manual-tag">Manual</span>' : ''}}
        <span class="cb-log-date" style="margin-left:auto">${{e.date}}</span>
        ${{e.rating ? '<span class="cb-log-rating">' + '&#9733;'.repeat(e.rating) + '</span>' : ''}}
        <button class="cb-log-del" onclick="deleteLogEntry('${{e.id}}')" title="Delete">&#10005;</button>
      </div>
      ${{subtitle ? '<div style="font-size:13px;color:var(--muted);font-style:italic;margin-bottom:6px">' + hx(subtitle) + '</div>' : ''}}
      ${{e.notes ? '<p class="cb-log-entry-notes">' + hx(e.notes) + '</p>' : ''}}
      ${{ingr ? '<details style="margin-top:8px"><summary style="font-size:12px;cursor:pointer;color:var(--muted)">Ingredients</summary><pre class="cb-manual-text">' + hx(ingr) + '</pre></details>' : ''}}
      ${{inst ? '<details style="margin-top:4px"><summary style="font-size:12px;cursor:pointer;color:var(--muted)">Instructions</summary><pre class="cb-manual-text">' + hx(inst) + '</pre></details>' : ''}}
    </div>`;
  }}).join('');
}}

function deleteLogEntry(id) {{
  if (!confirm('Delete this log entry?')) return;
  putCookLog(getCookLog().filter(e => e.id !== id));
  renderCookbook(); renderCookLog();
  showToast('Entry deleted.');
}}

// ── LOG MODAL ─────────────────────────────────────────────────────────────────
let _cbRating = 0;
let _cbType   = 'linked';
let _cbPreset = null; // pre-set recipeId when opened from a card

function openLogModal(recipeId, recipeTitle) {{
  _cbPreset = recipeId;
  _cbRating = 0;
  setCbType(recipeId ? 'linked' : 'manual');
  document.getElementById('cb-log-date').value  = todayStr();
  document.getElementById('cb-log-notes').value = '';
  document.getElementById('cb-manual-title').value = '';
  document.getElementById('cb-manual-ingr').value  = '';
  document.getElementById('cb-manual-inst').value  = '';
  document.querySelectorAll('.cb-rating-star').forEach(s => s.classList.remove('lit'));
  // Populate recipe dropdown
  const sel = document.getElementById('cb-recipe-select');
  const book = getCookbook();
  sel.innerHTML = '<option value="">Choose a saved recipe&hellip;</option>' +
    book.map(r => '<option value="' + hx(r.id) + '">' + hx(r.title) + ' &mdash; ' + hx(r.weekLabel) + '</option>').join('');
  if (recipeId) sel.value = recipeId;
  document.getElementById('cb-modal').style.display = 'flex';
}}

function closeLogModal() {{
  document.getElementById('cb-modal').style.display = 'none';
}}

function setCbType(t) {{
  _cbType = t;
  document.getElementById('cb-type-linked').classList.toggle('active', t === 'linked');
  document.getElementById('cb-type-manual').classList.toggle('active', t === 'manual');
  document.getElementById('cb-linked-fields').style.display = t === 'linked' ? '' : 'none';
  document.getElementById('cb-manual-fields').style.display = t === 'manual' ? '' : 'none';
}}

function setCbRating(v) {{
  _cbRating = v;
  document.querySelectorAll('.cb-rating-star').forEach(s => {{
    s.classList.toggle('lit', parseInt(s.dataset.v) <= v);
  }});
}}

function submitLogEntry() {{
  const date  = document.getElementById('cb-log-date').value || todayStr();
  const notes = document.getElementById('cb-log-notes').value.trim();
  let entry = {{ id: uid(), date, notes, rating: _cbRating, type: _cbType }};

  if (_cbType === 'linked') {{
    const sel = document.getElementById('cb-recipe-select');
    const id  = sel.value;
    if (!id) {{ alert('Please select a recipe.'); return; }}
    const r = getCookbook().find(x => x.id === id);
    entry.recipeId = id;
    entry.title    = r ? r.title : sel.options[sel.selectedIndex].text;
  }} else {{
    const title = document.getElementById('cb-manual-title').value.trim();
    if (!title) {{ alert('Please enter a recipe name.'); return; }}
    entry.title        = title;
    entry.ingredients  = document.getElementById('cb-manual-ingr').value.trim();
    entry.instructions = document.getElementById('cb-manual-inst').value.trim();
  }}

  const log = getCookLog();
  log.unshift(entry);
  putCookLog(log);
  closeLogModal();
  renderCookbook(); renderCookLog();
  showToast('Entry saved!');
}}

// ── NAV / RECIPE TABS / GROCERY FILTER ───────────────────────────────────────
document.querySelectorAll('.nav-link').forEach(l => {{
  l.addEventListener('click', e => {{
    e.preventDefault();
    document.querySelectorAll('.nav-link').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.section').forEach(x => x.classList.remove('active'));
    l.classList.add('active');
    document.getElementById(l.dataset.target).classList.add('active');
    window.scrollTo(0,0);
  }});
}});

document.querySelectorAll('.recipe-tab').forEach(tab => {{
  tab.addEventListener('click', () => {{
    document.querySelectorAll('.recipe-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.recipe-panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('recipe-' + tab.dataset.recipe).classList.add('active');
  }});
}});

function openRecipe(day) {{
  document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelector('[data-target="recipes"]').classList.add('active');
  document.getElementById('recipes').classList.add('active');
  document.querySelectorAll('.recipe-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.recipe-panel').forEach(p => p.classList.remove('active'));
  document.querySelector('[data-recipe="' + day + '"]').classList.add('active');
  document.getElementById('recipe-' + day).classList.add('active');
  window.scrollTo(0,0);
}}

function getSelectedDays() {{
  const sel = [];
  document.querySelectorAll('.filter-toggle input[type=checkbox]').forEach(cb => {{
    if (cb.checked) sel.push(parseInt(cb.dataset.filter));
  }});
  return sel;
}}

function applyFilter() {{
  const sel = getSelectedDays();
  document.querySelectorAll('.grocery-item').forEach(item => {{
    const days = (item.dataset.days || '').split(',').map(Number);
    item.classList.toggle('hidden', sel.length > 0 && !days.some(d => sel.includes(d)));
  }});
  document.querySelectorAll('.grocery-category').forEach(cat => {{
    const allHidden = Array.from(cat.querySelectorAll('.grocery-item')).every(i => i.classList.contains('hidden'));
    cat.style.display = allHidden ? 'none' : '';
  }});
}}

document.querySelectorAll('.filter-toggle').forEach(toggle => {{
  toggle.addEventListener('click', () => {{
    const cb = toggle.querySelector('input[type=checkbox]');
    cb.checked = !cb.checked;
    toggle.classList.toggle('selected', cb.checked);
    applyFilter();
  }});
}});

function toggleAllFilters() {{
  const cbs = document.querySelectorAll('.filter-toggle input[type=checkbox]');
  const any = Array.from(cbs).some(c => c.checked);
  cbs.forEach(cb => {{ cb.checked = !any; cb.closest('.filter-toggle').classList.toggle('selected', !any); }});
  applyFilter();
}}

// ── CHECKBOX PERSISTENCE ──────────────────────────────────────────────────────
document.querySelectorAll('input[type=checkbox][id]').forEach(cb => {{
  const lbl = document.querySelector('label[for="' + cb.id + '"]');
  const saved = localStorage.getItem('cb-' + cb.id);
  if (saved === 'true') {{ cb.checked = true; if (lbl) lbl.classList.add('checked'); }}
  cb.addEventListener('change', () => {{
    if (lbl) lbl.classList.toggle('checked', cb.checked);
    localStorage.setItem('cb-' + cb.id, cb.checked);
  }});
}});

// ── MARK ALREADY-SAVED RECIPES ────────────────────────────────────────────────
(function markSaved() {{
  const ids = new Set(getCookbook().map(r => r.id));
  Object.entries(WEEK_RECIPES).forEach(([key, r]) => {{
    if (ids.has(r.id)) {{
      const btn = document.getElementById('save-btn-' + key);
      if (btn) {{ btn.textContent = '✓ Saved'; btn.classList.add('saved'); btn.disabled = true; }}
    }}
  }});
}})();

// ── INIT ──────────────────────────────────────────────────────────────────────
renderCookbook();
renderCookLog();
</script>
</body>
</html>"""

# ─── NOTIFICATIONS ────────────────────────────────────────────────────────────

def notify(message):
    try:
        subprocess.run(["osascript", "-e",
            f'display notification "{message}" with title "Restaurant Dropout"'], check=False)
    except Exception:
        pass

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("🍽  Restaurant Dropout — Weekly Page Generator")
    config  = load_config()
    cookie  = config["cookie"]
    base    = config.get("substack_url", "https://restaurantdropout.substack.com").rstrip("/")
    out     = SCRIPT_DIR / config.get("output_path", "index.html")

    CACHE_DIR.mkdir(exist_ok=True)

    print("  Fetching latest post…")
    post = fetch_latest_post(base, cookie)
    meta = extract_post_meta(post)
    print(f"  → {meta['title']} ({meta['date']}, Week {meta['week_num']})")

    # Download PDFs
    print("  Downloading PDFs…")
    pdf_paths = {}
    for slot, url in meta["pdfs"].items():
        dest = CACHE_DIR / f"{slot}.pdf"
        try:
            download_file(url, dest, cookie)
            pdf_paths[slot] = dest
            print(f"    ✓ {slot}")
        except Exception as e:
            print(f"    ✗ {slot}: {e}")

    # Parse content
    print("  Parsing content…")
    grocery_items = parse_grocery(pdf_paths.get("grocery_list"))
    prep_blocks   = parse_prep(pdf_paths.get("prep_list"))
    recipes = [parse_recipe(pdf_paths.get(f"day{i}")) for i in range(1, 6)]
    sauces  = list(parse_sauces(pdf_paths.get("consolidated_recipes")))
    print(f"    {len(grocery_items)} grocery items, {len(prep_blocks)} prep blocks, "
          f"{sum(1 for r in recipes if r)} recipes, {len(sauces)} sauces")

    # Generate HTML
    print("  Generating index.html…")
    html = build_html(meta, grocery_items, prep_blocks, recipes, sauces)
    out.write_text(html, encoding="utf-8")
    print(f"  ✓ Saved to {out}")

    notify(f"Meal prep page updated — {meta['title']}")
    print("  Done! Open index.html in your browser.")

    # Deploy to GitHub Pages
    print("  Deploying to GitHub Pages…")
    try:
        subprocess.run(["git", "add", "index.html"], cwd=SCRIPT_DIR, check=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=SCRIPT_DIR)
        if result.returncode != 0:  # there are staged changes
            subprocess.run(
                ["git", "commit", "-m", f"Meal prep {meta['date']}: {meta['title'][:60]}"],
                cwd=SCRIPT_DIR, check=True
            )
            token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
            env = {**os.environ, "GIT_ASKPASS": "echo", "GIT_USERNAME": "nilamtrivedi-create", "GIT_PASSWORD": token}
            subprocess.run(["git", "push"], cwd=SCRIPT_DIR, check=True, env=env)
            notify("Meal prep site live on GitHub Pages!")
            print("  ✓ Deployed — site is live.")
        else:
            print("  ✓ No changes to deploy.")
    except subprocess.CalledProcessError as e:
        print(f"  ⚠ Deploy failed: {e}")

if __name__ == "__main__":
    main()
