#!/usr/bin/env python3
"""サイトの決まりを確かめる（ビルドではない。変更のあとに実行して、問題がないか確かめるだけ）

使い方:
  python3 tools/check_site.py                       # 問題があれば一覧を出して終了コード 1
  python3 tools/check_site.py --allow-placeholders  # ひな形そのものを確かめるとき（【要入力】【要確認】・仮画像を「参考」扱いにする）

- 標準ライブラリだけで動く。
- 会社ごとの値（会社名・本番URL・リポジトリ名・お問い合わせページなど）は tools/site_config.json に書く。
- グローバルナビの親子関係（どのページがどのページの子か）は index.html のナビから読み取る。
- 「OK」は、ここで確かめている項目についてだけ。新しい種類の変更をしたら、確認が足りているかも見直す。
"""
import argparse
import base64
import hashlib
import html
import json
import os
import posixpath
import re
import sys
import unicodedata
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(ROOT, "tools", "site_config.json")
MARK = "【要入力"  # ひな形の「ここを埋める」印
MARKS = ("【要入力", "【要確認")  # 埋めていない印（ひな形の【要入力】と、文章の案の【要確認】）


def count_marks(text):
    return sum(text.count(m) for m in MARKS)

ap = argparse.ArgumentParser(description="サイトの決まりを確かめる")
ap.add_argument("--allow-placeholders", action="store_true",
                help="【要入力】の印・仮画像・ひな形のままの設定値を、問題ではなく参考として表示する（ひな形そのものの確認用）")
args = ap.parse_args()

problems = []  # 直すまでコミットしないもの（終了コード 1）
notes = []     # 参考（人が判断するもの。終了コードには影響しない）


def problem(msg, placeholder=False):
    (notes if placeholder and args.allow_placeholders else problems).append(msg)


# ---------- 設定 ----------
try:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        CONF = json.load(f)
except FileNotFoundError:
    print("tools/site_config.json がない（会社名・本番URLなどの設定。ひな形からコピーして埋める）")
    sys.exit(1)
except json.JSONDecodeError as e:
    print(f"tools/site_config.json の書き方が正しくない（JSON）: {e}")
    sys.exit(1)

SITE_NAME = CONF.get("site_name", "")
SITE = CONF.get("site_url", "")
if not SITE.endswith("/"):
    SITE += "/"
if not re.fullmatch(r"https://[a-z0-9.-]+/", SITE):
    print(f"tools/site_config.json の site_url は本番のトップページの URL（https://…/）にする。いま: {SITE}")
    sys.exit(1)
HOST = urllib.parse.urlsplit(SITE).hostname
PROD_ORIGIN = SITE.rstrip("/")
BARE_HOST = HOST[4:] if HOST.startswith("www.") else HOST
SELF_URL_RE = re.compile(r"""\b(?:href|src)=["'](https?://(?:www\.)?""" + re.escape(BARE_HOST) + r"""(?:[/:?#][^"']*)?)["']""")
TITLE_SUFFIX = CONF["title_suffix"] if "title_suffix" in CONF else " - " + SITE_NAME
CONTACT_PAGE = CONF.get("contact_page", "contact/index.html")
SITEMAP_PAGE = CONF.get("sitemap_page", "sitemap/index.html")
NO_CURRENT = set(CONF.get("no_current", ["sitemap/", "privacy/", "404.html"]))
SITEMAP_XML_EXCLUDE = set(CONF.get("sitemap_xml_exclude", ["sitemap/", "privacy/", "404.html"]))
ALLOWED_IFRAMES = CONF.get("allowed_iframes", ["https://maps.google.com/maps?", "https://www.google.com/maps/embed"])
OLD_SERVICE = [m.lower().encode() for m in CONF.get("old_service_markers", []) if m]
# アクセス解析の計測画像（Cookie を使わず、画像を1つ読み込ませて数える型）の読み込み元。使わないなら空文字
ANALYTICS = CONF.get("analytics_pixel", "").rstrip("/")
if ANALYTICS and not re.fullmatch(r"https://[a-z0-9.-]+(?::\d+)?", ANALYTICS):
    print(f"tools/site_config.json の analytics_pixel は計測のサービスの読み込み元（https://ホスト名）だけを書く。いま: {ANALYTICS}")
    sys.exit(1)

if count_marks(SITE_NAME) or not SITE_NAME:
    problem("tools/site_config.json の site_name（会社名）を埋める", placeholder=True)
if re.search(r"(^|\.)example\.(com|jp|co\.jp)$", HOST):
    problem(f"tools/site_config.json の site_url がひな形のまま（{SITE}）", placeholder=True)

# ---------- セキュリティの指定（CSP） ----------
# 全ページの <head> に同じものを1つ置く（基準は index.html）
CSP_RE = re.compile(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">')
# 決まった形（ゆるめない）。変えてよいのは frame-src と form-action（地図・フォームを入れるとき）と、
# script-src のハッシュ（ページ内スクリプトの中身を変えたとき）だけ
CSP_FIXED = {"default-src": ["'none'"], "style-src": ["'self'"], "img-src": ["'self'", "data:"], "base-uri": ["'self'"]}
CSP_DIRECTIVES = set(CSP_FIXED) | {"script-src", "frame-src", "form-action"}
ORIGIN_RE = re.compile(r"https://[a-z0-9.-]+(?::\d+)?(?:/[^\s;,']*)?")
HASH_RE = re.compile(r"'sha256-[A-Za-z0-9+/]{43}='")

# 共通部分（全ページで同じ内容にする部分）の始まりと終わり
ALL_SHARED_BLOCKS = {
    "header": ("ヘッダー・グローバルナビ", "<!-- ===== 共通ヘッダー", "<!-- ===== 共通ここまで ===== -->"),
    "contact": ("お問い合わせ帯", "<!-- ===== 共通お問い合わせ帯", "</aside>"),
    "footer": ("フッター", "<!-- ===== 共通フッター", "</footer>"),
}
# 1ページ型はお問い合わせを本文のセクションに置くので、お問い合わせ帯を使わない（site_config.json の shared_blocks）
SHARED_BLOCKS = {ALL_SHARED_BLOCKS[k][0]: ALL_SHARED_BLOCKS[k][1:] for k in CONF.get("shared_blocks", ["header", "contact", "footer"]) if k in ALL_SHARED_BLOCKS}
if "ヘッダー・グローバルナビ" not in SHARED_BLOCKS:
    print("tools/site_config.json の shared_blocks には header を必ず入れる")
    sys.exit(1)


def parse_csp(csp):
    """CSP を {指定の種類: [値, …]} にする（同じ種類が2回あるときは None を入れて知らせる）"""
    out = {}
    for part in csp.split(";"):
        words = part.split()
        if words:
            out[words[0]] = None if words[0] in out else words[1:]
    return out


def csp_sources(csp, directive):
    return parse_csp(csp).get(directive) or []


def origin_of(url):
    m = re.match(r"https?://[^/?#]+", url)
    return m.group(0) if m else ""


def frame_allowed(url):
    """iframe の URL が CSP の frame-src に載っている読み込み元に当たるか（読み込み元だけ、または読み込み元＋パスの前方一致）"""
    for entry in FRAME_SRC:
        o = origin_of(entry)
        if entry == o and origin_of(url) == o:
            return True
        if entry != o and url.startswith(entry):
            return True
    return False


def script_hash(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode() + "'"


def rel(p):
    return os.path.relpath(p, ROOT).replace(os.sep, "/")


def read(r):
    with open(os.path.join(ROOT, r), encoding="utf-8") as f:
        return f.read()


def walk(skip_tools=True):
    for d, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "_work" and not (skip_tools and x in ("tools", "node_modules")))
        for f in sorted(files):
            yield os.path.join(d, f)


def pages():
    return [p for p in walk() if p.endswith(".html")]


def between(s, start, end):
    i = s.find(start)
    j = s.find(end, i + len(start)) if i >= 0 else -1
    return s[i:j] if i >= 0 and j >= 0 else ""


def normalize_shared(block, prefix):
    """相対パスの深さと「現在のページ」の印を取り除き、全ページで比べられる形にする"""
    if prefix:
        block = block.replace('="' + prefix, '="')
    block = re.sub(r' aria-current="page"', "", block)
    block = re.sub(r"\s?\bis-(?:current|parent)\b", "", block)
    block = re.sub(r' class=""', "", block)
    block = block.replace('href="./"', 'href=""')
    return block


def page_path(r):
    """ファイル（例 company/access/index.html）→ サイト内のパス（例 company/access/。ホームは ""）"""
    return r if r == "404.html" else r[: -len("index.html")]


def site_path(r, href):
    """ページ r に書かれたリンク href → サイト内のパス（ホームは ""）。404.html は <base href="/"> なのでルートから"""
    base = "" if r == "404.html" else posixpath.dirname(r)
    p = posixpath.normpath(posixpath.join(base, urllib.parse.unquote(html.unescape(href))))
    return "" if p == "." else p + "/"


def names(paths):
    return "、".join(p or "ホーム" for p in paths) or "なし"


ALL_PAGES = pages()
if not os.path.exists(os.path.join(ROOT, "index.html")):
    print("index.html がない（サイトのルートで実行しているか確かめる）")
    sys.exit(1)

# グローバルナビの親子関係：{親のパス: [子のパス, …]}（index.html のナビの並び順）
nav = between(read("index.html"), '<ul class="global-nav__list">', "<!-- ===== 共通ここまで")
# ページの中へ移るリンク（1ページ型の「#services」など）は、ページの親子関係・現在位置・サイトマップの対象にしない
GROUPS = {}
NAV_ITEMS = re.findall(r'<li class="global-nav__item[^"]*"><a href="([^"]*)"(.*?)(?=<li class="global-nav__item|\Z)', nav, re.S)
for href, rest in NAV_ITEMS:
    if "#" in href:
        continue
    GROUPS[site_path("index.html", href)] = [site_path("index.html", h) for h in re.findall(r'<a href="([^"]*)"', rest) if "#" not in h]
PARENT = {c: p for p, cs in GROUPS.items() for c in cs}
NAV_ORDER = [x for p, cs in GROUPS.items() for x in [p] + cs]
if not NAV_ITEMS:
    problems.append("index.html のグローバルナビ（<ul class=\"global-nav__list\">）が読み取れない")


def has_id(text, frag):
    return re.search(r'''\sid=["']''' + re.escape(urllib.parse.unquote(frag)) + r'''["']''', text) is not None

css = read("assets/css/style.css") if os.path.exists(os.path.join(ROOT, "assets/css/style.css")) else ""
js = read("assets/js/main.js") if os.path.exists(os.path.join(ROOT, "assets/js/main.js")) else ""
if not css:
    problems.append("assets/css/style.css がない（CSS は1ファイル）")
if not js:
    problems.append("assets/js/main.js がない（JS は1ファイル）")

# ---------- 1. ファイル：NFC・画像のファイル名・EXIF・旧サービスへの参照・必須ファイル ----------
IMG_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\.(?:jpg|png|svg|webp|gif|ico)")


def jpeg_meta(data):
    """JPEG の中の EXIF・XMP（撮影日時・撮影場所などが入る）の有無"""
    found, i = [], 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker in (0xD9, 0xDA):  # 画像の終わり・画素データの始まり
            break
        length = int.from_bytes(data[i + 2:i + 4], "big")
        seg = data[i + 4:i + 2 + length]
        if marker == 0xE1 and seg.startswith(b"Exif\0"):
            found.append("EXIF")
        if marker == 0xE1 and seg.startswith(b"http://ns.adobe.com/xap/1.0/"):
            found.append("XMP")
        i += 2 + length
    return found


def png_meta(data):
    found, i = [], 8
    while i + 8 <= len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        kind = data[i + 4:i + 8]
        if kind == b"eXIf":
            found.append("EXIF")
        if kind == b"iTXt" and data[i + 8:i + 8 + length].startswith(b"XML:com.adobe.xmp"):
            found.append("XMP")
        if kind == b"IEND":
            break
        i += 12 + length
    return found


for d, dirs, files in os.walk(ROOT):
    dirs[:] = [x for x in dirs if x not in (".git", "_work", "node_modules")]
    for name in dirs + files:
        path = os.path.join(d, name)
        if not unicodedata.is_normalized("NFC", name):
            problems.append(f"ファイル名が NFC ではない（macOS で作ると NFD になることがある）: {rel(path)}")
for path in walk(skip_tools=True):
    r = rel(path)
    with open(path, "rb") as f:
        data = f.read()
    if OLD_SERVICE and any(h in data.lower() for h in OLD_SERVICE):
        problems.append(f"旧サイトのサービス（画像配信など）への参照がある: {r}")
    if r.startswith("assets/img/"):
        if not IMG_NAME_RE.fullmatch(os.path.basename(r)):
            problems.append(f"画像のファイル名は半角英小文字・数字・ハイフンにする: {r}")
        meta = jpeg_meta(data) if data[:2] == b"\xff\xd8" else png_meta(data) if data[:8] == b"\x89PNG\r\n\x1a\n" else []
        if meta:
            problems.append(f"画像に撮影情報など（{'・'.join(sorted(set(meta)))}）が残っている。消してから置く: {r}")
        if len(data) > 1_000_000:
            notes.append(f"画像が大きい（{len(data) // 1024:,} KB）。表示サイズの約2倍まで縮小・圧縮できるか確かめる: {r}")
for need in (".nojekyll", "robots.txt", "sitemap.xml", "404.html"):
    if not os.path.exists(os.path.join(ROOT, need)):
        problems.append(f"{need} がない（消さない）")
if os.path.exists(os.path.join(ROOT, "CNAME")):
    cname = read("CNAME").strip()
    notes.append(f"CNAME あり（独自ドメイン {cname}）。DNS・ドメインはお客様・運営担当の指示どおりか確かめる")
    if cname != HOST:
        problems.append(f"CNAME（{cname}）と site_config.json の site_url（{HOST}）が違う")

# ---------- 2. CSP の基準（index.html） ----------
_csp_index = CSP_RE.findall(read("index.html"))
CSP = _csp_index[0] if len(_csp_index) == 1 else ""
if not CSP:
    problems.append('index.html: セキュリティの指定 <meta http-equiv="Content-Security-Policy" …> が1つではない')
else:
    pol = parse_csp(CSP)
    if set(pol) != CSP_DIRECTIVES or None in pol.values():
        problems.append(f"index.html: CSP の種類は {', '.join(sorted(CSP_DIRECTIVES))} を1つずつ"
                        f"（足りない: {', '.join(sorted(CSP_DIRECTIVES - set(pol))) or 'なし'}／"
                        f"余分・重複: {', '.join(sorted(k for k in pol if k not in CSP_DIRECTIVES or pol[k] is None)) or 'なし'}）")
    # 独自ドメインに切り替えた後は、Google 翻訳（translate.goog）経由でも崩れないよう、
    # 本番の読み込み元を script-src・style-src・img-src に足してよい
    for k in ("script-src", "style-src", "img-src"):
        if pol.get(k) and PROD_ORIGIN in pol[k]:
            pol[k] = [v for v in pol[k] if v != PROD_ORIGIN]
    # アクセス解析の計測画像の読み込み元は img-src に足してよい（site_config.json の analytics_pixel に書いたものだけ）
    if ANALYTICS:
        if pol.get("img-src") and ANALYTICS in pol["img-src"]:
            pol["img-src"] = [v for v in pol["img-src"] if v != ANALYTICS]
        else:
            problems.append(f"index.html: アクセス解析の計測画像が止められる。全ページの CSP の img-src に {ANALYTICS} を足す")
    for k, want in CSP_FIXED.items():
        if pol.get(k) is not None and sorted(pol[k]) != sorted(want):
            problems.append(f"index.html: CSP の {k} は {' '.join(want)} のまま（ゆるめない）。いま: {' '.join(pol[k])}")
    ss = pol.get("script-src") or []
    if not ss or ss[0] != "'self'" or not all(HASH_RE.fullmatch(v) for v in ss[1:]):
        problems.append("index.html: CSP の script-src は 'self' とページ内スクリプトのハッシュ（'sha256-…'）だけ"
                        f"（'unsafe-inline'・外部のURL・* は使わない）。いま: {' '.join(ss)}")
    fs = pol.get("frame-src") or []
    if fs != ["'none'"] and not all(ORIGIN_RE.fullmatch(v) for v in fs):
        problems.append(f"index.html: CSP の frame-src は 'none'（埋め込みなし）か、https://… の読み込み元だけ（*・https: だけ・'self' は使わない）。いま: {' '.join(fs)}")
    fa = pol.get("form-action") or []
    if fa != ["'none'"] and not (fa and all(v == "'self'" or ORIGIN_RE.fullmatch(v) for v in fa) and any(ORIGIN_RE.fullmatch(v) for v in fa)):
        problems.append(f"index.html: CSP の form-action は 'none'（フォームなし）か、'self' とフォームの送信先 https://… だけ。いま: {' '.join(fa)}")
FRAME_SRC = [u for u in csp_sources(CSP, "frame-src") if ORIGIN_RE.fullmatch(u)]
inline_hashes = set()

# ---------- 3. ページごとの確認 ----------
shared = {}
REDIRECTS = set()      # 転送用のページ（sitemap.xml に載せない）
placeholder_marks = {}  # ファイル → 【要入力】の数
placeholder_imgs = {}   # ファイル → 仮画像（placeholder-…）を使っている数
for page in ALL_PAGES:
    s = open(page, encoding="utf-8").read()
    r = rel(page)
    is404 = r == "404.html"
    depth = 0 if is404 or r == "index.html" else r.count("/")
    prefix = "../" * depth
    me = page_path(r)
    head = s[: s.find("<body")] if "<body" in s else s
    body = s[s.find("<body"):] if "<body" in s else ""

    if count_marks(s):
        placeholder_marks[r] = count_marks(s)

    # 転送用のページ（構成の見直しで URL を変えたとき、古い URL に置く小さなページ）。決まった形だけを確かめる
    refresh = re.search(r"""<meta http-equiv="refresh" content="0; url=([^"]+)">""", s)
    if refresh or 'http-equiv="refresh"' in s:
        REDIRECTS.add(me)
        if not refresh:
            problems.append(f"{r}: 転送用のページの指定は <meta http-equiv=\"refresh\" content=\"0; url=新しいページへの相対パス\"> の形にする")
            continue
        dest = html.unescape(refresh.group(1))
        dest_path = os.path.normpath(os.path.join(os.path.dirname(page), urllib.parse.unquote(dest.split("#")[0])))
        if dest.split("#")[0].endswith("/"):
            dest_path = os.path.join(dest_path, "index.html")
        if dest.startswith(("/", "http:", "https:")) or not os.path.exists(dest_path):
            problems.append(f"{r}: 転送先がサイトの中に無い（相対パスで、あるページを指す）: {dest}")
            continue
        dest_me = page_path(rel(dest_path))
        if CSP_RE.findall(s) != [CSP]:
            problems.append(f"{r}: 転送用のページにも、index.html と同じセキュリティの指定（CSP）を置く")
        if re.findall(r'<link rel="canonical" href="([^"]*)"', s) != [SITE + urllib.parse.quote(dest_me)] and \
                [urllib.parse.unquote(u) for u in re.findall(r'<link rel="canonical" href="([^"]*)"', s)] != [SITE + dest_me]:
            problems.append(f"{r}: 転送用のページの canonical は転送先の本番URL（{SITE + dest_me}）にする")
        if '<meta name="robots" content="noindex">' not in s:
            problems.append(f"{r}: 転送用のページには <meta name=\"robots\" content=\"noindex\"> を付ける")
        if not re.search(r'<a href="' + re.escape(dest) + r'"', s):
            problems.append(f"{r}: 自動で移らないときのために、転送先へのリンク <a href=\"{dest}\"> を本文に置く")
        if re.search(r"<script\b", s):
            problems.append(f"{r}: 転送用のページにスクリプトを書かない（meta refresh で転送する）")
        continue

    # 必須要素。canonical と og:url はこのページ自身のURL（ページをコピーして作ったときの直し忘れ）
    for need in ('<html lang="ja"', "<title>", '<meta charset="utf-8">', 'name="viewport"', "assets/css/style.css", "assets/js/main.js"):
        if need not in s:
            problems.append(f"{r}: {need} がない")
    title = re.search(r"<title>(.*?)</title>", s, re.S)
    if title and TITLE_SUFFIX and not html.unescape(title.group(1)).endswith(TITLE_SUFFIX):
        problems.append(f"{r}: <title> は「ページ名{TITLE_SUFFIX}」の形にする（いま: {html.unescape(title.group(1))}）")
    og_site = re.findall(r'<meta property="og:site_name" content="([^"]*)"', s)
    if og_site and og_site != [SITE_NAME]:
        problems.append(f"{r}: og:site_name は site_config.json の site_name（{SITE_NAME}）と同じにする（いま: {og_site[0]}）")
    if not is404:
        canon = [urllib.parse.unquote(u) for u in re.findall(r'<link rel="canonical" href="([^"]*)"', s)]
        og = [urllib.parse.unquote(u) for u in re.findall(r'<meta property="og:url" content="([^"]*)"', s)]
        if canon != [SITE + me] or og != [SITE + me]:
            problems.append(f"{r}: canonical と og:url をこのページのURL（{SITE + me}）にする: canonical={canon} og:url={og}")
    for img in re.findall(r'<meta property="og:image" content="([^"]*)"', s):
        if not img.startswith(SITE):
            problems.append(f"{r}: og:image は本番の絶対URL（{SITE}…）にして、サイトの中の画像を使う: {img}")
        elif not os.path.exists(os.path.join(ROOT, urllib.parse.unquote(img[len(SITE):]))):
            problems.append(f"{r}: og:image の画像がサイトの中にない: {img}")
        elif os.path.basename(img).startswith("placeholder-"):
            placeholder_imgs[r] = placeholder_imgs.get(r, 0) + 1

    # セキュリティの指定（CSP）：全ページ同じものを、<base> とスクリプトより前に1つ
    csps = CSP_RE.findall(s)
    if len(csps) != 1 or csps[0] != CSP:
        problems.append(f"{r}: セキュリティの指定（CSP）の <meta> を index.html と同じものにして1つだけ置く")
    elif re.search(r"<(script|base)\b", s[: s.find("Content-Security-Policy")]):
        problems.append(f"{r}: セキュリティの指定（CSP）の <meta> は <base> と <script> より前（viewport の次）に置く")
    for attrs, code in re.findall(r"<script\b([^>]*)>(.*?)</script>", s, re.S):
        if re.search(r"\bsrc\s*=", attrs):
            if not re.search(r"""\bsrc=["']([^"']*/)?assets/js/main\.js["']""", attrs):
                problems.append(f"{r}: 読み込んでよいスクリプトは assets/js/main.js だけ: <script{attrs}>")
            continue
        if re.search(r"""type=["']application/ld\+json["']""", attrs):
            continue
        # 決まった2つ（js クラスの1行・404 の基準パス）は、中身を書き換えてもハッシュを直せば動く。それ以外は増やさない
        known = (len(code) < 120 and "document.documentElement" in code and "js" in code) or (is404 and 'document.querySelector("base")' in code)
        if attrs.strip() or not known:
            problems.append(f"{r}: ページに直接スクリプトを書かない（CSP で止められる。main.js に書く）: <script{attrs}>{code.strip()[:50]}…")
            continue
        inline_hashes.add(script_hash(code))
        if CSP and script_hash(code) not in csp_sources(CSP, "script-src"):
            problems.append(f"{r}: ページ内のスクリプトの中身が変わったので止められる。全ページの CSP の script-src のハッシュを {script_hash(code)} に直す"
                            f"（古いハッシュは消す）: <script>{code[:50]}…")
    if re.search(r"<style\b", s):
        problems.append(f"{r}: <style> は使わない（CSP で止められる。style.css に書く）")
    if re.search(r"<(video|audio|source|track|object|embed)\b", s):
        problems.append(f"{r}: 動画・音声・PDF などの埋め込み（<video> <audio> <object> <embed>）は CSP で止められる"
                        "（動画は YouTube などへのリンク、PDF は普通のリンクにする。埋め込む必要があればお客様に確認）")
    if re.search(r"""\son[a-z]+\s*=\s*["']""", body):
        problems.append(f"{r}: onclick= などの属性にスクリプトを書かない（CSP で止められる。main.js に書く）")
    if re.search(r"""\sstyle\s*=\s*["']""", body):
        problems.append(f"{r}: style= 属性は使わない（CSP で止められる。style.css に書く）")

    # 埋め込み（iframe）：置いてよいのは site_config.json の allowed_iframes（地図など）と、お問い合わせフォームの埋め込み枠の中だけ
    form_at = s.find('id="contact-form"') if r == CONTACT_PAGE else -1
    for m in re.finditer(r"<iframe\b[^>]*>", s):
        tag = m.group(0)
        src = re.search(r"""\bsrc=["']([^"']*)""", tag)
        url = html.unescape(src.group(1)) if src else ""
        in_form = form_at >= 0 and m.start() > form_at
        if not url.startswith("https://") or not (any(url.startswith(p) for p in ALLOWED_IFRAMES) or in_form):
            problems.append(f"{r}: 埋め込み（iframe）はお客様の確認なしに増やさない（site_config.json の allowed_iframes とお問い合わせフォームだけ）: {url[:80]}")
        elif not frame_allowed(url):
            problems.append(f"{r}: 埋め込み {url[:60]} が CSP で止められる。全ページの CSP の frame-src に {origin_of(url)} を足す")
        if not re.search(r"""\btitle=["'][^"']+""", tag):
            problems.append(f"{r}: 埋め込み（iframe）に title を付ける（読み上げで何の枠か分かるように）: {url[:60]}")
    for m in re.finditer(r"<form\b([^>]*)>", s):
        action = re.search(r"""\baction=["']([^"']*)""", m.group(1))
        url = action.group(1) if action else ""
        if not url.startswith("https://"):
            problems.append(f"{r}: フォームの送信先は https://… の外部サービスだけ（自サイト・mailto: などは CSP で止められる）: action={url or '（なし）'}")
        elif origin_of(url) not in csp_sources(CSP, "form-action"):
            problems.append(f"{r}: フォームの送信先 {origin_of(url)} が CSP で止められる。全ページの CSP の form-action を 'self' {origin_of(url)} にする")
    # サイトの中に作ったフォームの入力欄には、見出し（<label for>）を付ける
    # （コメントの中は見ない。除くのは、迷惑な送信を見分ける欄〈<div class="form-trap"> の中〉と、送信・隠しの欄だけ）
    if re.search(r"<form\b", s, re.I):
        fs_ = re.sub(r"<!--.*?-->", "", s, flags=re.S)
        fs_ = re.sub(r"""<div\b[^>]*\bclass=["'][^"']*\bform-trap\b[^"']*["'][^>]*>.*?</div>""", "", fs_, flags=re.S | re.I)

        def attr(attrs, name):
            m_ = re.search(r"""(?<![\w-])""" + name + r"""\s*=\s*["']([^"']*)["']""", attrs, re.I)
            return m_.group(1) if m_ else None

        label_for = {v for v in re.findall(r"""<label\b[^>]*(?<![\w-])for\s*=\s*["']([^"']+)""", fs_, re.I)}
        wrapped = [lm.span() for lm in re.finditer(r"<label\b[^>]*>.*?</label>", fs_, re.S | re.I)]  # <label>見出し <input></label> の形
        for m in re.finditer(r"<(input|textarea|select)\b([^>]*)>", fs_, re.I):
            attrs = m.group(2)
            if (attr(attrs, "type") or "").lower() in ("hidden", "submit", "button", "reset", "image"):
                continue
            if any(a_ <= m.start() < b_ for a_, b_ in wrapped):
                continue
            fid = attr(attrs, "id")
            if not fid or fid not in label_for:
                problems.append(f"{r}: フォームの入力欄に <label for=\"…\"> を付ける（読み上げで何の欄か分かるように）: <{m.group(1)}{attrs[:60]}>")

    # 内部リンク・画像のリンク切れ（外部URL・#・mailto・tel は対象外）
    base_dir = ROOT if is404 else os.path.dirname(page)
    links = re.sub(r"<base\b[^>]*>", "", s)  # 404.html の基準パスの指定はリンクではない
    links = re.sub(r"<!--.*?-->", "", links, flags=re.S)
    for attr, _q, url in re.findall(r"""\b(href|src)=(["'])(.*?)\2""", links):
        url = html.unescape(url)
        if re.match(r"^(https?:|mailto:|tel:|data:)", url):
            continue
        if url.startswith("#"):
            # 404.html は <base href="/"> なので、#… はトップページの中の行き先になる（main.js が自分のページの中にあれば移す）
            frag = url[1:]
            if attr == "href" and frag and not has_id(s, frag) and not (is404 and has_id(read("index.html"), frag)):
                problems.append(f"{r}: ページの中の行き先が無い（id=\"{frag}\" の要素が無い）: href={url}")
            continue
        if url.startswith("/"):
            problems.append(f"{r}: ルートから始まるパスは使わない（相対パスにする）: {url}")
            continue
        target = os.path.normpath(os.path.join(base_dir, urllib.parse.unquote(url.split("#")[0].split("?")[0])))
        if url.split("#")[0].endswith("/") or url.split("#")[0] in ("./", "", "."):
            target = os.path.join(target, "index.html")
        if not os.path.exists(target):
            problems.append(f"{r}: リンク切れ {attr}={url}")
        elif "#" in url and url.split("#", 1)[1] and target.endswith(".html") and not has_id(open(target, encoding="utf-8").read(), url.split("#", 1)[1]):
            problems.append(f"{r}: リンク先のページに行き先が無い（id=\"{url.split('#', 1)[1]}\" の要素が無い）: href={url}")
        elif os.path.basename(target).startswith("placeholder-"):
            placeholder_imgs[r] = placeholder_imgs.get(r, 0) + 1
    for m in re.finditer(r"""\bsrcset=["']([^"']*)""", links):
        for cand in m.group(1).split(","):
            url = cand.strip().split(" ")[0]
            if url and not re.match(r"^(https?:|data:)", url) and not os.path.exists(os.path.join(base_dir, urllib.parse.unquote(url))):
                problems.append(f"{r}: リンク切れ srcset={url}")

    # 自サイト内のリンク・画像は相対パスにする（canonical と OGP（<head> の中）だけは絶対URL）
    for url in SELF_URL_RE.findall(body):
        problems.append(f"{r}: 自サイトへのリンク・画像が絶対URLになっている（相対パスにする）: {url}")

    # 外部のライブラリ・フォント・埋め込みは使わない（外部サイトへの <a> リンクと iframe は別に確かめる。
    # アクセス解析の計測画像は analytics_pixel に書いた読み込み元だけ、1ページに1つ）
    pixels = 0
    for m in re.finditer(r"""<(\w+)\b[^>]*?\b(?:src|href|srcset)=["']((?:https?:)?//[^"']*)""", s):
        tag, url = m.group(1).lower(), m.group(2)
        if tag in ("a", "iframe") or (tag == "link" and 'rel="canonical"' in m.group(0)):
            continue
        if tag == "img" and ANALYTICS and html.unescape(url).startswith(ANALYTICS + "/"):
            pixels += 1
            continue
        problems.append(f"{r}: 外部のライブラリ・フォント・画像は使わない（サイトの中に置く）: <{tag}> {url[:80]}")
    for url in re.findall(r"""url\(\s*["']?((?:https?:)?//[^"')\s]*)""", s):
        problems.append(f"{r}: 外部のファイルを読まない: url({url[:80]})")

    # 外部リンク：別タブで開くなら rel="noopener"。http:// は https に対応しているか確かめる（参考）
    for tag in re.findall(r"<a\b[^>]*>", body):
        if re.search(r"""\btarget=["']_blank["']""", tag) and not re.search(r"""\brel=["'][^"']*\bnoopener\b""", tag):
            problems.append(f"{r}: 別タブで開くリンクには rel=\"noopener\" を付ける: {tag[:90]}")
        h = re.search(r"""\bhref=["'](http://[^"']*)""", tag)
        if h:
            notes.append(f"{r}: http:// のリンク（相手が https に正しく対応していれば https にする。証明書に問題があれば残して報告）: {h.group(1)[:80]}")

    if ANALYTICS and pixels != 1:
        problems.append(f"{r}: アクセス解析の計測画像（{ANALYTICS}/…）は各ページに1つ置く（いま {pixels} 個）")

    # 画像：width・height・alt（飾りなら alt=""）
    for tag in re.findall(r"<img\b[^>]*>", body):
        if 'width="' not in tag or 'height="' not in tag:
            problems.append(f"{r}: width/height のない画像: {tag[:80]}")
        if not re.search(r"""\balt=["']""", tag):
            problems.append(f"{r}: alt のない画像（飾りの画像なら alt=\"\"）: {tag[:80]}")

    # id の重複
    ids = re.findall(r"""\sid=["']([^"']+)""", body)
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"{r}: 同じ id が2回以上ある: {dup}")

    # 見出し h1 はページに1つだけ（ページ見出し帯の .page-title。ホームは独自の位置）
    h1_count = len(re.findall(r"<h1\b", s))
    if h1_count != 1:
        problems.append(f"{r}: <h1> が1つではない（{h1_count}個）")
    hero = between(s, '<div class="page-hero">', "<!-- ===== 見出し帯ここまで")
    if me and '<h1 class="page-title"' not in hero:
        problems.append(f'{r}: 見出し帯（<div class="page-hero"> … <!-- ===== 見出し帯ここまで ===== -->）の中に <h1 class="page-title"> がない')

    # 共通部分が全ページで同じか
    blocks = {name: between(s, a, b) for name, (a, b) in SHARED_BLOCKS.items()}
    for name, block in blocks.items():
        if not block:
            problems.append(f"{r}: 共通部分「{name}」が見つからない（目印のコメント {SHARED_BLOCKS[name][0]} … {SHARED_BLOCKS[name][1]}）")
            continue
        shared.setdefault(name, {}).setdefault(normalize_shared(block, "" if is404 else prefix), []).append(r)

    # ページごとに変わる部分：グローバルナビの現在位置・タブ・パンくず
    hdr = blocks["ヘッダー・グローバルナビ"]
    items = re.findall(r'<li(?: class="([^"]*)")?><a href="([^"]*)"( aria-current="page")?>', hdr)
    want = [me] if me in NAV_ORDER and me not in NO_CURRENT else []
    cur_aria = [site_path(r, h) for c, h, a in items if a]
    cur_cls = [site_path(r, h) for c, h, a in items if "is-current" in (c or "").split()]
    if cur_aria != want or cur_cls != want or hdr.count('aria-current="page"') != len(cur_aria):
        problems.append(f"{r}: グローバルナビの現在位置（li の is-current と a の aria-current=\"page\"）を {names(want)} にする"
                        f"（いま: is-current={names(cur_cls)}／aria-current={names(cur_aria)}）")
    want_parent = [PARENT[me]] if me in PARENT else []
    got_parent = [site_path(r, h) for c, h, a in items if "is-parent" in (c or "").split()]
    if got_parent != want_parent:
        problems.append(f"{r}: グローバルナビの is-parent を {names(want_parent)} にする（いま: {names(got_parent)}）")

    tabs = between(hero, '<nav class="subnav subnav--tabs"', "</nav>")
    group = PARENT.get(me, me if me and GROUPS.get(me) else None)
    if group is not None and me:
        tab_items = re.findall(r'<li(?: class="([^"]*)")?><a href="([^"]*)"( aria-current="page")?>', tabs)
        got = [site_path(r, h) for c, h, a in tab_items]
        if got != GROUPS[group]:
            problems.append(f"{r}: 見出し帯のタブ（subnav--tabs）を {names(GROUPS[group])} の順にする（いま: {names(got)}）")
        want_tab = [me] if me in PARENT else []
        tab_aria = [site_path(r, h) for c, h, a in tab_items if a]
        tab_cls = [site_path(r, h) for c, h, a in tab_items if c == "is-current"]
        if tab_aria != want_tab or tab_cls != want_tab or tabs.count('aria-current="page"') != len(tab_aria):
            problems.append(f"{r}: 表示中のタブ（li の class=\"is-current\" と a の aria-current=\"page\"）を {names(want_tab)} にする"
                            f"（いま: is-current={names(tab_cls)}／aria-current={names(tab_aria)}）")
    elif tabs:
        problems.append(f"{r}: このページには見出し帯のタブ（subnav--tabs）を付けない（子ページのあるグループだけ）")

    bc = between(hero, '<nav class="breadcrumb"', "</nav>")
    if me in PARENT:
        want_bc = [""] + ([PARENT[me]] if PARENT[me] else []) + [me]
        bc_items = re.findall(r'<a href="([^"]*)"( aria-current="page")?>', bc)
        if [site_path(r, h) for h, a in bc_items] != want_bc or [bool(a) for h, a in bc_items] != [False] * (len(want_bc) - 1) + [True]:
            problems.append(f"{r}: パンくず（breadcrumb）を「{' » '.join(p or 'ホーム' for p in want_bc)}」にし、最後の項目の a に aria-current=\"page\" を付ける")
    elif bc:
        problems.append(f"{r}: パンくず（breadcrumb）は子ページだけに付ける")

for name, variants in shared.items():
    if len(variants) > 1:
        groups = sorted(variants.values(), key=len, reverse=True)
        odd = [p for g in groups[1:] for p in g]
        problems.append(f"共通部分「{name}」が他のページと違う: {', '.join(odd)}（違ってよいのは相対パスの前置きと現在位置の印だけ）")

# CSP の script-src に、どのページでも使われていないハッシュが残っていないか
for h in csp_sources(CSP, "script-src")[1:]:
    if HASH_RE.fullmatch(h) and h not in inline_hashes:
        problems.append(f"使われていないハッシュ {h} を全ページの CSP の script-src から消す")

# ---------- 4. サイトマップ ----------
# HTML のサイトマップ（sitemap ページと 404.html の一覧）はグローバルナビと同じページを同じ順に
for r in [x for x in (SITEMAP_PAGE, "404.html") if x]:
    if not os.path.exists(os.path.join(ROOT, r)):
        problems.append(f"{r} がない")
        continue
    lst = between(read(r), '<ul class="sitemap">', "</main>")
    got = [site_path(r, h) for h in re.findall(r'<a href="([^"]*)"', lst)]
    if got != NAV_ORDER:
        missing = [p for p in NAV_ORDER if p not in got]
        extra = [p for p in got if p not in NAV_ORDER]
        problems.append(f"{r}: サイトマップの一覧（<ul class=\"sitemap\">）がグローバルナビと違う（足りない: {names(missing)}／余分: {names(extra)}／順番も揃える）")

# sitemap.xml：全ページが載っていて、無いページは載っていない
if os.path.exists(os.path.join(ROOT, "sitemap.xml")):
    sm = read("sitemap.xml")
    locs = [urllib.parse.unquote(u) for u in re.findall(r"<loc>(.*?)</loc>", sm)]
    want_locs = []
    for page in ALL_PAGES:
        me = page_path(rel(page))
        if me in SITEMAP_XML_EXCLUDE or me == "404.html" or me in REDIRECTS:
            continue
        want_locs.append(SITE + me)
    for u in want_locs:
        if u not in locs:
            problems.append(f"sitemap.xml に載っていない: {u}")
    for u in locs:
        if u not in want_locs:
            problems.append(f"sitemap.xml に余分な URL（無いページ・載せない決まりのページ・本番URLと違う）: {u}")
    if count_marks(sm):
        placeholder_marks["sitemap.xml"] = count_marks(sm)
if os.path.exists(os.path.join(ROOT, "robots.txt")) and f"Sitemap: {SITE}sitemap.xml" not in read("robots.txt"):
    notes.append(f"robots.txt に「Sitemap: {SITE}sitemap.xml」がない")

# ---------- 5. 404.html：<base href="/"> と、github.io でのプレビュー用の基準パス（リポジトリ名と同じ） ----------
s404 = read("404.html") if os.path.exists(os.path.join(ROOT, "404.html")) else ""
if s404:
    if '<base href="/">' not in s404:
        problems.append('404.html: <base href="/"> がない（GitHub Pages の 404 ページでリンクと画像が切れる）')
    m = re.search(r'var p="([^"]*)"', s404)
    repo = CONF.get("repo_name", "")
    git_config = os.path.join(ROOT, ".git", "config")
    if os.path.exists(git_config):
        u = re.search(r'\[remote "origin"\][^\[]*?\burl\s*=\s*(\S+)', open(git_config, encoding="utf-8").read())
        if u:
            repo = re.sub(r"\.git$", "", u.group(1).rstrip("/")).rsplit("/", 1)[-1]
    if repo == "example-site":
        problem("tools/site_config.json の repo_name がひな形のまま（example-site）。確認用 URL の 404 ページの基準パスになるので、"
                "リポジトリ名が決まったら直し、404.html の var p と全ページの CSP のハッシュも直す", placeholder=True)
    if not m:
        problems.append('404.html: github.io 用の基準パスのスクリプト（var p="/リポジトリ名/"）がない')
    elif repo and m.group(1) != f"/{repo}/":
        problems.append(f'404.html: github.io 用の基準パス var p="{m.group(1)}" をリポジトリ名に合わせて "/{repo}/" にする'
                        "（スクリプトの中身が変わるので、全ページの CSP のハッシュも直す）")

# ---------- 6. style.css ----------
if css:
    if re.search(r"@import\b", css):
        problems.append("style.css: @import は使わない（外部のライブラリ・フォントは使わない。CSS は1ファイル）")
    if re.search(r"@font-face\b", css):
        problems.append("style.css: Webフォント（@font-face）は使わない（日本語は端末の標準フォント）")
    for url in re.findall(r"""url\(\s*["']?([^"')\s]+)""", css):
        if url.startswith("data:"):
            continue
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//)", url, re.I):
            problems.append(f"style.css: 外部のファイルを読まない: url({url[:80]})")
        elif url.startswith("/"):
            problems.append(f"style.css: ルートから始まるパスは使わない（相対パスにする）: url({url})")
        elif not os.path.exists(os.path.join(ROOT, "assets", "css", urllib.parse.unquote(url.split("#")[0].split("?")[0]))):
            problems.append(f"style.css: CSS で使っている画像が見つからない: url({url})")
        elif os.path.basename(url).startswith("placeholder-"):
            placeholder_imgs["assets/css/style.css"] = placeholder_imgs.get("assets/css/style.css", 0) + 1
    # 色の値は :root の変数にまとめる（ほかの場所に色を直接書かない）
    body_css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    body_css = re.sub(r"url\([^)]*\)", "url()", body_css)
    body_css = re.sub(r":root\s*\{[^{}]*\}", "", body_css)
    for m in re.finditer(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(\s*[\d.]", body_css):
        line = body_css[: m.start()].count("\n") + 1
        snippet = body_css[max(0, body_css.rfind("\n", 0, m.start()) + 1): body_css.find("\n", m.end())].strip()
        problems.append(f"style.css: 色の値は :root の変数に書き、ほかの場所では var(--…) を使う: {snippet[:80]}")
    if count_marks(css):
        placeholder_marks["assets/css/style.css"] = count_marks(css)

# ---------- 7. 画面幅の区切り ----------
# 「未満」側（max-width）は .98 で書く（拡大率 125% などの小数の画面幅で、どちらにも当たらない隙間を作らない）
bad_max = sorted({v for q in re.findall(r"@media[^{]*", css) for v in re.findall(r"max-width:\s*([\d.]+)px", q) if not v.endswith(".98")}, key=float)
for v in bad_max:
    problems.append(f"style.css: @media の (max-width: {v}px) は .98 で書く（例：960px 未満なら (max-width: 959.98px)）")
if js:
    bp = re.search(r"NAV_BREAKPOINT\s*=\s*(\d+)", js)
    if not bp:
        problems.append("main.js: NAV_BREAKPOINT が見つからない")
    else:
        n = int(bp.group(1))
        if f"(min-width: {n}px)" not in css or f"(max-width: {n - 1}.98px)" not in css:
            problems.append(f"main.js の NAV_BREAKPOINT（{n}）と style.css の区切り（min-width: {n}px／max-width: {n - 1}.98px）が合っていない")
    if count_marks(js):
        placeholder_marks["assets/js/main.js"] = count_marks(js)

# ---------- 8. 【要入力】の印・仮画像（ひな形から作ったときの埋め忘れ） ----------
for r in sorted(set(placeholder_marks) | set(placeholder_imgs)):
    left = [f"【要入力】【要確認】の印 {placeholder_marks[r]} か所"] if r in placeholder_marks else []
    left += [f"仮画像（placeholder-…）{placeholder_imgs[r]} か所"] if r in placeholder_imgs else []
    problem(f"{r}: ひな形のまま埋めていないところがある（{'・'.join(left)}）", placeholder=True)

# ---------- 結果 ----------
CHECKED = ("リンク切れ・ページの中の行き先・転送用のページ・アクセス解析の計測画像・h1と見出し帯・共通部分・ナビの現在位置・タブ・パンくず・canonical/og:url・title・"
           "セキュリティの指定（CSP）とページ内スクリプトのハッシュ・外部ファイル・iframe・フォーム・画像（width/height/alt・ファイル名・EXIF）・"
           "別タブのリンク・CSS の画像と色の変数・NFC・旧サービスへの参照・サイトマップ（HTML と sitemap.xml）・404 の基準パス・画面幅の区切り・【要入力】【要確認】の残り")
if notes:
    print("参考（人が判断するもの。OK/NG には影響しない）:")
    for n in notes:
        print(" -", n)
if problems:
    print("問題が見つかりました:")
    for p in problems:
        print(" -", p)
    sys.exit(1)
print(f"OK: {len(ALL_PAGES)} ページを確認しました（{CHECKED}）")
