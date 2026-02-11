#!/usr/bin/env python3
"""
Danbooru Favorites Downloader (API Key版)
- Danbooru API (login + api_key) で全 favorite を取得
- 重複排除して JSON 保存 (data-id, data-tags, data-rating, data-score 等)
- 原寸画像をダウンロード
- data-tags を XMP メタデータとして画像に埋め込み (Python 内蔵)

使い方:
  python danbooru_fav_downloader.py
  python danbooru_fav_downloader.py --skip-xmp
  python danbooru_fav_downloader.py --skip-download
  python danbooru_fav_downloader.py --skip-resize
"""
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
import numpy as np
from PIL import Image

# ============================================================
# 設定 — ここを自分の情報に書き換える
# ============================================================
DANBOORU_LOGIN = "palm_floods"
DANBOORU_API_KEY = "Vsq3KWK3pCUGbVPwnDSUtRXF"
TAGS_QUERY = "ordfav:palm_floods"
PER_PAGE = 200  # Danbooru API max

SCRIPT_DIR = Path(__file__).parent.resolve()
OUTPUT_DIR = SCRIPT_DIR / "danbooru_output_fav"
JSON_FILE = OUTPUT_DIR / "_posts_metadata.json"

BASE_URL = "https://danbooru.donmai.us"
AUTH = (DANBOORU_LOGIN, DANBOORU_API_KEY)


# ============================================================
# API で全ページ取得
# ============================================================
def fetch_all_posts() -> list:
    """全 favorite 投稿を取得（重複排除・ページネーション対応）"""
    session = requests.Session()
    session.auth = AUTH
    session.headers["User-Agent"] = "DanbooruFavDL/1.0"

    all_posts = []
    seen_ids = set()
    page = 1

    while True:
        params = {"tags": TAGS_QUERY, "limit": PER_PAGE, "page": page}
        url = f"{BASE_URL}/posts.json?{urlencode(params)}"
        print(f"  Page {page} ... ", end="", flush=True)

        resp = session.get(url)
        if resp.status_code != 200:
            print(f"HTTP {resp.status_code}")
            break

        posts = resp.json()
        if not posts:
            print("empty -> done!")
            break

        new = 0
        for p in posts:
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                all_posts.append(p)
                new += 1

        print(f"{len(posts)} posts ({new} new)")

        if len(posts) < PER_PAGE:
            break
        page += 1
        time.sleep(0.5)  # rate limit: 10 req/s

    return all_posts


# ============================================================
# メタデータ整理
# ============================================================
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "gif"}


def build_metadata(posts: list) -> list:
    """HTML の data-* 属性に対応する情報を抽出（動画は除外）"""
    records = []
    for p in posts:
        file_ext = p.get("file_ext", "jpg")
        if file_ext.lower() not in ALLOWED_EXT:
            continue
        file_url = p.get("file_url") or p.get("large_file_url") or ""
        file_ext = p.get("file_ext", "jpg")

        flags = []
        if p.get("is_flagged"):
            flags.append("flagged")
        if p.get("is_pending"):
            flags.append("pending")
        if p.get("is_deleted"):
            flags.append("deleted")

        records.append(
            {
                "data-id": p["id"],
                "data-tags": p.get("tag_string", "").replace(" ", ", "),
                "data-rating": p.get("rating", ""),
                "data-flags": ", ".join(flags),
                "data-score": p.get("score", 0),
                "data-uploader-id": p.get("uploader_id", 0),
                "file_url": file_url,
                "file_ext": file_ext,
                "source": p.get("source", ""),
                "tag_string_artist": p.get("tag_string_artist", "").replace(" ", ", "),
                "tag_string_character": p.get("tag_string_character", "").replace(
                    " ", ", "
                ),
                "tag_string_copyright": p.get("tag_string_copyright", "").replace(
                    " ", ", "
                ),
                "tag_string_general": p.get("tag_string_general", "").replace(
                    " ", ", "
                ),
                "tag_string_meta": p.get("tag_string_meta", "").replace(" ", ", "),
                "image_width": p.get("image_width", 0),
                "image_height": p.get("image_height", 0),
                "md5": p.get("md5", ""),
            }
        )
    return records


# ============================================================
# SDXL 標準解像度リサイズ (平均色パディング)
# ============================================================
# SDXL recommended resolutions (~1 megapixel)
SDXL_RESOLUTIONS = [
    (1024, 1024),
    (1152, 896),
    (896, 1152),
    (1216, 832),
    (832, 1216),
    (1344, 768),
    (768, 1344),
    (1536, 640),
    (640, 1536),
]


def find_closest_sdxl_resolution(w: int, h: int) -> tuple:
    """アスペクト比が最も近い SDXL 解像度を返す"""
    aspect = w / h
    best = None
    best_diff = float("inf")
    for rw, rh in SDXL_RESOLUTIONS:
        diff = abs((rw / rh) - aspect)
        if diff < best_diff:
            best_diff = diff
            best = (rw, rh)
    return best


def get_average_color(img: Image.Image) -> tuple:
    """画像の平均色を (R, G, B) で返す"""
    arr = np.array(img.convert("RGB"))
    avg = arr.mean(axis=(0, 1)).astype(int)
    return tuple(avg)


def resize_to_sdxl(filepath: Path) -> bool:
    """
    画像を最も近い SDXL 解像度にリサイズ。
    - bicubic 補間
    - アスペクト比維持
    - パディングは画像の平均色
    """
    try:
        img = Image.open(filepath).convert("RGB")
        orig_w, orig_h = img.size
        target_w, target_h = find_closest_sdxl_resolution(orig_w, orig_h)

        # 既にターゲットサイズなら skip
        if orig_w == target_w and orig_h == target_h:
            return True

        # 平均色を取得 (リサイズ前)
        avg_color = get_average_color(img)

        # アスペクト比維持でリサイズ (fit inside target)
        scale = min(target_w / orig_w, target_h / orig_h)
        new_w = round(orig_w * scale)
        new_h = round(orig_h * scale)
        img_resized = img.resize((new_w, new_h), Image.BICUBIC)

        # 平均色でパディング
        canvas = Image.new("RGB", (target_w, target_h), avg_color)
        paste_x = (target_w - new_w) // 2
        paste_y = (target_h - new_h) // 2
        canvas.paste(img_resized, (paste_x, paste_y))

        # 保存 (元ファイルを上書き)
        ext = filepath.suffix.lower()
        if ext in (".jpg", ".jpeg"):
            canvas.save(filepath, "JPEG", quality=95)
        elif ext == ".png":
            canvas.save(filepath, "PNG")
        elif ext == ".webp":
            canvas.save(filepath, "WEBP", quality=95)
        else:
            canvas.save(filepath)

        return True
    except Exception as e:
        print(f"  Resize error {filepath.name}: {e}")
        return False


def resize_all_images(records: list, output_dir: Path) -> int:
    """全画像を SDXL 解像度にリサイズ"""
    resized = 0
    total = 0
    for rec in records:
        fp = output_dir / f"{rec['data-id']}.{rec['file_ext']}"
        if not fp.exists():
            continue
        total += 1
        if resize_to_sdxl(fp):
            resized += 1
            if resized <= 3 or resized % 20 == 0:
                img = Image.open(fp)
                print(f"  Resized [{resized}/{total}] #{rec['data-id']} -> {img.size}")
    return resized


# ============================================================
# 画像ダウンロード
# ============================================================
def download_images(records: list, output_dir: Path) -> int:
    """原寸画像をダウンロード。ファイル名は {post_id}.{ext}"""
    session = requests.Session()
    session.headers["User-Agent"] = "DanbooruFavDL/1.0"

    to_dl = []
    for rec in records:
        if not rec["file_url"]:
            continue
        if rec["file_ext"].lower() not in ALLOWED_EXT:
            continue
        fp = output_dir / f"{rec['data-id']}.{rec['file_ext']}"
        if not fp.exists():
            to_dl.append((rec, fp))

    if not to_dl:
        print("  All images already exist.")
        return 0

    print(f"  {len(to_dl)} images to download ...")
    downloaded = 0
    failed = 0

    for i, (rec, fp) in enumerate(to_dl, 1):
        pid = rec["data-id"]
        print(f"  [{i}/{len(to_dl)}] #{pid}: ", end="", flush=True)
        try:
            r = session.get(rec["file_url"], stream=True, timeout=60)
            if r.status_code == 200:
                with open(fp, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                size_kb = fp.stat().st_size / 1024
                print(f"OK ({size_kb:.0f} KB)")
                downloaded += 1
            else:
                print(f"HTTP {r.status_code}")
                failed += 1
        except Exception as e:
            print(f"Error: {e}")
            failed += 1

        if i % 10 == 0:
            time.sleep(0.3)

    if failed:
        print(f"  Failed: {failed}")
    return downloaded


# ============================================================
# XMP 埋め込み (Python 内蔵)
# ============================================================
def _build_xmp_packet(tags_str: str) -> str:
    """XMP XML パケットを構築 (dc:description + dc:title のみ)"""

    # XML エスケープ
    def esc(s):
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    xmp = f"""<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description
      xmlns:dc="http://purl.org/dc/elements/1.1/">
      <dc:description>
        <rdf:Alt>
          <rdf:li xml:lang="x-default">{esc(tags_str)}</rdf:li>
        </rdf:Alt>
      </dc:description>
      <dc:title>
        <rdf:Alt>
          <rdf:li xml:lang="x-default">{esc(tags_str)}</rdf:li>
        </rdf:Alt>
      </dc:title>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    return xmp


def _embed_xmp_to_jpeg(filepath: Path, xmp_packet: str) -> bool:
    """JPEG に XMP パケットを埋め込む (APP1 マーカー)"""
    xmp_bytes = xmp_packet.encode("utf-8")
    xmp_header = b"http://ns.adobe.com/xap/1.0/\x00"
    app1_data = xmp_header + xmp_bytes
    app1_length = len(app1_data) + 2  # +2 for length bytes

    with open(filepath, "rb") as f:
        data = f.read()

    if data[:2] != b"\xff\xd8":
        return False  # Not JPEG

    # 既存の XMP APP1 セグメントを除去
    pos = 2
    new_data = b"\xff\xd8"
    while pos < len(data):
        if data[pos : pos + 2] == b"\xff\xe1":
            seg_len = int.from_bytes(data[pos + 2 : pos + 4], "big")
            seg_body = data[pos + 4 : pos + 2 + seg_len]
            if seg_body.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
                pos += 2 + seg_len
                continue
        break

    # 残りのデータ
    rest = data[pos:]

    # 新しい APP1 を挿入
    app1_marker = b"\xff\xe1" + app1_length.to_bytes(2, "big") + app1_data
    result = b"\xff\xd8" + app1_marker + rest

    with open(filepath, "wb") as f:
        f.write(result)
    return True


def _embed_xmp_to_png(filepath: Path, xmp_packet: str) -> bool:
    """PNG に XMP を iTXt チャンクとして埋め込む"""
    from PIL import Image as PILImage
    from PIL.PngImagePlugin import PngInfo

    img = PILImage.open(filepath)
    meta = PngInfo()
    meta.add_text("XML:com.adobe.xmp", xmp_packet, zip=False)
    img.save(filepath, "PNG", pnginfo=meta)
    return True


def _embed_xmp_to_webp(filepath: Path, xmp_packet: str) -> bool:
    """WebP に XMP を埋め込む (RIFF チャンク操作)"""
    xmp_bytes = xmp_packet.encode("utf-8")

    with open(filepath, "rb") as f:
        data = f.read()

    # RIFF/WEBP 形式確認
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        # フォールバック: サイドカー .xmp ファイル
        xmp_path = filepath.with_suffix(".xmp")
        xmp_path.write_text(xmp_packet, encoding="utf-8")
        return True

    # 既存の XMP チャンクを除去
    pos = 12
    chunks = []
    while pos < len(data):
        if pos + 8 > len(data):
            break
        chunk_id = data[pos : pos + 4]
        chunk_size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        chunk_data = data[pos + 8 : pos + 8 + chunk_size]
        # パディング (偶数バイト境界)
        padded_size = chunk_size + (chunk_size % 2)
        if chunk_id != b"XMP ":
            chunks.append((chunk_id, chunk_data))
        pos += 8 + padded_size

    # 新しい XMP チャンクを追加
    chunks.append((b"XMP ", xmp_bytes))

    # RIFF を再構築
    body = b"WEBP"
    for chunk_id, chunk_data in chunks:
        size = len(chunk_data)
        body += chunk_id + size.to_bytes(4, "little") + chunk_data
        if size % 2 == 1:
            body += b"\x00"  # パディング

    result = b"RIFF" + len(body).to_bytes(4, "little") + body

    with open(filepath, "wb") as f:
        f.write(result)
    return True


def embed_xmp_single(filepath: Path, tags_str: str) -> bool:
    """1ファイルに XMP を埋め込む (Python 内蔵)"""
    xmp_packet = _build_xmp_packet(tags_str)

    ext = filepath.suffix.lower()
    try:
        if ext in (".jpg", ".jpeg"):
            return _embed_xmp_to_jpeg(filepath, xmp_packet)
        elif ext == ".png":
            return _embed_xmp_to_png(filepath, xmp_packet)
        elif ext == ".webp":
            return _embed_xmp_to_webp(filepath, xmp_packet)
    except Exception as e:
        print(f"  XMP embed error {filepath.name}: {e}")

    return False


def embed_xmp(records: list, output_dir: Path) -> int:
    """全ファイルに XMP を埋め込む"""
    print("  Using: Python built-in XMP writer")

    embedded = 0
    total = sum(
        1 for r in records if (output_dir / f"{r['data-id']}.{r['file_ext']}").exists()
    )

    for i, rec in enumerate(records, 1):
        fp = output_dir / f"{rec['data-id']}.{rec['file_ext']}"
        if not fp.exists():
            continue

        tags_str = rec["data-tags"]

        if embed_xmp_single(fp, tags_str):
            embedded += 1
            if embedded <= 3 or embedded % 20 == 0:
                print(f"  XMP [{embedded}/{total}] #{rec['data-id']}")

    return embedded


# ============================================================
# メイン
# ============================================================
def main():
    skip_xmp = "--skip-xmp" in sys.argv
    skip_dl = "--skip-download" in sys.argv
    skip_resize = "--skip-resize" in sys.argv

    print("=" * 60)
    print("  Danbooru Favorites Downloader (API Key)")
    print(f"  User: {DANBOORU_LOGIN}")
    print(f"  Tags: {TAGS_QUERY}")
    print(f"  Out:  {OUTPUT_DIR}")
    print("=" * 60)
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # [1] 全 favorites 取得
    print("[1/4] Fetching all favorites ...")
    posts = fetch_all_posts()
    print(f"  Total: {len(posts)} unique posts")
    print()

    if not posts:
        print("No posts found.")
        sys.exit(1)

    # [2] JSON 保存
    print("[2/4] Saving metadata JSON ...")
    records = build_metadata(posts)
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"  {len(records)} records -> {JSON_FILE.name}")
    print()

    # [3] 画像ダウンロード
    if skip_dl:
        print("[3/4] Download: SKIPPED")
        downloaded = 0
    else:
        print("[3/4] Downloading images ...")
        downloaded = download_images(records, OUTPUT_DIR)
        print(f"  Downloaded: {downloaded}")
    print()

    # [4] SDXL リサイズ
    if skip_resize:
        print("[4/5] Resize: SKIPPED")
        resized_count = 0
    else:
        print("[4/5] Resizing to closest SDXL resolution (avg-color padding) ...")
        resized_count = resize_all_images(records, OUTPUT_DIR)
        print(f"  Resized: {resized_count}")
    print()

    # [5] XMP 埋め込み
    if skip_xmp:
        print("[5/5] XMP: SKIPPED")
        embedded = 0
    else:
        print("[5/5] Embedding XMP metadata ...")
        embedded = embed_xmp(records, OUTPUT_DIR)
        print(f"  Embedded: {embedded}")
    print()

    print("=" * 60)
    print("  DONE!")
    print(f"  Posts:      {len(records)}")
    print(f"  Downloaded: {downloaded}")
    print(f"  Resized:    {resized_count}")
    print(f"  XMP:        {embedded}")
    print(f"  Folder:     {OUTPUT_DIR}")
    print(f"  JSON:       {JSON_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    main()
