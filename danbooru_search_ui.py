#!/usr/bin/env python3
"""
Danbooru Multi-Tag Search & Download (Gradio UI)
- 2タグ制限を回避: APIに2タグ送信 → Python側で残りをフィルタ
- Gradio でブラウザ上からタグ検索・プレビュー・ダウンロード
- SDXL リサイズ + 平均色パディング
- XMP メタデータ埋め込み

使い方:
  python danbooru_search_ui.py
  → ブラウザで http://localhost:7860 を開く
"""
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
import numpy as np
from PIL import Image
import gradio as gr

# ============================================================
# 設定
# ============================================================
DANBOORU_LOGIN = "palm_floods"
DANBOORU_API_KEY = "Vsq3KWK3pCUGbVPwnDSUtRXF"
PER_PAGE = 200
BASE_URL = "https://danbooru.donmai.us"
AUTH = (DANBOORU_LOGIN, DANBOORU_API_KEY)

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "danbooru_search_results"

# SDXL 標準解像度
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

ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "gif"}

PREVIEW_PER_PAGE = 20  # ギャラリー1ページあたりの表示数 (5列×4行)
GRID_COLS = 5
GRID_ROWS = 4


# ============================================================
# Danbooru API 検索 (2タグ制限回避)
# ============================================================
def search_danbooru(
    tags_input: str,
    max_results: int = 100,
    rating_filter: str = "all",
    min_score: int = 0,
    progress_cb=None,
) -> tuple:
    """
    複数タグ検索:
    - 最初の2タグを API に送信
    - 残りのタグは Python 側でフィルタリング
    - 除外タグ (-tag) にも対応
    """
    tags = tags_input.strip().split()
    if not tags:
        return [], "タグを入力してください"

    # 除外タグを除いた検索タグが3つ未満なら拒否
    include_only = [t for t in tags if not t.startswith("-")]
    if len(include_only) < 3:
        return [], f"⚠️ 検索タグを3つ以上入力してください（現在 {len(include_only)} 個）"

    # +タグ と -タグ を分離
    include_tags = [t for t in tags if not t.startswith("-")]
    exclude_tags = [t.lstrip("-") for t in tags if t.startswith("-")]

    # API に送る2タグ (最初の2つ)
    api_tags = include_tags[:2]
    # Python 側でフィルタする追加タグ
    extra_include = include_tags[2:]

    session = requests.Session()
    session.auth = AUTH
    session.headers["User-Agent"] = "DanbooruSearchUI/1.0"

    all_posts = []
    seen_ids = set()
    page = 1
    api_tag_str = " ".join(api_tags)

    status_log = f'API検索: "{api_tag_str}"\n'
    if extra_include:
        status_log += f"追加フィルタ: {', '.join(extra_include)}\n"
    if exclude_tags:
        status_log += f"除外タグ: {', '.join(exclude_tags)}\n"

    # 十分な結果を得るために多めに取得
    fetch_limit = max_results * 5 if extra_include else max_results * 2
    fetch_limit = min(fetch_limit, 5000)

    while len(all_posts) < fetch_limit:
        if progress_cb:
            progress_cb(
                len(all_posts) / fetch_limit,
                desc=f"API取得中... {len(all_posts)}/{fetch_limit} posts (page {page})",
            )
        params = {"tags": api_tag_str, "limit": PER_PAGE, "page": page}
        resp = session.get(f"{BASE_URL}/posts.json", params=params)

        if resp.status_code != 200:
            status_log += f"API Error: HTTP {resp.status_code}\n"
            break

        posts = resp.json()
        if not posts:
            break

        for p in posts:
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                all_posts.append(p)

        if len(posts) < PER_PAGE:
            break
        page += 1
        time.sleep(0.3)

    status_log += f"API取得: {len(all_posts)} posts\n"

    # --- Python 側フィルタリング ---
    filtered = []
    for p in all_posts:
        post_tags = set(p.get("tag_string", "").split())
        rating = p.get("rating", "")
        score = p.get("score", 0)
        ext = p.get("file_ext", "")

        # 画像のみ
        if ext.lower() not in ALLOWED_EXT:
            continue

        # 追加タグフィルタ
        if extra_include and not all(t in post_tags for t in extra_include):
            continue

        # 除外タグ
        if exclude_tags and any(t in post_tags for t in exclude_tags):
            continue

        # Rating フィルタ
        if rating_filter != "all":
            if rating_filter == "safe" and rating != "g":
                continue
            elif rating_filter == "sensitive" and rating != "s":
                continue
            elif rating_filter == "questionable" and rating != "q":
                continue
            elif rating_filter == "explicit" and rating != "e":
                continue

        # スコアフィルタ
        if score < min_score:
            continue

        filtered.append(p)

        if len(filtered) >= max_results:
            break

    status_log += f"フィルタ後: {len(filtered)} posts\n"

    return filtered, status_log


# ============================================================
# プレビュー画像取得
# ============================================================
def get_preview_data(posts: list, progress_cb=None) -> list:
    """各投稿のプレビュー画像をダウンロードしてギャラリー用リストを返す"""
    import tempfile

    preview_dir = Path(tempfile.gettempdir()) / "danbooru_previews"
    preview_dir.mkdir(exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = "DanbooruSearchUI/1.0"

    total = len(posts)
    gallery_items = []
    for idx, p in enumerate(posts):
        if progress_cb and total > 0:
            progress_cb(
                idx / total,
                desc=f"プレビュー取得中... {idx}/{total}",
            )
        pid = p["id"]
        # プレビューURL (小さい画像)
        preview_url = (
            p.get("preview_file_url")
            or p.get("large_file_url")
            or p.get("file_url", "")
        )
        if not preview_url:
            continue

        # ローカルにキャッシュ
        ext = preview_url.rsplit(".", 1)[-1].split("?")[0] or "jpg"
        local_path = preview_dir / f"{pid}.{ext}"
        if not local_path.exists():
            try:
                r = session.get(preview_url, timeout=10)
                if r.status_code == 200:
                    local_path.write_bytes(r.content)
                else:
                    continue
            except:
                continue

        # キャプション
        tags_short = p.get("tag_string", "")[:100]
        rating = p.get("rating", "?")
        score = p.get("score", 0)
        caption = f"#{pid} | r:{rating} s:{score} | {tags_short}..."

        gallery_items.append((str(local_path), caption))

    return gallery_items


# ============================================================
# SDXL リサイズ
# ============================================================
def find_closest_sdxl_resolution(w: int, h: int) -> tuple:
    aspect = w / h
    best = min(SDXL_RESOLUTIONS, key=lambda r: abs((r[0] / r[1]) - aspect))
    return best


def resize_to_sdxl(filepath: Path) -> bool:
    try:
        img = Image.open(filepath).convert("RGB")
        orig_w, orig_h = img.size
        target_w, target_h = find_closest_sdxl_resolution(orig_w, orig_h)

        if orig_w == target_w and orig_h == target_h:
            return True

        # 平均色
        arr = np.array(img)
        avg_color = tuple(arr.mean(axis=(0, 1)).astype(int))

        # bicubic リサイズ (fit inside)
        scale = min(target_w / orig_w, target_h / orig_h)
        new_w = round(orig_w * scale)
        new_h = round(orig_h * scale)
        img_resized = img.resize((new_w, new_h), Image.BICUBIC)

        # 平均色パディング
        canvas = Image.new("RGB", (target_w, target_h), avg_color)
        paste_x = (target_w - new_w) // 2
        paste_y = (target_h - new_h) // 2
        canvas.paste(img_resized, (paste_x, paste_y))

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
        print(f"Resize error: {e}")
        return False


# ============================================================
# XMP 埋め込み (Python 内蔵)
# ============================================================
def _build_xmp_packet(tags_str: str) -> str:
    """XMP XML パケットを構築 (dc:description + dc:title のみ)"""

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
    app1_length = len(app1_data) + 2

    with open(filepath, "rb") as f:
        data = f.read()

    if data[:2] != b"\xff\xd8":
        return False

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

    rest = data[pos:]
    app1_marker = b"\xff\xe1" + app1_length.to_bytes(2, "big") + app1_data
    result = b"\xff\xd8" + app1_marker + rest

    with open(filepath, "wb") as f:
        f.write(result)
    return True


def _embed_xmp_to_png(filepath: Path, xmp_packet: str) -> bool:
    """PNG に XMP を iTXt チャンクとして埋め込む"""
    from PIL.PngImagePlugin import PngInfo

    img = Image.open(filepath)
    meta = PngInfo()
    meta.add_text("XML:com.adobe.xmp", xmp_packet, zip=False)
    img.save(filepath, "PNG", pnginfo=meta)
    return True


def _embed_xmp_to_webp(filepath: Path, xmp_packet: str) -> bool:
    """WebP に XMP を埋め込む (RIFF チャンク操作)"""
    xmp_bytes = xmp_packet.encode("utf-8")

    with open(filepath, "rb") as f:
        data = f.read()

    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        xmp_path = filepath.with_suffix(".xmp")
        xmp_path.write_text(xmp_packet, encoding="utf-8")
        return True

    pos = 12
    chunks = []
    while pos < len(data):
        if pos + 8 > len(data):
            break
        chunk_id = data[pos:pos+4]
        chunk_size = int.from_bytes(data[pos+4:pos+8], "little")
        chunk_data = data[pos+8:pos+8+chunk_size]
        padded_size = chunk_size + (chunk_size % 2)
        if chunk_id != b"XMP ":
            chunks.append((chunk_id, chunk_data))
        pos += 8 + padded_size

    chunks.append((b"XMP ", xmp_bytes))

    body = b"WEBP"
    for chunk_id, chunk_data in chunks:
        size = len(chunk_data)
        body += chunk_id + size.to_bytes(4, "little") + chunk_data
        if size % 2 == 1:
            body += b"\x00"

    result = b"RIFF" + len(body).to_bytes(4, "little") + body

    with open(filepath, "wb") as f:
        f.write(result)
    return True


def embed_xmp(filepath: Path, tags_str: str) -> bool:
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


# ============================================================
# ダウンロード＆処理
# ============================================================
def download_selected(
    posts_json: str,
    do_resize: bool,
    do_xmp: bool,
    output_folder: str,
    progress=gr.Progress(),
) -> str:
    """選択された投稿の画像をダウンロード・リサイズ・XMP埋め込み"""
    if not posts_json:
        return "データがありません。まず検索してください。"

    posts = json.loads(posts_json)
    if not posts:
        return "投稿がありません。"

    out_dir = Path(output_folder) if output_folder else DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers["User-Agent"] = "DanbooruSearchUI/1.0"

    log = f"出力先: {out_dir}\n"
    downloaded = 0
    resized = 0
    xmp_count = 0

    for i, p in enumerate(progress.tqdm(posts, desc="Downloading")):
        pid = p["id"]
        ext = p.get("file_ext", "jpg")
        file_url = p.get("file_url") or p.get("large_file_url") or ""
        tags_str = p.get("tag_string", "").replace(" ", ", ")

        if not file_url:
            continue

        fp = out_dir / f"{pid}.{ext}"

        # ダウンロード
        if not fp.exists():
            try:
                r = session.get(file_url, stream=True, timeout=60)
                if r.status_code == 200:
                    with open(fp, "wb") as f:
                        for chunk in r.iter_content(8192):
                            f.write(chunk)
                    downloaded += 1
                else:
                    log += f"#{pid}: HTTP {r.status_code}\n"
                    continue
            except Exception as e:
                log += f"#{pid}: Error {e}\n"
                continue
            time.sleep(0.2)

        # SDXL リサイズ
        if do_resize and fp.exists():
            if resize_to_sdxl(fp):
                resized += 1

        # XMP 埋め込み
        if do_xmp and fp.exists():
            if embed_xmp(fp, tags_str):
                xmp_count += 1

    # JSON メタデータ保存
    metadata = []
    for p in posts:
        ext = p.get("file_ext", "jpg")
        if ext.lower() not in ALLOWED_EXT:
            continue
        metadata.append(
            {
                "data-id": p["id"],
                "data-tags": p.get("tag_string", "").replace(" ", ", "),
                "data-rating": p.get("rating", ""),
                "data-score": p.get("score", 0),
                "data-uploader-id": p.get("uploader_id", 0),
                "file_url": p.get("file_url", ""),
                "file_ext": ext,
                "source": p.get("source", ""),
                "tag_string_artist": p.get("tag_string_artist", "").replace(" ", ", "),
                "tag_string_character": p.get("tag_string_character", "").replace(
                    " ", ", "
                ),
                "tag_string_copyright": p.get("tag_string_copyright", "").replace(
                    " ", ", "
                ),
            }
        )

    json_path = out_dir / "_search_metadata.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    log += f"\nDone!\n"
    log += f"  Downloaded: {downloaded}\n"
    log += f"  Resized:    {resized}\n"
    log += f"  XMP:        {xmp_count}\n"
    log += f"  JSON:       {json_path.name}\n"
    log += f"  Folder:     {out_dir}\n"
    return log


# ============================================================
# Gradio による検索・フィルタ状態の保持
# ============================================================
_current_posts = []


def _build_page_data(posts, page, selected_indices=None):
    """現在ページの画像パスとチェック状態のリストを返す"""
    import tempfile

    preview_dir = Path(tempfile.gettempdir()) / "danbooru_previews"

    if selected_indices is None:
        selected_indices = set()

    start = page * PREVIEW_PER_PAGE
    end = min(start + PREVIEW_PER_PAGE, len(posts))
    page_posts = posts[start:end]

    results = []  # list of (image_path_or_None, label, is_checked)
    for i, p in enumerate(page_posts):
        pid = p["id"]
        rating = p.get("rating", "?")
        score = p.get("score", 0)
        ext = (p.get("preview_file_url") or "").rsplit(".", 1)[-1].split("?")[
            0
        ] or "jpg"
        local_path = preview_dir / f"{pid}.{ext}"
        img_path = str(local_path) if local_path.exists() else None
        label = f"#{pid} r:{rating} s:{score}"
        checked = (start + i) in selected_indices
        results.append((img_path, label, checked))

    # 不足分は None で埋める
    while len(results) < PREVIEW_PER_PAGE:
        results.append((None, "", False))

    return results


def do_search(
    tags: str, max_results: int, rating: str, min_score: int, progress=gr.Progress()
):
    """検索を実行してカードグリッドとステータスを返す"""
    global _current_posts

    posts, status = search_danbooru(
        tags, max_results, rating, min_score, progress_cb=progress
    )
    _current_posts = posts
    posts_json = json.dumps(posts)

    # 最初のページのプレビュー取得
    page_posts = posts[:PREVIEW_PER_PAGE]
    get_preview_data(page_posts, progress_cb=progress)

    total_pages = max(1, (len(posts) + PREVIEW_PER_PAGE - 1) // PREVIEW_PER_PAGE)
    pg_info = f"ページ 1 / {total_pages}（全 {len(posts)} 件）"
    sel_info = f"選択: 0 / {len(posts)} 件"

    page_data = _build_page_data(posts, 0)

    # 各スロットの更新値を生成
    outputs = []
    for img_path, label, checked in page_data:
        outputs.append(gr.update(value=img_path, visible=img_path is not None))
        outputs.append(
            gr.update(value=checked, label=label, visible=img_path is not None)
        )

    return [status, posts_json, "[]", sel_info, 0, pg_info] + outputs


def do_page_change(
    posts_json: str,
    selected_json: str,
    current_page: int,
    direction: int,
    progress=gr.Progress(),
):
    """ページ切り替え"""
    if not posts_json:
        outputs = []
        for _ in range(PREVIEW_PER_PAGE):
            outputs.append(gr.update(value=None, visible=False))
            outputs.append(gr.update(value=False, visible=False))
        return [0, "データなし"] + outputs

    posts = json.loads(posts_json)
    selected_indices = set(json.loads(selected_json)) if selected_json else set()
    total_pages = max(1, (len(posts) + PREVIEW_PER_PAGE - 1) // PREVIEW_PER_PAGE)

    new_page = current_page + direction
    new_page = max(0, min(new_page, total_pages - 1))

    # プレビュー取得 (未キャッシュのみ)
    start = new_page * PREVIEW_PER_PAGE
    end = min(start + PREVIEW_PER_PAGE, len(posts))
    get_preview_data(posts[start:end], progress_cb=progress)

    pg_info = f"ページ {new_page + 1} / {total_pages}（全 {len(posts)} 件）"
    page_data = _build_page_data(posts, new_page, selected_indices)

    outputs = []
    for img_path, label, checked in page_data:
        outputs.append(gr.update(value=img_path, visible=img_path is not None))
        outputs.append(
            gr.update(value=checked, label=label, visible=img_path is not None)
        )

    return [new_page, pg_info] + outputs


def do_download(
    posts_json: str,
    selected_json: str,
    do_resize: bool,
    do_xmp: bool,
    output_folder: str,
):
    """選択された投稿のみダウンロード"""
    if not posts_json:
        return "データがありません。まず検索してください。"

    all_posts = json.loads(posts_json)
    selected_indices = set(json.loads(selected_json)) if selected_json else set()

    if not selected_indices:
        return "⚠️ ダウンロードする画像を選択してください。\nギャラリーの画像をクリックして選択/解除できます。"

    # 選択されたインデックスの投稿だけ抽出
    selected_posts = [
        all_posts[i] for i in sorted(selected_indices) if i < len(all_posts)
    ]
    selected_posts_json = json.dumps(selected_posts)

    return download_selected(selected_posts_json, do_resize, do_xmp, output_folder)


# ============================================================
# Gradio UI
# ============================================================
def create_ui():
    with gr.Blocks(
        title="Danbooru Multi-Tag Search",
    ) as app:
        gr.Markdown("# Danbooru Multi-Tag Search")
        gr.Markdown(
            "2タグ制限を回避！ 複数タグで検索 → プレビュー → ダウンロード\n\n"
            "**除外タグ**: `-tag` で除外 (例: `1girl blue_hair -comic`)"
        )

        posts_state = gr.State("")
        selected_state = gr.State("[]")
        page_state = gr.State(0)  # 現在のページ (0-indexed)

        with gr.Row():
            with gr.Column(scale=3):
                tags_input = gr.Textbox(
                    label="タグ (スペース区切り・3個以上必須)",
                    placeholder="1girl blue_hair large_breasts highres solo -comic -monochrome",
                    lines=2,
                )
                tag_warning = gr.Markdown(
                    value="⚠️ **検索タグを3つ以上入力してください**（除外タグ `-tag` はカウントしません）",
                    visible=True,
                )
            with gr.Column(scale=1):
                max_results = gr.Slider(
                    minimum=10, maximum=1000, value=200, step=10, label="最大件数"
                )
                min_score = gr.Number(value=0, label="最低スコア", precision=0)

        with gr.Row():
            rating_filter = gr.Radio(
                choices=["all", "safe", "sensitive", "questionable", "explicit"],
                value="all",
                label="Rating フィルタ",
            )
            search_btn = gr.Button(
                "🔍 検索", variant="primary", size="lg", interactive=False
            )

        status_text = gr.Textbox(label="検索ステータス", interactive=False, lines=4)

        # --- 画像カードグリッド (5列×4行 = 20スロット) ---
        # 各スロット: Image + Checkbox
        image_slots = []  # gr.Image のリスト
        check_slots = []  # gr.Checkbox のリスト

        for row in range(GRID_ROWS):
            with gr.Row():
                for col in range(GRID_COLS):
                    with gr.Column(min_width=120):
                        img = gr.Image(
                            value=None,
                            label=None,
                            show_label=False,
                            height=150,
                            width=150,
                            interactive=False,
                            visible=False,
                        )
                        cb = gr.Checkbox(
                            value=False,
                            label="",
                            visible=False,
                        )
                        image_slots.append(img)
                        check_slots.append(cb)

        # --- ページナビゲーション ---
        with gr.Row():
            prev_page_btn = gr.Button("◀ 前ページ", size="sm")
            page_info = gr.Markdown(value="ページ 0 / 0")
            next_page_btn = gr.Button("次ページ ▶", size="sm")

        selected_info = gr.Markdown(value="選択: 0 / 0 件")

        gr.Markdown("---")
        gr.Markdown("### ダウンロード設定")

        with gr.Row():
            do_resize = gr.Checkbox(
                value=True, label="SDXL リサイズ (平均色パディング)"
            )
            do_xmp = gr.Checkbox(value=True, label="XMP タグ埋め込み")

        output_folder = gr.Textbox(
            label="保存先フォルダ",
            value=str(DEFAULT_OUTPUT_DIR),
        )

        with gr.Row():
            download_btn = gr.Button(
                "⬇️ 選択画像をダウンロード", variant="primary", size="lg"
            )

        download_log = gr.Textbox(label="ダウンロードログ", interactive=False, lines=8)

        # --- タグ数バリデーション ---
        def validate_tags(text):
            include = [t for t in text.strip().split() if not t.startswith("-")]
            count = len(include)
            if count >= 3:
                return (
                    gr.update(interactive=True),
                    gr.update(
                        value=f"✅ **検索タグ: {count} 個** — 検索できます",
                        visible=True,
                    ),
                )
            else:
                return (
                    gr.update(interactive=False),
                    gr.update(
                        value=f"⚠️ **検索タグを3つ以上入力してください**（現在 {count} 個、除外タグ `-tag` はカウントしません）",
                        visible=True,
                    ),
                )

        tags_input.change(
            fn=validate_tags,
            inputs=[tags_input],
            outputs=[search_btn, tag_warning],
        )

        # --- チェックボックス変更時: 選択状態を反映 ---
        def on_checkbox_change(
            slot_idx, checked, selected_json, posts_json, current_page
        ):
            selected = set(json.loads(selected_json)) if selected_json else set()
            posts = json.loads(posts_json) if posts_json else []
            global_idx = current_page * PREVIEW_PER_PAGE + slot_idx
            if global_idx < len(posts):
                if checked:
                    selected.add(global_idx)
                else:
                    selected.discard(global_idx)
            total = len(posts)
            info = f"**選択: {len(selected)} / {total} 件**"
            if len(selected) > 0:
                info += " — ダウンロード可能"
            return json.dumps(sorted(selected)), info

        for slot_i, cb in enumerate(check_slots):
            cb.change(
                fn=lambda checked, sj, pj, cp, _i=slot_i: on_checkbox_change(
                    _i, checked, sj, pj, cp
                ),
                inputs=[cb, selected_state, posts_state, page_state],
                outputs=[selected_state, selected_info],
            )

        # --- ページナビゲーション ---
        # interleave image_slots and check_slots for outputs
        page_grid_outputs = []
        for img, cb in zip(image_slots, check_slots):
            page_grid_outputs.append(img)
            page_grid_outputs.append(cb)

        prev_page_btn.click(
            fn=lambda pj, sj, cp: do_page_change(pj, sj, cp, -1),
            inputs=[posts_state, selected_state, page_state],
            outputs=[page_state, page_info] + page_grid_outputs,
        )

        next_page_btn.click(
            fn=lambda pj, sj, cp: do_page_change(pj, sj, cp, +1),
            inputs=[posts_state, selected_state, page_state],
            outputs=[page_state, page_info] + page_grid_outputs,
        )

        # --- 検索イベント ---
        search_grid_outputs = []
        for img, cb in zip(image_slots, check_slots):
            search_grid_outputs.append(img)
            search_grid_outputs.append(cb)

        search_btn.click(
            fn=do_search,
            inputs=[tags_input, max_results, rating_filter, min_score],
            outputs=[
                status_text,
                posts_state,
                selected_state,
                selected_info,
                page_state,
                page_info,
            ]
            + search_grid_outputs,
        )

        # Enter キーでも検索
        tags_input.submit(
            fn=do_search,
            inputs=[tags_input, max_results, rating_filter, min_score],
            outputs=[
                status_text,
                posts_state,
                selected_state,
                selected_info,
                page_state,
                page_info,
            ]
            + search_grid_outputs,
        )

        download_btn.click(
            fn=do_download,
            inputs=[posts_state, selected_state, do_resize, do_xmp, output_folder],
            outputs=[download_log],
        )

    return app


# ============================================================
# メイン
# ============================================================
if __name__ == "__main__":
    app = create_ui()
    app.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        inbrowser=True,
    )
